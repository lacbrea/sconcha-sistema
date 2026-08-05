"""Prepara un negocio nuevo para usar el sistema de conciliación.

Hace tres cosas, en orden, y es seguro correrlo varias veces (idempotente):

1. Crea en Google Drive, por API, la carpeta raíz del negocio
   (drive.raiz_nombre, dentro de "Mi unidad") y las subcarpetas
   00_BUZON, 01_PROCESADO y 02_REVISAR. Si una carpeta ya existe (mismo
   nombre y mismo padre), la reutiliza; nunca la recrea ni la vacía. Los 4
   ids resultantes se guardan en config.yaml -> drive.carpetas.
   Si config.yaml trae la sección 'conciliacion' (opcional), crea además la
   carpeta CONCILIACION y guarda su id en conciliacion.carpeta.
2. Crea los dos Google Sheets del negocio ("contable" y "detalle") con sus
   cabeceras, usando la cuenta de Google autenticada por auth_google.py. Si
   config.yaml ya trae un ID de Sheet configurado, verifica que siga siendo
   accesible y lo reutiliza en vez de crear uno nuevo.
3. Escribe de vuelta en config.yaml los IDs de las carpetas y de los Sheets
   recién creados (si corresponde), sin tocar el resto del archivo
   (comentarios incluidos).

Uso:
    C:\\Python312\\python.exe init_negocio.py --config config.yaml
    C:\\Python312\\python.exe init_negocio.py --config config.yaml --dry-run

Con --dry-run no se llama a la API de Google (ni Drive ni Sheets) y no se
escribe config.yaml: solo se informa qué haría cada paso. No hace falta
tener credenciales configuradas para correr --dry-run.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import re
import sys

import yaml

from almacen_drive import AlmacenDrive
from registro_sheets import COLUMNAS_CONTABLE, COLUMNAS_DETALLE

logger = logging.getLogger("procesar.init_negocio")

# Nombres fijos de las subcarpetas de trabajo dentro de la carpeta raíz del
# negocio. No son configurables por config.yaml (a diferencia de
# drive.raiz_nombre): son una convención del sistema, igual en todos los
# negocios que lo usan.
NOMBRE_BUZON = "00_BUZON"
NOMBRE_PROCESADO = "01_PROCESADO"
NOMBRE_REVISAR = "02_REVISAR"

# Carpeta de la conciliación bancaria. A diferencia de las tres anteriores,
# esta es OPCIONAL: solo se crea si config.yaml trae la sección
# 'conciliacion'. Un negocio que use el sistema únicamente para procesar
# comprobantes no la necesita, y crearla igual dejaría una carpeta vacía en
# su Drive pidiendo explicación. Su id se guarda en conciliacion.carpeta (no
# en drive.carpetas: las de ahí son las del pipeline del buzón).
NOMBRE_CONCILIACION = "CONCILIACION"

# -----------------------------------------------------------------------------
# Cabeceras de los dos Sheets del negocio. Fila 1 de cada spreadsheet.
#
# Se IMPORTAN de registro_sheets.py en vez de copiarse: ese módulo escribe
# cada fila del Sheet como una lista posicional (sin mapear por nombre de
# columna), así que la cabecera y la fila tienen que salir siempre de la
# misma fuente. Una copia local -por exacta que sea el día que se escribe-
# diverge en el primer cambio que se haga de un solo lado: eso ya pasó (esta
# lista llegó a quedarse en 31 columnas mientras COLUMNAS_CONTABLE ya tenía
# 32, con ARCHIVO agregada al final; el Sheet se habría creado con una
# cabecera de menos y cada dato a partir de ahí habría quedado bajo la
# etiqueta equivocada, en silencio). Importar en vez de copiar elimina esa
# clase de bug de raíz, no solo esta instancia.
#
# registro_sheets.py NO importa googleapiclient a nivel de módulo (solo lo
# menciona en un docstring; el import real vive en auth_google.py y en
# Registro._obtener_servicio, ambos diferidos) -verificado leyendo sus
# imports antes de este cambio-, así que este import es liviano: no toca
# red ni credenciales y --dry-run sigue funcionando igual.
# -----------------------------------------------------------------------------
ENCABEZADOS_CONTABLE = COLUMNAS_CONTABLE
ENCABEZADOS_DETALLE = COLUMNAS_DETALLE


def cargar_config(ruta_config: pathlib.Path) -> dict:
    if not ruta_config.exists():
        sys.exit(
            f"No se encontró '{ruta_config}'. Copia config.ejemplo.yaml como "
            f"config.yaml y rellénalo antes de correr init_negocio.py."
        )
    with ruta_config.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def preparar_carpetas(config: dict, almacen: "AlmacenDrive | None", dry_run: bool) -> dict[str, str]:
    """Asegura (crea si hace falta) la carpeta raíz del negocio en "Mi
    unidad" y las 3 subcarpetas de trabajo, todo por API de Drive.

    Devuelve {"raiz": id, "buzon": id, "procesado": id, "revisar": id} en
    modo real; {} en --dry-run (no hay nada que devolver: no se llama a la
    API, así que no hay ids que informar, solo la intención).

    Nota: a diferencia de la versión anterior (basada en pathlib), esta
    función no distingue en el mensaje impreso entre "creada" y "ya
    existía": AlmacenDrive.asegurar_carpeta() no expone esa información (es
    idempotente por diseño, pero no dice si encontró o creó), y el
    contrato de la clase no incluye un método aparte para verificarlo. La
    garantía de que no duplica carpetas se prueba por comportamiento en
    tests/test_init_negocio.py, no por el texto impreso.
    """
    raiz_nombre = config["drive"]["raiz_nombre"]
    con_conciliacion = bool(config.get("conciliacion"))

    print(f"\n== Carpetas en Drive (raíz: '{raiz_nombre}') ==")
    if dry_run:
        print(f"[DRY-RUN] se aseguraría (creándola si no existe) la carpeta raíz '{raiz_nombre}' en Mi unidad")
        nombres = [NOMBRE_BUZON, NOMBRE_PROCESADO, NOMBRE_REVISAR]
        if con_conciliacion:
            nombres.append(NOMBRE_CONCILIACION)
        for nombre in nombres:
            print(f"[DRY-RUN] se aseguraría (creándola si no existe): {raiz_nombre}/{nombre}")
        if not con_conciliacion:
            print("[DRY-RUN] config.yaml no trae sección 'conciliacion': no se crearía la carpeta CONCILIACION.")
        return {}

    id_raiz = almacen.asegurar_carpeta(raiz_nombre)
    print(f"raíz: {raiz_nombre} ({id_raiz})")
    id_buzon = almacen.asegurar_carpeta(NOMBRE_BUZON, id_raiz)
    print(f"  {NOMBRE_BUZON} ({id_buzon})")
    id_procesado = almacen.asegurar_carpeta(NOMBRE_PROCESADO, id_raiz)
    print(f"  {NOMBRE_PROCESADO} ({id_procesado})")
    id_revisar = almacen.asegurar_carpeta(NOMBRE_REVISAR, id_raiz)
    print(f"  {NOMBRE_REVISAR} ({id_revisar})")

    ids = {"raiz": id_raiz, "buzon": id_buzon, "procesado": id_procesado, "revisar": id_revisar}

    if con_conciliacion:
        id_conciliacion = almacen.asegurar_carpeta(NOMBRE_CONCILIACION, id_raiz)
        print(f"  {NOMBRE_CONCILIACION} ({id_conciliacion})")
        ids["conciliacion"] = id_conciliacion

    return ids


def _spreadsheet_accesible(servicio_sheets, spreadsheet_id: str) -> bool:
    try:
        servicio_sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        return True
    except Exception as exc:  # ID inválido, borrado, o sin permiso
        logger.warning(
            "El Sheet configurado (%s) no es accesible: %s", spreadsheet_id, exc
        )
        return False


def _crear_spreadsheet(servicio_sheets, titulo: str, encabezados: list[str]) -> str:
    cuerpo = {"properties": {"title": titulo}, "sheets": [{"properties": {"title": "Hoja 1"}}]}
    resultado = servicio_sheets.spreadsheets().create(body=cuerpo, fields="spreadsheetId").execute()
    spreadsheet_id = resultado["spreadsheetId"]

    servicio_sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="A1",
        valueInputOption="RAW",
        body={"values": [encabezados]},
    ).execute()

    # Encabezado en negrita y fila 1 congelada; no es crítico si falla.
    try:
        servicio_sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold",
                        }
                    },
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                ]
            },
        ).execute()
    except Exception as exc:
        logger.warning("No se pudo aplicar el formato de encabezado (no crítico): %s", exc)

    return spreadsheet_id


def preparar_sheet(
    servicio_sheets, negocio: str, sufijo: str, encabezados: list[str], id_actual: str, dry_run: bool
) -> str:
    # Defecto de replicabilidad encontrado y corregido en la Fase 2: la
    # version anterior anteponia "SCONCHA" al titulo sin importar el
    # negocio (ej. "SCONCHA EL FARO - contable" para un negocio que no es
    # SCONCHA), confuso para el dueno de otro negocio que ve su propio
    # Sheet con el nombre de una cevicheria ajena. El titulo ahora usa solo
    # el nombre configurado en config.yaml -> negocio. Para SCONCHA mismo
    # el resultado no cambia ("SCONCHA - contable"), porque negocio ya es
    # "SCONCHA".
    titulo = f"{negocio} - {sufijo}"

    if id_actual:
        if dry_run:
            print(f"[DRY-RUN] se verificaría y reutilizaría el Sheet '{sufijo}' existente: {id_actual}")
            return id_actual
        if _spreadsheet_accesible(servicio_sheets, id_actual):
            print(f"Sheet '{sufijo}' ya existe, se reutiliza: {id_actual}")
            return id_actual
        sys.exit(
            f"config.yaml trae un ID de Sheet '{sufijo}' ({id_actual}) que ya no es "
            f"accesible con la cuenta de Google configurada. Revísalo a mano: si el "
            f"Sheet fue borrado o movido, borra el valor en config.yaml (déjalo como "
            f'"") y vuelve a correr init_negocio.py para crear uno nuevo.'
        )

    if dry_run:
        print(f"[DRY-RUN] se crearía un Sheet nuevo '{sufijo}' titulado '{titulo}'")
        return ""

    spreadsheet_id = _crear_spreadsheet(servicio_sheets, titulo, encabezados)
    print(f"Sheet '{sufijo}' creado: {spreadsheet_id} ({titulo})")
    return spreadsheet_id


def _reemplazar_clave(texto: str, clave: str, valor: str) -> str:
    """Reescribe la línea 'clave: ...' (a cualquier profundidad de
    indentación) dentro de 'texto', preservando el comentario al final de
    la línea si lo hay. No toca ninguna otra línea del archivo."""
    patron = re.compile(rf'(?m)^(\s*{clave}:\s*)(".*?"|\S*)(\s*(?:#.*)?)$')
    if not patron.search(texto):
        logger.warning("No se encontró la clave '%s:' en config.yaml; no se actualiza.", clave)
        return texto
    return patron.sub(lambda m: f'{m.group(1)}"{valor}"{m.group(3)}', texto, count=1)


def _actualizar_ids_en_config(ruta_config: pathlib.Path, id_contable: str, id_detalle: str) -> None:
    """Reescribe solo las líneas 'contable:' y 'detalle:' dentro de config.yaml,
    preservando el resto del archivo (comentarios, orden, formato) tal cual.
    """
    texto = ruta_config.read_text(encoding="utf-8")
    texto = _reemplazar_clave(texto, "contable", id_contable)
    texto = _reemplazar_clave(texto, "detalle", id_detalle)
    ruta_config.write_text(texto, encoding="utf-8")


def _actualizar_carpetas_en_config(ruta_config: pathlib.Path, ids_carpetas: dict[str, str]) -> None:
    """Reescribe las líneas 'raiz:'/'buzon:'/'procesado:'/'revisar:' dentro
    de drive.carpetas en config.yaml, preservando el resto del archivo.
    Reusa el mismo mecanismo de reemplazo por regex que
    _actualizar_ids_en_config (_reemplazar_clave), para no duplicar esa
    lógica.
    """
    texto = ruta_config.read_text(encoding="utf-8")
    for clave in ("raiz", "buzon", "procesado", "revisar"):
        if clave in ids_carpetas:
            texto = _reemplazar_clave(texto, clave, ids_carpetas[clave])
    ruta_config.write_text(texto, encoding="utf-8")


def _actualizar_carpeta_conciliacion_en_config(ruta_config: pathlib.Path, id_carpeta: str) -> None:
    """Reescribe la línea 'carpeta:' de la sección conciliacion en config.yaml.

    Va aparte de _actualizar_carpetas_en_config porque este id no vive en
    drive.carpetas: la conciliación es opcional y su carpeta se guarda dentro
    de su propia sección, que un negocio sin conciliación no tiene.
    """
    texto = ruta_config.read_text(encoding="utf-8")
    texto = _reemplazar_clave(texto, "carpeta", id_carpeta)
    ruta_config.write_text(texto, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepara un negocio nuevo (carpetas + Sheets).")
    parser.add_argument("--config", default="config.yaml", help="Ruta al config.yaml del negocio.")
    parser.add_argument("--dry-run", action="store_true", help="Solo informa qué haría, sin tocar nada.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    ruta_config = pathlib.Path(args.config)
    config = cargar_config(ruta_config)

    print(f"== init_negocio.py: {config.get('negocio', '(sin nombre)')} ==")
    if args.dry_run:
        print("Modo --dry-run: no se modifica nada, solo se informa.\n")

    sheets_config = config.get("sheets", {}) or {}
    id_contable_actual = (sheets_config.get("contable") or "").strip()
    id_detalle_actual = (sheets_config.get("detalle") or "").strip()

    if args.dry_run:
        preparar_carpetas(config, None, True)

        print("\n== Google Sheets ==")
        preparar_sheet(None, config["negocio"], "contable", ENCABEZADOS_CONTABLE, id_contable_actual, True)
        preparar_sheet(None, config["negocio"], "detalle", ENCABEZADOS_DETALLE, id_detalle_actual, True)
        print("\n[DRY-RUN] no se escribe config.yaml.")
        return 0

    import auth_google  # import diferido: no hace falta en --dry-run

    try:
        servicio_sheets = auth_google.servicio_sheets()
    except auth_google.ErrorAutenticacion as exc:
        sys.exit(str(exc))
    try:
        servicio_drive = auth_google.servicio_drive()
    except auth_google.ErrorAutenticacion as exc:
        sys.exit(str(exc))

    almacen = AlmacenDrive(servicio_drive)
    ids_carpetas = preparar_carpetas(config, almacen, False)

    print("\n== Google Sheets ==")
    id_contable = preparar_sheet(
        servicio_sheets, config["negocio"], "contable", ENCABEZADOS_CONTABLE, id_contable_actual, False
    )
    id_detalle = preparar_sheet(
        servicio_sheets, config["negocio"], "detalle", ENCABEZADOS_DETALLE, id_detalle_actual, False
    )

    carpetas_cfg_actual = (config.get("drive", {}).get("carpetas") or {})
    hubo_cambios = False

    if any(ids_carpetas.get(clave) != (carpetas_cfg_actual.get(clave) or "") for clave in ("raiz", "buzon", "procesado", "revisar")):
        _actualizar_carpetas_en_config(ruta_config, ids_carpetas)
        print(f"\nconfig.yaml actualizado con los IDs de las carpetas de Drive ({ruta_config}).")
        hubo_cambios = True

    id_conciliacion = ids_carpetas.get("conciliacion", "")
    conciliacion_cfg_actual = ((config.get("conciliacion") or {}).get("carpeta") or "").strip()
    if id_conciliacion and id_conciliacion != conciliacion_cfg_actual:
        _actualizar_carpeta_conciliacion_en_config(ruta_config, id_conciliacion)
        print(f"config.yaml actualizado con el ID de la carpeta CONCILIACION ({ruta_config}).")
        hubo_cambios = True

    if id_contable != id_contable_actual or id_detalle != id_detalle_actual:
        _actualizar_ids_en_config(ruta_config, id_contable, id_detalle)
        print(f"config.yaml actualizado con los IDs de los Sheets ({ruta_config}).")
        hubo_cambios = True

    if not hubo_cambios:
        print("\nconfig.yaml ya tenía los valores correctos; no se modifica.")

    print("\nListo. El negocio está preparado para correr procesar.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
