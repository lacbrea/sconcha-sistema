"""Tests de egresos_caja.py — parser del reporte de egresos de caja
(sistema de ventas Restaurant.pe) que consume conciliar.py para armar el
JSON intermedio de --egresos.

Corren sin red y sin archivos externos: se prueban contra fixtures
SINTÉTICOS que reproducen la estructura real de un reporte (frameset +
sheet001.htm, tabla suelta, 10 <td> por fila de datos) verificada contra un
reporte real de jul-2026. El reporte real NO se versiona (dato del negocio).

Correr con:
    C:\\Python312\\python.exe -m pytest tests/test_egresos_caja.py -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

RAIZ_PROYECTO = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROYECTO))

import egresos_caja  # noqa: E402


# -----------------------------------------------------------------------------
# Fixtures sintéticos: mismo shape que el reporte real (10 <td> por fila de
# datos: Fecha, Usuario, Categoria, Caja, Motivo, Entregado A, Moneda,
# Tarjeta, Estado, Monto), con cabeceras de reporte (título/usuario/generado)
# que se descartan por forma (colspan, no 10 <td>), no por posición fija.
# -----------------------------------------------------------------------------
def _fila(fecha, motivo, entregado_a, estado='ACTIVO', moneda='Soles', monto='66.9', usuario='CAJA.LINCE'):
    return (
        f"<tr><td>{fecha}</td><td>{usuario}</td><td colspan=2>Otros</td><td>Caja 01</td>"
        f"<td colspan=4>{motivo}</td><td>{entregado_a}</td><td>{moneda}</td><td>-</td>"
        f"<td>{estado}</td><td>{monto}</td></tr>"
    )


def _tabla_html(filas_extra=()):
    """Tabla completa: cabeceras de reporte (que NO tienen 10 <td>) + fila de
    encabezado de columnas (SÍ tiene 10 <td>, se descarta por texto 'Fecha')
    + filas de datos + fila de total (3 <td>, se descarta por forma)."""
    cabeceras = (
        "<tr><td colspan=7 rowspan=2>EL TEMPLO SAC</td>"
        "<td colspan=7 rowspan=2>Reporte de Egresos para el Periodo...</td></tr>"
        "<tr></tr>"
        "<tr><td colspan=7>Usuario: Luis Castro</td><td colspan=7>Generado: 06/08/2026</td></tr>"
        "<tr><td colspan=14>&nbsp;</td></tr>"
        "<tr><td>Fecha</td><td>Usuario</td><td colspan=2>Categoria</td><td>Caja</td>"
        "<td colspan=4>Motivo</td><td>Entregado A</td><td>Moneda</td><td>Tarjeta</td>"
        "<td>Estado</td><td>Monto</td></tr>"
    )
    total = "<tr><td colspan=9>&nbsp;</td><td colspan=4>Total Egresos Activos (En Moneda Local)</td><td>9999</td></tr>"
    return f"<html><body><table>{cabeceras}{''.join(filas_extra)}{total}</table></body></html>"


FILAS_TIPICAS = [
    _fila('31/07/2026 16:20', 'COMPRA DE VERDURAS Y POLLO', 'EDWIN'),
    _fila('29/07/2026 14:16', 'DEPOSITO DE VENTA EN EFECTIVO', 'BANCO', monto='1400'),
    _fila('15/07/2026 10:00', 'COMPRA ANULADA DE PRUEBA', 'EDWIN', estado='ANULADO', monto='30'),
    _fila('10/07/2026 09:00', 'PROPINA EN DOLARES DE PRUEBA', 'EDWIN', moneda='Dolares', monto='5'),
]


def _frameset_html(href_sheet):
    return (
        '<html xmlns:x="urn:schemas-microsoft-com:office:excel">\n'
        '<head>\n<meta name="Excel Workbook Frameset">\n'
        f'<link id="shLink" href="{href_sheet}">\n</head>\n<body></body></html>'
    )


# -----------------------------------------------------------------------------
# Caso 3: HTML de tabla único (ni frameset ni sheet001.htm "oficial")
# -----------------------------------------------------------------------------
def test_tabla_unica_separa_gastos_y_depositos(tmp_path):
    ruta = tmp_path / "tabla.htm"
    ruta.write_text(_tabla_html(FILAS_TIPICAS), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert r['local'] == 'LINCE'
    assert len(r['gastos']) == 1
    assert r['gastos'][0] == {
        'fecha': '31/07/2026', 'hora': '16:20', 'motivo': 'COMPRA DE VERDURAS Y POLLO',
        'entregado_a': 'EDWIN', 'monto': 66.9,
    }
    assert len(r['depositos']) == 1
    assert r['depositos'][0]['monto'] == 1400.0
    assert r['depositos'][0]['entregado_a'] == 'BANCO'
    assert r['total_gastos'] == 66.9
    assert r['total_depositos'] == 1400.0


def test_fila_anulada_se_ignora_con_motivo_no_en_silencio(tmp_path):
    ruta = tmp_path / "tabla.htm"
    ruta.write_text(_tabla_html(FILAS_TIPICAS), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    ignoradas_anuladas = [x for x in r['filas_ignoradas'] if 'ANULADO' in x]
    assert len(ignoradas_anuladas) == 1
    assert 'ANULADO' in ignoradas_anuladas[0]
    # no debe aparecer entre gastos ni depositos
    motivos = [g['motivo'] for g in r['gastos']] + [d['motivo'] for d in r['depositos']]
    assert not any('ANULADA DE PRUEBA' in m for m in motivos)


def test_moneda_extrana_se_ignora_con_motivo_no_en_silencio(tmp_path):
    ruta = tmp_path / "tabla.htm"
    ruta.write_text(_tabla_html(FILAS_TIPICAS), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    ignoradas_moneda = [x for x in r['filas_ignoradas'] if 'MONEDA' in x]
    assert len(ignoradas_moneda) == 1
    assert 'DOLARES' in ignoradas_moneda[0].upper()


def test_local_extraido_de_caja_punto_local(tmp_path):
    ruta = tmp_path / "tabla.htm"
    filas = [_fila('01/07/2026 10:00', 'COMPRA X', 'EDWIN', usuario='CAJA.MIRAFLORES')]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert r['local'] == 'MIRAFLORES'


def test_sin_usuario_reconocible_local_es_none(tmp_path):
    ruta = tmp_path / "tabla.htm"
    filas = [_fila('01/07/2026 10:00', 'COMPRA X', 'EDWIN', usuario='ADMIN')]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert r['local'] is None


def test_deposito_por_motivo_sin_entregado_a_banco(tmp_path):
    """La regla de depósito es 'entregado_a==BANCO' O 'motivo empieza con
    DEPOSITO DE VENTA' — cualquiera de las dos alcanza."""
    ruta = tmp_path / "tabla.htm"
    filas = [_fila('01/07/2026 10:00', 'DEPOSITO DE VENTA EN EFECTIVO', 'CAJERO', monto='500')]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert len(r['depositos']) == 1
    assert r['gastos'] == []


# -----------------------------------------------------------------------------
# CONCEPTO de los depositos: propina vs venta vs indeterminado (2026-08).
# Los 4 casos de abajo son los mismos que trae el reporte real de MIRAFLORES
# de julio 2026 (ver CLAUDE.md de la tarea): 3 de los 5 depositos son propina
# en efectivo, no venta, y el motivo es el unico dato que lo distingue.
# -----------------------------------------------------------------------------
def test_concepto_propina_mayuscula_singular(tmp_path):
    ruta = tmp_path / "tabla.htm"
    filas = [_fila('02/07/2026 16:17', 'PROPINA EN EFECTIVO 26 AL  02', 'CTA DE LA EMPRESA', monto='120')]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert len(r['depositos']) == 1
    assert r['depositos'][0]['concepto'] == 'propina'


def test_concepto_propina_minuscula_plural(tmp_path):
    ruta = tmp_path / "tabla.htm"
    filas = [_fila('23/07/2026 16:39', 'propinas en efectivo 17 al 23', 'cta d la empresa', monto='150')]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert r['depositos'][0]['concepto'] == 'propina'


def test_concepto_venta_deposito_de_venta_en_efectivo(tmp_path):
    ruta = tmp_path / "tabla.htm"
    filas = [_fila('29/07/2026 14:16', 'DEPOSITO DE VENTA EN EFECTIVO', 'BANCO', monto='1400')]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert r['depositos'][0]['concepto'] == 'venta'


def test_concepto_indeterminado_casos_reales_miraflores(tmp_path):
    """'DEPOSITO' a secas y '400' son los dos depositos reales de MIRAFLORES
    (jul-2026, ver CLAUDE.md de la tarea) cuyo motivo NO alcanza para decidir
    propina o venta: no se adivina, se marca 'indeterminado' — inventar
    'venta' ensuciaria el cuadre de ingresos de la fase siguiente."""
    ruta = tmp_path / "tabla.htm"
    filas = [
        _fila('15/07/2026 16:22', 'DEPOSITO', 'INTERBANK', monto='400'),
        _fila('30/07/2026 16:48', '400', 'cta de la empresa', monto='400'),
    ]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert len(r['depositos']) == 2
    assert all(d['concepto'] == 'indeterminado' for d in r['depositos'])


def test_gastos_no_llevan_concepto(tmp_path):
    """'concepto' solo aplica a depositos; un gasto no es ni propina ni
    venta, ese campo no le corresponde."""
    ruta = tmp_path / "tabla.htm"
    ruta.write_text(_tabla_html(FILAS_TIPICAS), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert len(r['gastos']) == 1
    assert 'concepto' not in r['gastos'][0]
    assert len(r['depositos']) == 1
    assert 'concepto' in r['depositos'][0]


def test_fecha_sin_cero_a_la_izquierda_se_normaliza(tmp_path):
    ruta = tmp_path / "tabla.htm"
    filas = [_fila('1/07/2026 09:56', 'COMPRA X', 'EDWIN', monto='30')]
    ruta.write_text(_tabla_html(filas), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert r['gastos'][0]['fecha'] == '01/07/2026'


# -----------------------------------------------------------------------------
# Caso 2: sheet001.htm suelto (ya extraído de la carpeta hermana a mano)
# -----------------------------------------------------------------------------
def test_sheet001_suelto_parsea_igual_que_la_tabla(tmp_path):
    ruta = tmp_path / "sheet001.htm"
    ruta.write_text(_tabla_html(FILAS_TIPICAS), encoding="utf-8")

    r = egresos_caja.parsear_egresos(ruta)

    assert len(r['gastos']) == 1
    assert len(r['depositos']) == 1


# -----------------------------------------------------------------------------
# Caso 1: frameset + carpeta hermana — incluye LA TRAMPA (referencia interna
# con el nombre ORIGINAL, archivo y carpeta ya renombrados por el usuario).
# -----------------------------------------------------------------------------
def test_frameset_referencia_directa_sin_renombrar(tmp_path):
    """Caso simple: nadie renombró nada, la referencia interna del frameset
    coincide con el nombre real del archivo en disco."""
    carpeta = tmp_path / "Egresos (5)_archivos"
    carpeta.mkdir()
    (carpeta / "sheet001.htm").write_text(_tabla_html(FILAS_TIPICAS), encoding="utf-8")
    xls = tmp_path / "Egresos (5).xls"
    xls.write_text(_frameset_html("Egresos%20(5)_archivos/sheet001.htm"), encoding="utf-8")

    r = egresos_caja.parsear_egresos(xls)

    assert len(r['gastos']) == 1
    assert len(r['depositos']) == 1


def test_frameset_renombrado_la_trampa_referencia_interna_apunta_al_nombre_original(tmp_path):
    """LA TRAMPA verificada contra un reporte real (jul-2026): el .xls se
    renombró de 'Egresos (5).xls' a 'Egresos_LINCE_2026-07.xls' y su carpeta
    hermana se renombró junto con él a 'Egresos_LINCE_2026-07_archivos' —
    pero el HTML interno del frameset sigue apuntando a la referencia
    ORIGINAL 'Egresos%20(5)_archivos/sheet001.htm' (así lo dejó Excel al
    exportar, y nadie edita ese HTML a mano). Derivar la carpeta a partir del
    nombre ACTUAL del .xls ('<stem>_archivos') es lo único que funciona acá;
    seguir ciegamente la referencia interna del frameset falla (esa carpeta
    'Egresos (5)_archivos' no existe)."""
    carpeta_renombrada = tmp_path / "Egresos_LINCE_2026-07_archivos"
    carpeta_renombrada.mkdir()
    (carpeta_renombrada / "sheet001.htm").write_text(_tabla_html(FILAS_TIPICAS), encoding="utf-8")
    xls = tmp_path / "Egresos_LINCE_2026-07.xls"
    # referencia interna con el nombre ORIGINAL, no el renombrado:
    xls.write_text(_frameset_html("Egresos%20(5)_archivos/sheet001.htm"), encoding="utf-8")

    r = egresos_caja.parsear_egresos(xls)

    assert len(r['gastos']) == 1
    assert len(r['depositos']) == 1
    assert r['local'] == 'LINCE'


def test_frameset_sin_carpeta_hermana_ni_fallback_lanza_error_claro(tmp_path):
    xls = tmp_path / "Egresos_SIN_DATOS.xls"
    xls.write_text(_frameset_html("Egresos%20(5)_archivos/sheet001.htm"), encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        egresos_caja.parsear_egresos(xls)


def test_ruta_sin_tabla_ni_frameset_lanza_value_error(tmp_path):
    ruta = tmp_path / "vacio.htm"
    ruta.write_text("<html><body>nada aca</body></html>", encoding="utf-8")

    with pytest.raises(ValueError):
        egresos_caja.parsear_egresos(ruta)


# -----------------------------------------------------------------------------
# Variantes por local (verificadas contra los reportes reales de jul-2026)
# -----------------------------------------------------------------------------
def test_fecha_acepta_guiones_y_segundos():
    """MIRAFLORES exporta '31-07-2026 16:40:05' y LINCE '31/07/2026 16:20'.
    Antes solo se aceptaban barras: el reporte de MIRAFLORES entero salía con
    la fecha en crudo, sin que nada avisara."""
    assert egresos_caja._fecha_hora("31-07-2026 16:40:05") == ("31/07/2026", "16:40")
    assert egresos_caja._fecha_hora("31/07/2026 16:20") == ("31/07/2026", "16:20")
    assert egresos_caja._fecha_hora("1/7/2026") == ("01/07/2026", None)


def test_fecha_no_reconocida_devuelve_none_en_vez_de_texto_crudo():
    """Un dato que no se entiende se señala, no se propaga: quien llama lo
    manda a 'filas_ignoradas'."""
    assert egresos_caja._fecha_hora("no es fecha") == (None, None)


def test_local_acepta_punto_y_espacio():
    """LINCE exporta 'CAJA.LINCE' y MIRAFLORES 'CAJA MIRAFLORES'."""
    assert egresos_caja._RE_LOCAL.search("CAJA.LINCE").group(1).upper() == "LINCE"
    assert egresos_caja._RE_LOCAL.search("CAJA MIRAFLORES").group(1).upper() == "MIRAFLORES"


def test_clasificar_concepto_directo():
    """Mismos casos que test_concepto_* de arriba, pero contra la función
    privada directamente (mismo patrón que los demás tests de esta sección
    para las regex/helpers privados de egresos_caja)."""
    assert egresos_caja._clasificar_concepto('PROPINA EN EFECTIVO 26 AL  02') == 'propina'
    assert egresos_caja._clasificar_concepto('propinas en efectivo 17 al 23') == 'propina'
    assert egresos_caja._clasificar_concepto('DEPOSITO DE VENTA EN EFECTIVO') == 'venta'
    assert egresos_caja._clasificar_concepto('DEPOSITO') == 'indeterminado'
    assert egresos_caja._clasificar_concepto('400') == 'indeterminado'


def test_destino_banco_reconoce_las_variantes_de_miraflores():
    """El dueño confirmó (2026-08-09) que 'cta de la empresa' en MIRAFLORES es
    lo mismo que 'BANCO' en LINCE. Se van a estandarizar a 'BANCO', pero los
    reportes ya exportados conservan el texto viejo y hay que reprocesarlos."""
    for destino in ["BANCO", "banco", "CTA DE LA EMPRESA", "cta d la empresa",
                    "cta de empresa", "INTERBANK"]:
        assert egresos_caja._RE_DESTINO_BANCO.match(destino), destino
    for destino in ["EDWIN", "MERCADO", "INVERSIONES", "MULTICOPY"]:
        assert not egresos_caja._RE_DESTINO_BANCO.match(destino), destino
