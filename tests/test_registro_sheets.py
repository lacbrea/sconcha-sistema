"""Tests de registro_sheets.py. Corren sin red y sin credenciales:
usan el modo dry_run (escribe CSV en vez de llamar a la API) y un doble de
prueba local para el Resource de sheets v4 de googleapiclient.
"""
from __future__ import annotations

import csv
import pathlib
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import registro_sheets  # noqa: E402
from registro_sheets import COLUMNAS_CONTABLE, COLUMNAS_DETALLE, Registro  # noqa: E402

# ---------------------------------------------------------------------------
# Stub local del contrato de esquema.py (todavia no existe: lo crea otro
# agente en paralelo). Si ya existe, se usa la version real.
# ---------------------------------------------------------------------------
try:
    from esquema import ComprobanteExtraido, ItemExtraido  # type: ignore
except ImportError:

    @dataclass
    class ItemExtraido:
        orden: int
        descripcion: str
        cantidad: float | None = None
        unidad: str | None = None
        precio_unitario: float | None = None
        total_linea: float | None = None

    @dataclass
    class ComprobanteExtraido:
        origen: str
        confianza: float
        proveedor_ruc: str | None = None
        proveedor_razon_social: str | None = None
        cliente_ruc: str | None = None
        cliente_razon_social: str | None = None
        tipo_documento: str | None = None
        serie_numero: str | None = None
        fecha_emision: str | None = None
        fecha_vencimiento: str | None = None
        condicion: str | None = None
        moneda: str = "PEN"
        tipo_cambio: float | None = None
        subtotal: float | None = None
        igv: float | None = None
        icbper: float | None = None
        descuento_global: float | None = None
        total: float | None = None
        detraccion_pct: float | None = None
        detraccion_monto: float | None = None
        detraccion_codigo: str | None = None
        retencion: float | None = None
        documento_referencia: str | None = None
        items: list = field(default_factory=list)
        advertencias: list = field(default_factory=list)

        def clave(self) -> str:
            total_str = f"{self.total:.2f}" if self.total is not None else "0.00"
            return f"{self.proveedor_ruc}|{self.serie_numero}|{total_str}"


# ---------------------------------------------------------------------------
# Doble de prueba del Resource de sheets v4 (googleapiclient), sin red.
# ---------------------------------------------------------------------------
class _FakeValues:
    def __init__(self, store: dict[str, list[list]]):
        self._store = store
        self._pendiente = None

    def get(self, spreadsheetId, range):  # noqa: A002 - firma igual a la API real
        self._pendiente = ("get", spreadsheetId)
        return self

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: A002
        self._pendiente = ("update", spreadsheetId, body["values"])
        return self

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):  # noqa: A002
        self._pendiente = ("append", spreadsheetId, body["values"])
        return self

    def execute(self):
        accion = self._pendiente[0]
        if accion == "get":
            _, sid = self._pendiente
            return {"values": self._store.get(sid, [])}
        if accion == "update":
            _, sid, filas = self._pendiente
            actuales = self._store.get(sid, [])
            self._store[sid] = filas + actuales
            return {}
        if accion == "append":
            _, sid, filas = self._pendiente
            self._store.setdefault(sid, []).extend(filas)
            return {}
        raise AssertionError(f"accion no esperada: {accion}")


class FakeServicioSheets:
    """Doble minimo del Resource sheets v4: guarda todo en memoria."""

    def __init__(self):
        self.store: dict[str, list[list]] = {}
        self._values = _FakeValues(self.store)

    def spreadsheets(self):
        return self

    def values(self):
        return self._values


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _catalogo_csv_minimo(tmp_path: pathlib.Path) -> pathlib.Path:
    ruta = tmp_path / "insumos_test.csv"
    ruta.write_text(
        "insumo,categoria,unidad_base,alias\n"
        "ACEITE DE OLIVA,ABARROTES,L,ACEITE DE OLIVA\n",
        encoding="utf-8",
    )
    return ruta


def _comprobante_con_items() -> ComprobanteExtraido:
    return ComprobanteExtraido(
        origen="xml",
        confianza=0.98,
        proveedor_ruc="20123456789",
        proveedor_razon_social="DISTRIBUIDORA SCONCHA S.A.C.",
        tipo_documento="FACTURA",
        serie_numero="F001-1234",
        fecha_emision="2026-06-15",
        condicion="contado",
        moneda="PEN",
        subtotal=100.0,
        igv=18.0,
        total=118.0,
        advertencias=["monto redondeado"],
        items=[
            ItemExtraido(orden=1, descripcion="ACEITE DE OLIVA", cantidad=2, unidad="L", precio_unitario=50.0, total_linea=100.0),
            ItemExtraido(orden=2, descripcion="PRODUCTO SIN CATALOGAR XYZ", cantidad=1, unidad="und", precio_unitario=18.0, total_linea=18.0),
        ],
    )


def _comprobante_sin_items() -> ComprobanteExtraido:
    return ComprobanteExtraido(
        origen="pdf",
        confianza=0.7,
        proveedor_ruc="20999999999",
        proveedor_razon_social="MERCADO",
        tipo_documento="BOLETA",
        serie_numero="B055-7007734",
        fecha_emision="2026-06-30",
        condicion="contado",
        total=6.0,
        items=[],
    )


def _config_dry_run(tmp_path: pathlib.Path) -> dict:
    return {
        "dry_run": True,
        "catalogo_csv": str(_catalogo_csv_minimo(tmp_path)),
        "salida_dir": str(tmp_path / "salida"),
        "sheets": {"contable": "id_contable_test", "detalle": "id_detalle_test"},
    }


# ---------------------------------------------------------------------------
# Columnas: el orden y los nombres exactos son lo que rompe la conciliacion
# si alguien los toca sin querer.
# ---------------------------------------------------------------------------
def test_columnas_contable_orden_y_nombres_exactos():
    assert COLUMNAS_CONTABLE == [
        "FECHA_EMISION", "EMPRESA", "LOCAL", "PROVEEDOR", "RUC", "TIPO",
        "SERIE_NUMERO", "SUBTOTAL", "IGV", "TOTAL", "CONDICION", "ESTADO_PAGO",
        "FECHA_PAGO", "CAJA_CHICA", "LINK_DRIVE", "REGISTRADO_POR",
        "FECHA_REGISTRO", "OBSERVACIONES",
        "MONEDA", "TIPO_CAMBIO", "FECHA_VENCIMIENTO", "DETRACCION_PCT",
        "DETRACCION_MONTO", "RETENCION", "ICBPER", "DESCUENTO_GLOBAL",
        "CLIENTE_RUC", "DOC_REFERENCIA", "ORIGEN", "CONFIANZA", "ADVERTENCIAS",
        "ARCHIVO",
    ]


def test_columnas_detalle_orden_y_nombres_exactos():
    assert COLUMNAS_DETALLE == [
        "FECHA_EMISION", "EMPRESA", "LOCAL", "RUC", "SERIE_NUMERO", "ORDEN",
        "DESCRIPCION_FACTURA", "INSUMO", "CATEGORIA", "CANTIDAD", "UNIDAD",
        "PRECIO_UNITARIO", "TOTAL_LINEA", "MATCH", "FECHA_REGISTRO",
    ]


def test_columna_link_drive_no_link_comprobante():
    """Regresion explicita del caso documentado: el Excel historico llama a
    esta columna LINK_COMPROBANTE, pero el motor de conciliacion espera
    LINK_DRIVE. Si alguien la "corrige" de vuelta, la conciliacion se rompe
    en silencio."""
    assert "LINK_DRIVE" in COLUMNAS_CONTABLE
    assert "LINK_COMPROBANTE" not in COLUMNAS_CONTABLE
    assert COLUMNAS_CONTABLE.index("LINK_DRIVE") == 14  # columna 15 (1-indexada)


# ---------------------------------------------------------------------------
# dry_run: CSV
# ---------------------------------------------------------------------------
def test_dry_run_escribe_csvs_contable_y_detalle(tmp_path):
    config = _config_dry_run(tmp_path)
    registro = Registro(config)
    comp = _comprobante_con_items()

    registro.escribir(comp, empresa="SCONCHA", local="MIRAFLORES", link_drive="https://drive/x", archivo="factura.pdf")

    csv_contable = tmp_path / "salida" / "contable.csv"
    csv_detalle = tmp_path / "salida" / "detalle.csv"
    assert csv_contable.exists()
    assert csv_detalle.exists()

    with csv_contable.open(encoding="utf-8", newline="") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) == 1
    fila = filas[0]
    assert fila["RUC"] == "20123456789"
    assert fila["SERIE_NUMERO"] == "F001-1234"
    assert fila["TOTAL"] == "118.0"
    assert fila["ESTADO_PAGO"] == ""
    assert fila["LINK_DRIVE"] == "https://drive/x"
    assert fila["REGISTRADO_POR"] == "skill-comprobantes"
    assert fila["ADVERTENCIAS"] == "monto redondeado"
    # OBSERVACIONES es una columna de notas humanas: debe quedar vacia al
    # registrar. La trazabilidad del archivo origen va aparte, en ARCHIVO.
    assert fila["OBSERVACIONES"] == ""
    assert fila["ARCHIVO"] == "factura.pdf"

    with csv_detalle.open(encoding="utf-8", newline="") as f:
        filas_det = list(csv.DictReader(f))
    assert len(filas_det) == 2
    assert filas_det[0]["INSUMO"] == "ACEITE DE OLIVA"
    assert filas_det[0]["CATEGORIA"] == "ABARROTES"
    assert filas_det[0]["MATCH"] == "1.00"
    assert filas_det[1]["INSUMO"] == ""
    assert filas_det[1]["MATCH"] == "SIN MATCH"


def test_comprobante_sin_items_no_escribe_detalle(tmp_path):
    config = _config_dry_run(tmp_path)
    registro = Registro(config)
    comp = _comprobante_sin_items()

    registro.escribir(comp, empresa="SCONCHA", local="LINCE", link_drive="", archivo="boleta.pdf")

    csv_contable = tmp_path / "salida" / "contable.csv"
    csv_detalle = tmp_path / "salida" / "detalle.csv"
    assert csv_contable.exists()
    assert not csv_detalle.exists()

    with csv_contable.open(encoding="utf-8", newline="") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) == 1
    assert filas[0]["SERIE_NUMERO"] == "B055-7007734"


def test_dry_run_cabecera_una_sola_vez_en_escrituras_sucesivas(tmp_path):
    config = _config_dry_run(tmp_path)
    registro = Registro(config)
    registro.escribir(_comprobante_con_items(), empresa="SCONCHA", local="MIRAFLORES", link_drive="", archivo="a.pdf")
    registro.escribir(_comprobante_sin_items(), empresa="SCONCHA", local="LINCE", link_drive="", archivo="b.pdf")

    csv_contable = tmp_path / "salida" / "contable.csv"
    lineas = csv_contable.read_text(encoding="utf-8").splitlines()
    assert lineas[0] == ",".join(COLUMNAS_CONTABLE)
    assert len(lineas) == 3  # cabecera + 2 comprobantes


def test_claves_existentes_dry_run_refleja_lo_escrito(tmp_path):
    config = _config_dry_run(tmp_path)
    registro = Registro(config)
    comp = _comprobante_con_items()
    registro.escribir(comp, empresa="SCONCHA", local="MIRAFLORES", link_drive="", archivo="a.pdf")

    claves = registro.claves_existentes()

    assert claves == {"20123456789|F001-1234|118.00"}


def test_claves_existentes_dry_run_vacio_si_no_hay_csv(tmp_path):
    config = _config_dry_run(tmp_path)
    registro = Registro(config)
    assert registro.claves_existentes() == set()


# ---------------------------------------------------------------------------
# Doble de prueba de la API de Sheets (sin dry_run, sin red).
# ---------------------------------------------------------------------------
def test_servicio_fake_escribe_cabecera_una_vez_y_acumula_filas(tmp_path):
    fake = FakeServicioSheets()
    config = {
        "dry_run": False,
        "catalogo_csv": str(_catalogo_csv_minimo(tmp_path)),
        "sheets": {"contable": "SHEET_CONTABLE", "detalle": "SHEET_DETALLE"},
    }
    registro = Registro(config, servicio=fake)

    registro.escribir(_comprobante_con_items(), empresa="SCONCHA", local="MIRAFLORES", link_drive="x", archivo="a.pdf")
    registro.escribir(_comprobante_sin_items(), empresa="SCONCHA", local="LINCE", link_drive="", archivo="b.pdf")

    filas_contable = fake.store["SHEET_CONTABLE"]
    assert filas_contable[0] == COLUMNAS_CONTABLE  # cabecera una sola vez
    assert len(filas_contable) == 1 + 2  # cabecera + 2 comprobantes

    filas_detalle = fake.store["SHEET_DETALLE"]
    assert filas_detalle[0] == COLUMNAS_DETALLE
    assert len(filas_detalle) == 1 + 2  # cabecera + 2 items del primer comprobante


def test_claves_existentes_via_servicio_fake(tmp_path):
    fake = FakeServicioSheets()
    config = {
        "dry_run": False,
        "catalogo_csv": str(_catalogo_csv_minimo(tmp_path)),
        "sheets": {"contable": "SHEET_CONTABLE", "detalle": "SHEET_DETALLE"},
    }
    registro = Registro(config, servicio=fake)
    registro.escribir(_comprobante_con_items(), empresa="SCONCHA", local="MIRAFLORES", link_drive="", archivo="a.pdf")

    assert registro.claves_existentes() == {"20123456789|F001-1234|118.00"}
