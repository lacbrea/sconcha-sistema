"""Tests de almacen_drive.AlmacenDrive contra un doble en memoria del
Resource de drive v3 (FakeServicioDrive). No usan red ni credenciales.

Correr con:
    C:\\Python312\\python.exe -m pytest tests/test_almacen_drive.py -q
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from almacen_drive import AlmacenDrive, MIME_CARPETA  # noqa: E402


# -----------------------------------------------------------------------------
# Doble de prueba del Resource de drive v3 (googleapiclient), sin red.
#
# Reconoce en la query ('q') las cláusulas que de verdad construye
# AlmacenDrive: "'{id}' in parents", "name = '...'" y
# "mimeType = '<carpeta>'", si aparecen. A propósito NO interpreta
# "trashed = false" (aunque esté en el texto de la query): así la prueba de
# listar() excluyendo papelera ejercita el filtro de Python real de
# AlmacenDrive, no un filtrado que ya viniera hecho por el doble.
#
# El patrón de "name = '...'" acepta comillas simples escapadas con
# backslash dentro del valor (\\.), para poder deshacer el escape de
# _escapar() de la misma forma en que lo haría el servidor real de Drive y
# así probar buscar_por_nombre() con nombres que traen comilla simple.
# -----------------------------------------------------------------------------
class FakeServicioDrive:
    def __init__(self):
        self._archivos: dict[str, dict] = {}
        self._contador = 0
        self._pendiente = None

    # -- helpers de test para poblar estado ------------------------------
    def agregar(
        self,
        name: str,
        parents: list[str],
        mimeType: str = "application/pdf",
        trashed: bool = False,
        content: bytes = b"",
        web_view_link: str | None = None,
    ) -> str:
        self._contador += 1
        file_id = f"id-{self._contador}"
        self._archivos[file_id] = {
            "id": file_id,
            "name": name,
            "parents": list(parents),
            "mimeType": mimeType,
            "trashed": trashed,
            "content": content,
            "webViewLink": web_view_link or f"https://drive.google.com/file/d/{file_id}/view",
        }
        return file_id

    # -- interfaz que imita googleapiclient -------------------------------
    def files(self):
        return self

    def list(self, q, fields=None, pageSize=None, pageToken=None):  # noqa: N803
        self._pendiente = ("list", q)
        return self

    def get(self, fileId, fields=None):  # noqa: N803
        self._pendiente = ("get", fileId)
        return self

    def get_media(self, fileId):  # noqa: N803
        self._pendiente = ("get_media", fileId)
        return self

    def create(self, body, fields=None, media_body=None):
        self._pendiente = ("create", body, media_body)
        return self

    def update(self, fileId, addParents=None, removeParents=None, body=None, fields=None):  # noqa: N803
        self._pendiente = ("update", fileId, addParents, removeParents, body)
        return self

    def execute(self):
        accion = self._pendiente[0]

        if accion == "list":
            _, q = self._pendiente
            return {"files": self._filtrar(q)}

        if accion == "get":
            _, file_id = self._pendiente
            archivo = self._archivos[file_id]
            return {"id": file_id, "parents": archivo["parents"], "webViewLink": archivo["webViewLink"]}

        if accion == "get_media":
            _, file_id = self._pendiente
            return self._archivos[file_id]["content"]

        if accion == "create":
            _, body, media_body = self._pendiente
            self._contador += 1
            file_id = f"id-{self._contador}"
            contenido = b""
            mimetype = body.get("mimeType", "text/plain")
            if media_body is not None:
                contenido = media_body.getbytes(0, media_body.size())
                # Igual que en Drive real: si el metadata (body) no trae
                # mimeType propio (caso de subir()/crear_texto(), que solo
                # ponen name+parents en el body), el tipo lo define el
                # media_body (MediaIoBaseUpload), no un default fijo.
                mimetype = body.get("mimeType") or media_body.mimetype()
            self._archivos[file_id] = {
                "id": file_id,
                "name": body.get("name", ""),
                "parents": list(body.get("parents") or []),
                "mimeType": mimetype,
                "trashed": False,
                "content": contenido,
                "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
            }
            return {"id": file_id}

        if accion == "update":
            _, file_id, add_parents, remove_parents, body = self._pendiente
            archivo = self._archivos[file_id]
            if remove_parents:
                quitar = set(remove_parents.split(","))
                archivo["parents"] = [p for p in archivo["parents"] if p not in quitar]
            if add_parents:
                for p in add_parents.split(","):
                    if p and p not in archivo["parents"]:
                        archivo["parents"].append(p)
            if body and body.get("name"):
                archivo["name"] = body["name"]
            return {"id": file_id, "parents": archivo["parents"], "name": archivo["name"]}

        raise AssertionError(f"acción no esperada: {accion}")

    def _filtrar(self, q: str) -> list[dict]:
        m_nombre = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        m_padre = re.search(r"'([^']*)' in parents", q)
        pide_carpeta = f"mimeType = '{MIME_CARPETA}'" in q

        resultado = []
        for archivo in self._archivos.values():
            if m_nombre and archivo["name"] != m_nombre.group(1).replace("\\'", "'"):
                continue
            if m_padre and m_padre.group(1) not in archivo["parents"]:
                continue
            if pide_carpeta and archivo["mimeType"] != MIME_CARPETA:
                continue
            resultado.append(
                {
                    "id": archivo["id"],
                    "name": archivo["name"],
                    "mimeType": archivo["mimeType"],
                    "size": str(len(archivo["content"])),
                    "trashed": archivo["trashed"],
                }
            )
        return resultado


# -----------------------------------------------------------------------------
# listar()
# -----------------------------------------------------------------------------
def test_listar_excluye_carpetas_y_papelera():
    servicio = FakeServicioDrive()
    servicio.agregar("a.xml", parents=["carpeta-1"], mimeType="text/xml")
    servicio.agregar("b.pdf", parents=["carpeta-1"], mimeType="application/pdf")
    servicio.agregar("SUBCARPETA", parents=["carpeta-1"], mimeType=MIME_CARPETA)
    servicio.agregar("borrado.pdf", parents=["carpeta-1"], mimeType="application/pdf", trashed=True)
    servicio.agregar("otro.pdf", parents=["carpeta-2"], mimeType="application/pdf")  # otra carpeta

    almacen = AlmacenDrive(servicio)
    resultado = almacen.listar("carpeta-1")

    nombres = {a["name"] for a in resultado}
    assert nombres == {"a.xml", "b.pdf"}


def test_listar_carpeta_vacia():
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)
    assert almacen.listar("carpeta-vacia") == []


# -----------------------------------------------------------------------------
# asegurar_carpeta()
# -----------------------------------------------------------------------------
def test_asegurar_carpeta_es_idempotente():
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    id_1 = almacen.asegurar_carpeta("00_BUZON", "raiz-id")
    cantidad_tras_primera = len(servicio._archivos)

    id_2 = almacen.asegurar_carpeta("00_BUZON", "raiz-id")
    cantidad_tras_segunda = len(servicio._archivos)

    assert id_1 == id_2
    assert cantidad_tras_primera == 1
    assert cantidad_tras_segunda == 1  # no crea una segunda vez


def test_asegurar_carpeta_usa_root_si_no_hay_padre():
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    id_raiz = almacen.asegurar_carpeta("SCONCHA")

    assert servicio._archivos[id_raiz]["parents"] == ["root"]


def test_asegurar_carpeta_distingue_por_padre():
    """Dos carpetas con el mismo nombre pero distinto padre no colisionan."""
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    id_a = almacen.asegurar_carpeta("2026-07", "empresa-a")
    id_b = almacen.asegurar_carpeta("2026-07", "empresa-b")

    assert id_a != id_b


# -----------------------------------------------------------------------------
# mover()
# -----------------------------------------------------------------------------
def test_mover_usa_add_remove_parents_y_renombra():
    servicio = FakeServicioDrive()
    file_id = servicio.agregar("original.xml", parents=["buzon-id"])
    almacen = AlmacenDrive(servicio)

    almacen.mover(file_id, "destino-id", "20100000001_F001-1_118.00.xml")

    archivo = servicio._archivos[file_id]
    assert archivo["parents"] == ["destino-id"]
    assert archivo["name"] == "20100000001_F001-1_118.00.xml"


def test_mover_sin_nuevo_nombre_no_renombra():
    servicio = FakeServicioDrive()
    file_id = servicio.agregar("foto.heic", parents=["buzon-id"])
    almacen = AlmacenDrive(servicio)

    almacen.mover(file_id, "revisar-id")

    archivo = servicio._archivos[file_id]
    assert archivo["parents"] == ["revisar-id"]
    assert archivo["name"] == "foto.heic"


# -----------------------------------------------------------------------------
# crear_texto()
# -----------------------------------------------------------------------------
def test_crear_texto_devuelve_id_y_contenido_correcto():
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    file_id = almacen.crear_texto("revisar-id", "foto.heic.motivo.txt", "formato no soportado")

    archivo = servicio._archivos[file_id]
    assert archivo["name"] == "foto.heic.motivo.txt"
    assert archivo["parents"] == ["revisar-id"]
    assert archivo["content"] == b"formato no soportado"


# -----------------------------------------------------------------------------
# enlace()
# -----------------------------------------------------------------------------
def test_enlace_devuelve_web_view_link():
    servicio = FakeServicioDrive()
    file_id = servicio.agregar("a.xml", parents=["carpeta-1"], web_view_link="https://drive.example/a")
    almacen = AlmacenDrive(servicio)

    assert almacen.enlace(file_id) == "https://drive.example/a"


# -----------------------------------------------------------------------------
# descargar()
# -----------------------------------------------------------------------------
def test_descargar_escribe_bytes_en_destino(tmp_path):
    servicio = FakeServicioDrive()
    file_id = servicio.agregar("a.xml", parents=["carpeta-1"], content=b"<xml>contenido</xml>")
    almacen = AlmacenDrive(servicio)

    destino = tmp_path / "subcarpeta" / "a.xml"
    resultado = almacen.descargar(file_id, destino)

    assert resultado == destino
    assert destino.read_bytes() == b"<xml>contenido</xml>"


# -----------------------------------------------------------------------------
# subir()
# -----------------------------------------------------------------------------
def test_subir_desde_path_y_desde_bytes_suben_el_mismo_contenido(tmp_path):
    """subir() acepta pathlib.Path, str y bytes en memoria; en los tres casos
    el contenido que termina en Drive es el mismo."""
    contenido = b"contenido del xlsx"
    archivo_local = tmp_path / "conciliacion.xlsx"
    archivo_local.write_bytes(contenido)

    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    id_desde_path = almacen.subir("carpeta-1", "a.xlsx", archivo_local)
    id_desde_str = almacen.subir("carpeta-1", "b.xlsx", str(archivo_local))
    id_desde_bytes = almacen.subir("carpeta-1", "c.xlsx", contenido)

    assert servicio._archivos[id_desde_path]["content"] == contenido
    assert servicio._archivos[id_desde_str]["content"] == contenido
    assert servicio._archivos[id_desde_bytes]["content"] == contenido


def test_subir_llama_a_create_y_nunca_a_update_ni_delete(tmp_path):
    """Garantía de diseño: subir() siempre CREA, nunca sobrescribe. Se
    verifica sobre el doble que update() no se invocó (delete ni siquiera
    existe como método en el doble ni en AlmacenDrive)."""
    servicio = FakeServicioDrive()
    llamadas_update = []
    servicio.update = lambda *a, **k: llamadas_update.append((a, k)) or servicio  # type: ignore[method-assign]
    assert not hasattr(servicio, "delete")

    almacen = AlmacenDrive(servicio)
    file_id = almacen.subir("carpeta-1", "nuevo.xlsx", b"datos")

    assert file_id in servicio._archivos
    assert llamadas_update == []


def test_subir_pone_carpeta_nombre_y_mimetype_correctos():
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    file_id = almacen.subir(
        "carpeta-destino", "reporte.xlsx", b"datos",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    archivo = servicio._archivos[file_id]
    assert archivo["parents"] == ["carpeta-destino"]
    assert archivo["name"] == "reporte.xlsx"
    assert archivo["mimeType"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_subir_usa_mimetype_por_defecto_si_no_se_indica():
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    file_id = almacen.subir("carpeta-1", "archivo.bin", b"datos")

    assert servicio._archivos[file_id]["mimeType"] == "application/octet-stream"


# -----------------------------------------------------------------------------
# buscar_por_nombre()
# -----------------------------------------------------------------------------
def test_buscar_por_nombre_encuentra_el_archivo():
    servicio = FakeServicioDrive()
    file_id = servicio.agregar("EC_1234_062026.pdf", parents=["carpeta-1"])
    almacen = AlmacenDrive(servicio)

    resultado = almacen.buscar_por_nombre("carpeta-1", "EC_1234_062026.pdf")

    assert resultado is not None
    assert resultado["id"] == file_id
    assert resultado["name"] == "EC_1234_062026.pdf"


def test_buscar_por_nombre_devuelve_none_si_no_existe():
    servicio = FakeServicioDrive()
    servicio.agregar("otro.pdf", parents=["carpeta-1"])
    almacen = AlmacenDrive(servicio)

    assert almacen.buscar_por_nombre("carpeta-1", "no-existe.pdf") is None


def test_buscar_por_nombre_ignora_carpetas():
    """Una carpeta con el mismo nombre no cuenta como resultado: sirve para
    decidir si ya existe un ARCHIVO con ese nombre antes de subir()."""
    servicio = FakeServicioDrive()
    servicio.agregar("CONCILIACION", parents=["carpeta-1"], mimeType=MIME_CARPETA)
    almacen = AlmacenDrive(servicio)

    assert almacen.buscar_por_nombre("carpeta-1", "CONCILIACION") is None


def test_buscar_por_nombre_escapa_comilla_simple_en_la_query():
    """El módulo tiene _escapar() justamente para que un nombre con comilla
    simple no rompa la query de Drive (name = '...')."""
    servicio = FakeServicioDrive()
    nombre_con_comilla = "EC O'Higgins_062026.pdf"
    file_id = servicio.agregar(nombre_con_comilla, parents=["carpeta-1"])
    almacen = AlmacenDrive(servicio)

    resultado = almacen.buscar_por_nombre("carpeta-1", nombre_con_comilla)

    assert resultado is not None
    assert resultado["id"] == file_id
