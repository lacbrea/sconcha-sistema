"""Capa de almacenamiento: todo el trato con la API de Google Drive v3 vive
aquí, encapsulado en AlmacenDrive.

procesar.py e init_negocio.py NUNCA llaman a googleapiclient directamente:
reciben una instancia de esta clase inyectada (construida a partir de
auth_google.servicio_drive()), para que sus tests puedan sustituirla por un
doble en memoria sin tocar red ni credenciales. Ver tests/test_almacen_drive.py
para las pruebas de esta clase contra un doble del Resource de drive v3.
"""
from __future__ import annotations

import io
import pathlib

from googleapiclient.http import MediaIoBaseUpload

MIME_CARPETA = "application/vnd.google-apps.folder"


def _escapar(valor: str) -> str:
    """Escapa comillas simples para incrustar 'valor' en una query de Drive."""
    return valor.replace("'", "\\'")


class AlmacenDrive:
    """Encapsula las operaciones de Drive que necesita el skill: listar el
    buzón, descargar un archivo a disco para que lo lea un extractor, mover
    (y renombrar) un archivo entre carpetas, crear un .motivo.txt, obtener
    el link público de un archivo y asegurar que una carpeta exista.

    No borra archivos nunca (no hay método para eso en esta clase, a
    propósito: la regla "ningún archivo se borra jamás" queda imposible de
    violar por construcción).
    """

    def __init__(self, servicio_drive):
        self._servicio = servicio_drive

    # -------------------------------------------------------------------
    def listar(self, carpeta_id: str) -> list[dict]:
        """Archivos directos de la carpeta (no recursivo). Cada dict trae
        id, name, mimeType, size.

        Excluye carpetas y archivos en papelera SIEMPRE del lado de
        Python, aunque la query ya se lo pida al servidor: es defensa en
        profundidad (protege incluso si algún día cambia el criterio del
        servidor, o si llega un resultado inconsistente por timing).
        """
        archivos: list[dict] = []
        token = None
        while True:
            query = f"'{carpeta_id}' in parents and trashed = false"
            resp = (
                self._servicio.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType, size, trashed)",
                    pageSize=1000,
                    pageToken=token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                if f.get("mimeType") == MIME_CARPETA:
                    continue
                if f.get("trashed"):
                    continue
                archivos.append(
                    {
                        "id": f["id"],
                        "name": f["name"],
                        "mimeType": f.get("mimeType", ""),
                        "size": f.get("size"),
                    }
                )
            token = resp.get("nextPageToken")
            if not token:
                break
        return archivos

    # -------------------------------------------------------------------
    def descargar(self, file_id: str, destino: pathlib.Path) -> pathlib.Path:
        """Descarga el contenido completo del archivo a 'destino'.

        Usa execute() directo sobre get_media() en vez de
        MediaIoBaseDownload (la descarga por chunks con progreso y
        reintentos de googleapiclient): los comprobantes son archivos
        chicos (una factura de una página, un XML, una foto suelta), así
        que la complejidad de chunking no aporta nada aquí y esto es mucho
        más simple de leer y de testear sin red.
        """
        destino = pathlib.Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        contenido = self._servicio.files().get_media(fileId=file_id).execute()
        if isinstance(contenido, str):
            contenido = contenido.encode("utf-8")
        destino.write_bytes(contenido)
        return destino

    # -------------------------------------------------------------------
    def mover(self, file_id: str, carpeta_destino_id: str, nuevo_nombre: str | None = None) -> None:
        """Cambia el padre del archivo (addParents/removeParents) y lo
        renombra si se pide, en una sola llamada a update()."""
        actual = self._servicio.files().get(fileId=file_id, fields="parents").execute()
        padres_actuales = ",".join(actual.get("parents", []))
        body = {"name": nuevo_nombre} if nuevo_nombre else {}
        self._servicio.files().update(
            fileId=file_id,
            addParents=carpeta_destino_id,
            removeParents=padres_actuales,
            body=body,
            fields="id, parents, name",
        ).execute()

    # -------------------------------------------------------------------
    def crear_texto(self, carpeta_id: str, nombre: str, contenido: str) -> str:
        """Crea un archivo de texto plano (para los .motivo.txt). Devuelve
        su id."""
        media = MediaIoBaseUpload(io.BytesIO(contenido.encode("utf-8")), mimetype="text/plain")
        metadata = {"name": nombre, "parents": [carpeta_id]}
        resultado = self._servicio.files().create(body=metadata, media_body=media, fields="id").execute()
        return resultado["id"]

    # -------------------------------------------------------------------
    def buscar_por_nombre(self, carpeta_id: str, nombre: str) -> dict | None:
        """Devuelve el archivo llamado 'nombre' dentro de 'carpeta_id', o None
        si no está. Ignora carpetas.

        Existe para que quien vaya a subir algo decida ANTES qué hacer si ya
        hay un archivo con ese nombre: esta clase nunca sobrescribe ni borra,
        y Drive permite nombres repetidos, así que subir dos veces el mismo
        nombre deja dos archivos distintos conviviendo en la carpeta.
        """
        query = (
            f"name = '{_escapar(nombre)}' and '{carpeta_id}' in parents and "
            f"trashed = false"
        )
        resp = (
            self._servicio.files()
            .list(q=query, fields="files(id, name, mimeType, size)", pageSize=5)
            .execute()
        )
        encontrados = [
            f for f in resp.get("files", []) if f.get("mimeType") != MIME_CARPETA
        ]
        return encontrados[0] if encontrados else None

    # -------------------------------------------------------------------
    def subir(
        self,
        carpeta_id: str,
        nombre: str,
        origen: pathlib.Path | str | bytes,
        mimetype: str = "application/octet-stream",
    ) -> str:
        """Sube un archivo binario a la carpeta y devuelve su id.

        'origen' puede ser una ruta local (el .xlsx que acaba de generar el
        motor de conciliación) o los bytes ya en memoria (un adjunto de correo,
        que así nunca toca el disco).

        Siempre CREA, igual que el resto de la clase: no sobrescribe, no
        versiona y no borra. Si el nombre ya existe en la carpeta, es quien
        llama el que tiene que resolverlo antes con buscar_por_nombre().
        """
        if isinstance(origen, (bytes, bytearray)):
            datos = bytes(origen)
        else:
            datos = pathlib.Path(origen).read_bytes()
        media = MediaIoBaseUpload(io.BytesIO(datos), mimetype=mimetype, resumable=False)
        metadata = {"name": nombre, "parents": [carpeta_id]}
        resultado = (
            self._servicio.files()
            .create(body=metadata, media_body=media, fields="id")
            .execute()
        )
        return resultado["id"]

    # -------------------------------------------------------------------
    def enlace(self, file_id: str) -> str:
        """webViewLink del archivo, tal como lo reporta Drive (link real,
        no adivinado)."""
        resultado = self._servicio.files().get(fileId=file_id, fields="webViewLink").execute()
        return resultado.get("webViewLink", "")

    # -------------------------------------------------------------------
    def buscar_carpeta(self, nombre: str, padre_id: str | None = None) -> str | None:
        """Devuelve el id de la carpeta 'nombre' dentro de 'padre_id' (o de
        'root' -> "Mi unidad" si padre_id es None), o None si no existe.

        A diferencia de buscar_por_nombre() (que ignora carpetas a
        propósito, porque existe para archivos), esta busca únicamente
        carpetas y nunca crea nada: existe para quien necesita saber si una
        carpeta ya está ahí sin arriesgarse a crearla de paso, como la
        detección de carpetas planas huérfanas en 00_BUZON (ver
        procesar.py -> construir_planes_enrutados), que antes de esto no
        tenía forma de mirar sin también crear.
        """
        padre_efectivo = padre_id or "root"
        query = (
            f"name = '{_escapar(nombre)}' and mimeType = '{MIME_CARPETA}' and "
            f"'{padre_efectivo}' in parents and trashed = false"
        )
        resp = self._servicio.files().list(q=query, fields="files(id, name)", pageSize=5).execute()
        encontrados = resp.get("files", [])
        return encontrados[0]["id"] if encontrados else None

    # -------------------------------------------------------------------
    def asegurar_carpeta(self, nombre: str, padre_id: str | None = None) -> str:
        """Devuelve el id de la carpeta 'nombre' dentro de 'padre_id' (o de
        'root' -> "Mi unidad" si padre_id es None), creándola si no existe.

        Idempotente: busca por nombre+padre antes de crear (con
        buscar_carpeta), así que correr esto dos veces siempre devuelve el
        mismo id sin duplicar la carpeta.
        """
        padre_efectivo = padre_id or "root"
        encontrada = self.buscar_carpeta(nombre, padre_efectivo)
        if encontrada is not None:
            return encontrada

        metadata = {"name": nombre, "mimeType": MIME_CARPETA, "parents": [padre_efectivo]}
        resultado = self._servicio.files().create(body=metadata, fields="id").execute()
        return resultado["id"]
