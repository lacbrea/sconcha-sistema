"""Sube al buzon de Drive los comprobantes de una carpeta local.

Existe para el backfill del proceso viejo: hasta julio 2026 los comprobantes
en papel se escaneaban a carpetas de OneDrive (una por empresa/anio/mes) y
nunca entraban al sistema. procesar.py NO tiene entrada local -- solo lee del
buzon de Drive, a proposito, para que todas las vias de entrada compartan un
solo camino. Esta herramienta es el puente: deja los archivos en el buzon y
de ahi en adelante los procesa el mismo pipeline que a un archivo arrastrado
a mano.

Es idempotente por nombre: un archivo que ya esta en el buzon no se vuelve a
subir. Correrla dos veces sobre la misma carpeta no duplica nada, asi que
sirve para cargar una carpeta que todavia se esta completando (el caso real
de julio 2026: los comprobantes llegaron en tandas).

No borra ni modifica nada en la carpeta de origen: OneDrive es la operacion
en produccion y aca solo se lee.

Uso:
    C:\\Python312\\python.exe herramientas\\subir_carpeta_al_buzon.py <carpeta> [--dry-run]
    C:\\Python312\\python.exe herramientas\\subir_carpeta_al_buzon.py <carpeta> --patron "*.pdf"

Ejemplo real:
    ... subir_carpeta_al_buzon.py "C:\\Users\\luisa\\OneDrive\\SCONCHA\\Sconcha 2\\FACTURAS\\EL TEMPLO\\2026\\JULIO"
"""
from __future__ import annotations

import argparse
import mimetypes
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from almacen_drive import AlmacenDrive  # noqa: E402
from procesar import cargar_config  # noqa: E402

# Extensiones que procesar.py sabe manejar. Subir un .docx o un .zip al buzon
# solo lograria que termine en 02_REVISAR con un motivo, asi que se filtran
# antes en vez de ensuciar la carpeta de revision.
EXTENSIONES_VALIDAS = {".pdf", ".xml", ".jpg", ".jpeg", ".png", ".heic"}


def archivos_a_subir(carpeta: pathlib.Path, patron: str) -> list[pathlib.Path]:
    """Archivos de 'carpeta' (recursivo) con extension soportada, ordenados.

    Recursivo porque las carpetas del proceso viejo tienen subcarpetas por
    tipo de documento (BOLETAS/, FACTURAS/), y el buzon es plano: lo que
    importa para el sistema es el contenido del comprobante, no en que
    subcarpeta estaba archivado.
    """
    encontrados = [
        ruta
        for ruta in sorted(carpeta.rglob(patron))
        if ruta.is_file() and ruta.suffix.lower() in EXTENSIONES_VALIDAS
    ]
    return encontrados


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sube los comprobantes de una carpeta local al buzon de Drive."
    )
    parser.add_argument("carpeta", help="Carpeta local con los comprobantes (se recorre recursivamente).")
    parser.add_argument("--config", default="config.yaml", help="Ruta al config.yaml del negocio.")
    parser.add_argument("--patron", default="*", help="Patron de nombre a subir (por defecto todos).")
    parser.add_argument(
        "--dry-run", action="store_true", help="Solo informa que subiria, sin tocar Drive."
    )
    args = parser.parse_args(argv)

    carpeta = pathlib.Path(args.carpeta)
    if not carpeta.is_dir():
        print(f"No existe la carpeta: {carpeta}")
        return 1

    config = cargar_config(pathlib.Path(args.config))
    buzon_id = ((config.get("drive", {}).get("carpetas") or {}).get("buzon") or "").strip()
    if not buzon_id:
        print("drive.carpetas.buzon esta vacio en config.yaml; corre init_negocio.py primero.")
        return 1

    archivos = archivos_a_subir(carpeta, args.patron)
    if not archivos:
        print(f"No hay archivos con extension soportada en {carpeta}")
        return 1

    print(f"Carpeta: {carpeta}")
    print(f"Archivos con extension soportada: {len(archivos)}")
    if args.dry_run:
        print("\n[DRY-RUN] no se sube nada. Se subiria:")
        for ruta in archivos:
            print(f"  {ruta.name}")
        return 0

    import auth_google  # import diferido: --dry-run no necesita credenciales

    almacen = AlmacenDrive(auth_google.servicio_drive())

    subidos = omitidos = 0
    for ruta in archivos:
        # El buzon es plano y procesar.py identifica el comprobante por su
        # contenido, no por su nombre, asi que el nombre original se conserva
        # tal cual: es lo unico que permite volver al archivo de OneDrive si
        # hay que compararlo con el original.
        if almacen.buscar_por_nombre(buzon_id, ruta.name) is not None:
            print(f"  ya estaba en el buzon, se omite: {ruta.name}")
            omitidos += 1
            continue
        mimetype = mimetypes.guess_type(ruta.name)[0] or "application/octet-stream"
        almacen.subir(buzon_id, ruta.name, ruta, mimetype=mimetype)
        print(f"  subido: {ruta.name}")
        subidos += 1

    print(f"\nSubidos: {subidos} | Ya estaban: {omitidos} | Total revisados: {len(archivos)}")
    if subidos:
        print("Ahora corre procesar.py para que los lea desde el buzon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
