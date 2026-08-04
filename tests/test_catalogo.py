"""Tests de catalogo.py. Corren sin red y sin credenciales."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from catalogo import Catalogo, normalizar  # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parent.parent
CSV_REAL = RAIZ / "insumos.csv"


def test_insumos_csv_existe_y_tiene_filas():
    assert CSV_REAL.exists(), "corre herramientas/exportar_insumos.py primero"
    contenido = CSV_REAL.read_text(encoding="utf-8").splitlines()
    assert len(contenido) > 1


def test_coincidencia_exacta_por_alias():
    cat = Catalogo(CSV_REAL)
    insumo, categoria, score = cat.emparejar("PALTA KILOS")
    assert insumo == "PALTA"
    assert categoria == "VERDURAS"
    assert score == 1.0


def test_prefijo_insumo_se_limpia_antes_de_emparejar():
    """Descripcion real de Restaurant.pe con el prefijo '( INSUMO )'."""
    cat = Catalogo(CSV_REAL)
    insumo, categoria, score = cat.emparejar("( INSUMO ) ACEITE DE OLIVA LITRO")
    assert insumo == "ACEITE DE OLIVA"
    assert categoria == "ABARROTES"
    assert score == 1.0


def test_coincidencia_dificil_por_substring():
    """Una descripcion de factura real no siempre calza exacto con el alias;
    'ACEITE DE OLIVA' aparece como prefijo de una descripcion mas larga."""
    cat = Catalogo(CSV_REAL)
    insumo, categoria, score = cat.emparejar(
        "ACEITE DE OLIVA EXTRA VIRGEN BOTELLA 500ML"
    )
    assert insumo == "ACEITE DE OLIVA"
    assert categoria == "ABARROTES"
    assert score >= 0.55


def test_descripcion_sin_relacion_no_empareja():
    cat = Catalogo(CSV_REAL)
    insumo, categoria, score = cat.emparejar("SERVICIO DE FUMIGACION TRIMESTRAL")
    assert insumo is None
    assert categoria is None
    assert score < 0.55


def test_no_empareja_solo_por_tokens_genericos_compartidos(tmp_path):
    """Replica el bug real del motor de conciliacion de este proyecto: un
    cargo de MAPFRE cruzo con 'GRUPO GIOBRE PERU S.A.C.' solo por compartir
    el token generico 'PERU' (ver build_conciliacion.py GENERIC_TOKENS).

    Aqui el alias 'CAJA UNIDAD' esta compuesto solo por tokens genericos
    (unidades/envase: CAJA y UNIDAD). Sin la salvaguarda, una descripcion que
    tambien contenga ambas palabras genericas (pero ningun token especifico
    del insumo) alcanzaria score = (2/2) * 0.7 = 0.70, por encima del umbral
    de 0.55, y produciria un emparejamiento erroneo. Con la salvaguarda debe
    quedar sin emparejar porque ningun token compartido tiene 4+ letras y no
    es generico.
    """
    csv_path = tmp_path / "insumos.csv"
    csv_path.write_text(
        "insumo,categoria,unidad_base,alias\n"
        "CAJA DE LAPICERO,UTILES DE OFICINA,und,CAJA UNIDAD\n",
        encoding="utf-8",
    )
    cat = Catalogo(csv_path)

    insumo, categoria, score = cat.emparejar("CAJA CHICA UNIDAD DE MEDIDA")

    assert insumo is None
    assert categoria is None


def test_normalizar_quita_tildes_puntuacion_y_prefijo():
    assert normalizar("( INSUMO ) Ají Panca") == "AJI PANCA"
    assert normalizar("  Café   en    grano.  ") == "CAFE EN GRANO"


def test_normalizar_quita_codigo_de_barras_inicial():
    assert normalizar("7501234567890 GASEOSA COCA COLA") == "GASEOSA COCA COLA"


def test_emparejar_descripcion_vacia_no_falla():
    cat = Catalogo(CSV_REAL)
    insumo, categoria, score = cat.emparejar("")
    assert insumo is None
    assert categoria is None
    assert score == 0.0
