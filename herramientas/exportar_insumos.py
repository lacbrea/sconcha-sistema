"""Script de conversion de un solo uso: SQL semilla -> insumos.csv.

Lee C:\\Users\\luisa\\OneDrive\\SCONCHA\\AUTO\\sconcha-app\\supabase\\migrations\\020_seed_insumos.sql
(solo lectura, no se modifica) y produce insumos.csv en la raiz del proyecto con
columnas: insumo,categoria,unidad_base,alias

Una fila por alias declarado en aliases_insumos, mas una fila adicional por
insumo con su propio nombre como alias (para que la coincidencia exacta
tambien funcione contra el nombre "limpio" del insumo, que no siempre esta
duplicado como alias en el SQL).

Este script se puede volver a correr en cualquier momento para regenerar el
CSV si el SQL semilla cambia; no es necesario borrarlo.
"""
from __future__ import annotations

import csv
import pathlib
import re

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SQL_PATH = pathlib.Path(
    r"C:\Users\luisa\OneDrive\SCONCHA\AUTO\sconcha-app\supabase\migrations\020_seed_insumos.sql"
)
CSV_PATH = RAIZ / "insumos.csv"

# Captura cada tupla de la sentencia INSERT INTO insumos (...) VALUES (...), (...), ...
RE_INSUMO = re.compile(
    r"\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)"
)

# Captura cada sentencia INSERT INTO aliases_insumos (una por linea).
RE_ALIAS = re.compile(
    r"INSERT INTO aliases_insumos\s*\(descripcion,\s*insumo_id\)\s*"
    r"SELECT\s*'([^']*)'\s*,\s*id\s*FROM\s*insumos\s*WHERE\s*nombre\s*=\s*'([^']*)'"
)


def parsear_insumos(sql: str) -> list[tuple[str, str, str]]:
    """Devuelve lista de (nombre, unidad, categoria) desde el bloque INSERT INTO insumos."""
    inicio = sql.index("INSERT INTO insumos")
    fin = sql.index("ON CONFLICT (nombre)", inicio)
    bloque = sql[inicio:fin]
    filas = RE_INSUMO.findall(bloque)
    # RE_INSUMO captura (nombre, unidad, categoria) en ese orden porque el
    # VALUES es (nombre, unidad, categoria, costo_unitario, stock, stock_minimo).
    return [(nombre, unidad, categoria) for nombre, unidad, categoria in filas]


def parsear_aliases(sql: str) -> list[tuple[str, str]]:
    """Devuelve lista de (descripcion, nombre_insumo) desde las sentencias aliases_insumos."""
    return RE_ALIAS.findall(sql)


def main() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    insumos = parsear_insumos(sql)
    aliases = parsear_aliases(sql)

    # categoria por nombre de insumo, para poder anotar tambien las filas de alias.
    categoria_por_nombre = {nombre: categoria for nombre, unidad, categoria in insumos}
    unidad_por_nombre = {nombre: unidad for nombre, unidad, categoria in insumos}

    filas_csv: list[tuple[str, str, str, str]] = []

    # Una fila por alias.
    aliases_sin_insumo = []
    for descripcion, nombre_insumo in aliases:
        if nombre_insumo not in categoria_por_nombre:
            aliases_sin_insumo.append((descripcion, nombre_insumo))
            continue
        filas_csv.append(
            (
                nombre_insumo,
                categoria_por_nombre[nombre_insumo],
                unidad_por_nombre[nombre_insumo],
                descripcion,
            )
        )

    # Una fila por insumo con su propio nombre como alias.
    for nombre, unidad, categoria in insumos:
        filas_csv.append((nombre, categoria, unidad, nombre))

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["insumo", "categoria", "unidad_base", "alias"])
        for insumo, categoria, unidad_base, alias in filas_csv:
            writer.writerow([insumo, categoria, unidad_base, alias])

    print(f"SQL leido: {SQL_PATH}")
    print(f"Insumos encontrados (INSERT INTO insumos): {len(insumos)}")
    print(f"Aliases encontrados (INSERT INTO aliases_insumos): {len(aliases)}")
    if aliases_sin_insumo:
        print(
            f"ADVERTENCIA: {len(aliases_sin_insumo)} alias(es) apuntan a un "
            f"nombre de insumo que no aparece en el bloque INSERT INTO insumos:"
        )
        for descripcion, nombre_insumo in aliases_sin_insumo:
            print(f"  - alias={descripcion!r} nombre={nombre_insumo!r}")
    print(f"Filas escritas en {CSV_PATH}: {len(filas_csv)}")
    print(
        f"  ({len(aliases) - len(aliases_sin_insumo)} de alias + {len(insumos)} de nombre propio)"
    )


if __name__ == "__main__":
    main()
