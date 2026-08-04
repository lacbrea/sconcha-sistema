"""Prepara un negocio nuevo para usar el sistema de conciliación.

Hace tres cosas, en orden, y es seguro correrlo varias veces (idempotente):

1. Crea en disco la estructura de carpetas descrita en config.yaml
   (drive.raiz / buzon, procesado, revisar). Si una carpeta ya existe, la
   reutiliza y lo informa; nunca la recrea ni la vacía.
2. Crea los dos Google Sheets del negocio ("contable" y "detalle") con sus
   cabeceras, usando la cuenta de Google autenticada por auth_google.py. Si
   config.yaml ya trae un ID de Sheet configurado, verifica que siga siendo
   accesible y lo reutiliza en vez de crear uno nuevo.
3. Escribe de vuelta en config.yaml los IDs de los Sheets recién creados
   (si corresponde), sin tocar el resto del archivo (comentarios incluidos).

Uso:
    C:\\Python312\\python.exe init_negocio.py --config config.yaml
    C:\\Python312\\python.exe init_negocio.py --config config.yaml --dry-run

Con --dry-run no se toca el disco, no se llama a la API de Google y no se
escribe config.yaml: solo se informa qué haría cada paso.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import re
import sys

import yaml

from registro_sheets import COLUMNAS_CONTABLE, COLUMNAS_DETALLE

logger = logging.getLogger("procesar.init_negocio")

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


def preparar_carpetas(config: dict, dry_run: bool) -> None:
    raiz = pathlib.Path(config["drive"]["raiz"])
    subcarpetas = [
        config["drive"]["buzon"],
        config["drive"]["procesado"],
        config["drive"]["revisar"],
    ]

    print(f"\n== Carpetas ({raiz}) ==")
    if dry_run:
        print(f"[DRY-RUN] se crearía (si no existe): {raiz}")
        for nombre in subcarpetas:
            print(f"[DRY-RUN] se crearía (si no existe): {raiz / nombre}")
        return

    for ruta in [raiz, *(raiz / nombre for nombre in subcarpetas)]:
        ya_existia = ruta.exists()
        ruta.mkdir(parents=True, exist_ok=True)
        print(f"{'ya existía' if ya_existia else 'creada'}: {ruta}")


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


def _actualizar_ids_en_config(ruta_config: pathlib.Path, id_contable: str, id_detalle: str) -> None:
    """Reescribe solo las líneas 'contable:' y 'detalle:' dentro de config.yaml,
    preservando el resto del archivo (comentarios, orden, formato) tal cual.
    """
    texto = ruta_config.read_text(encoding="utf-8")

    def reemplazar(clave: str, valor: str, texto: str) -> str:
        patron = re.compile(rf'(?m)^(\s*{clave}:\s*)(".*?"|\S*)(\s*(?:#.*)?)$')
        if not patron.search(texto):
            logger.warning("No se encontró la clave '%s:' en config.yaml; no se actualiza.", clave)
            return texto
        return patron.sub(lambda m: f'{m.group(1)}"{valor}"{m.group(3)}', texto, count=1)

    texto = reemplazar("contable", id_contable, texto)
    texto = reemplazar("detalle", id_detalle, texto)
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

    preparar_carpetas(config, args.dry_run)

    print("\n== Google Sheets ==")
    sheets_config = config.get("sheets", {}) or {}
    id_contable_actual = (sheets_config.get("contable") or "").strip()
    id_detalle_actual = (sheets_config.get("detalle") or "").strip()

    if args.dry_run:
        preparar_sheet(None, config["negocio"], "contable", ENCABEZADOS_CONTABLE, id_contable_actual, True)
        preparar_sheet(None, config["negocio"], "detalle", ENCABEZADOS_DETALLE, id_detalle_actual, True)
        print("\n[DRY-RUN] no se escribe config.yaml.")
        return 0

    import auth_google  # import diferido: no hace falta en --dry-run

    try:
        servicio = auth_google.servicio_sheets()
    except auth_google.ErrorAutenticacion as exc:
        sys.exit(str(exc))

    id_contable = preparar_sheet(
        servicio, config["negocio"], "contable", ENCABEZADOS_CONTABLE, id_contable_actual, False
    )
    id_detalle = preparar_sheet(
        servicio, config["negocio"], "detalle", ENCABEZADOS_DETALLE, id_detalle_actual, False
    )

    if id_contable != id_contable_actual or id_detalle != id_detalle_actual:
        _actualizar_ids_en_config(ruta_config, id_contable, id_detalle)
        print(f"\nconfig.yaml actualizado con los IDs de los Sheets ({ruta_config}).")
    else:
        print("\nconfig.yaml ya tenía los IDs correctos; no se modifica.")

    print("\nListo. El negocio está preparado para correr procesar.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
