"""Envoltorio que arma los argumentos del motor de conciliación bancaria
vendorizado (conciliacion/build_conciliacion.py) a partir de config.yaml, y
lo invoca como subproceso.

El motor recibe un EECC principal (posicional), EECC adicionales de la
misma empresa (--eecc, repetible), un único JSON de constancias, un CSV de
comprobantes y opcionalmente un .xlsx anterior para heredar depuración
manual. Este módulo hace el trabajo de juntar esas piezas desde Drive y el
Sheet contable, y de subir el resultado de vuelta a Drive.

Uso:
    C:\\Python312\\python.exe conciliar.py --empresa "EL TEMPLO" --mes 2026-06
    C:\\Python312\\python.exe conciliar.py --empresa "EL TEMPLO" --mes 2026-06 --dry-run --verbose
    C:\\Python312\\python.exe conciliar.py --empresa "INSTITUCION" --mes 2026-06 --sin-heredar
    C:\\Python312\\python.exe conciliar.py --empresa "EL TEMPLO" --mes 2026-06 --comprobantes facturas.csv

Ver conciliacion/README.md para el origen del motor vendorizado y
config.yaml -> conciliacion para la configuración de empresas/cuentas.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import pathlib
import re
import subprocess
import sys
from typing import Any

from almacen_drive import AlmacenDrive
from procesar import cargar_config
from registro_sheets import COLUMNAS_CONTABLE

logger = logging.getLogger("procesar.conciliar")

RAIZ = pathlib.Path(__file__).resolve().parent
MOTOR = RAIZ / "conciliacion" / "build_conciliacion.py"

MESES_ES = [
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
]

# Un comprobante emitido a fin de mes se paga al mes siguiente (y viceversa,
# uno pagado a inicio de mes puede haberse emitido el mes anterior): sin
# este margen el CSV derivado del Sheet contable dejaría fuera comprobantes
# reales que el motor sí debería poder cruzar.
MARGEN_DIAS_COMPROBANTES = 15

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Formatos de fecha que puede traer el Sheet contable (ComprobanteExtraido
# no fija uno solo; ver procesar.anio_mes). Mismo set que usa el propio
# motor para su columna FECHA_EMISION/FECHA_PAGO (pdate_flex en
# build_conciliacion.py), para no inventar una tercera convención.
FORMATOS_FECHA = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")


# -----------------------------------------------------------------------------
# Config y validación
# -----------------------------------------------------------------------------
def resolver_empresa(config: dict, nombre_corto: str) -> tuple[dict | None, str | None]:
    """Busca 'nombre_corto' en conciliacion.empresas. Devuelve (empresa, None)
    o (None, motivo) — mismo patrón que resolver_empresa/resolver_local en
    procesar.py, para que main() solo tenga que loguear y salir."""
    empresas = ((config.get("conciliacion") or {}).get("empresas")) or []
    for empresa in empresas:
        if empresa.get("nombre_corto") == nombre_corto:
            return empresa, None
    disponibles = ", ".join(e.get("nombre_corto", "?") for e in empresas) or "(ninguna configurada)"
    return None, (
        f"'{nombre_corto}' no está en conciliacion.empresas de config.yaml. "
        f"Empresas disponibles: {disponibles}."
    )


def resolver_ruc_empresa(config: dict, nombre_corto: str) -> str | None:
    """Busca el RUC de 'nombre_corto' en config['empresas'] (la lista de
    comprobantes: nombre_corto/razon_social/ruc), NO en
    config['conciliacion']['empresas'], que no tiene ese campo. Existe para
    poder pasarle --pdf-password al motor: Interbank empezo en jul-2026 a
    cifrar los EECC en PDF de "Cuenta Negocio" con el RUC del titular como
    contrasena (ver conciliacion/README.md). Devuelve None si no lo
    encuentra; el llamador sigue sin contrasena, y si el PDF de verdad viene
    cifrado el motor fallara con un mensaje claro en vez de un traceback
    críptico de pypdf."""
    for empresa in config.get("empresas") or []:
        if empresa.get("nombre_corto") == nombre_corto:
            ruc = empresa.get("ruc")
            return str(ruc) if ruc else None
    return None


def directorio_trabajo(nombre_corto: str, mes: str) -> pathlib.Path:
    return pathlib.Path("salida") / "conciliacion" / nombre_corto / mes


def mes_en_espanol(mes: str) -> tuple[str, str]:
    anio, mes_num = mes.split("-")
    return MESES_ES[int(mes_num) - 1], anio


def configurar_logging(carpeta_salida: pathlib.Path, verbose: bool) -> None:
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    handler_archivo = logging.FileHandler(carpeta_salida / "conciliar.log", encoding="utf-8")
    handler_archivo.setLevel(logging.DEBUG)
    handler_consola = logging.StreamHandler()
    handler_consola.setLevel(logging.DEBUG if verbose else logging.INFO)

    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler_archivo.setFormatter(formato)
    handler_consola.setFormatter(formato)

    raiz = logging.getLogger("procesar.conciliar")
    raiz.setLevel(logging.DEBUG)
    raiz.handlers.clear()
    raiz.addHandler(handler_archivo)
    raiz.addHandler(handler_consola)


# -----------------------------------------------------------------------------
# EECC: selección por número de cuenta, descarga a disco local
# -----------------------------------------------------------------------------
def descargar_eecc(
    almacen: AlmacenDrive, carpeta_eecc_id: str, cuentas: list[dict], destino_dir: pathlib.Path
) -> tuple[dict[str, list[pathlib.Path]], list[str]]:
    """Descarga los EECC de carpeta_eecc_id cuyo nombre contenga el 'numero'
    de alguna cuenta de esta empresa (criterio del config: así nombra el
    banco el archivo, ej. EC_4134_062026.pdf).

    Devuelve (por_cuenta, ignorados):
      por_cuenta: {numero_cuenta: [Path, ...]} (normalmente un archivo por
        cuenta, pero no se asume: si hay más de uno se descargan todos).
      ignorados: nombres que no calzaron con ninguna cuenta configurada. Se
        registran siempre en el log, nunca se descartan en silencio.
    """
    archivos = almacen.listar(carpeta_eecc_id)
    por_cuenta: dict[str, list[pathlib.Path]] = {}
    ignorados: list[str] = []
    for archivo in archivos:
        nombre = archivo["name"]
        numero_match = next((c["numero"] for c in cuentas if str(c["numero"]) in nombre), None)
        if numero_match is None:
            ignorados.append(nombre)
            logger.warning("EECC ignorado (no calza con ninguna cuenta configurada de esta empresa): %s", nombre)
            continue
        destino = destino_dir / nombre
        almacen.descargar(archivo["id"], destino)
        por_cuenta.setdefault(numero_match, []).append(destino)
        logger.info("EECC descargado (cuenta %s): %s", numero_match, nombre)
    return por_cuenta, ignorados


def separar_principal(
    por_cuenta: dict[str, list[pathlib.Path]], cuentas: list[dict]
) -> tuple[pathlib.Path | None, list[pathlib.Path]]:
    """La cuenta marcada 'principal: true' en config.yaml va como posicional
    del motor; el resto (otras cuentas, y cualquier archivo extra de la
    misma cuenta principal) va con --eecc repetible."""
    numero_principal = next((c["numero"] for c in cuentas if c.get("principal")), None)
    principal: pathlib.Path | None = None
    adicionales: list[pathlib.Path] = []
    for numero, rutas in por_cuenta.items():
        for ruta in rutas:
            if numero == numero_principal and principal is None:
                principal = ruta
            else:
                adicionales.append(ruta)
    return principal, adicionales


# -----------------------------------------------------------------------------
# Constancias: selección por cuenta, fusión en un único JSON
# -----------------------------------------------------------------------------
def _version_de_json_constancias(nombre: str, numero: str) -> int | None:
    """Número de versión de un 'cons_<numero>.json' de esta cuenta, o None si
    'nombre' no es un archivo de constancias de esa cuenta.

    Mismo criterio que correo_gmail._fusionar_y_subir() usa para versionar:
    'cons_4134.json' es la versión 1, 'cons_4134 v2.json' la 2, etc.
    """
    patron = re.compile(rf"^cons_{re.escape(numero)}(?: v(\d+))?\.json$", re.IGNORECASE)
    m = patron.match(nombre)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 1


def descargar_constancias(
    almacen: AlmacenDrive, carpeta_constancias_id: str, cuentas: list[dict], destino_dir: pathlib.Path
) -> pathlib.Path | None:
    """Descarga el cons_<cuenta>.json MÁS RECIENTE de cada cuenta de esta
    empresa y los fusiona en un único archivo (el motor recibe un solo JSON,
    no una lista de archivos). Devuelve None si no hay ninguna constancia (el
    motor acepta 'none').

    Toma solo la última versión por cuenta, no todas las que calcen con el
    número: correo_gmail.py sube cons_<cuenta> v2.json, v3.json... como
    SUPERSETS acumulativos (ya fusionados con lo anterior), nunca como
    archivos independientes. Sumar el contenido de todas las versiones
    duplicaría cada constancia una vez por versión existente.
    """
    archivos = almacen.listar(carpeta_constancias_id)
    elementos: list[dict] = []
    encontrados: list[str] = []
    for cuenta in cuentas:
        numero = str(cuenta["numero"])
        mejor_archivo = None
        mejor_version = -1
        for archivo in archivos:
            version = _version_de_json_constancias(archivo["name"], numero)
            if version is not None and version > mejor_version:
                mejor_version = version
                mejor_archivo = archivo
        if mejor_archivo is None:
            continue

        nombre = mejor_archivo["name"]
        destino = destino_dir / nombre
        almacen.descargar(mejor_archivo["id"], destino)
        try:
            contenido = json.loads(destino.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("No se pudo leer '%s' como JSON de constancias: %s", nombre, exc)
            continue
        if not isinstance(contenido, list):
            logger.warning("'%s' no es una lista JSON; se ignora su contenido.", nombre)
            continue
        elementos.extend(contenido)
        encontrados.append(nombre)

    if not elementos:
        logger.info("No hay constancias para esta empresa en CONSTANCIAS; se pasa 'none' al motor.")
        return None

    ruta_fusion = destino_dir / "constancias.json"
    ruta_fusion.write_text(json.dumps(elementos, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Constancias fusionadas de %d archivo(s) (%s) -> %s", len(encontrados), ", ".join(encontrados), ruta_fusion)
    return ruta_fusion


# -----------------------------------------------------------------------------
# Comprobantes: derivar el CSV desde el Sheet contable
#
# El motor (conciliacion/build_conciliacion.py, ~líneas 448-471) lee este
# CSV con csv.DictReader, encoding='utf-8-sig', delimitador por defecto
# (coma), y sube a mayúsculas los NOMBRES de columna al vuelo (no hace falta
# que el CSV los traiga ya en mayúsculas). Tres trampas ya conocidas, no
# tocar el motor para "arreglarlas": el link va en la columna LINK_DRIVE
# (líneas 732 y 742, no LINK_COMPROBANTE); solo cruza filas con
# ESTADO_PAGO == 'PAGADA' (línea 484); y filtra por EMPRESA con
# `EMP_KEY not in norm(row['EMPRESA'])` donde EMP_KEY sale de si 'TEMPLO'
# está en el argumento posicional 'empresa' (linea 89) — ese argumento es
# nombre_motor, no nombre_corto. Para INSTITUCION, nombre_corto="INSTITUCION"
# NO contiene "CEVICHERA" (EMP_KEY para esa empresa), así que si el CSV trae
# EMPRESA=nombre_corto el filtro interno del motor descartaría TODAS sus
# filas en silencio. Por eso este CSV escribe nombre_motor en la columna
# EMPRESA (ya filtramos por nombre_corto de este lado antes de escribir).
# -----------------------------------------------------------------------------
def _parsear_fecha_flex(valor) -> datetime.date | None:
    texto = str(valor or "").strip()
    if not texto:
        return None
    for formato in FORMATOS_FECHA:
        try:
            return datetime.datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    return None


def _rango_mes_con_margen(mes: str, margen_dias: int) -> tuple[datetime.date, datetime.date]:
    anio, mes_num = (int(x) for x in mes.split("-"))
    inicio_mes = datetime.date(anio, mes_num, 1)
    if mes_num == 12:
        fin_mes = datetime.date(anio, 12, 31)
    else:
        fin_mes = datetime.date(anio, mes_num + 1, 1) - datetime.timedelta(days=1)
    return inicio_mes - datetime.timedelta(days=margen_dias), fin_mes + datetime.timedelta(days=margen_dias)


def leer_filas_sheet_contable(servicio_sheets, spreadsheet_id: str, rango: str) -> list[dict[str, Any]]:
    """Lee el Sheet contable y lo devuelve como lista de dicts (cabecera de
    la fila 1 como llaves). Separado de filtrar_y_escribir_csv() para poder
    testear cada mitad por separado: esta función solo sabe hablar con la
    API de Sheets; la otra solo sabe de filtrado y CSV.

    Las dos opciones de renderizado no son cosméticas, son correctitud de los
    importes. `registro_sheets` escribe con USER_ENTERED, así que el Sheet
    guarda números y fechas de verdad, no texto; al leerlos, el modo por
    defecto (FORMATTED_VALUE) los devuelve según el idioma del Sheet y un
    subtotal sale como "686,44" con COMA decimal. El motor parsea importes con
    `float(str(x).replace(',', ''))` (build_conciliacion.py, fnum), que sobre
    "1507,16" da 150716.0: el monto queda inflado x100 y ningún cargo cruza.
    UNFORMATTED_VALUE arregla el número pero devuelve la fecha como serial de
    Sheets (46214), que `pdate_flex` no sabe leer. La combinación de abajo es
    la única que devuelve las dos cosas bien — verificado contra el Sheet real
    el 2026-08-06, no deducido de la documentación."""
    resp = (
        servicio_sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        )
        .execute()
    )
    valores = resp.get("values", [])
    if not valores:
        return []
    cabecera = [h.strip() for h in valores[0]]
    filas = []
    for fila in valores[1:]:
        filas.append({cabecera[i]: (fila[i] if i < len(fila) else "") for i in range(len(cabecera))})
    return filas


def filtrar_y_escribir_csv(
    filas: list[dict[str, Any]],
    nombre_corto: str,
    nombre_motor: str,
    mes: str,
    destino_csv: pathlib.Path,
    margen_dias: int = MARGEN_DIAS_COMPROBANTES,
) -> int:
    """Filtra las filas de 'nombre_corto' cuya FECHA_EMISION o FECHA_PAGO cae
    en 'mes' +/- margen_dias, y las escribe en destino_csv con las columnas
    que espera el motor (COLUMNAS_CONTABLE, importado de registro_sheets.py:
    es la fuente de verdad, no se duplica esa lista acá). Devuelve cuántas
    filas quedaron. La columna EMPRESA se reescribe con nombre_motor (ver
    comentario arriba del filtro interno del motor)."""
    inicio, fin = _rango_mes_con_margen(mes, margen_dias)

    filtradas = []
    for fila in filas:
        if (fila.get("EMPRESA") or "").strip() != nombre_corto:
            continue
        fecha_emision = _parsear_fecha_flex(fila.get("FECHA_EMISION"))
        fecha_pago = _parsear_fecha_flex(fila.get("FECHA_PAGO"))
        en_rango = (fecha_emision is not None and inicio <= fecha_emision <= fin) or (
            fecha_pago is not None and inicio <= fecha_pago <= fin
        )
        if not en_rango:
            continue
        filtradas.append(fila)

    destino_csv.parent.mkdir(parents=True, exist_ok=True)
    with destino_csv.open("w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_CONTABLE)
        escritor.writeheader()
        for fila in filtradas:
            salida = {columna: fila.get(columna, "") for columna in COLUMNAS_CONTABLE}
            salida["EMPRESA"] = nombre_motor
            salida["ESTADO_PAGO"] = _estado_pago_para_el_motor(fila)
            escritor.writerow(salida)

    return len(filtradas)


def _estado_pago_para_el_motor(fila: dict[str, Any]) -> str:
    """ESTADO_PAGO con el que la fila entra al CSV que consume el motor.

    El motor SOLO intenta cruzar filas con ESTADO_PAGO == 'PAGADA'
    (build_conciliacion.py:484): un comprobante "pendiente" todavía no generó
    el cargo, y cruzarlo daría falsos positivos por coincidencia de monto y
    fecha. Ese diseño asume que alguien marca a mano cuándo se pagó, que es lo
    que se hacía en el backfill de junio (59 de 66 filas en PAGADA).

    En el flujo nuevo nadie marca nada: `procesar.py` extrae el comprobante del
    documento y el documento no dice si ya se pagó, así que ESTADO_PAGO llega
    vacío y el motor ignoraría TODOS los comprobantes — que es exactamente lo
    que pasó al conciliar julio 2026 la primera vez (8 filas en el CSV, 0
    cruces nuevos).

    Lo que sí trae el documento es la CONDICION (contado/crédito), que
    `procesar.py` ahora extrae. Un comprobante al contado se paga contra
    entrega, así que se ofrece al motor como candidato a cruzar; uno a crédito
    no, porque su cargo puede caer semanas después y ahí sí el riesgo de falso
    positivo es real.

    Esto es una INFERENCIA y vive solo en el CSV de la corrida: no se escribe
    de vuelta al Sheet contable, que debe seguir diciendo la verdad ("no se
    sabe si se pagó"). Es la conciliación la que descubre el pago al encontrar
    el cargo del banco. Un ESTADO_PAGO que ya venga puesto (por ejemplo
    corregido a mano en el Sheet) siempre gana sobre esta inferencia.
    """
    estado = str(fila.get("ESTADO_PAGO") or "").strip()
    if estado:
        return estado
    condicion = str(fila.get("CONDICION") or "").strip().lower()
    return "PAGADA" if condicion == "contado" else ""


def contar_filas_csv(ruta: pathlib.Path) -> int:
    with ruta.open("r", encoding="utf-8-sig", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


# -----------------------------------------------------------------------------
# Heredar: .xlsx del mes anterior de la misma empresa
# -----------------------------------------------------------------------------
def version_de_xlsx(nombre: str) -> int:
    """Número de versión de un .xlsx de conciliación: 2 para '... v2.xlsx',
    y 1 para el archivo original sin sufijo.

    Existe porque ordenar por nombre y tomar el último NO da la versión más
    alta: en ASCII el espacio de ' v2' (0x20) es menor que el punto de
    '.xlsx' (0x2E), así que 'CONCILIACION X - MAYO 2026 v3.xlsx' ordena
    ANTES que 'CONCILIACION X - MAYO 2026.xlsx' y el último elemento
    resultaría ser el archivo original, el más viejo. Como subir_resultado()
    nunca sobrescribe, el original y sus versiones conviven en la carpeta:
    heredar el original en vez de la última versión perdería en silencio la
    depuración manual más reciente, que es justo lo que --heredar viene a
    conservar. Mismo criterio que usa correo_gmail._fusionar_y_subir() para
    elegir el cons_<cuenta>.json vigente.
    """
    coincidencia = re.search(r" v(\d+)\.xlsx$", nombre, re.IGNORECASE)
    return int(coincidencia.group(1)) if coincidencia else 1


def mes_anterior(mes: str) -> str:
    anio, mes_num = (int(x) for x in mes.split("-"))
    ultimo_dia_anterior = datetime.date(anio, mes_num, 1) - datetime.timedelta(days=1)
    return f"{ultimo_dia_anterior.year:04d}-{ultimo_dia_anterior.month:02d}"


def resolver_heredar(
    almacen: AlmacenDrive, carpeta_conciliacion_id: str, nombre_corto: str, mes: str, destino_dir: pathlib.Path
) -> pathlib.Path | None:
    """Busca en Drive la carpeta del mes anterior y, dentro, un .xlsx de esta
    misma empresa (nombrado 'CONCILIACION <nombre_corto> - ...'). Si hay
    varios (el original más las subidas con sufijo ' v2', ' v3'...) usa el
    de versión más alta, calculada con version_de_xlsx() y NO por orden
    alfabético (ver ahí por qué el orden alfabético da la respuesta
    equivocada). Si no existe nada, NO es error: se loguea y se sigue sin
    heredar.

    AlmacenDrive no tiene un "buscar carpeta por nombre" que no cree: usa
    asegurar_carpeta() igual que para EECC/CONSTANCIAS (idempotente, mismo
    criterio que el paso 2 de main()). En el caso límite de que el mes
    anterior nunca se haya conciliado, esto deja una carpeta AAAA-MM vacía
    en Drive — mismo costo que aceptamos para las carpetas EECC/CONSTANCIAS
    del mes actual.
    """
    mes_prev = mes_anterior(mes)
    carpeta_mes_prev_id = almacen.asegurar_carpeta(mes_prev, carpeta_conciliacion_id)
    archivos = almacen.listar(carpeta_mes_prev_id)
    prefijo = f"CONCILIACION {nombre_corto} - "
    candidatos = [a for a in archivos if a["name"].startswith(prefijo) and a["name"].lower().endswith(".xlsx")]
    if not candidatos:
        logger.info(
            "No se encontró un .xlsx de '%s' en la carpeta del mes anterior (%s); se sigue sin heredar.",
            nombre_corto, mes_prev,
        )
        return None

    elegido = max(candidatos, key=lambda a: (version_de_xlsx(a["name"]), a["name"]))
    destino = destino_dir / f"heredar_{elegido['name']}"
    almacen.descargar(elegido["id"], destino)
    logger.info("Heredando depuración manual de '%s' (mes anterior: %s).", elegido["name"], mes_prev)
    return destino


# -----------------------------------------------------------------------------
# Invocación del motor (subproceso: hace argparse a nivel de módulo, no se
# puede importar sin disparar el parseo de argumentos)
# -----------------------------------------------------------------------------
def construir_argumentos_motor(
    eecc_principal: pathlib.Path | None,
    eecc_adicionales: list[pathlib.Path],
    constancias: pathlib.Path | None,
    salida_xlsx: pathlib.Path,
    nombre_motor: str,
    comprobantes_csv: pathlib.Path | None,
    pendientes_json: pathlib.Path,
    heredar_xlsx: pathlib.Path | None,
    pdf_password: str | None = None,
) -> list[str]:
    argumentos = [
        str(eecc_principal) if eecc_principal is not None else "none",
        str(constancias) if constancias is not None else "none",
        str(salida_xlsx),
        nombre_motor,
    ]
    for extra in eecc_adicionales:
        argumentos += ["--eecc", str(extra)]
    if comprobantes_csv is not None:
        argumentos += ["--comprobantes", str(comprobantes_csv)]
    argumentos += ["--pendientes", str(pendientes_json)]
    if heredar_xlsx is not None:
        argumentos += ["--heredar", str(heredar_xlsx)]
    if pdf_password:
        argumentos += ["--pdf-password", pdf_password]
    return argumentos


def invocar_motor(argumentos: list[str]) -> None:
    comando = [sys.executable, str(MOTOR), *argumentos]
    logger.info("Invocando motor de conciliación: %s", " ".join(comando))
    resultado = subprocess.run(comando, capture_output=True, encoding="utf-8", errors="replace")
    if resultado.stdout:
        logger.info("Salida del motor:\n%s", resultado.stdout)
    if resultado.stderr:
        logger.debug("Stderr del motor:\n%s", resultado.stderr)
    if resultado.returncode != 0:
        raise RuntimeError(
            f"El motor de conciliación (build_conciliacion.py) terminó con código "
            f"{resultado.returncode}.\n--- stdout ---\n{resultado.stdout}\n--- stderr ---\n{resultado.stderr}"
        )


# -----------------------------------------------------------------------------
# Subida a Drive sin sobrescribir
# -----------------------------------------------------------------------------
def subir_resultado(
    almacen: AlmacenDrive, carpeta_destino_id: str, nombre_deseado: str, ruta_local: pathlib.Path
) -> tuple[str, str]:
    """Sube ruta_local a carpeta_destino_id. AlmacenDrive.subir() siempre
    CREA (no puede sobrescribir), así que si el nombre ya existe se sube con
    sufijo ' v2', ' v3'... en vez de dejar dos archivos con el mismo nombre
    conviviendo sin explicación. Devuelve (nombre_final, file_id)."""
    stem = pathlib.PurePosixPath(nombre_deseado).stem
    suffix = pathlib.PurePosixPath(nombre_deseado).suffix
    nombre_final = nombre_deseado
    version = 2
    while almacen.buscar_por_nombre(carpeta_destino_id, nombre_final) is not None:
        nombre_final = f"{stem} v{version}{suffix}"
        version += 1

    if nombre_final != nombre_deseado:
        logger.warning(
            "Ya existe '%s' en Drive; se sube como '%s' para no sobrescribirlo.", nombre_deseado, nombre_final
        )

    file_id = almacen.subir(carpeta_destino_id, nombre_final, ruta_local, mimetype=MIME_XLSX)
    return nombre_final, file_id


# -----------------------------------------------------------------------------
# Resumen final
# -----------------------------------------------------------------------------
def imprimir_resumen(
    *,
    empresa_cfg: dict,
    mes: str,
    total_eecc: int,
    ignorados: list[str],
    ruta_constancias: pathlib.Path | None,
    n_comprobantes: int,
    ruta_heredar: pathlib.Path | None,
    ruta_salida_xlsx: pathlib.Path,
    enlace_drive: str | None,
    dry_run: bool,
) -> None:
    print("\n" + "=" * 60)
    print("RESUMEN DE LA CONCILIACION")
    print("=" * 60)
    print(f"Empresa:      {empresa_cfg['nombre_corto']} (motor: {empresa_cfg['nombre_motor']})")
    print(f"Mes:          {mes}")
    print(f"EECC:         {total_eecc} descargado(s)" + (f", {len(ignorados)} ignorado(s)" if ignorados else ""))
    print(f"Constancias:  {'sí (' + ruta_constancias.name + ')' if ruta_constancias else 'no'}")
    print(f"Comprobantes: {n_comprobantes} en el CSV")
    print(f"Heredó:       {'sí (' + ruta_heredar.name + ')' if ruta_heredar else 'no'}")
    print(f"Resultado:    {ruta_salida_xlsx}")
    if dry_run:
        print("[DRY-RUN] no se subió nada a Drive.")
    elif enlace_drive:
        print(f"Drive:        {enlace_drive}")
    print("=" * 60)


# -----------------------------------------------------------------------------
# CLI y orquestación
# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Corre la conciliación bancaria de una empresa/mes con el motor vendorizado."
    )
    parser.add_argument(
        "--empresa", required=True,
        help="nombre_corto tal como aparece en conciliacion.empresas de config.yaml (ej. 'EL TEMPLO').",
    )
    parser.add_argument("--mes", required=True, help="Mes a conciliar, formato AAAA-MM (ej. 2026-06).")
    parser.add_argument("--config", default="config.yaml", help="Ruta al config.yaml del negocio.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Lee y genera el .xlsx local para revisarlo, pero no sube nada a Drive ni llama al correo.",
    )
    parser.add_argument(
        "--sin-heredar", action="store_true",
        help="No intenta heredar la depuración manual de proveedores/categorías del mes anterior.",
    )
    parser.add_argument(
        "--comprobantes", default=None,
        help="CSV de comprobantes a mano; si se indica, no se deriva del Sheet contable.",
    )
    parser.add_argument("--verbose", action="store_true", help="Log detallado (DEBUG) también en consola.")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"\d{4}-\d{2}", args.mes):
        logger.error("--mes debe tener el formato AAAA-MM (ej. 2026-06); recibido: '%s'", args.mes)
        return 1

    ruta_config = pathlib.Path(args.config)
    config = cargar_config(ruta_config)

    configurar_logging(pathlib.Path("salida"), args.verbose)

    empresa_cfg, motivo = resolver_empresa(config, args.empresa)
    if empresa_cfg is None:
        logger.error(motivo)
        return 1

    carpeta_conciliacion_id = ((config.get("conciliacion") or {}).get("carpeta") or "").strip()
    if not carpeta_conciliacion_id:
        logger.error("conciliacion.carpeta está vacío en config.yaml; corre init_negocio.py primero.")
        return 1

    import auth_google  # import diferido: no hace falta hasta este punto (valida CLI/config antes)

    try:
        servicio_drive = auth_google.servicio_drive()
    except Exception as exc:
        logger.error(str(exc))
        return 1
    almacen = AlmacenDrive(servicio_drive)

    logger.info("Resolviendo carpetas de Drive para %s / %s ...", args.empresa, args.mes)
    carpeta_mes_id = almacen.asegurar_carpeta(args.mes, carpeta_conciliacion_id)
    carpeta_eecc_id = almacen.asegurar_carpeta("EECC", carpeta_mes_id)
    carpeta_constancias_id = almacen.asegurar_carpeta("CONSTANCIAS", carpeta_mes_id)

    # --- correo (opcional), ANTES de juntar archivos -------------------------
    # La garantía de --dry-run ("no llama al correo") es este 'and not
    # args.dry_run' en el propio if, no un comentario: con dry-run activo la
    # rama de importar/llamar correo_gmail directamente no se ejecuta nunca.
    correo_habilitado = bool((config.get("correo") or {}).get("habilitado"))
    if correo_habilitado and not args.dry_run:
        try:
            import correo_gmail

            carpetas_correo = {
                "EECC": carpeta_eecc_id,
                "CONSTANCIAS": carpeta_constancias_id,
                "BUZON": (config.get("drive", {}).get("carpetas") or {}).get("buzon"),
            }
            resumen_correo = correo_gmail.descargar(
                config, almacen, carpetas_correo, servicio=None, dry_run=args.dry_run
            )
            logger.info("Correo: %s", resumen_correo)
        except Exception as exc:
            # Un fallo del correo NO aborta la conciliación: se sigue con lo
            # que ya haya en Drive (bajado a mano o de una corrida anterior).
            logger.error("Fallo al descargar correo (no aborta la conciliación): %s", exc)
    elif correo_habilitado and args.dry_run:
        logger.info("--dry-run: se omite la descarga de correo aunque correo.habilitado=true.")

    trabajo_dir = directorio_trabajo(empresa_cfg["nombre_corto"], args.mes)
    trabajo_dir.mkdir(parents=True, exist_ok=True)

    por_cuenta, ignorados = descargar_eecc(almacen, carpeta_eecc_id, empresa_cfg["cuentas"], trabajo_dir)
    total_eecc = sum(len(v) for v in por_cuenta.values())
    if total_eecc == 0:
        raiz_nombre = config.get("drive", {}).get("raiz_nombre", "?")
        logger.error(
            "No se encontró ningún EECC para '%s' en la carpeta de Drive '%s/CONCILIACION/%s/EECC' (id: %s).",
            args.empresa, raiz_nombre, args.mes, carpeta_eecc_id,
        )
        return 1

    eecc_principal, eecc_adicionales = separar_principal(por_cuenta, empresa_cfg["cuentas"])

    ruta_constancias = descargar_constancias(almacen, carpeta_constancias_id, empresa_cfg["cuentas"], trabajo_dir)

    if args.comprobantes:
        ruta_csv = pathlib.Path(args.comprobantes)
        n_comprobantes = contar_filas_csv(ruta_csv)
    else:
        id_contable = ((config.get("sheets") or {}).get("contable") or "").strip()
        if not id_contable:
            logger.error(
                "config['sheets']['contable'] está vacío: no hay Sheet contable del que derivar el CSV de "
                "comprobantes. Usa --comprobantes <ruta.csv> para pasar uno a mano."
            )
            return 1
        try:
            servicio_sheets = auth_google.servicio_sheets()
        except Exception as exc:
            logger.error(str(exc))
            return 1
        rango = (config.get("sheets") or {}).get("rango_contable", "A1:AF")
        filas = leer_filas_sheet_contable(servicio_sheets, id_contable, rango)
        ruta_csv = trabajo_dir / "comprobantes.csv"
        n_comprobantes = filtrar_y_escribir_csv(
            filas, empresa_cfg["nombre_corto"], empresa_cfg["nombre_motor"], args.mes, ruta_csv
        )
        logger.info("CSV de comprobantes derivado del Sheet contable: %d fila(s) -> %s", n_comprobantes, ruta_csv)

    ruta_heredar = None
    if args.sin_heredar:
        logger.info("--sin-heredar: no se intenta heredar la depuración del mes anterior.")
    else:
        try:
            ruta_heredar = resolver_heredar(
                almacen, carpeta_conciliacion_id, empresa_cfg["nombre_corto"], args.mes, trabajo_dir
            )
        except Exception as exc:
            logger.warning("No se pudo resolver la herencia del mes anterior (se sigue sin heredar): %s", exc)

    mes_nombre, anio = mes_en_espanol(args.mes)
    nombre_xlsx = f"CONCILIACION {empresa_cfg['nombre_corto']} - {mes_nombre} {anio}.xlsx"
    ruta_salida_xlsx = trabajo_dir / nombre_xlsx
    ruta_pendientes = trabajo_dir / "pendientes.json"

    pdf_password = resolver_ruc_empresa(config, empresa_cfg["nombre_corto"])
    argumentos_motor = construir_argumentos_motor(
        eecc_principal,
        eecc_adicionales,
        ruta_constancias,
        ruta_salida_xlsx,
        empresa_cfg["nombre_motor"],
        ruta_csv,
        ruta_pendientes,
        ruta_heredar,
        pdf_password=pdf_password,
    )
    try:
        invocar_motor(argumentos_motor)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    if not ruta_salida_xlsx.exists():
        logger.error("El motor terminó sin error pero no generó '%s'.", ruta_salida_xlsx)
        return 1

    # Igual que con el correo: la garantía de --dry-run está en este if, no
    # solo en un comentario. Nada se sube a Drive cuando args.dry_run es True.
    enlace_drive = None
    if args.dry_run:
        logger.info("--dry-run: no se sube nada a Drive. Resultado local: %s", ruta_salida_xlsx)
    else:
        nombre_subido, file_id = subir_resultado(almacen, carpeta_mes_id, nombre_xlsx, ruta_salida_xlsx)
        enlace_drive = almacen.enlace(file_id)
        logger.info("Subido a Drive como '%s': %s", nombre_subido, enlace_drive)

    imprimir_resumen(
        empresa_cfg=empresa_cfg,
        mes=args.mes,
        total_eecc=total_eecc,
        ignorados=ignorados,
        ruta_constancias=ruta_constancias,
        n_comprobantes=n_comprobantes,
        ruta_heredar=ruta_heredar,
        ruta_salida_xlsx=ruta_salida_xlsx,
        enlace_drive=enlace_drive,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
