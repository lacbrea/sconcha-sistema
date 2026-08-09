"""Parser del reporte de egresos de caja (sistema de ventas Restaurant.pe) —
SCONCHA conciliación.

Desde agosto 2026 la caja chica ya no se documenta escaneando boletas: el
sistema de ventas exporta un "Reporte de Egresos" por local, y ese reporte
es lo que se cruza contra el banco (ver conciliacion/README.md, entrada
2026-08-06, y CLAUDE.md de este repo para la decisión del dueño). Este
módulo solo sabe parsear ese reporte; `conciliar.py` lo usa para armar el
JSON intermedio que consume `--egresos` en build_conciliacion.py — el motor
vendorizado (conciliacion/) no parsea HTML directamente, a propósito: separa
"leer el reporte" (acá, se puede tocar libremente) de "el motor" (vendorizado
tal cual, ver conciliacion/README.md).

El archivo es un .xls que en realidad es HTML de Excel (mismo truco que el
.xls de BBVA hasta jun-2026 — ver parsers_eecc.parse_eecc_bbva_html en el
motor), pero con una vuelta de tuerca: Excel lo exporta como FRAMESET. El
.xls principal casi no tiene datos, solo referencia a un "sheet001.htm"
dentro de una carpeta hermana "<nombre-original>_archivos/". La TRAMPA
verificada contra un reporte real: la referencia interna (atributo href del
`<link id=shLink>`) trae el nombre ORIGINAL del archivo tal como lo exportó
Excel (URL-encoded, ej. "Egresos%20(18)_archivos/sheet001.htm"), no el
nombre que tenga hoy en disco. Drive/Gmail entrega el archivo con ese nombre
original y alguien lo renombra después (ej. a "Egresos_LINCE_2026-07.xls");
derivar la carpeta como "<stem-actual>_archivos" a partir del nombre ACTUAL
falla en ese caso. `_resolver_tabla()` por eso lee el propio href, lo
decodifica (urllib.parse.unquote) y solo cae al nombre por convención como
respaldo.

Acepta 3 formas de llegar el archivo (ver `_resolver_tabla`):
  1. El frameset .xls + su carpeta hermana "<nombre-original>_archivos/".
  2. El "sheet001.htm" suelto (ya extraído de la carpeta).
  3. Un HTML de tabla único que no es frameset (ya trae <tr> con datos).

Estructura real de la tabla (verificada contra un reporte de jul-2026):
filas <tr> con 10 <td> — Fecha ("31/07/2026 16:20"), Usuario ("CAJA.LINCE"),
Categoria, Caja, Motivo, Entregado A, Moneda, Tarjeta, Estado, Monto. Antes
de esas filas hay cabeceras de reporte (título, usuario, generado) que se
descartan por FORMA (no traen 10 <td>: usan colspan para fusionar celdas),
no por posición fija de fila.
"""
from __future__ import annotations

import html.parser as _htmlparser
import os
import re
import urllib.parse

# ---------------------------------------------------------------------------
# Extracción de filas <tr>/<td>. No se importa parsers_eecc._TableRowParser
# (el motor vendorizado en conciliacion/, que se copia tal cual desde
# OneDrive) para no darle una dependencia nueva a ese directorio; la lógica
# es la misma (colspan/rowspan no se resuelven — cada <td> cuenta una vez,
# que es justo lo que permite distinguir filas de datos, siempre con 10
# <td>, de las de título/encabezado, que fusionan celdas con colspan).
# ---------------------------------------------------------------------------
class _TableRowParser(_htmlparser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.cur = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.cur = []
        elif tag == 'td':
            self.cell = ''

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            if self.cur is not None:
                self.cur.append(re.sub(r'[\s\xa0]+', ' ', self.cell).strip())
            self.cell = None
        elif tag == 'tr':
            if self.cur:
                self.rows.append(self.cur)
            self.cur = None


_RE_SHLINK = re.compile(r'<link\s+id=["\']?shLink["\']?\s+href=["\']([^"\']+)["\']', re.I)
# El separador entre CAJA y el local varia por local: LINCE exporta
# 'CAJA.LINCE' (punto) y MIRAFLORES 'CAJA MIRAFLORES' (espacio). Verificado
# contra los reportes reales de jul-2026 de ambos.
_RE_LOCAL = re.compile(r'CAJA[.\s_-]*([A-Za-zÁÉÍÓÚÑáéíóúñ]+)', re.I)
# La fecha tambien varia por local: LINCE '31/07/2026 16:20' (barras, sin
# segundos) y MIRAFLORES '31-07-2026 16:40:05' (guiones, con segundos). Se
# aceptan ambos separadores y los segundos son opcionales.
_RE_FECHA_HORA = re.compile(
    r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s+(\d{1,2}:\d{2})(?::\d{2})?)?'
)

N_COLUMNAS_FILA_DATOS = 10  # Fecha, Usuario, Categoria, Caja, Motivo, Entregado A, Moneda, Tarjeta, Estado, Monto

# Destinos de "Entregado A" que significan "salio de la caja hacia la cuenta
# del negocio". LINCE escribe 'BANCO'; MIRAFLORES escribe la cuenta de formas
# libres ('CTA DE LA EMPRESA', 'CTA D LA EMPRESA', 'cta de empresa') o el
# nombre del banco ('INTERBANK'). El dueño confirmo el 2026-08-09 que son el
# mismo concepto y va a pedir que ambos locales usen 'BANCO' en adelante;
# estas variantes se mantienen porque los reportes YA EXPORTADOS conservan el
# texto viejo y hay que poder reprocesarlos.
_RE_DESTINO_BANCO = re.compile(
    r'^\s*(?:BANCO|INTERBANK|BBVA|CTA\.?\s*(?:DE\s+|D\s+|DEL\s+)?(?:LA\s+)?EMPRESA)\s*$',
    re.I,
)

# Clasificación de CONCEPTO dentro de los depósitos (propina vs venta vs
# indeterminado). Es un problema distinto de gasto/depósito (_RE_DESTINO_BANCO
# arriba): un depósito ya sabemos que salió de la caja hacia la cuenta del
# negocio, pero eso no dice SI es plata de venta o propina entregada en
# efectivo (ver el hallazgo real de MIRAFLORES jul-2026 en el docstring del
# módulo). El MOTIVO es el único dato disponible para distinguirlos — no hay
# columna separada ni otra señal en la fila — así que el criterio es
# exclusivamente ese texto. Cuando no alcanza (ej. 'DEPOSITO' a secas o un
# número suelto como '400', ambos reales de MIRAFLORES) no se adivina: se
# marca 'indeterminado', que es honesto, en vez de inventar 'venta' y
# ensuciar el cuadre de ingresos de la fase siguiente.
_RE_PROPINA = re.compile(r'PROPINA', re.I)


def _clasificar_concepto(motivo):
    """Devuelve 'propina' | 'venta' | 'indeterminado' a partir del texto de
    MOTIVO de un depósito (ver comentario arriba)."""
    m = (motivo or '').upper()
    if _RE_PROPINA.search(m):
        return 'propina'
    if m.startswith('DEPOSITO DE VENTA'):
        return 'venta'
    return 'indeterminado'


def _leer_texto(path):
    with open(path, 'rb') as f:
        raw = f.read()
    return raw.decode('utf-8-sig', errors='replace')


def _resolver_tabla(path):
    """Devuelve la ruta al HTML que trae la TABLA de datos, resolviendo la
    trampa del frameset renombrado (ver docstring del módulo). 'path' puede
    ser el .xls frameset, el sheet001.htm suelto, o un HTML de tabla único."""
    path = os.fspath(path)
    texto = _leer_texto(path)

    m = _RE_SHLINK.search(texto)
    if m:
        href = urllib.parse.unquote(m.group(1))
        candidato = os.path.join(os.path.dirname(path), href)
        if os.path.exists(candidato):
            return candidato
        # Fallback: convención "<stem-actual>_archivos/sheet001.htm" (funciona
        # si nadie renombró el archivo después de exportarlo de Excel).
        stem = os.path.splitext(os.path.basename(path))[0]
        candidato2 = os.path.join(os.path.dirname(path), f'{stem}_archivos', 'sheet001.htm')
        if os.path.exists(candidato2):
            return candidato2
        raise FileNotFoundError(
            f"'{path}' es un frameset de Excel pero no se encontró su tabla de datos "
            f"(se probó '{candidato}' según la referencia interna del frameset, y "
            f"'{candidato2}' por convención de nombre)."
        )

    # No es un frameset: si el propio archivo ya tiene filas de tabla, se
    # parsea directo (caso 2: sheet001.htm suelto; caso 3: HTML de tabla único).
    if re.search(r'<tr[\s>]', texto, re.I):
        return path

    raise ValueError(f"'{path}' no es un frameset de Excel ni un HTML con tabla reconocible.")


def _fnum(s):
    s = str(s or '').strip().replace(',', '')
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fecha_hora(bruto):
    """'31/07/2026 16:20' o '31-07-2026 16:40:05' -> ('31/07/2026', '16:20').
    Sin hora -> (fecha, None). Se normaliza siempre a DD/MM/AAAA (el reporte
    trae el día sin cero a la izquierda en algunas filas, y el separador
    cambia según el local).

    Devuelve (None, None) si la fecha no se reconoce. Antes devolvía el texto
    crudo, que se colaba tal cual al JSON del motor y de ahí a la hoja CAJA
    CHICA: el reporte de MIRAFLORES entero salió con fechas '31-07-2026
    16:40:05' sin que nada avisara. Un dato que no se entiende tiene que
    señalarse, no propagarse — quien llama lo manda a 'filas_ignoradas'."""
    bruto = str(bruto or '').strip()
    m = _RE_FECHA_HORA.match(bruto)
    if not m:
        return None, None
    d, mo, y, hora = m.group(1), m.group(2), m.group(3), m.group(4)
    return f'{int(d):02d}/{int(mo):02d}/{y}', hora


def parsear_egresos(path):
    """Parsea un reporte de egresos de caja. Devuelve:

        {"local": "LINCE"|None (extraído de 'CAJA.LINCE' en la columna
                   Usuario, None si ninguna fila lo trae reconocible),
         "gastos": [item, ...], "depositos": [item, ...],
         "total_gastos": float, "total_depositos": float,
         "filas_ignoradas": [str, ...]}

    item = {"fecha": "DD/MM/AAAA", "hora": "HH:MM"|None, "motivo": str,
            "entregado_a": str, "monto": float}

    Los items de "depositos" (solamente esos; "gastos" no lo lleva, no
    aplica) traen además "concepto": "propina" | "venta" | "indeterminado",
    clasificado por el texto de MOTIVO (ver _clasificar_concepto y su
    comentario: es el único dato disponible para distinguir propina de
    venta dentro de la plata que entra al banco).

    Clasificación gasto/depósito: una fila es DEPÓSITO si "Entregado A" es
    'BANCO' o el motivo empieza con 'DEPOSITO DE VENTA' (mayúsc./minúsc.
    indistinto); todo lo demás es GASTO.

    Filas con Estado != ACTIVO (ej. ANULADO) o Moneda != Soles nunca se
    descartan en silencio: van a 'filas_ignoradas' con el motivo. No hay
    caso real de Moneda != Soles todavía, así que no se inventa manejo de
    USD — solo se registra y se ignora.
    """
    ruta_tabla = _resolver_tabla(path)
    texto = _leer_texto(ruta_tabla)

    parser = _TableRowParser()
    parser.feed(texto)
    filas = [r for r in parser.rows if len(r) == N_COLUMNAS_FILA_DATOS]

    local = None
    gastos = []
    depositos = []
    filas_ignoradas = []

    for row in filas:
        fecha_raw, usuario, categoria, caja, motivo, entregado_a, moneda, tarjeta, estado, monto_raw = row
        if fecha_raw.strip().upper() == 'FECHA':
            continue  # encabezado de columnas (mismo shape que las filas de datos: 10 <td>)

        if local is None:
            m = _RE_LOCAL.search(usuario)
            if m:
                local = m.group(1).upper()

        fecha, hora = _fecha_hora(fecha_raw)
        if fecha is None:
            filas_ignoradas.append(
                f'{fecha_raw.strip()!r}: FECHA no reconocida, se ignora la fila '
                f'(motivo {motivo.strip()[:40]!r}, monto {monto_raw})'
            )
            continue
        motivo_s = motivo.strip()
        entregado_s = entregado_a.strip()
        estado_s = estado.strip().upper()
        moneda_s = moneda.strip()

        if estado_s != 'ACTIVO':
            filas_ignoradas.append(
                f'{fecha} {motivo_s[:40]!r} (S/{monto_raw}): ESTADO={estado_s or "(vacío)"}, se ignora (no ACTIVO)'
            )
            continue
        if moneda_s.upper() != 'SOLES':
            filas_ignoradas.append(
                f'{fecha} {motivo_s[:40]!r} (monto {monto_raw} {moneda_s}): MONEDA no soportada (solo Soles), se ignora'
            )
            continue

        monto = _fnum(monto_raw)
        if monto is None:
            filas_ignoradas.append(f'{fecha} {motivo_s[:40]!r}: MONTO inválido ({monto_raw!r}), se ignora')
            continue

        item = {'fecha': fecha, 'hora': hora, 'motivo': motivo_s, 'entregado_a': entregado_s, 'monto': monto}
        # El motivo se conserva tal cual a proposito: entre estos depositos hay
        # PROPINAS en efectivo (MIRAFLORES, jul-2026), que van a la misma cuenta
        # pero no son venta. Para cruzar contra el abono del banco da igual -es
        # plata que entra-, pero para el cuadre de ingresos (fase posterior) hay
        # que poder separarlas, y el motivo es el unico dato que lo permite.
        es_deposito = bool(_RE_DESTINO_BANCO.match(entregado_s)) or motivo_s.upper().startswith('DEPOSITO DE VENTA')
        if es_deposito:
            # 'concepto' solo aplica a depositos (ver _clasificar_concepto):
            # un gasto no es ni propina ni venta, ese campo no le corresponde.
            item['concepto'] = _clasificar_concepto(motivo_s)
            depositos.append(item)
        else:
            gastos.append(item)

    return {
        'local': local,
        'gastos': gastos,
        'depositos': depositos,
        'total_gastos': round(sum(g['monto'] for g in gastos), 2),
        'total_depositos': round(sum(d['monto'] for d in depositos), 2),
        'filas_ignoradas': filas_ignoradas,
    }
