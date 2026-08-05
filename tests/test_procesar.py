"""Tests de procesar.py.

Corren sin red y sin credenciales: los módulos que todavía no existen o que
requieren Google/Anthropic (extractores.xml_ubl, extractores.modelo,
catalogo, registro_sheets, auth_google) se sustituyen por dobles de prueba
registrados directamente en sys.modules ANTES de importar procesar, así que
procesar.py se importa igual aunque esos archivos no existan en disco
todavía (los están escribiendo otros dos agentes en paralelo).

El trato con Google Drive (almacen_drive.AlmacenDrive) se sustituye por
AlmacenDriveFalso, un doble en memoria que implementa la misma interfaz sin
pasar por googleapiclient en absoluto: eso se prueba aparte, contra un
doble del Resource real, en tests/test_almacen_drive.py. Acá lo que se
prueba es que procesar.py orquesta correctamente esas llamadas.

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
# Doble en memoria de AlmacenDrive (orquestación de procesar.py). El doble
# del Resource de drive v3 (googleapiclient) se prueba aparte, en
# tests/test_almacen_drive.py, contra la clase real.
# -----------------------------------------------------------------------------
class AlmacenDriveFalso:
    def __init__(self, servicio_drive=None):
        self._contador = 0
        self.archivos: dict[str, dict] = {}
        self.carpetas: dict[str, dict] = {}
        self.textos_creados: list[tuple[str, str, str]] = []
        self.movimientos: list[tuple[str, str, str | None]] = []
        self.carpetas_aseguradas: list[tuple[str, str | None]] = []

    def _nuevo_id(self, prefijo: str) -> str:
        self._contador += 1
        return f"{prefijo}-{self._contador}"

    # -- helpers de test para poblar estado -------------------------------
    def agregar_archivo(self, carpeta_id: str, nombre: str, contenido: bytes = b"contenido de prueba") -> str:
        file_id = self._nuevo_id("archivo")
        self.archivos[file_id] = {"name": nombre, "parent": carpeta_id, "contenido": contenido, "mimeType": "application/octet-stream"}
        return file_id

    def agregar_carpeta(self, nombre: str, padre_id: str | None = None) -> str:
        carpeta_id = self._nuevo_id("carpeta")
        self.carpetas[carpeta_id] = {"nombre": nombre, "padre_id": padre_id}
        return carpeta_id

    # -- interfaz de AlmacenDrive ------------------------------------------
    def listar(self, carpeta_id: str) -> list[dict]:
        return [
            {"id": fid, "name": f["name"], "mimeType": f["mimeType"], "size": len(f["contenido"])}
            for fid, f in self.archivos.items()
            if f["parent"] == carpeta_id
        ]

    def descargar(self, file_id: str, destino):
        destino = pathlib.Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(self.archivos[file_id]["contenido"])
        return destino

    def mover(self, file_id: str, carpeta_destino_id: str, nuevo_nombre: str | None = None) -> None:
        self.movimientos.append((file_id, carpeta_destino_id, nuevo_nombre))
        archivo = self.archivos[file_id]
        archivo["parent"] = carpeta_destino_id
        if nuevo_nombre:
            archivo["name"] = nuevo_nombre

    def crear_texto(self, carpeta_id: str, nombre: str, contenido: str) -> str:
        file_id = self._nuevo_id("texto")
        self.archivos[file_id] = {
            "name": nombre, "parent": carpeta_id, "contenido": contenido.encode("utf-8"), "mimeType": "text/plain"
        }
        self.textos_creados.append((carpeta_id, nombre, contenido))
        return file_id

    def enlace(self, file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/view"

    def asegurar_carpeta(self, nombre: str, padre_id: str | None = None) -> str:
        self.carpetas_aseguradas.append((nombre, padre_id))
        for cid, c in self.carpetas.items():
            if c["nombre"] == nombre and c["padre_id"] == padre_id:
                return cid
        return self.agregar_carpeta(nombre, padre_id)


# -----------------------------------------------------------------------------
# Fixtures / helpers de prueba
# -----------------------------------------------------------------------------
def _config_base() -> dict:
    return {
        "negocio": "SCONCHA",
        "cuenta_google": "administracion.sconcha@gmail.com",
        "drive": {
            "raiz_nombre": "SCONCHA",
            "carpetas": {"raiz": "", "buzon": "", "procesado": "", "revisar": ""},
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


def _entorno():
    config = _config_base()
    almacen = AlmacenDriveFalso()
    carpeta_raiz_id = almacen.agregar_carpeta("SCONCHA")
    carpeta_buzon_id = almacen.agregar_carpeta("00_BUZON", carpeta_raiz_id)
    carpeta_procesado_id = almacen.agregar_carpeta("01_PROCESADO", carpeta_raiz_id)
    carpeta_revisar_id = almacen.agregar_carpeta("02_REVISAR", carpeta_raiz_id)
    config["drive"]["carpetas"] = {
        "raiz": carpeta_raiz_id,
        "buzon": carpeta_buzon_id,
        "procesado": carpeta_procesado_id,
        "revisar": carpeta_revisar_id,
    }
    registro = _RegistroFalso(config)
    catalogo_obj = _CatalogoFalso(None)
    return config, almacen, carpeta_buzon_id, carpeta_procesado_id, carpeta_revisar_id, registro, catalogo_obj


def _crear_archivo_local(carpeta: pathlib.Path, nombre: str, contenido: bytes = b"contenido de prueba") -> pathlib.Path:
    """Crea un archivo real en disco. Solo lo usa test_clasificacion_por_extension,
    que ejercita extraer_comprobante() directo (sigue recibiendo pathlib.Path,
    eso no cambió: lo que cambió es cómo procesar.py obtiene esa ruta local
    -descargándola de Drive a un temporal- antes de llamarlo)."""
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / nombre
    ruta.write_bytes(contenido)
    return ruta


def _crear_archivo(almacen: AlmacenDriveFalso, carpeta_id: str, nombre: str, contenido: bytes = b"contenido de prueba") -> "procesar.ArchivoDrive":
    file_id = almacen.agregar_archivo(carpeta_id, nombre, contenido)
    return procesar.ArchivoDrive(id=file_id, name=nombre, mime_type="application/octet-stream", size=len(contenido))


def _procesar_uno(archivo, respaldos, config, registro, catalogo_obj, almacen, carpeta_procesado_id, carpeta_revisar_id, dry_run=False):
    return procesar.procesar_uno(
        archivo,
        respaldos,
        config=config,
        registro=registro,
        catalogo_obj=catalogo_obj,
        almacen=almacen,
        claves_procesadas_en_lote=set(),
        nombres_por_carpeta={},
        carpeta_procesado_id=carpeta_procesado_id,
        carpeta_revisar_id=carpeta_revisar_id,
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

    ruta_xml = _crear_archivo_local(tmp_path, "a.xml")
    ruta_zip = _crear_archivo_local(tmp_path, "b.zip")
    ruta_pdf = _crear_archivo_local(tmp_path, "c.pdf")
    ruta_jpg = _crear_archivo_local(tmp_path, "d.jpg")
    ruta_png = _crear_archivo_local(tmp_path, "e.png")

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


def test_construir_planes_agrupa_xml_y_pdf_por_nombre():
    xml = procesar.ArchivoDrive(id="1", name="F001-1.xml", mime_type="text/xml")
    pdf_respaldo = procesar.ArchivoDrive(id="2", name="F001-1.pdf", mime_type="application/pdf")
    pdf_suelto = procesar.ArchivoDrive(id="3", name="F001-2.pdf", mime_type="application/pdf")

    planes = procesar.construir_planes([xml, pdf_respaldo, pdf_suelto])

    plan_agrupado = next(p for p in planes if p[0].name == "F001-1.xml")
    assert plan_agrupado[1] == [pdf_respaldo]

    plan_suelto = next(p for p in planes if p[0].name == "F001-2.pdf")
    assert plan_suelto[1] == []


# -----------------------------------------------------------------------------
# nombre_destino_unico (reemplaza el viejo ruta_destino_unica basado en disco)
# -----------------------------------------------------------------------------
def test_nombre_destino_unico_devuelve_igual_si_no_choca():
    assert procesar.nombre_destino_unico(set(), "x.xml") == "x.xml"


def test_nombre_destino_unico_agrega_sufijos_incrementales():
    existentes = {"x.xml", "x_2.xml"}
    assert procesar.nombre_destino_unico(existentes, "x.xml") == "x_3.xml"


# -----------------------------------------------------------------------------
# .heic va a revisar
# -----------------------------------------------------------------------------
def test_heic_va_a_revisar():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    archivo = _crear_archivo(almacen, buzon_id, "foto.heic")

    resultado = _procesar_uno(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id)

    assert resultado.estado == "revisar"
    assert resultado.motivo == procesar.MOTIVO_HEIC
    assert almacen.archivos[archivo.id]["parent"] == revisar_id
    assert almacen.archivos[archivo.id]["name"] == "foto.heic"
    motivo_id = next(fid for fid, f in almacen.archivos.items() if f["name"] == "foto.heic.motivo.txt")
    assert almacen.archivos[motivo_id]["contenido"] == procesar.MOTIVO_HEIC.encode("utf-8")
    assert registro.escritos == []


# -----------------------------------------------------------------------------
# RUC de cliente desconocido va a revisar
# -----------------------------------------------------------------------------
def test_ruc_cliente_desconocido_va_a_revisar():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    _modulo_xml_ubl.extraer = lambda ruta: ComprobanteFalso(cliente_ruc="99999999999")
    archivo = _crear_archivo(almacen, buzon_id, "f.xml")

    resultado = _procesar_uno(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id)

    assert resultado.estado == "revisar"
    assert "no corresponde a ninguna empresa" in resultado.motivo
    assert almacen.archivos[archivo.id]["parent"] == revisar_id
    assert registro.escritos == []


def test_sin_ruc_cliente_va_a_revisar():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    _modulo_xml_ubl.extraer = lambda ruta: ComprobanteFalso(cliente_ruc=None)
    archivo = _crear_archivo(almacen, buzon_id, "f.xml")

    resultado = _procesar_uno(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id)

    assert resultado.estado == "revisar"
    assert "no trae RUC de cliente" in resultado.motivo
    assert registro.escritos == []


# -----------------------------------------------------------------------------
# Duplicado va a revisar sin escribir
# -----------------------------------------------------------------------------
def test_duplicado_va_a_revisar_sin_escribir():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    comp = ComprobanteFalso()
    _modulo_xml_ubl.extraer = lambda ruta: comp
    registro._claves_existentes.add(comp.clave())
    archivo = _crear_archivo(almacen, buzon_id, "f.xml")

    resultado = _procesar_uno(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id)

    assert resultado.estado == "duplicado"
    assert "duplicado" in resultado.motivo
    assert registro.escritos == []
    assert almacen.archivos[archivo.id]["parent"] == revisar_id


# -----------------------------------------------------------------------------
# Caso feliz: procesado, con emparejado de ítems
# -----------------------------------------------------------------------------
def test_comprobante_valido_se_procesa_y_empareja_items():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    item = ItemFalso(descripcion="ACEITE CRISOL X20 LT")
    comp = ComprobanteFalso(items=[item])
    _modulo_xml_ubl.extraer = lambda ruta: comp

    class CatalogoConMatch(_CatalogoFalso):
        def emparejar(self, descripcion):
            return "ACEITE CRISOL X20 LT", "ABARROTES", 0.95

    archivo = _crear_archivo(almacen, buzon_id, "f.xml")
    resultado = _procesar_uno(archivo, [], config, registro, CatalogoConMatch(None), almacen, procesado_id, revisar_id)

    assert resultado.estado == "procesado"
    assert resultado.n_items == 1
    assert item.insumo_catalogo == "ACEITE CRISOL X20 LT"
    assert item.categoria_catalogo == "ABARROTES"
    assert item.confianza_match == 0.95
    assert len(registro.escritos) == 1
    assert registro.escritos[0]["empresa"] == "INSTITUCION"
    assert registro.escritos[0]["local"] == "MIRAFLORES"
    assert registro.escritos[0]["link_drive"] == f"https://drive.google.com/file/d/{archivo.id}/view"

    nombre_esperado = procesar.nombre_destino(comp, ".xml")
    assert almacen.archivos[archivo.id]["name"] == nombre_esperado
    carpeta_destino_id = almacen.archivos[archivo.id]["parent"]
    assert carpeta_destino_id != buzon_id

    carpeta_empresa = almacen.carpetas[carpeta_destino_id]
    assert carpeta_empresa["nombre"] == "INSTITUCION"
    carpeta_mes = almacen.carpetas[carpeta_empresa["padre_id"]]
    assert carpeta_mes["nombre"] == "2026-07"
    assert carpeta_mes["padre_id"] == procesado_id


# -----------------------------------------------------------------------------
# Un archivo que falla no detiene el lote
# -----------------------------------------------------------------------------
def test_archivo_que_falla_no_detiene_el_lote(tmp_path, monkeypatch):
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    ruta_config = tmp_path / "config.yaml"
    ruta_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    archivo_malo = _crear_archivo(almacen, buzon_id, "malo.xml")
    archivo_bueno = _crear_archivo(almacen, buzon_id, "bueno.xml")

    comp_bueno = ComprobanteFalso(serie_numero="F001-999", total=50.0)

    def extraer_falible(ruta):
        if ruta.name == "malo.xml":
            raise RuntimeError("XML corrupto de prueba")
        return comp_bueno

    _modulo_xml_ubl.extraer = extraer_falible
    _modulo_registro_sheets.Registro = lambda cfg: registro
    monkeypatch.setattr(procesar, "AlmacenDrive", lambda servicio: almacen)

    try:
        codigo = procesar.main(["--config", str(ruta_config)])
    finally:
        _modulo_registro_sheets.Registro = _RegistroFalso

    assert codigo == 0
    assert almacen.archivos[archivo_malo.id]["parent"] == revisar_id
    motivo_id = next(fid for fid, f in almacen.archivos.items() if f["name"] == "malo.xml.motivo.txt")
    assert "XML corrupto de prueba" in almacen.archivos[motivo_id]["contenido"].decode("utf-8")

    assert almacen.archivos[archivo_bueno.id]["parent"] not in (buzon_id, revisar_id)
    assert len(registro.escritos) == 1
    assert registro.escritos[0]["archivo"] == procesar.nombre_destino(comp_bueno, ".xml")


# -----------------------------------------------------------------------------
# Ningún archivo se borra jamás
# -----------------------------------------------------------------------------
def test_ningun_archivo_se_borra(tmp_path, monkeypatch):
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
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
    monkeypatch.setattr(procesar, "AlmacenDrive", lambda servicio: almacen)

    id_bueno = _crear_archivo(almacen, buzon_id, "bueno.xml").id
    id_dup = _crear_archivo(almacen, buzon_id, "duplicado.xml").id
    id_heic = _crear_archivo(almacen, buzon_id, "foto.heic").id

    try:
        codigo = procesar.main(["--config", str(ruta_config)])
    finally:
        _modulo_registro_sheets.Registro = _RegistroFalso

    assert codigo == 0

    # AlmacenDriveFalso no tiene método de borrado: ningún id original puede
    # haber desaparecido, solo cambiar de padre/nombre.
    assert {id_bueno, id_dup, id_heic} <= set(almacen.archivos.keys())
    assert almacen.listar(buzon_id) == []  # el buzón quedó vacío: todo se movió

    assert almacen.archivos[id_bueno]["parent"] not in (buzon_id, revisar_id)
    assert almacen.archivos[id_bueno]["name"] == procesar.nombre_destino(comp_ok, ".xml")
    assert almacen.archivos[id_dup]["parent"] == revisar_id
    assert almacen.archivos[id_heic]["parent"] == revisar_id


# -----------------------------------------------------------------------------
# El renombrado nunca sobrescribe un archivo existente en el destino
# -----------------------------------------------------------------------------
def test_renombrado_no_sobrescribe():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    comp = ComprobanteFalso(proveedor_ruc="20100000001", serie_numero="F001-500", total=200.0)
    _modulo_xml_ubl.extraer = lambda ruta: comp

    nombre_esperado = procesar.nombre_destino(comp, ".xml")
    carpeta_mes_id = almacen.agregar_carpeta(procesar.anio_mes(comp), procesado_id)
    carpeta_empresa_id = almacen.agregar_carpeta("INSTITUCION", carpeta_mes_id)
    id_previo = almacen.agregar_archivo(carpeta_empresa_id, nombre_esperado, b"CONTENIDO_ORIGINAL_NO_TOCAR")

    archivo = _crear_archivo(almacen, buzon_id, "nuevo.xml", b"CONTENIDO_NUEVO")
    resultado = _procesar_uno(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id)

    assert resultado.estado == "procesado"
    assert almacen.archivos[id_previo]["contenido"] == b"CONTENIDO_ORIGINAL_NO_TOCAR"
    assert almacen.archivos[id_previo]["name"] == nombre_esperado

    stem, ext = nombre_esperado.rsplit(".", 1)
    nombre_con_sufijo = f"{stem}_2.{ext}"
    assert almacen.archivos[archivo.id]["name"] == nombre_con_sufijo
    assert almacen.archivos[archivo.id]["parent"] == carpeta_empresa_id


# -----------------------------------------------------------------------------
# --dry-run no escribe ni mueve nada en Drive
# -----------------------------------------------------------------------------
def test_dry_run_no_mueve_y_delega_escritura_en_registro():
    """En dry-run no se mueve nada en Drive, pero la escritura SÍ se delega
    en Registro.

    El contrato cambió a propósito: antes procesar.py cortaba antes de llamar
    a `registro.escribir()` en dry-run, lo que dejaba muerto el modo CSV de
    Registro y hacía que la corrida de prueba no produjera nada revisable.
    Ahora se llama siempre y Registro decide según config['dry_run'] si
    escribe en Sheets o en salida/*.csv. Así la corrida de calibración
    ejercita el mismo camino de código que la real (construcción de filas,
    orden de columnas, emparejado de ítems) en vez de solo anunciarlo.
    """
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    comp = ComprobanteFalso()
    _modulo_xml_ubl.extraer = lambda ruta: comp
    archivo = _crear_archivo(almacen, buzon_id, "f.xml")

    resultado = _procesar_uno(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, dry_run=True)

    assert resultado.estado == "procesado"
    assert almacen.archivos[archivo.id]["parent"] == buzon_id  # no se movió
    assert almacen.movimientos == []
    assert almacen.carpetas_aseguradas == []  # no se creó ninguna subcarpeta
    assert almacen.textos_creados == []
    assert len(registro.escritos) == 1  # se delegó en Registro, que decide el destino


def test_main_dry_run_llama_a_drive_pero_no_escribe(tmp_path, monkeypatch):
    """--dry-run sigue necesitando autenticarse contra Drive (listar y
    descargar son lecturas contra la API, ya no lectura de disco local),
    pero no debe mover nada, ni crear carpetas, ni crear .motivo.txt."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    ruta_config = tmp_path / "config.yaml"
    ruta_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    comp = ComprobanteFalso()
    _modulo_xml_ubl.extraer = lambda ruta: comp
    _modulo_registro_sheets.Registro = lambda cfg: registro
    _crear_archivo(almacen, buzon_id, "f.xml")

    llamadas_servicio_drive = []
    monkeypatch.setattr(_modulo_auth_google, "servicio_drive", lambda: llamadas_servicio_drive.append(1) or None)
    monkeypatch.setattr(procesar, "AlmacenDrive", lambda servicio: almacen)

    try:
        codigo = procesar.main(["--config", str(ruta_config), "--dry-run"])
    finally:
        _modulo_registro_sheets.Registro = _RegistroFalso

    assert codigo == 0
    assert llamadas_servicio_drive == [1]  # se autenticó igual, aunque sea dry-run
    assert almacen.movimientos == []
    assert almacen.textos_creados == []
    assert almacen.carpetas_aseguradas == []
    assert len(registro.escritos) == 1
