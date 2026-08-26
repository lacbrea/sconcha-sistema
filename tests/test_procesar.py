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

import datetime
import logging
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


def _extraer_modelo_por_defecto(ruta, tipo, config=None, tipo_esperado=None):
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
        self.respaldos_caja = []
        self._respaldos_existentes = set()

    def claves_existentes(self):
        return set(self._claves_existentes)

    def escribir(self, comp, empresa, local, link_drive, archivo):
        self.escritos.append(
            {"comp": comp, "empresa": empresa, "local": local, "link_drive": link_drive, "archivo": archivo}
        )

    def respaldos_existentes(self):
        return set(self._respaldos_existentes)

    def registrar_respaldo_caja(self, fecha, empresa, local, archivo, link_drive):
        clave = f"{archivo.strip().upper()}|{(empresa or '').strip().upper()}"
        if clave in self._respaldos_existentes:
            return False
        self._respaldos_existentes.add(clave)
        self.respaldos_caja.append(
            {"fecha": fecha, "empresa": empresa, "local": local, "archivo": archivo, "link_drive": link_drive}
        )
        return True


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
        self.carpetas_buscadas: list[tuple[str, str | None]] = []

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

    def buscar_carpeta(self, nombre: str, padre_id: str | None = None) -> str | None:
        self.carpetas_buscadas.append((nombre, padre_id))
        for cid, c in self.carpetas.items():
            if c["nombre"] == nombre and c["padre_id"] == padre_id:
                return cid
        return None

    def asegurar_carpeta(self, nombre: str, padre_id: str | None = None) -> str:
        self.carpetas_aseguradas.append((nombre, padre_id))
        encontrada = self.buscar_carpeta(nombre, padre_id)
        if encontrada is not None:
            return encontrada
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


def _entorno_con_buzon_tipos():
    """Igual que _entorno(), pero con las 4 subcarpetas de 00_BUZON por tipo
    (FACTURAS/NOTAS_DE_VENTA/LIQUIDACIONES/OTROS) ya creadas y cableadas en
    config['drive']['carpetas']['buzon_tipos'], como quedaría un negocio
    después de correr init_negocio.py con esa sección configurada."""
    config, almacen, carpeta_buzon_id, carpeta_procesado_id, carpeta_revisar_id, registro, catalogo_obj = _entorno()
    ids_tipos = {
        "facturas": almacen.agregar_carpeta("FACTURAS", carpeta_buzon_id),
        "notas_venta": almacen.agregar_carpeta("NOTAS_DE_VENTA", carpeta_buzon_id),
        "liquidaciones": almacen.agregar_carpeta("LIQUIDACIONES", carpeta_buzon_id),
        "otros": almacen.agregar_carpeta("OTROS", carpeta_buzon_id),
    }
    config["drive"]["carpetas"]["buzon_tipos"] = dict(ids_tipos)
    return config, almacen, carpeta_buzon_id, carpeta_procesado_id, carpeta_revisar_id, registro, catalogo_obj, ids_tipos


def _entorno_con_buzon_empresas(nombres_empresas: list[str] | None = None):
    """Igual que _entorno(), pero con 00_BUZON/<EMPRESA>/<TIPO> ya creadas y
    cableadas en config['drive']['carpetas']['buzon_empresas'], como
    quedaría un negocio de varias empresas después de correr init_negocio.py
    con esa sección configurada. Devuelve además ids_por_empresa: dict
    nombre_corto -> {tipo: id}."""
    config, almacen, carpeta_buzon_id, carpeta_procesado_id, carpeta_revisar_id, registro, catalogo_obj = _entorno()
    if nombres_empresas is None:
        nombres_empresas = [e["nombre_corto"] for e in config["empresas"]]

    ids_por_empresa: dict[str, dict[str, str]] = {}
    buzon_empresas_cfg: dict[str, dict[str, str]] = {}
    for nombre_empresa in nombres_empresas:
        carpeta_empresa_id = almacen.agregar_carpeta(procesar.nombre_empresa_carpeta(nombre_empresa), carpeta_buzon_id)
        ids_tipos = {
            "facturas": almacen.agregar_carpeta("FACTURAS", carpeta_empresa_id),
            "notas_venta": almacen.agregar_carpeta("NOTAS_DE_VENTA", carpeta_empresa_id),
            "liquidaciones": almacen.agregar_carpeta("LIQUIDACIONES", carpeta_empresa_id),
            "otros": almacen.agregar_carpeta("OTROS", carpeta_empresa_id),
        }
        ids_por_empresa[nombre_empresa] = ids_tipos
        buzon_empresas_cfg[nombre_empresa] = dict(ids_tipos)

    config["drive"]["carpetas"]["buzon_empresas"] = buzon_empresas_cfg
    return config, almacen, carpeta_buzon_id, carpeta_procesado_id, carpeta_revisar_id, registro, catalogo_obj, ids_por_empresa


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

    def falso_modelo(ruta, tipo, config=None, tipo_esperado=None):
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


# -----------------------------------------------------------------------------
# resolver_buzon_tipos_ids / construir_planes_enrutados: enrutado del buzón
# por subcarpeta de tipo (drive.carpetas.buzon_tipos). Ver config.ejemplo.yaml.
# -----------------------------------------------------------------------------
def test_resolver_buzon_tipos_ids_todas_vacias_si_no_hay_seccion():
    assert procesar.resolver_buzon_tipos_ids({}) == {
        "facturas": "", "notas_venta": "", "liquidaciones": "", "otros": "",
    }


def test_resolver_buzon_tipos_ids_lee_las_4_claves():
    carpetas_cfg = {"buzon_tipos": {"facturas": "id1", "notas_venta": "id2", "liquidaciones": "id3", "otros": "id4"}}
    assert procesar.resolver_buzon_tipos_ids(carpetas_cfg) == {
        "facturas": "id1", "notas_venta": "id2", "liquidaciones": "id3", "otros": "id4",
    }


def test_construir_planes_enrutados_sin_buzon_tipos_usa_comportamiento_historico():
    """Regresión: un negocio SIN 'buzon_tipos' (o con las 4 claves vacías)
    sigue viendo el comportamiento de siempre -solo se lista la raíz del
    buzón, tipo es None para todos los planes-, sin la advertencia de
    "archivo en la raíz" que sí ve un negocio ya migrado."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    _crear_archivo(almacen, buzon_id, "f.xml")
    _crear_archivo(almacen, buzon_id, "g.pdf")

    ids_vacios = procesar.resolver_buzon_tipos_ids(config["drive"]["carpetas"])
    assert not any(ids_vacios.values())

    planes = procesar.construir_planes_enrutados(almacen, buzon_id, ids_vacios)

    assert {p[0].name for p in planes} == {"f.xml", "g.pdf"}
    assert all(tipo is None for _, _, tipo, _ in planes)
    assert all(empresa_carpeta is None for _, _, _, empresa_carpeta in planes)


def test_construir_planes_enrutados_enruta_por_subcarpeta_y_raiz():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_tipos = _entorno_con_buzon_tipos()
    _crear_archivo(almacen, ids_tipos["facturas"], "fact.xml")
    _crear_archivo(almacen, ids_tipos["notas_venta"], "01.07 BOLETAS.pdf")
    _crear_archivo(almacen, ids_tipos["liquidaciones"], "compra.jpg")
    _crear_archivo(almacen, ids_tipos["otros"], "recibo.pdf")
    _crear_archivo(almacen, buzon_id, "suelto.pdf")  # raíz, compatibilidad

    buzon_tipos_ids = procesar.resolver_buzon_tipos_ids(config["drive"]["carpetas"])
    planes = procesar.construir_planes_enrutados(almacen, buzon_id, buzon_tipos_ids)

    por_nombre = {p[0].name: p[2] for p in planes}
    assert por_nombre["fact.xml"] == "facturas"
    assert por_nombre["01.07 BOLETAS.pdf"] == "notas_venta"
    assert por_nombre["compra.jpg"] == "liquidaciones"
    assert por_nombre["recibo.pdf"] == "otros"
    assert por_nombre["suelto.pdf"] == procesar.TIPO_RAIZ_BUZON
    # buzon_tipos (sin buzon_empresas) nunca trae empresa_carpeta: eso es
    # exclusivo de buzon_empresas.
    assert all(empresa_carpeta is None for _, _, _, empresa_carpeta in planes)


# -----------------------------------------------------------------------------
# Enrutado en procesar_uno: tipo_esperado según la subcarpeta de origen.
# -----------------------------------------------------------------------------
def test_liquidaciones_pasa_tipo_esperado_liquidacion_al_extractor_modelo():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    tipos_esperados_recibidos = []

    def falso_modelo(ruta, tipo, config=None, tipo_esperado=None):
        tipos_esperados_recibidos.append(tipo_esperado)
        return ComprobanteFalso()

    _modulo_extractor_modelo.extraer = falso_modelo
    archivo = _crear_archivo(almacen, buzon_id, "compra.pdf")

    resultado = _procesar_uno_con_tipo(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo="liquidaciones")

    assert resultado.estado == "procesado"
    assert tipos_esperados_recibidos == ["liquidacion"]


def test_otros_pasa_tipo_esperado_recibo_servicio_al_extractor_modelo():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    tipos_esperados_recibidos = []

    def falso_modelo(ruta, tipo, config=None, tipo_esperado=None):
        tipos_esperados_recibidos.append(tipo_esperado)
        return ComprobanteFalso()

    _modulo_extractor_modelo.extraer = falso_modelo
    archivo = _crear_archivo(almacen, buzon_id, "recibo_luz.pdf")

    resultado = _procesar_uno_con_tipo(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo="otros")

    assert resultado.estado == "procesado"
    assert tipos_esperados_recibidos == ["recibo_servicio"]


def test_facturas_no_pasa_tipo_esperado():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    tipos_esperados_recibidos = []

    def falso_modelo(ruta, tipo, config=None, tipo_esperado=None):
        tipos_esperados_recibidos.append(tipo_esperado)
        return ComprobanteFalso()

    _modulo_extractor_modelo.extraer = falso_modelo
    archivo = _crear_archivo(almacen, buzon_id, "factura.pdf")

    resultado = _procesar_uno_con_tipo(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo="facturas")

    assert resultado.estado == "procesado"
    assert tipos_esperados_recibidos == [None]


def test_archivo_en_raiz_se_procesa_como_factura_con_advertencia_en_log(caplog):
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    comp = ComprobanteFalso()
    _modulo_xml_ubl.extraer = lambda ruta: comp
    archivo = _crear_archivo(almacen, buzon_id, "suelto.xml")

    with caplog.at_level(logging.WARNING, logger="procesar"):
        resultado = _procesar_uno_con_tipo(
            archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo=procesar.TIPO_RAIZ_BUZON
        )

    assert resultado.estado == "procesado"
    assert len(registro.escritos) == 1  # pipeline normal de factura, sí escribe
    assert any("raíz del buzón" in r.getMessage() for r in caplog.records)


def _procesar_uno_con_tipo(archivo, respaldos, config, registro, catalogo_obj, almacen, carpeta_procesado_id, carpeta_revisar_id, tipo, dry_run=False):
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
        tipo=tipo,
    )


# -----------------------------------------------------------------------------
# extraer_fecha_nombre_archivo: fecha de un respaldo de caja chica a partir
# del patrón DD.MM en el nombre.
# -----------------------------------------------------------------------------
def test_extraer_fecha_nombre_archivo_patron_dd_mm():
    hoy = datetime.date(2026, 8, 6)
    assert procesar.extraer_fecha_nombre_archivo("01.07 BOLETAS.pdf", hoy=hoy) == "2026-07-01"


def test_extraer_fecha_nombre_archivo_sin_patron_devuelve_vacio():
    assert procesar.extraer_fecha_nombre_archivo("boletas_de_julio.pdf") == ""


def test_extraer_fecha_nombre_archivo_fecha_invalida_devuelve_vacio():
    hoy = datetime.date(2026, 8, 6)
    assert procesar.extraer_fecha_nombre_archivo("31.02 BOLETAS.pdf", hoy=hoy) == ""  # 31 de febrero no existe


# -----------------------------------------------------------------------------
# resolver_empresa_local_nota_venta
# -----------------------------------------------------------------------------
def test_resolver_empresa_local_nota_venta_ambigua_con_varias_empresas():
    config = _config_base()  # 3 empresas
    empresa, local, motivo = procesar.resolver_empresa_local_nota_venta(config)
    assert empresa == ""
    assert local == ""
    assert motivo is not None
    assert "3 empresas" in motivo


def test_resolver_empresa_local_nota_venta_unica_empresa_un_solo_local():
    config = _config_base()
    config["empresas"] = [e for e in config["empresas"] if e["nombre_corto"] == "INSTITUCION"]
    empresa, local, motivo = procesar.resolver_empresa_local_nota_venta(config)
    assert empresa == "INSTITUCION"
    assert local == "MIRAFLORES"
    assert motivo is None


# -----------------------------------------------------------------------------
# notas_venta: sin modelo, registro en RESPALDOS_CAJA, movido con nombre
# original.
# -----------------------------------------------------------------------------
def test_notas_venta_no_llama_al_modelo_y_registra_en_respaldos_caja():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()

    def modelo_no_debe_llamarse(*args, **kwargs):
        raise AssertionError("notas_venta no debe llamar al extractor de modelo")

    _modulo_extractor_modelo.extraer = modelo_no_debe_llamarse
    archivo = _crear_archivo(almacen, buzon_id, "01.07 BOLETAS.pdf")

    resultado = _procesar_uno_con_tipo(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo="notas_venta")

    assert resultado.estado == "procesado"
    assert resultado.llamadas_modelo == 0
    assert len(registro.respaldos_caja) == 1
    assert registro.escritos == []  # nunca pasa por Registro.escribir() (esa es la vía de facturas)

    fila = registro.respaldos_caja[0]
    assert fila["archivo"] == "01.07 BOLETAS.pdf"
    assert fila["fecha"] == f"{datetime.date.today().year}-07-01"
    assert fila["empresa"] == ""  # config base tiene 3 empresas: ambiguo

    # Se movió fuera del buzón, con el nombre ORIGINAL (no se renombra a
    # RUC_SERIE_TOTAL: un respaldo de caja chica no tiene esos datos).
    assert almacen.archivos[archivo.id]["parent"] not in (buzon_id, revisar_id)
    assert almacen.archivos[archivo.id]["name"] == "01.07 BOLETAS.pdf"


def test_notas_venta_asigna_empresa_unica_si_solo_hay_una():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    config["empresas"] = [e for e in config["empresas"] if e["nombre_corto"] == "INSTITUCION"]
    archivo = _crear_archivo(almacen, buzon_id, "05.07 GASTOS.pdf")

    resultado = _procesar_uno_con_tipo(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo="notas_venta")

    assert resultado.estado == "procesado"
    fila = registro.respaldos_caja[0]
    assert fila["empresa"] == "INSTITUCION"
    assert fila["local"] == "MIRAFLORES"


def test_notas_venta_fecha_vacia_si_el_nombre_no_trae_el_patron():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    archivo = _crear_archivo(almacen, buzon_id, "boletas_de_julio.pdf")

    resultado = _procesar_uno_con_tipo(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo="notas_venta")

    assert resultado.estado == "procesado"
    assert registro.respaldos_caja[0]["fecha"] == ""


def test_notas_venta_es_idempotente_por_archivo_y_empresa():
    """El doble de Registro replica la idempotencia real (ver
    registro_sheets.Registro.registrar_respaldo_caja): registrar el mismo
    archivo dos veces no duplica la fila. Se ejercita llamando
    procesar_nota_venta dos veces con el mismo archivo/registro."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat = _entorno()
    archivo = _crear_archivo(almacen, buzon_id, "01.07 BOLETAS.pdf")

    primero = procesar.procesar_nota_venta(
        archivo, config=config, registro=registro, almacen=almacen,
        nombres_por_carpeta={}, carpeta_procesado_id=procesado_id, dry_run=False,
    )
    segundo = procesar.procesar_nota_venta(
        archivo, config=config, registro=registro, almacen=almacen,
        nombres_por_carpeta={}, carpeta_procesado_id=procesado_id, dry_run=False,
    )

    assert primero.estado == "procesado"
    assert segundo.estado == "procesado"  # no falla ni queda como error
    assert len(registro.respaldos_caja) == 1  # pero no duplica el registro


# -----------------------------------------------------------------------------
# main() end-to-end con buzon_tipos configurado.
# -----------------------------------------------------------------------------
def test_main_enruta_por_buzon_tipos_end_to_end(tmp_path, monkeypatch):
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_tipos = _entorno_con_buzon_tipos()
    ruta_config = tmp_path / "config.yaml"
    ruta_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    comp_factura = ComprobanteFalso(serie_numero="F001-1", total=10.0)
    tipos_esperados_recibidos = []

    def falso_xml(ruta):
        return comp_factura

    def falso_modelo(ruta, tipo, config=None, tipo_esperado=None):
        tipos_esperados_recibidos.append((ruta.name, tipo_esperado))
        return ComprobanteFalso(serie_numero=f"L-{ruta.name}", total=5.0)

    _modulo_xml_ubl.extraer = falso_xml
    _modulo_extractor_modelo.extraer = falso_modelo
    _modulo_registro_sheets.Registro = lambda cfg: registro
    monkeypatch.setattr(procesar, "AlmacenDrive", lambda servicio: almacen)

    _crear_archivo(almacen, ids_tipos["facturas"], "fact.xml")
    _crear_archivo(almacen, ids_tipos["liquidaciones"], "compra.jpg")
    _crear_archivo(almacen, ids_tipos["notas_venta"], "01.07 BOLETAS.pdf")

    try:
        codigo = procesar.main(["--config", str(ruta_config)])
    finally:
        _modulo_registro_sheets.Registro = _RegistroFalso

    assert codigo == 0
    assert ("compra.jpg", "liquidacion") in tipos_esperados_recibidos
    assert len(registro.respaldos_caja) == 1
    assert registro.respaldos_caja[0]["archivo"] == "01.07 BOLETAS.pdf"
    # 2 comprobantes normales (factura + liquidación) escritos por Registro.escribir()
    assert len(registro.escritos) == 2


# -----------------------------------------------------------------------------
# resolver_buzon_empresas_ids
# -----------------------------------------------------------------------------
def test_resolver_buzon_empresas_ids_vacio_si_no_hay_seccion():
    assert procesar.resolver_buzon_empresas_ids({}) == {}


def test_resolver_buzon_empresas_ids_lee_por_empresa_y_tipo():
    carpetas_cfg = {
        "buzon_empresas": {
            "EL TEMPLO": {"facturas": "f1", "notas_venta": "n1", "liquidaciones": "l1", "otros": "o1"},
            "ILLAWARA": {"facturas": "f2"},  # las demás claves faltan a propósito
        }
    }
    resultado = procesar.resolver_buzon_empresas_ids(carpetas_cfg)
    assert resultado["EL TEMPLO"] == {"facturas": "f1", "notas_venta": "n1", "liquidaciones": "l1", "otros": "o1"}
    assert resultado["ILLAWARA"] == {"facturas": "f2", "notas_venta": "", "liquidaciones": "", "otros": ""}


# -----------------------------------------------------------------------------
# construir_planes_enrutados con buzon_empresas: enruta por empresa Y tipo,
# detecta carpetas planas huérfanas y sigue viendo la raíz (compatibilidad).
# -----------------------------------------------------------------------------
def test_construir_planes_enrutados_con_buzon_empresas():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_por_empresa = _entorno_con_buzon_empresas(
        ["EL TEMPLO", "ILLAWARA"]
    )
    _crear_archivo(almacen, ids_por_empresa["EL TEMPLO"]["facturas"], "templo.xml")
    _crear_archivo(almacen, ids_por_empresa["ILLAWARA"]["notas_venta"], "01.07 BOLETAS.pdf")
    _crear_archivo(almacen, buzon_id, "suelto.pdf")  # raíz, compatibilidad

    buzon_tipos_ids = procesar.resolver_buzon_tipos_ids(config["drive"]["carpetas"])
    buzon_empresas_ids = procesar.resolver_buzon_empresas_ids(config["drive"]["carpetas"])
    planes = procesar.construir_planes_enrutados(almacen, buzon_id, buzon_tipos_ids, buzon_empresas_ids)

    por_nombre = {p[0].name: (p[2], p[3]) for p in planes}
    assert por_nombre["templo.xml"] == ("facturas", "EL TEMPLO")
    assert por_nombre["01.07 BOLETAS.pdf"] == ("notas_venta", "ILLAWARA")
    assert por_nombre["suelto.pdf"] == (procesar.TIPO_RAIZ_BUZON, None)


def test_construir_planes_enrutados_detecta_carpeta_plana_huerfana(caplog):
    """Con buzon_empresas configurado, un archivo dejado en 00_BUZON/FACTURAS
    (carpeta plana vieja, huérfana desde la migración) se procesa igual -
    por RUC nada más, sin empresa de carpeta- y deja advertencia en el log."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_por_empresa = _entorno_con_buzon_empresas(
        ["EL TEMPLO"]
    )
    carpeta_huerfana_id = almacen.agregar_carpeta("FACTURAS", buzon_id)
    _crear_archivo(almacen, carpeta_huerfana_id, "huerfano.xml")

    buzon_tipos_ids = procesar.resolver_buzon_tipos_ids(config["drive"]["carpetas"])
    buzon_empresas_ids = procesar.resolver_buzon_empresas_ids(config["drive"]["carpetas"])

    with caplog.at_level(logging.WARNING, logger="procesar"):
        planes = procesar.construir_planes_enrutados(almacen, buzon_id, buzon_tipos_ids, buzon_empresas_ids)

    por_nombre = {p[0].name: (p[2], p[3]) for p in planes}
    assert por_nombre["huerfano.xml"] == ("facturas", None)
    assert any("huérfana" in r.getMessage() for r in caplog.records)


def test_construir_planes_enrutados_sin_carpetas_planas_no_crea_ni_advierte(caplog):
    """Negocio con buzon_empresas que nunca tuvo las 4 carpetas planas viejas
    (FACTURAS/NOTAS_DE_VENTA/LIQUIDACIONES/OTROS): la detección busca cada
    una con buscar_carpeta (encuentra, no crea), no las encuentra, y por lo
    tanto no crea ninguna carpeta basura ni advierte nada. Esto reemplaza al
    bug real: antes se usaba asegurar_carpeta (encuentra-o-crea) para esta
    misma detección, así que cada corrida de un negocio así dejaba 4
    carpetas vacías colgando en 00_BUZON."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_por_empresa = _entorno_con_buzon_empresas(
        ["EL TEMPLO"]
    )
    buzon_tipos_ids = procesar.resolver_buzon_tipos_ids(config["drive"]["carpetas"])
    buzon_empresas_ids = procesar.resolver_buzon_empresas_ids(config["drive"]["carpetas"])
    cantidad_carpetas_antes = len(almacen.carpetas)

    with caplog.at_level(logging.WARNING, logger="procesar"):
        procesar.construir_planes_enrutados(almacen, buzon_id, buzon_tipos_ids, buzon_empresas_ids)

    assert almacen.carpetas_aseguradas == []  # nunca se llamó al encuentra-o-crea
    assert len(almacen.carpetas) == cantidad_carpetas_antes  # ni una carpeta nueva
    assert set(almacen.carpetas_buscadas) == {
        ("FACTURAS", buzon_id), ("NOTAS_DE_VENTA", buzon_id), ("LIQUIDACIONES", buzon_id), ("OTROS", buzon_id),
    }
    assert caplog.records == []  # nada que advertir: las carpetas no existen


# -----------------------------------------------------------------------------
# Precedencia empresa: gana el RUC del papel; la carpeta solo rellena cuando
# el documento no puede decirlo (decisión del dueño, ver
# resolver_empresa_con_carpeta). Caso real: ULTRAFRIO/APUDEX facturadas a
# ILLAWARA (RUC 20614321734) pero subidas por el personal de EL TEMPLO.
# -----------------------------------------------------------------------------
def test_ruc_gana_sobre_carpeta_y_advierte_con_los_dos_nombres(caplog):
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_por_empresa = _entorno_con_buzon_empresas(
        ["EL TEMPLO", "ILLAWARA"]
    )
    comp = ComprobanteFalso(cliente_ruc="20614321734")  # RUC de ILLAWARA
    _modulo_xml_ubl.extraer = lambda ruta: comp
    archivo = _crear_archivo(almacen, ids_por_empresa["EL TEMPLO"]["facturas"], "ultrafrio.xml")

    with caplog.at_level(logging.WARNING, logger="procesar"):
        resultado = procesar.procesar_uno(
            archivo, [], config=config, registro=registro, catalogo_obj=cat, almacen=almacen,
            claves_procesadas_en_lote=set(), nombres_por_carpeta={}, carpeta_procesado_id=procesado_id,
            carpeta_revisar_id=revisar_id, dry_run=False, tipo="facturas", empresa_carpeta="EL TEMPLO",
        )

    assert resultado.estado == "procesado"
    assert registro.escritos[0]["empresa"] == "ILLAWARA"  # gana el RUC del papel
    mensajes = [r.getMessage() for r in caplog.records]
    assert any("ILLAWARA" in m and "EL TEMPLO" in m for m in mensajes)


def test_sin_ruc_usa_la_empresa_de_la_carpeta():
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_por_empresa = _entorno_con_buzon_empresas(
        ["EL TEMPLO", "INSTITUCION"]
    )
    comp = ComprobanteFalso(cliente_ruc=None)  # el documento no dice nada
    _modulo_xml_ubl.extraer = lambda ruta: comp
    archivo = _crear_archivo(almacen, ids_por_empresa["INSTITUCION"]["facturas"], "sinruc.xml")

    resultado = procesar.procesar_uno(
        archivo, [], config=config, registro=registro, catalogo_obj=cat, almacen=almacen,
        claves_procesadas_en_lote=set(), nombres_por_carpeta={}, carpeta_procesado_id=procesado_id,
        carpeta_revisar_id=revisar_id, dry_run=False, tipo="facturas", empresa_carpeta="INSTITUCION",
    )

    assert resultado.estado == "procesado"
    assert registro.escritos[0]["empresa"] == "INSTITUCION"
    assert registro.escritos[0]["local"] == "MIRAFLORES"


def test_nota_venta_en_carpeta_de_empresa_sale_con_empresa_y_local():
    """Con buzon_empresas, el agujero de antes (EMPRESA/LOCAL vacíos con 3
    empresas configuradas) queda cerrado: la carpeta resuelve la empresa."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_por_empresa = _entorno_con_buzon_empresas(
        ["EL TEMPLO", "INSTITUCION", "ILLAWARA"]
    )
    archivo = _crear_archivo(almacen, ids_por_empresa["INSTITUCION"]["notas_venta"], "01.07 BOLETAS.pdf")

    resultado = procesar.procesar_uno(
        archivo, [], config=config, registro=registro, catalogo_obj=cat, almacen=almacen,
        claves_procesadas_en_lote=set(), nombres_por_carpeta={}, carpeta_procesado_id=procesado_id,
        carpeta_revisar_id=revisar_id, dry_run=False, tipo="notas_venta", empresa_carpeta="INSTITUCION",
    )

    assert resultado.estado == "procesado"
    fila = registro.respaldos_caja[0]
    assert fila["empresa"] == "INSTITUCION"
    assert fila["local"] == "MIRAFLORES"


def test_nota_venta_sin_buzon_empresas_sigue_vacia_sin_regresion():
    """Con buzon_tipos (config viejo, varias empresas) la nota de venta
    sigue saliendo vacía y con advertencia: sin regresión (empresa_carpeta
    es siempre None ahí)."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_tipos = _entorno_con_buzon_tipos()
    archivo = _crear_archivo(almacen, ids_tipos["notas_venta"], "01.07 BOLETAS.pdf")

    resultado = _procesar_uno_con_tipo(archivo, [], config, registro, cat, almacen, procesado_id, revisar_id, tipo="notas_venta")

    assert resultado.estado == "procesado"
    fila = registro.respaldos_caja[0]
    assert fila["empresa"] == ""
    assert fila["local"] == ""


def test_archivo_en_carpeta_plana_huerfana_se_procesa_y_advierte(caplog):
    """Un archivo en una carpeta plana huérfana (00_BUZON/OTROS, con
    buzon_empresas configurado) se procesa igual -por RUC- y deja
    advertencia en el log de que debe moverse a la carpeta de su empresa."""
    config, almacen, buzon_id, procesado_id, revisar_id, registro, cat, ids_por_empresa = _entorno_con_buzon_empresas(
        ["EL TEMPLO"]
    )
    carpeta_huerfana_id = almacen.agregar_carpeta("OTROS", buzon_id)
    _crear_archivo(almacen, carpeta_huerfana_id, "recibo.pdf")

    def falso_modelo(ruta, tipo, config=None, tipo_esperado=None):
        return ComprobanteFalso()

    _modulo_extractor_modelo.extraer = falso_modelo

    buzon_tipos_ids = procesar.resolver_buzon_tipos_ids(config["drive"]["carpetas"])
    buzon_empresas_ids = procesar.resolver_buzon_empresas_ids(config["drive"]["carpetas"])

    with caplog.at_level(logging.WARNING, logger="procesar"):
        planes = procesar.construir_planes_enrutados(almacen, buzon_id, buzon_tipos_ids, buzon_empresas_ids)

    plan_huerfano = next(p for p in planes if p[0].name == "recibo.pdf")
    principal, respaldos, tipo, empresa_carpeta = plan_huerfano
    assert tipo == "otros"
    assert empresa_carpeta is None

    resultado = procesar.procesar_uno(
        principal, respaldos, config=config, registro=registro, catalogo_obj=cat, almacen=almacen,
        claves_procesadas_en_lote=set(), nombres_por_carpeta={}, carpeta_procesado_id=procesado_id,
        carpeta_revisar_id=revisar_id, dry_run=False, tipo=tipo, empresa_carpeta=empresa_carpeta,
    )

    assert resultado.estado == "procesado"
    assert registro.escritos[0]["empresa"] == "INSTITUCION"  # cliente_ruc por defecto del doble
    assert any("huérfana" in r.getMessage() for r in caplog.records)
