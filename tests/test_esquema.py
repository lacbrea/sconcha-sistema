"""Tests de esquema.py: clave() de deduplicación y validar(). Sin red, sin credenciales."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from esquema import ComprobanteExtraido, ItemExtraido


def _comprobante_base(**overrides) -> ComprobanteExtraido:
    datos = dict(
        origen="xml",
        confianza=1.0,
        proveedor_ruc="20608901494",
        proveedor_razon_social="EL TEMPLO S.A.C.",
        tipo_documento="factura",
        serie_numero="F001-123",
        fecha_emision="2026-07-15",
        total=150.50,
        subtotal=127.54,
        igv=22.96,
    )
    datos.update(overrides)
    return ComprobanteExtraido(**datos)


# --- clave() ---------------------------------------------------------------

def test_clave_normaliza_ruc_y_serie_a_mayusculas_sin_espacios():
    comp = _comprobante_base(proveedor_ruc=" 20608901494 ", serie_numero=" f001-123 ", total=150.5)
    assert comp.clave() == "20608901494|F001-123|150.50"


def test_clave_es_estable_para_el_mismo_comprobante():
    comp = _comprobante_base()
    assert comp.clave() == comp.clave()


def test_clave_sin_ruc_incluye_sin_clave():
    comp = _comprobante_base(proveedor_ruc=None)
    assert "SIN_CLAVE" in comp.clave()


def test_clave_sin_serie_incluye_sin_clave():
    comp = _comprobante_base(serie_numero=None)
    assert "SIN_CLAVE" in comp.clave()


def test_clave_sin_total_incluye_sin_clave():
    comp = _comprobante_base(total=None)
    assert "SIN_CLAVE" in comp.clave()


def test_clave_sin_datos_nunca_colisiona_entre_comprobantes_distintos():
    comp_a = _comprobante_base(proveedor_ruc=None)
    comp_b = _comprobante_base(proveedor_ruc=None)
    # Dos comprobantes distintos con los mismos huecos NO deben deduplicarse
    # entre sí solo por compartir un RUC faltante.
    assert comp_a.clave() != comp_b.clave()


# --- validar() ---------------------------------------------------------------

def test_validar_sin_advertencias_cuando_todo_cuadra():
    comp = _comprobante_base(
        subtotal=100.00,
        igv=18.00,
        total=118.00,
        items=[ItemExtraido(orden=1, descripcion="Item 1", total_linea=100.00)],
    )
    assert comp.validar() == []


def test_validar_tolera_diferencia_de_centimos_por_redondeo():
    # Caso explícito pedido: suma de ítems 2348.76 vs subtotal 2348.75 debe pasar.
    comp = _comprobante_base(
        subtotal=2348.75,
        total=2771.53,
        items=[
            ItemExtraido(orden=1, descripcion="Item 1", total_linea=1000.00),
            ItemExtraido(orden=2, descripcion="Item 2", total_linea=1348.76),
        ],
    )
    advertencias = comp.validar()
    assert not any("no cuadra" in a for a in advertencias)


def test_validar_detecta_diferencia_real_entre_items_y_subtotal():
    comp = _comprobante_base(
        subtotal=100.00,
        items=[ItemExtraido(orden=1, descripcion="Item 1", total_linea=50.00)],
    )
    advertencias = comp.validar()
    assert any("no cuadra" in a for a in advertencias)


def test_validar_marca_total_faltante():
    comp = _comprobante_base(total=None)
    advertencias = comp.validar()
    assert any("total" in a.lower() for a in advertencias)


def test_validar_marca_ruc_invalido():
    comp = _comprobante_base(proveedor_ruc="12345")
    advertencias = comp.validar()
    assert any("RUC" in a for a in advertencias)


def test_validar_acepta_ruc_de_11_digitos():
    comp = _comprobante_base(proveedor_ruc="20608901494")
    advertencias = comp.validar()
    assert not any("RUC" in a for a in advertencias)


def test_validar_marca_fecha_con_formato_invalido():
    comp = _comprobante_base(fecha_emision="15/07/2026")
    advertencias = comp.validar()
    assert any("fecha" in a.lower() for a in advertencias)


def test_validar_acepta_fecha_iso():
    comp = _comprobante_base(fecha_emision="2026-07-15")
    advertencias = comp.validar()
    assert not any("fecha" in a.lower() for a in advertencias)


def test_validar_detraccion_coherente_no_genera_advertencia():
    comp = _comprobante_base(total=1000.00, detraccion_pct=12.0, detraccion_monto=120.00)
    advertencias = comp.validar()
    assert not any("detracci" in a.lower() for a in advertencias)


def test_validar_detraccion_incoherente_genera_advertencia():
    comp = _comprobante_base(total=1000.00, detraccion_pct=12.0, detraccion_monto=50.00)
    advertencias = comp.validar()
    assert any("detracci" in a.lower() for a in advertencias)


def test_validar_detraccion_con_porcentaje_pero_sin_monto():
    comp = _comprobante_base(total=1000.00, detraccion_pct=12.0, detraccion_monto=None)
    advertencias = comp.validar()
    assert any("detracci" in a.lower() for a in advertencias)


def test_validar_nunca_lanza_excepcion_con_comprobante_vacio():
    comp = ComprobanteExtraido(origen="pdf", confianza=0.5)
    # No debe lanzar, incluso sin ningún dato.
    advertencias = comp.validar()
    assert isinstance(advertencias, list)
