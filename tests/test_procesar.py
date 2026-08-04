"""Tests de procesar.py.

Corren sin red y sin credenciales: los módulos que todavía no existen o que
requieren Google/Anthropic (extractores.xml_ubl, extractores.modelo,
catalogo, registro_sheets, auth_google) se sustituyen por dobles de prueba
registrados directamente en sys.modules ANTES de importar procesar, así que
procesar.py se importa igual aunque esos archivos no existan en disco
todavía (los están escribiendo otros dos agentes en paralelo).

Correr con:
    C:\\Python312\\python.exe -m pytest tests/test_procesar.py -q
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest
import yaml

RAIZ_PROYECTO = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROYECTO))


def _crear_modulo_falso(nombre: str) -> types.ModuleType:
    """Crea un módulo doble SIN registrarlo en sys.modules.

    Registrarlo globalmente (como se hacía antes) contaminaba a toda la
    suite: pytest corre todos los archivos de test en el mismo proceso, así
    que tests/test_registro_sheets.py y tests/test_xml_ubl.py recibían estos
    dobles en lugar de los módulos reales y fallaban. Los dobles se inyectan
    ahora por test, con la fixture `_aislar_dependencias` de más abajo, que
    los revierte automáticamente al terminar cada prueba.
    """
    return types.ModuleType(nombre)


# -----------------------------------------------------------------------------
# Dobles de prueba de los módulos compartidos (contrato descrito en el
# encargo del proyecto). Se inyectan por test, nunca de forma global.
# -----------------------------------------------------------------------------
_paquete_extractores = _crear_modulo_falso("extractores")
_paquete_extractores.__path__ = []  # lo marca como paquete para el import system
_modulo_xml_ubl = _crear_modulo_falso("extractores.xml_ubl")
_modulo_extractor_modelo = _crear_modulo_falso("extractores.modelo")
_paquete_extractores.xml_ubl = _modulo_xml_ubl
_paquete_extractores.modelo = _modulo_extractor_modelo

_modulo_catalogo = _crear_modulo_falso("catalogo")
_modulo_registro_sheets = _crear_modulo_falso("registro_sheets")
_modulo_auth_google = _crear_modulo_falso("auth_google")


def _extraer_xml_por_defecto(ruta):
    raise NotImplementedError("configurar _modulo_xml_ubl.extraer en el test")


def _extraer_modelo_por_defecto(ruta, tipo, config=None):
    raise NotImplementedError("configurar _modulo_extractor_modelo.extraer en el test")


_modulo_xml_ubl.extraer = _extraer_xml_por_defecto
_modulo_extractor_modelo.extraer = _extraer_modelo_por_defecto
_modulo_auth_google.servicio_drive = lambda: None
_modulo_auth_google.servicio_sheets = lambda: None
_modulo_auth_google.ErrorAutenticacion = RuntimeError


class _CatalogoFalso:
    def __init__(self, csv_path):
        self.csv_path = csv_path

    def emparejar(self, descripcion):
        return None, None, 0.0


_modulo_catalogo.Catalogo = _CatalogoFalso


class _RegistroFalso:
    """Doble de prueba de registro_sheets.Registro: no hace red, guarda en memoria."""

    def __init__(self, config):
        self.config = config
        self.escritos = []
        self._claves_existentes = set()

    def claves_existentes(self):
        return set(self._claves_existentes)

    def escribir(self, comp, empresa, local, link_drive, archivo):
        self.escritos.append(
            {"comp": comp, "empresa": empresa, "local": local, "link_drive": link_drive, "archivo": archivo}
        )


_modulo_registro_sheets.Registro = _RegistroFalso

import procesar  # noqa: E402  (los módulos reales ya existen; se parchean por test)


@pytest.fixture(autouse=True)
def _aislar_dependencias(monkeypatch):
    """Sustituye las dependencias de procesar.py por dobles, solo en este test.

    procesar.py enlaza los módulos como atributos propios
    (`extractor_xml`, `extractor_modelo`, `catalogo_mod`, `registro_mod`),
    así que basta con parchear esos atributos. monkeypatch los restaura al
    terminar cada prueba, de modo que ningún otro archivo de test ve los
    dobles. Es la corrección al problema que detectaron los otros dos
    agentes al correr la suite completa.
    """
    monkeypatch.setattr(procesar, "extractor_xml", _modulo_xml_ubl)
    monkeypatch.setattr(procesar, "extractor_modelo", _modulo_extractor_modelo)
    monkeypatch.setattr(procesar, "catalogo_mod", _modulo_catalogo)
    monkeypatch.setattr(procesar, "registro_mod", _modulo_registro_sheets)
    monkeypatch.setitem(sys.modules, "auth_google", _modulo_auth_google)

    # Cada test parte de extractores que fallan si no se configuran, para
    # que una prueba no herede el doble que dejó la anterior.
    _modulo_xml_ubl.extraer = _extraer_xml_por_defecto
    _modulo_extractor_modelo.extraer = _extraer_modelo_por_defecto
    yield


# -----------------------------------------------------------------------------
# Doble de prueba de esquema.ComprobanteExtraido / ItemExtraido. No importa
# esquema.py (procesar.py tampoco lo hace en tiempo de ejecución): basta con
# que tenga la misma forma (duck typing).
# -----------------------------------------------------------------------------
class ItemFalso:
    def __init__(self, descripcion="ACEITE CRISOL X20 LT", orden=1, cantidad=1.0, unidad="und",
                 precio_unitario=10.0, total_linea=10.0):
        self.orden = orden
        self.descripcion = descripcion
        self.cantidad = cantidad
        self.unidad = unidad
        self.precio_unitario = precio_unitario
        self.total_linea = total_linea


class ComprobanteFalso:
    def __init__(self, **kwargs):
        valores = dict(
            origen="xml",
            confianza=0.99,
            proveedor_ruc="20100000001",
            proveedor_razon_social="PROVEEDOR SAC",
            cliente_ruc="20612506036",  # INSTITUCION (un solo local: MIRAFLORES)
            cliente_razon_social="INSTITUCION CEVICHERA S.A.C.",
            tipo_documento="factura",
            serie_numero="F001-123",
            fecha_emision="2026-07-15",
            fecha_vencimiento=None,
            condicion="contado",
            moneda="PEN",
            tipo_cambio=None,
            subtotal=100.0,
            igv=18.0,
            icbper=0.0,
            descuento_global=0.0,
            total=118.0,
            detraccion_pct=None,
            detraccion_monto=None,
            detraccion_codigo=None,
            retencion=None,
            documento_referencia=None,
            items=[],
            advertencias=[],
        )
        valores.update(kwargs)
        for clave, valor in valores.items():
            setattr(self, clave, valor)
        self.problemas_validacion = []

    def clave(self) -> str:
        return f"{self.proveedor_ruc}|{self.serie_numero}|{self.total:.2f}"

    def validar(self) -> list[str]:
        return list(self.problemas_validacion)


# -----------------------------------------------------------------------------
# Fixtures / helpers de prueba
# -----------------------------------------------------------------------------
def _config_base(tmp_path: pathlib.Path) -> dict:
    return {
        "negocio": "SCONCHA",
        "cuenta_google": "administracion.sconcha@gmail.com",
        "drive": {
            "raiz": str(tmp_path),
            "buzon": "00_BUZON",
            "procesado": "01_PROCESADO",
            "revisar": "02_REVISAR",
        },
        "modelo": "claude-opus-5",
        "esfuerzo": "low",
        "empresas": [
            {"nombre_corto": "EL TEMPLO", "razon_social": "EL TEMPLO S.A.C.", "ruc": "20608901494", "locales": ["LINCE", "CP"]},
            {"nombre_corto": "INSTITUCION", "razon_social": "INSTITUCION CEVICHERA S.A.C.", "ruc": "20612506036", "locales": ["MIRAFLORES"]},
            {"nombre_corto": "ILLAWARA", "razon_social": "ILLAWARA E.I.R.L.", "ruc": "20614321734", "locales": []},
        ],
        "sheets": {"contable": "", "detalle": ""},
    }


def _entorno(tmp_path: pathlib.Path):
    config = _config_base(tmp_path)
    carpeta_buzon = tmp_path / "00_BUZON"
    carpeta_procesado = tmp_path / "01_PROCESADO"
    carpeta_revisar = tmp_path / "02_REVISAR"
    carpeta_buzon.mkdir(parents=True, exist_ok=True)
    registro = _RegistroFalso(config)
    catalogo_obj = _CatalogoFalso(None)
    resolutor = procesar.ResolutorLinkDrive(None, config)  # servicio_drive=None -> nunca hace red
    return config, carpeta_buzon, carpeta_procesado, carpeta_revisar, registro, catalogo_obj, resolutor


def _crear_archivo(carpeta: pathlib.Path, nombre: str, contenido: bytes = b"contenido de prueba") -> pathlib.Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / nombre
    ruta.write_bytes(contenido)
    return ruta


def _procesar_uno(ruta, respaldos, config, registro, catalogo_obj, resolutor, procesado, revisar, dry_run=False):
    return procesar.procesar_uno(
        ruta,
        respaldos,
        config=config,
        registro=registro,
        catalogo_obj=catalogo_obj,
        resolutor_link=resolutor,
        claves_procesadas_en_lote=set(),
        carpeta_procesado=procesado,
        carpeta_revisar=revisar,
        dry_run=dry_run,
    )


# -----------------------------------------------------------------------------
# Clasificación por extensión
# -----------------------------------------------------------------------------
def test_clasificacion_por_extension(tmp_path):
    llamadas = []

    def falso_xml(ruta):
        llamadas.append(("xml", ruta.name))
        return ComprobanteFalso()

    def falso_modelo(ruta, tipo, config=None):
        llamadas.append((tipo, ruta.name))
        return ComprobanteFalso()

    _modulo_xml_ubl.extraer = falso_xml
    _modulo_extractor_modelo.extraer = falso_modelo

    ruta_xml = _crear_archivo(tmp_path, "a.xml")
    ruta_zip = _crear_archivo(tmp_path, "b.zip")
    ruta_pdf = _crear_archivo(tmp_path, "c.pdf")
    ruta_jpg = _crear_archivo(tmp_path, "d.jpg")
    ruta_png = _crear_archivo(tmp_path, "e.png")

    procesar.extraer_comprobante(ruta_xml, ".xml")
    procesar.extraer_comprobante(ruta_zip, ".zip")
    procesar.extraer_comprobante(ruta_pdf, ".pdf")
    procesar.extraer_comprobante(ruta_jpg, ".jpg")
    procesar.extraer_comprobante(ruta_png, ".png")

    assert llamadas == [
        ("xml", "a.xml"),
        ("xml", "b.zip"),
        ("pdf", "c.pdf"),
        ("imagen", "d.jpg"),
        ("imagen", "e.png"),
    ]


def test_construir_planes_agrupa_xml_y_pdf_por_nombre(tmp_path):
    xml = _crear_archivo(tmp_path, "F001-1.xml")
    pdf_respaldo = _crear_archivo(tmp_path, "F001-1.pdf")
    pdf_suelto = _crear_archivo(tmp_path, "F001-2.pdf")

    planes = procesar.construir_planes([xml, pdf_respaldo, pdf_suelto])

    plan_agrupado = next(p for p in planes if p[0].name == "F001-1.xml")
    assert plan_agrupado[1] == [pdf_respaldo]

    plan_suelto = next(p for p in planes if p[0].name == "F001-2.pdf")
    assert plan_suelto[1] == []


# -----------------------------------------------------------------------------
# .heic va a revisar
# -----------------------------------------------------------------------------
def test_heic_va_a_revisar(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    ruta = _crear_archivo(buzon, "foto.heic")

    resultado = _procesar_uno(ruta, [], config, registro, cat, resolutor, procesado, revisar)

    assert resultado.estado == "revisar"
    assert resultado.motivo == procesar.MOTIVO_HEIC
    assert not ruta.exists()
    assert (revisar / "foto.heic").exists()
    assert (revisar / "foto.heic.motivo.txt").read_text(encoding="utf-8") == procesar.MOTIVO_HEIC
    assert registro.escritos == []


# -----------------------------------------------------------------------------
# RUC de cliente desconocido va a revisar
# -----------------------------------------------------------------------------
def test_ruc_cliente_desconocido_va_a_revisar(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    _modulo_xml_ubl.extraer = lambda ruta: ComprobanteFalso(cliente_ruc="99999999999")
    ruta = _crear_archivo(buzon, "f.xml")

    resultado = _procesar_uno(ruta, [], config, registro, cat, resolutor, procesado, revisar)

    assert resultado.estado == "revisar"
    assert "no corresponde a ninguna empresa" in resultado.motivo
    assert not ruta.exists()
    assert (revisar / "f.xml").exists()
    assert registro.escritos == []


def test_sin_ruc_cliente_va_a_revisar(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    _modulo_xml_ubl.extraer = lambda ruta: ComprobanteFalso(cliente_ruc=None)
    ruta = _crear_archivo(buzon, "f.xml")

    resultado = _procesar_uno(ruta, [], config, registro, cat, resolutor, procesado, revisar)

    assert resultado.estado == "revisar"
    assert "no trae RUC de cliente" in resultado.motivo
    assert registro.escritos == []


# -----------------------------------------------------------------------------
# Duplicado va a revisar sin escribir
# -----------------------------------------------------------------------------
def test_duplicado_va_a_revisar_sin_escribir(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    comp = ComprobanteFalso()
    _modulo_xml_ubl.extraer = lambda ruta: comp
    registro._claves_existentes.add(comp.clave())
    ruta = _crear_archivo(buzon, "f.xml")

    resultado = _procesar_uno(ruta, [], config, registro, cat, resolutor, procesado, revisar)

    assert resultado.estado == "duplicado"
    assert "duplicado" in resultado.motivo
    assert registro.escritos == []
    assert not ruta.exists()
    assert (revisar / "f.xml").exists()


# -----------------------------------------------------------------------------
# Caso feliz: procesado, con emparejado de ítems
# -----------------------------------------------------------------------------
def test_comprobante_valido_se_procesa_y_empareja_items(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    item = ItemFalso(descripcion="ACEITE CRISOL X20 LT")
    comp = ComprobanteFalso(items=[item])
    _modulo_xml_ubl.extraer = lambda ruta: comp

    _modulo_catalogo.Catalogo = _CatalogoFalso  # por si otro test lo cambió
    cat_local = _CatalogoFalso(None)

    class CatalogoConMatch(_CatalogoFalso):
        def emparejar(self, descripcion):
            return "ACEITE CRISOL X20 LT", "ABARROTES", 0.95

    ruta = _crear_archivo(buzon, "f.xml")
    resultado = _procesar_uno(ruta, [], config, registro, CatalogoConMatch(None), resolutor, procesado, revisar)

    assert resultado.estado == "procesado"
    assert resultado.n_items == 1
    assert item.insumo_catalogo == "ACEITE CRISOL X20 LT"
    assert item.categoria_catalogo == "ABARROTES"
    assert item.confianza_match == 0.95
    assert len(registro.escritos) == 1
    assert registro.escritos[0]["empresa"] == "INSTITUCION"
    assert registro.escritos[0]["local"] == "MIRAFLORES"
    assert not ruta.exists()

    destino_esperado = procesado / "2026-07" / "INSTITUCION" / procesar.nombre_destino(comp, ".xml")
    assert destino_esperado.exists()


# -----------------------------------------------------------------------------
# Un archivo que falla no detiene el lote
# -----------------------------------------------------------------------------
def test_archivo_que_falla_no_detiene_el_lote(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    ruta_config = tmp_path / "config.yaml"
    ruta_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    ruta_mala = _crear_archivo(buzon, "malo.xml")
    ruta_buena = _crear_archivo(buzon, "bueno.xml")

    comp_bueno = ComprobanteFalso(serie_numero="F001-999", total=50.0)

    def extraer_falible(ruta):
        if ruta.name == "malo.xml":
            raise RuntimeError("XML corrupto de prueba")
        return comp_bueno

    _modulo_xml_ubl.extraer = extraer_falible
    _modulo_registro_sheets.Registro = lambda cfg: registro

    try:
        codigo = procesar.main(["--config", str(ruta_config)])
    finally:
        _modulo_registro_sheets.Registro = _RegistroFalso

    assert codigo == 0
    assert not ruta_mala.exists()
    assert (revisar / "malo.xml").exists()
    assert "XML corrupto de prueba" in (revisar / "malo.xml.motivo.txt").read_text(encoding="utf-8")

    assert not ruta_buena.exists()
    assert len(registro.escritos) == 1
    assert registro.escritos[0]["archivo"] == procesar.nombre_destino(comp_bueno, ".xml")


# -----------------------------------------------------------------------------
# Ningún archivo se borra jamás
# -----------------------------------------------------------------------------
def test_ningun_archivo_se_borra(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    ruta_config = tmp_path / "config.yaml"
    ruta_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    comp_ok = ComprobanteFalso(serie_numero="F001-1", total=10.0)
    comp_dup = ComprobanteFalso(serie_numero="F001-2", total=20.0)
    registro._claves_existentes.add(comp_dup.clave())

    def extraer(ruta):
        if ruta.name == "duplicado.xml":
            return comp_dup
        return comp_ok

    _modulo_xml_ubl.extraer = extraer
    _modulo_registro_sheets.Registro = lambda cfg: registro

    _crear_archivo(buzon, "bueno.xml")
    _crear_archivo(buzon, "duplicado.xml")
    _crear_archivo(buzon, "foto.heic")

    try:
        codigo = procesar.main(["--config", str(ruta_config)])
    finally:
        _modulo_registro_sheets.Registro = _RegistroFalso

    assert codigo == 0

    archivos_reales = [
        p for p in tmp_path.rglob("*")
        if p.is_file() and p.suffix != ".yaml" and not p.name.endswith(".motivo.txt")
    ]
    assert len(archivos_reales) == 3
    nombres_encontrados = {p.name for p in archivos_reales}
    assert nombres_encontrados == {"foto.heic", procesar.nombre_destino(comp_ok, ".xml"), "duplicado.xml"}


# -----------------------------------------------------------------------------
# El renombrado nunca sobrescribe un archivo existente en el destino
# -----------------------------------------------------------------------------
def test_renombrado_no_sobrescribe(tmp_path):
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    comp = ComprobanteFalso(proveedor_ruc="20100000001", serie_numero="F001-500", total=200.0)
    _modulo_xml_ubl.extraer = lambda ruta: comp

    nombre_esperado = procesar.nombre_destino(comp, ".xml")
    carpeta_destino = procesado / procesar.anio_mes(comp) / procesar.nombre_empresa_carpeta("INSTITUCION")
    carpeta_destino.mkdir(parents=True, exist_ok=True)
    archivo_previo = carpeta_destino / nombre_esperado
    archivo_previo.write_bytes(b"CONTENIDO_ORIGINAL_NO_TOCAR")

    ruta = _crear_archivo(buzon, "nuevo.xml", b"CONTENIDO_NUEVO")
    resultado = _procesar_uno(ruta, [], config, registro, cat, resolutor, procesado, revisar)

    assert resultado.estado == "procesado"
    assert archivo_previo.read_bytes() == b"CONTENIDO_ORIGINAL_NO_TOCAR"

    hermanos = {p.name: p.read_bytes() for p in carpeta_destino.iterdir()}
    assert len(hermanos) == 2
    stem, ext = nombre_esperado.rsplit(".", 1)
    nombre_con_sufijo = f"{stem}_2.{ext}"
    assert nombre_con_sufijo in hermanos
    assert hermanos[nombre_con_sufijo] == b"CONTENIDO_NUEVO"
    assert hermanos[nombre_esperado] == b"CONTENIDO_ORIGINAL_NO_TOCAR"


def test_ruta_destino_unica_agrega_sufijos_incrementales(tmp_path):
    (tmp_path / "x.xml").write_bytes(b"1")
    (tmp_path / "x_2.xml").write_bytes(b"2")

    destino = procesar.ruta_destino_unica(tmp_path, "x.xml")

    assert destino.name == "x_3.xml"


# -----------------------------------------------------------------------------
# --dry-run no escribe ni mueve nada
# -----------------------------------------------------------------------------
def test_dry_run_no_mueve_y_delega_escritura_en_registro(tmp_path):
    """En dry-run no se mueve nada, pero la escritura SÍ se delega en Registro.

    El contrato cambió a propósito: antes procesar.py cortaba antes de llamar
    a `registro.escribir()` en dry-run, lo que dejaba muerto el modo CSV de
    Registro y hacía que la corrida de prueba no produjera nada revisable.
    Ahora se llama siempre y Registro decide según config['dry_run'] si
    escribe en Sheets o en salida/*.csv. Así la corrida de calibración
    ejercita el mismo camino de código que la real (construcción de filas,
    orden de columnas, emparejado de ítems) en vez de solo anunciarlo.
    """
    config, buzon, procesado, revisar, registro, cat, resolutor = _entorno(tmp_path)
    comp = ComprobanteFalso()
    _modulo_xml_ubl.extraer = lambda ruta: comp
    ruta = _crear_archivo(buzon, "f.xml")

    resultado = _procesar_uno(ruta, [], config, registro, cat, resolutor, procesado, revisar, dry_run=True)

    assert resultado.estado == "procesado"
    assert ruta.exists()  # no se movió: eso es lo que dry-run garantiza
    assert not any(procesado.rglob("*")), "dry-run no debe crear nada en 01_PROCESADO"
    assert len(registro.escritos) == 1  # se delegó en Registro, que decide el destino
