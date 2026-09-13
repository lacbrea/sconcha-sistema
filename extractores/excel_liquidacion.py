"""Parser determinista de liquidaciones de compra en Excel (verdura del
mercado Caquetá, pescado del terminal pesquero).

Desde septiembre de 2026 estas compras llegan como un `.xlsx` con un formato
propio del negocio (no un comprobante SUNAT: no hay RUC, ni serie, ni IGV).
Igual que `extractores.xml_ubl`, esta es la vía preferente frente al modelo
Claude: el archivo ya trae los datos en celdas, así que leerlo por posición
de encabezado es exacto y no cuesta nada (0 llamadas al modelo).

Filosofía defensiva, igual que xml_ubl.py: un archivo real trae typos (año
mal tipeado en el título, "PQUE" en vez de "PQTE", un espacio de más en
"IMPORTE ") y filas irregulares (producto sin precio, sin fila TOTAL). Cada
paso está aislado -- si algo no cuadra se agrega una advertencia a
`comp.advertencias` y se sigue, salvo los dos casos que sí son un error real
de formato (no hay encabezados, no hay ninguna fecha reconocible): esos sí
lanzan `ValueError`, porque sin encabezados o sin fecha no hay nada
razonable que extraer.
"""
from __future__ import annotations

import pathlib
import re
import unicodedata
from datetime import date

import openpyxl

from esquema import ComprobanteExtraido, ItemExtraido

# Unidades vistas en los archivos reales, normalizadas al vocabulario de
# esquema.py ('kg'|'g'|'L'|'mL'|'unid'|'paq'|'caja'). "PQUE" es un typo real
# del negocio (no "PQTE"): se acepta igual, no se corrige el archivo original.
_UNIDADES = {
    "KG": "kg", "KGS": "kg", "KILO": "kg",
    "G": "g", "GR": "g", "GRS": "g",
    "L": "L", "LT": "L", "LTS": "L", "LITRO": "L",
    "ML": "mL",
    "UNIDAD": "unid", "UNID": "unid", "UND": "unid", "UN": "unid", "U": "unid",
    "PQTE": "paq", "PQUE": "paq", "PAQ": "paq", "PAQUETE": "paq",
    "CAJA": "caja", "CJ": "caja",
}

# DD.MM.YY o DD.MM.YYYY, aceptando '.', '-' o '/' como separador (indistinto
# entre los tres huecos: más tolerante que exigir el mismo separador dos
# veces, y no hay ambigüedad real en estos nombres de archivo).
_PATRON_FECHA = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})")

# Tolerancia para comparar el importe cacheado de la fórmula (=E4*B4) contra
# cantidad*precio recalculado -- redondeos de Excel, no error de lectura.
_TOLERANCIA_IMPORTE = 0.01


def extraer(ruta: pathlib.Path) -> ComprobanteExtraido:
    """Parsea una liquidación de compra en Excel (formato propio del negocio).

    Carga el archivo dos veces con openpyxl: una vez `data_only=False` (para
    poder ubicar la fila TOTAL por su fórmula/texto sin depender de que
    Excel haya guardado un valor cacheado) y otra `data_only=True` (para leer
    los importes ya calculados). Usa siempre la primera hoja: los 3 archivos
    reales verificados solo traen 'Hoja1', y no hay necesidad de adivinar
    cuál hoja usar si en el futuro aparece una segunda.
    """
    ruta = pathlib.Path(ruta)
    advertencias: list[str] = []

    wb_formulas = openpyxl.load_workbook(ruta, data_only=False)
    wb_valores = openpyxl.load_workbook(ruta, data_only=True)
    ws = wb_formulas.worksheets[0]
    ws_valores = wb_valores.worksheets[0]

    fila_encabezado, columnas = _buscar_fila_encabezados(ws)
    if fila_encabezado is None:
        raise ValueError(
            "el Excel no tiene el formato de liquidación: faltan los encabezados "
            "CANTIDAD/PRODUCTO/IMPORTE"
        )

    col_cantidad = columnas.get("cantidad")
    col_unidad = columnas.get("unidad")
    col_producto = columnas.get("producto")
    col_precio = columnas.get("precio")
    col_importe = columnas.get("importe")

    titulo = _buscar_titulo(ws, fila_encabezado)

    items, fila_total, total_cacheado = _leer_filas(
        ws, ws_valores, fila_encabezado, col_cantidad, col_unidad, col_producto, col_precio, col_importe, advertencias
    )

    if fila_total is None:
        advertencias.append("no se encontró la fila TOTAL")

    suma_items = round(
        sum(item.total_linea for item in items if item.total_linea is not None), 2
    )
    total = _a_numero(total_cacheado)
    total = suma_items if total is None else round(total, 2)

    fecha_emision, proveedor = _resolver_fecha_y_proveedor(ruta, titulo, advertencias)

    comp = ComprobanteExtraido(origen="excel", confianza=1.0)
    comp.tipo_documento = "liquidacion"
    comp.moneda = "PEN"
    comp.condicion = "contado"
    comp.proveedor_ruc = None
    comp.serie_numero = None
    comp.cliente_ruc = None
    comp.subtotal = None
    comp.igv = None
    comp.total = total
    comp.proveedor_razon_social = proveedor
    comp.fecha_emision = fecha_emision
    comp.items = items
    comp.advertencias = advertencias
    return comp


# -----------------------------------------------------------------------------
# Encabezados
# -----------------------------------------------------------------------------
def _normalizar(texto) -> str:
    """Mayúsculas, sin tildes, espacios colapsados. Usado tanto para ubicar
    encabezados (nombres de columna) como para normalizar unidades."""
    if texto is None:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().upper()


def _identificar_columna(texto_normalizado: str) -> str | None:
    if texto_normalizado == "CANTIDAD":
        return "cantidad"
    if texto_normalizado == "PRODUCTO":
        return "producto"
    if texto_normalizado == "IMPORTE":
        return "importe"
    if texto_normalizado == "U. DE MEDIDA" or texto_normalizado.startswith("UNIDAD") or texto_normalizado.startswith("U."):
        return "unidad"
    if texto_normalizado.startswith("P. UNITARIO") or texto_normalizado.startswith("PRECIO"):
        return "precio"
    return None


def _buscar_fila_encabezados(ws) -> tuple[int | None, dict[str, int]]:
    """Busca la primera fila que traiga CANTIDAD, PRODUCTO e IMPORTE, sin
    asumir que empieza en una fila o columna fija -- el archivo real arranca
    en la columna B, pero nada garantiza que siempre sea así."""
    for fila in ws.iter_rows():
        columnas: dict[str, int] = {}
        for celda in fila:
            tipo = _identificar_columna(_normalizar(celda.value))
            if tipo and tipo not in columnas:
                columnas[tipo] = celda.column
        if "cantidad" in columnas and "producto" in columnas and "importe" in columnas:
            return fila[0].row, columnas
    return None, {}


def _buscar_titulo(ws, fila_encabezado: int) -> str | None:
    """Primera celda de texto no vacía por encima de la fila de encabezados."""
    for fila in ws.iter_rows(min_row=1, max_row=fila_encabezado - 1):
        for celda in fila:
            if celda.value is not None and str(celda.value).strip():
                return str(celda.value).strip()
    return None


# -----------------------------------------------------------------------------
# Filas de ítems
# -----------------------------------------------------------------------------
def _leer_filas(
    ws, ws_valores, fila_encabezado, col_cantidad, col_unidad, col_producto, col_precio, col_importe, advertencias
):
    items: list[ItemExtraido] = []
    fila_total = None
    total_cacheado = None
    orden = 0

    fila_actual = fila_encabezado + 1
    max_col = ws.max_column
    while fila_actual <= ws.max_row:
        valores_fila = [ws.cell(row=fila_actual, column=c).value for c in range(1, max_col + 1)]

        if any(_normalizar(v) == "TOTAL" for v in valores_fila):
            fila_total = fila_actual
            if col_importe:
                total_cacheado = ws_valores.cell(row=fila_actual, column=col_importe).value
            break

        if all(v is None or (isinstance(v, str) and not v.strip()) for v in valores_fila):
            fila_actual += 1
            continue

        producto_raw = ws.cell(row=fila_actual, column=col_producto).value if col_producto else None
        producto = str(producto_raw).strip() if producto_raw is not None else ""
        if not producto:
            # Fila sin producto (por debajo del TOTAL suele haber celdas
            # vacías formateadas, pero por si acaso, cualquier fila sin
            # producto no es un ítem real y se salta sin advertencia).
            fila_actual += 1
            continue

        cantidad_val = ws_valores.cell(row=fila_actual, column=col_cantidad).value if col_cantidad else None
        unidad_val = ws.cell(row=fila_actual, column=col_unidad).value if col_unidad else None
        precio_val = ws_valores.cell(row=fila_actual, column=col_precio).value if col_precio else None
        importe_cacheado = ws_valores.cell(row=fila_actual, column=col_importe).value if col_importe else None

        cantidad = _a_numero(cantidad_val)
        precio = _a_numero(precio_val)

        if cantidad is None or precio is None:
            advertencias.append(
                f"fila {fila_actual} ('{producto}'): falta la cantidad o el precio unitario"
            )

        importe = _a_numero(importe_cacheado)
        if importe is None:
            if cantidad is not None and precio is not None:
                importe = cantidad * precio
        elif cantidad is not None and precio is not None:
            calculado = cantidad * precio
            if abs(round(importe, 2) - round(calculado, 2)) > _TOLERANCIA_IMPORTE:
                advertencias.append(
                    f"fila {fila_actual} ('{producto}'): el importe (S/ {round(importe, 2)}) no "
                    f"coincide con cantidad x precio (S/ {round(calculado, 2)})"
                )
        if importe is not None:
            importe = round(importe, 2)

        orden += 1
        unidad = _normalizar_unidad(unidad_val, advertencias, f"fila {fila_actual} ('{producto}')")

        items.append(
            ItemExtraido(
                orden=orden,
                descripcion=producto,
                cantidad=cantidad,
                unidad=unidad,
                precio_unitario=precio,
                total_linea=importe,
            )
        )
        fila_actual += 1

    return items, fila_total, total_cacheado


def _normalizar_unidad(valor, advertencias: list[str], contexto: str) -> str | None:
    if valor is None:
        return None
    texto_original = str(valor).strip()
    if not texto_original:
        return None
    resultado = _UNIDADES.get(_normalizar(texto_original))
    if resultado is None:
        advertencias.append(f"{contexto}: unidad desconocida '{texto_original}', no se pudo normalizar")
        return None
    return resultado


def _a_numero(valor) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        # Por si algún valor viniera como texto con coma decimal (no se ha
        # visto en los archivos reales, pero xlsx puede traer texto suelto
        # en una celda que debería ser numérica).
        return float(texto.replace(",", "."))
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Fecha de emisión y proveedor (a partir del nombre del archivo y el título)
# -----------------------------------------------------------------------------
def _extraer_fecha(texto: str | None) -> tuple[str | None, tuple[int, int] | None]:
    """Busca un DD.MM.YY(YY) dentro de `texto`. Devuelve (fecha ISO, span) o
    (None, None) si no hay ningún patrón reconocible o la fecha no es válida
    como calendario (ej. mes 13)."""
    if not texto:
        return None, None
    coincidencia = _PATRON_FECHA.search(texto)
    if not coincidencia:
        return None, None
    dia, mes, anio = (int(g) for g in coincidencia.groups())
    if anio < 100:
        anio += 2000
    try:
        fecha = date(anio, mes, dia)
    except ValueError:
        return None, None
    return fecha.strftime("%Y-%m-%d"), coincidencia.span()


def _resolver_fecha_y_proveedor(
    ruta: pathlib.Path, titulo: str | None, advertencias: list[str]
) -> tuple[str, str | None]:
    """La fecha manda desde el NOMBRE del archivo (convención del negocio);
    el título es respaldo si el nombre no trae fecha. Caso real verificado:
    'CAQUETA 01.09.26.xlsx' con título 'VERDURA CAQUETA 01.09.23' (año mal
    tipeado en el título) -- se usa la del nombre y se advierte, nunca se
    lanza una excepción por esto solo.
    """
    fecha_nombre, _ = _extraer_fecha(ruta.stem)
    fecha_titulo, span_titulo = _extraer_fecha(titulo) if titulo else (None, None)

    if fecha_nombre:
        fecha_emision = fecha_nombre
        if fecha_titulo and fecha_titulo != fecha_nombre:
            advertencias.append(
                f"la fecha en el nombre del archivo ({fecha_nombre}) difiere de la fecha en el "
                f"título ('{titulo}': {fecha_titulo}); se usa la del nombre del archivo"
            )
    elif fecha_titulo:
        fecha_emision = fecha_titulo
    else:
        raise ValueError(
            "no se pudo determinar la fecha de emisión: ni el nombre del archivo ni el título "
            "traen una fecha DD.MM.AA reconocible"
        )

    proveedor = None
    if titulo:
        if span_titulo:
            titulo_sin_fecha = titulo[: span_titulo[0]] + titulo[span_titulo[1]:]
        else:
            titulo_sin_fecha = titulo
        proveedor = re.sub(r"\s+", " ", titulo_sin_fecha).strip() or None

    return fecha_emision, proveedor
