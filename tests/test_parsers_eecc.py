"""Tests de conciliacion/parsers_eecc.py — parser de BBVA en PDF y guarda
contra el parseo vacio.

Corren sin red y sin archivos externos: se prueban las funciones puras sobre
texto sintetico que reproduce las trampas REALES del estado de cuenta de BBVA
de julio 2026 (numeros partidos, filas en varias lineas, lineas informativas,
pie 'TOTALES POR ITF'). El PDF real no se versiona: es un documento bancario
de la empresa.

Correr con:
    C:\\Python312\\python.exe -m pytest tests/test_parsers_eecc.py -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

RAIZ_PROYECTO = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ_PROYECTO / "conciliacion") not in sys.path:
    sys.path.insert(0, str(RAIZ_PROYECTO / "conciliacion"))

import parsers_eecc  # noqa: E402


# -----------------------------------------------------------------------------
# Normalización de importes partidos por la extracción de texto
# -----------------------------------------------------------------------------
def test_normalizar_importes_pega_el_decimal_separado():
    """pypdf parte el importe antes del punto decimal: '4,327 .15'. Sin pegarlo,
    el monto se lee como 4327 y la cadena de saldos no cierra."""
    assert parsers_eecc._normalizar_importes_pdf("saldo 4,327 .15 fin") == "saldo 4,327.15 fin"
    assert parsers_eecc._normalizar_importes_pdf("7 .58") == "7.58"
    assert parsers_eecc._normalizar_importes_pdf("1,877 .05 y 5,877 .10") == "1,877.05 y 5,877.10"


def test_normalizar_importes_no_toca_abreviaturas():
    """El patrón exige dígito antes del espacio, así que 'T .C:' (letra) queda
    intacto: si se pegara cualquier espacio-punto se corromperían descripciones."""
    assert parsers_eecc._normalizar_importes_pdf("T .C: 3.9060") == "T .C: 3.9060"
    assert parsers_eecc._normalizar_importes_pdf("IMP .OP . USD") == "IMP .OP . USD"


# -----------------------------------------------------------------------------
# Agrupado de líneas físicas en filas lógicas
# -----------------------------------------------------------------------------
def test_filas_logicas_reensambla_fila_partida_en_varias_lineas():
    """Caso real: la descripción larga parte la fila y los importes quedan en el
    último pedazo. Las tres líneas son UNA sola fila."""
    texto = (
        "05-07 05-07 DEPOS. EN CTA. 001103530100038579 OF DOMINGO\n"
        "CU\n"
        "VEN 17425 1,550.00 0.05 5,877.10\n"
    )
    filas = parsers_eecc._filas_logicas_bbva(texto)
    assert len(filas) == 1
    assert filas[0][0] == "05-07"
    assert "1,550.00" in filas[0][2] and "5,877.10" in filas[0][2]


def test_filas_logicas_separa_dos_movimientos():
    texto = (
        "01-07 01-07 VERISURE .RECIBOS M.68948 17422 -213.76 4,080.88\n"
        "02-07 02-07 *COMIS BBVA EMPRESAS 17423 -40.00 4,040.88\n"
    )
    assert len(parsers_eecc._filas_logicas_bbva(texto)) == 2


def test_filas_logicas_descarta_lineas_informativas():
    """'IMP .OP . USD ... T .C: ...' es el detalle de un consumo en dólares, no
    un movimiento: si se pegara a la fila, sus números desplazarían al saldo."""
    texto = (
        "10-07 10-07 Google YouTubePremiu 17432 -31.83 1,905.60\n"
        "IMP .OP . USD             8.15 T .C: 3.9060\n"
    )
    filas = parsers_eecc._filas_logicas_bbva(texto)
    assert len(filas) == 1
    assert "8.15" not in filas[0][2]


def test_filas_logicas_corta_en_totales_por_itf():
    """El pie del estado trae importes que no son movimientos. Sin cortar ahí se
    pegan a la ÚLTIMA fila y la arruinan — y como el saldo final se calcula de
    lo parseado, la fila perdida no descuadra nada y el error pasa inadvertido.
    Fue un bug real encontrado con el estado de julio 2026."""
    texto = (
        "31-07 31-07 COMIS. TRANSF. INMEDIATA 17466 -5.80 9.44\n"
        "TOTALES POR ITF 1.25\n"
        "OTRA COSA 99.99\n"
    )
    filas = parsers_eecc._filas_logicas_bbva(texto)
    assert len(filas) == 1
    assert "9.44" in filas[0][2]
    assert "99.99" not in filas[0][2]


# -----------------------------------------------------------------------------
# Guarda contra el parseo vacío disfrazado de éxito
# -----------------------------------------------------------------------------
def test_validar_parseo_lanza_si_no_hay_movimientos_ni_saldos():
    """El caso que motivó la guarda: un PDF de BBVA cayó en el parser de
    Interbank y salió con 0 movimientos y anchor_ok=True. La conciliación
    habría reportado que la cuenta cuadra mientras ignoraba el mes entero."""
    meta = {"banco": "INTERBANK", "formato": "pdf", "saldo_inicial": None, "saldo_final": None}
    with pytest.raises(ValueError) as exc:
        parsers_eecc._validar_parseo("EC_lo_que_sea.pdf", [], meta)
    assert "0 movimientos" in str(exc.value)
    assert "EC_lo_que_sea.pdf" in str(exc.value)


def test_validar_parseo_acepta_cuenta_legitimamente_sin_movimientos():
    """Distinto del anterior: la cuenta 4388 de EL TEMPLO no tuvo movimientos en
    junio 2026 y eso NO es un error. Un documento leído bien siempre trae sus
    saldos; uno mal parseado no trae ninguno. Esa es la única señal fiable para
    distinguirlos."""
    meta = {"banco": "INTERBANK", "formato": "pdf", "saldo_inicial": 0.0, "saldo_final": 0.0}
    movimientos, devuelto = parsers_eecc._validar_parseo("EC_4388.pdf", [], meta)
    assert movimientos == []
    assert devuelto is meta


def test_validar_parseo_devuelve_intacto_lo_que_recibe():
    meta = {"banco": "BBVA", "formato": "bbva_pdf", "saldo_inicial": 4294.64, "saldo_final": 9.44}
    movs = [{"fop": "01/07/2026", "cargo": 213.76, "abono": None, "saldo": 4080.88}]
    assert parsers_eecc._validar_parseo("x.pdf", movs, meta) == (movs, meta)
