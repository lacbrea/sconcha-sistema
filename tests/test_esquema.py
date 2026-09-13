"""Tests de esquema.py: clave() de deduplicación y validar(). Sin red, sin credenciales."""
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from esquema import ComprobanteExtraido, ESQUEMA_JSON, ItemExtraido, _ITEM_JSON


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


def test_validar_acepta_lineas_con_igv_incluido():
    """Caso real (jul-2026, TAI LOY F581-0280271, el primer comprobante que el
    sistema proceso de verdad): subtotal 7.11 + IGV 1.29 = total 8.40, y la
    unica linea trae 8.40 porque el proveedor imprime precio de venta al
    publico. La extraccion era correcta campo por campo, pero exigir que las
    lineas cuadren solo contra el subtotal lo mandaba a revision manual."""
    comp = _comprobante_base(
        subtotal=7.11,
        igv=1.29,
        total=8.40,
        items=[ItemExtraido(orden=1, descripcion="FORRO ADH TRANSP", cantidad=2,
                            precio_unitario=4.20, total_linea=8.40)],
    )
    assert not any("no cuadra" in a for a in comp.validar())


def test_validar_detecta_diferencia_cuando_no_cuadra_ni_con_subtotal_ni_con_total():
    """Relajar la regla para aceptar lineas con IGV no puede dejar pasar un
    descuadre real: 50 no es ni el subtotal (100) ni el total (118)."""
    comp = _comprobante_base(
        subtotal=100.00,
        igv=18.00,
        total=118.00,
        items=[ItemExtraido(orden=1, descripcion="Item 1", total_linea=50.00)],
    )
    advertencias = comp.validar()
    assert any("no cuadra" in a for a in advertencias)
    assert any("100" in a and "118" in a for a in advertencias)


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


# --- plausibilidad de fecha (bug: el modelo inventa el año) ------------------
#
# Bug real: sin esta validación, un "2024" pasaba en silencio porque
# `_fecha_valida()` solo mira el FORMATO (strptime), no si la fecha tiene
# sentido -- así el comprobante quedaba fuera de la ventana de conciliación
# sin que nadie se enterara. La ventana es holgada a propósito: dos meses de
# atraso es un caso legítimo y NO debe marcarse.

def test_validar_marca_fecha_de_2024_como_demasiado_antigua():
    comp = _comprobante_base(fecha_emision="2024-05-15")
    advertencias = comp.validar(hoy=date(2026, 9, 11))
    assert any("antigua" in a.lower() for a in advertencias)


def test_validar_fecha_de_hace_dos_meses_pasa_limpia():
    hoy = date(2026, 9, 11)
    comp = _comprobante_base(fecha_emision=(hoy - timedelta(days=60)).isoformat())
    advertencias = comp.validar(hoy=hoy)
    assert not any("antigua" in a.lower() or "futura" in a.lower() for a in advertencias)


def test_validar_marca_fecha_a_30_dias_en_el_futuro():
    hoy = date(2026, 9, 11)
    comp = _comprobante_base(fecha_emision=(hoy + timedelta(days=30)).isoformat())
    advertencias = comp.validar(hoy=hoy)
    assert any("futura" in a.lower() for a in advertencias)


def test_validar_acepta_fecha_futura_dentro_de_la_ventana_de_7_dias():
    hoy = date(2026, 9, 11)
    comp = _comprobante_base(fecha_emision=(hoy + timedelta(days=5)).isoformat())
    advertencias = comp.validar(hoy=hoy)
    assert not any("futura" in a.lower() for a in advertencias)


def test_validar_evalua_tambien_la_plausibilidad_de_fecha_vencimiento():
    hoy = date(2026, 9, 11)
    comp = _comprobante_base(fecha_vencimiento="2024-01-01")
    advertencias = comp.validar(hoy=hoy)
    assert any("vencimiento" in a.lower() and "antigua" in a.lower() for a in advertencias)


def test_validar_acepta_vencimiento_futuro_de_factura_a_credito():
    # Una factura a 60 días vence en el futuro por diseño: no debe caer a
    # 02_REVISAR (validar() devuelve advertencias y procesar.py manda a
    # revisar ante cualquiera).
    hoy = date(2026, 9, 11)
    comp = _comprobante_base(fecha_emision=hoy.isoformat(), fecha_vencimiento=(hoy + timedelta(days=60)).isoformat())
    advertencias = comp.validar(hoy=hoy)
    assert not any("futura" in a.lower() for a in advertencias)


def test_validar_sin_hoy_explicito_usa_la_fecha_de_hoy_del_sistema(monkeypatch):
    import esquema

    class _FechaFalsa(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 11)

    monkeypatch.setattr(esquema, "date", _FechaFalsa)
    comp = _comprobante_base(fecha_emision="2024-05-15")

    advertencias = comp.validar()

    assert any("antigua" in a.lower() for a in advertencias)


# --- límite de uniones de la API --------------------------------------------
#
# La API de Anthropic rechazó con HTTP 400 el esquema original (24 propiedades
# con unión de tipos) con este mensaje real:
#   "Schemas contains too many parameters with union types (24 parameters with
#   type arrays or anyOf). This causes exponential compilation cost. Reduce
#   the number of nullable or union-typed parameters (limit: 16 parameters
#   with unions)."
# Este test cuenta las propiedades con unión (raíz + ítems) para que nadie
# vuelva a pasarse del límite sin enterarse hasta que la API lo rechace en
# producción.

def _contar_uniones(propiedades: dict) -> int:
    total = 0
    for definicion in propiedades.values():
        tipo = definicion.get("type")
        if isinstance(tipo, list) or "anyOf" in definicion:
            total += 1
    return total


def test_esquema_json_no_supera_el_limite_de_16_uniones_de_la_api():
    uniones_raiz = _contar_uniones(ESQUEMA_JSON["properties"])
    uniones_item = _contar_uniones(_ITEM_JSON["properties"])
    assert uniones_raiz + uniones_item <= 16


def test_campos_de_texto_de_la_raiz_ya_no_son_nullable():
    # Los 11 campos de texto de la raíz deben ser {"type": "string"} a secas
    # -- si alguno vuelve a tener unión con "null" se pasa de 16 otra vez.
    campos_texto = [
        "proveedor_ruc", "proveedor_razon_social", "cliente_ruc", "cliente_razon_social",
        "tipo_documento", "serie_numero", "fecha_emision", "fecha_vencimiento", "condicion",
        "detraccion_codigo", "documento_referencia",
    ]
    for campo in campos_texto:
        assert ESQUEMA_JSON["properties"][campo] == {"type": "string"}
    assert _ITEM_JSON["properties"]["unidad"] == {"type": "string"}
