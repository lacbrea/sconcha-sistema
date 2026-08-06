"""Tests de correo_gmail.descargar() contra dobles en memoria del Resource
de gmail v1 (FakeServicioGmail) y de AlmacenDrive (FakeAlmacenDrive). Sin
red, sin credenciales -- mismo criterio que tests/test_almacen_drive.py.

Correr con:
    python -m pytest tests/test_correo_gmail.py -q
"""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import correo_gmail  # noqa: E402

CARPETAS = {"EECC": "eecc-id", "CONSTANCIAS": "constancias-id", "BUZON": "buzon-id"}


# -----------------------------------------------------------------------------
# Doble en memoria del Resource de gmail v1 (googleapiclient), sin red.
#
# Reproduce la cadena real: servicio.users().messages().list(...).execute(),
# servicio.users().messages().get(...).execute() y
# servicio.users().messages().attachments().get(...).execute() (esta última
# la usa el tipo de regla 'adjunto' para bajar adjuntos grandes, que Gmail no
# manda inline en el mensaje completo).
# -----------------------------------------------------------------------------
class _FakeAdjuntosResource:
    """Doble de servicio.users().messages().attachments(): un recurso
    aparte porque su get() tiene una firma distinta (messageId + id) a la
    de messages().get() (id + format), aunque ambos viven bajo el mismo
    'servicio' en la API real."""

    def __init__(self, servicio: "FakeServicioGmail"):
        self._servicio = servicio
        self._pendiente_id: str | None = None

    def get(self, userId=None, messageId=None, id=None):  # noqa: N803,A002
        self._servicio.llamadas_attachments.append((messageId, id))
        self._pendiente_id = id
        return self

    def execute(self):
        if self._pendiente_id in self._servicio._fallos_adjunto:
            raise self._servicio._fallos_adjunto[self._pendiente_id]
        contenido = self._servicio._adjuntos.get(self._pendiente_id)
        if contenido is None:
            raise AssertionError(f"adjunto '{self._pendiente_id}' no fue registrado con agregar_adjunto()")
        return {"attachmentId": self._pendiente_id, "size": len(contenido), "data": _b64url_bytes(contenido)}


class FakeServicioGmail:
    def __init__(self):
        self._mensajes: dict[str, dict] = {}
        self._orden_ids: list[str] = []
        self._fallos: dict[str, Exception] = {}
        self._adjuntos: dict[str, bytes] = {}
        self._fallos_adjunto: dict[str, Exception] = {}
        self.llamadas_list: list[dict] = []
        self.llamadas_get: list[str] = []
        self.llamadas_attachments: list[tuple] = []
        self._pendiente = None

    # -- helpers de test para poblar estado ------------------------------
    def agregar(self, msg_id: str, payload: dict) -> None:
        self._mensajes[msg_id] = payload
        self._orden_ids.append(msg_id)

    def fallar_en_get(self, msg_id: str, excepcion: Exception) -> None:
        """El mensaje existe (aparece en list()) pero get() revienta, para
        simular un error de la API de Gmail."""
        self._orden_ids.append(msg_id)
        self._fallos[msg_id] = excepcion

    def agregar_adjunto(self, attachment_id: str, contenido: bytes) -> None:
        """Registra el contenido que devuelve attachments().get() para ese
        attachment_id -- imita a Gmail entregando el adjunto aparte del
        mensaje completo."""
        self._adjuntos[attachment_id] = contenido

    def fallar_en_attachment(self, attachment_id: str, excepcion: Exception) -> None:
        self._fallos_adjunto[attachment_id] = excepcion

    # -- interfaz que imita googleapiclient -------------------------------
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId=None, q=None, maxResults=None, pageToken=None):  # noqa: N803
        self.llamadas_list.append({"q": q, "maxResults": maxResults, "pageToken": pageToken})
        self._pendiente = ("list", pageToken, maxResults)
        return self

    def get(self, userId=None, id=None, format=None):  # noqa: N803,A002
        self.llamadas_get.append(id)
        self._pendiente = ("get", id)
        return self

    def attachments(self):
        return _FakeAdjuntosResource(self)

    def execute(self):
        accion = self._pendiente[0]

        if accion == "list":
            _, page_token, max_results = self._pendiente
            inicio = int(page_token) if page_token else 0
            fin = inicio + (max_results if max_results is not None else len(self._orden_ids))
            pagina = self._orden_ids[inicio:fin]
            resp = {"messages": [{"id": i} for i in pagina]}
            if fin < len(self._orden_ids):
                resp["nextPageToken"] = str(fin)
            return resp

        if accion == "get":
            _, msg_id = self._pendiente
            if msg_id in self._fallos:
                raise self._fallos[msg_id]
            return {"id": msg_id, "payload": self._mensajes[msg_id]}

        raise AssertionError(f"acción no esperada: {accion}")


# -----------------------------------------------------------------------------
# Doble en memoria de AlmacenDrive (no del Resource de Drive crudo: este
# módulo nunca llama a googleapiclient de Drive directamente, solo a la
# interfaz pública de AlmacenDrive).
# -----------------------------------------------------------------------------
class FakeAlmacenDrive:
    def __init__(self):
        self._archivos: dict[str, dict] = {}
        self._contador = 0
        self.llamadas_subir: list[dict] = []
        self.llamadas_descargar: list[tuple] = []

    def agregar_archivo(self, carpeta_id: str, nombre: str, contenido: bytes) -> str:
        self._contador += 1
        file_id = f"file-{self._contador}"
        self._archivos[file_id] = {"id": file_id, "name": nombre, "parents": [carpeta_id], "content": contenido}
        return file_id

    def listar(self, carpeta_id: str) -> list[dict]:
        return [
            {"id": a["id"], "name": a["name"], "mimeType": "application/json", "size": str(len(a["content"]))}
            for a in self._archivos.values()
            if carpeta_id in a["parents"]
        ]

    def descargar(self, file_id: str, destino):
        destino = pathlib.Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(self._archivos[file_id]["content"])
        self.llamadas_descargar.append((file_id, destino))
        return destino

    def buscar_por_nombre(self, carpeta_id: str, nombre: str) -> dict | None:
        """Mismo contrato que AlmacenDrive.buscar_por_nombre: el archivo de esa
        carpeta con ese nombre exacto, o None si no está.

        El doble no lo tenía y la regla `adjunto` sí lo usa (comprueba si el
        adjunto ya está en Drive antes de subirlo, que es lo que hace
        idempotente la bajada de correo). Sin este método, 11 tests fallaban
        con un AttributeError que parecía un bug del módulo cuando en realidad
        era el doble el que se había quedado atrás.
        """
        for archivo in self._archivos.values():
            if archivo["name"] == nombre and carpeta_id in archivo["parents"]:
                return {
                    "id": archivo["id"],
                    "name": archivo["name"],
                    "mimeType": "application/octet-stream",
                    "size": str(len(archivo["content"])),
                }
        return None

    def subir(self, carpeta_id: str, nombre: str, origen, mimetype: str = "application/octet-stream") -> str:
        contenido = bytes(origen) if isinstance(origen, (bytes, bytearray)) else pathlib.Path(origen).read_bytes()
        self._contador += 1
        file_id = f"file-{self._contador}"
        self._archivos[file_id] = {"id": file_id, "name": nombre, "parents": [carpeta_id], "content": contenido}
        self.llamadas_subir.append(
            {"carpeta_id": carpeta_id, "nombre": nombre, "contenido": contenido, "mimetype": mimetype}
        )
        return file_id


# -----------------------------------------------------------------------------
# Helpers para construir HTML de constancia y payloads de Gmail
# -----------------------------------------------------------------------------
def _b64url(texto: str) -> str:
    """Codifica como Gmail: base64 URL-safe SIN relleno. Ejercita a propósito
    el arreglo de padding de correo_gmail._decodificar_base64url."""
    return base64.urlsafe_b64encode(texto.encode("utf-8")).decode("ascii").rstrip("=")


def _b64url_bytes(datos: bytes) -> str:
    """Igual que _b64url pero para bytes crudos (el contenido de un
    adjunto), sin pasar por texto/utf-8."""
    return base64.urlsafe_b64encode(datos).decode("ascii").rstrip("=")


def _html_constancia(
    solicitud: str | None = "100001",
    fecha: str = "15/06/2026",
    para: str = "NOMBRE DEL BENEFICIARIO",
    cuenta: str = "003-1234567890-4134",
    monto: str = "1,234.50",
    marca: str | None = None,
) -> str:
    """HTML mínimo con la misma estructura que un correo real de Interbank
    (los fragmentos <strong>/<td> que buscan los regex de correo_gmail).
    'marca' agrega un div con texto que NO forma parte de ningún campo
    extraído, para el test que verifica que el cuerpo nunca se guarda."""
    bloque_solicitud = (
        f"<p><strong>Número de solicitud:</strong> {solicitud}</p>" if solicitud is not None else ""
    )
    marca_html = f"<div>{marca}</div>" if marca else ""
    return f"""<!DOCTYPE html>
<html><body>
{marca_html}
{bloque_solicitud}
<p><strong>Fecha:</strong> {fecha}</p>
<table>
<tr><td>Para:</td><td>{para}</td></tr>
<tr><td>Cuenta de cargo:</td><td>{cuenta}</td></tr>
<tr><td>Monto:</td><td><strong>S/ {monto}</strong></td></tr>
</table>
</body></html>"""


def _payload_simple(html: str) -> dict:
    return {"mimeType": "text/html", "body": {"data": _b64url(html)}}


def _payload_anidado(html: str) -> dict:
    """multipart/mixed > multipart/alternative > text/html: el caso real de
    un correo con versión texto plano y versión HTML."""
    return {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url("version en texto plano, se ignora")}},
                    {"mimeType": "text/html", "body": {"data": _b64url(html)}},
                ],
            },
        ],
    }


def _parte_adjunto(
    filename: str,
    contenido: bytes,
    mime_type: str = "application/pdf",
    attachment_id: str | None = "attach-1",
    inline: bool = False,
) -> dict:
    """Parte de un mensaje con un adjunto: 'filename' no vacío, como los
    identifica _partes_con_adjunto. Por defecto trae 'body.attachmentId'
    (el caso normal, verificado el 2026-08-05 contra un correo real: Gmail
    no manda el contenido de adjuntos dentro del mensaje completo). Con
    inline=True en cambio simula el caso de un adjunto chico que sí trae
    'body.data' directo."""
    if inline:
        body = {"size": len(contenido), "data": _b64url_bytes(contenido)}
    else:
        body = {"attachmentId": attachment_id, "size": len(contenido)}
    return {"mimeType": mime_type, "filename": filename, "body": body}


def _payload_con_adjunto(
    filename: str,
    contenido: bytes,
    attachment_id: str | None = "attach-1",
    anidado: bool = False,
    inline: bool = False,
    mime_type: str = "application/pdf",
) -> dict:
    """multipart/mixed con la parte de texto (multipart/alternative, se
    ignora) y el adjunto como hermano -misma estructura que la verificada
    el 2026-08-05 contra un correo real de Interbank con un EECC adjunto.
    Con anidado=True el adjunto queda un nivel más abajo (multipart/mixed >
    multipart/mixed > adjunto), para ejercitar la recursión de
    _partes_con_adjunto más allá de un solo nivel."""
    parte_texto = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("cuerpo de correo, se ignora")}},
            {"mimeType": "text/html", "body": {"data": _b64url("<html><body>cuerpo html, se ignora</body></html>")}},
        ],
    }
    parte_adjunto = _parte_adjunto(filename, contenido, mime_type=mime_type, attachment_id=attachment_id, inline=inline)
    if anidado:
        return {
            "mimeType": "multipart/mixed",
            "parts": [parte_texto, {"mimeType": "multipart/mixed", "parts": [parte_adjunto]}],
        }
    return {"mimeType": "multipart/mixed", "parts": [parte_texto, parte_adjunto]}


def _regla_adjunto(
    nombre: str = "eecc-interbank",
    destino: str = "EECC",
    extensiones=(".pdf",),
    consulta: str = "subject:(te enviamos el estado) has:attachment",
) -> dict:
    return {
        "nombre": nombre,
        "tipo": "adjunto",
        "destino": destino,
        "consulta": consulta,
        "extensiones": list(extensiones),
    }


def _config(habilitado=True, reglas=None, numeros_cuenta=("4134",), max_mensajes=200, dias_atras=45):
    if reglas is None:
        reglas = [
            {
                "nombre": "constancias-interbank",
                "tipo": "constancia_interbank",
                "destino": "CONSTANCIAS",
                "consulta": "from:interbank.pe subject:(constancia de transferencia)",
            },
        ]
    cfg: dict = {
        "correo": {
            "habilitado": habilitado,
            "dias_atras": dias_atras,
            "max_mensajes": max_mensajes,
            "reglas": reglas,
        },
    }
    if numeros_cuenta:
        cfg["conciliacion"] = {
            "empresas": [{"nombre_corto": "EL TEMPLO", "cuentas": [{"numero": n} for n in numeros_cuenta]}],
        }
    return cfg


# -----------------------------------------------------------------------------
# habilitado: false
# -----------------------------------------------------------------------------
def test_habilitado_false_no_consulta_gmail_ni_construye_servicio(monkeypatch):
    config = _config(habilitado=False)
    almacen = FakeAlmacenDrive()

    def _no_deberia_construirse(*a, **k):
        raise AssertionError("servicio_gmail() no debería construirse si correo.habilitado es false")

    monkeypatch.setattr(correo_gmail.auth_google, "servicio_gmail", _no_deberia_construirse)

    # Sin pasar 'servicio': el default (None) tampoco debe disparar auth_google.
    resultado = correo_gmail.descargar(config, almacen, CARPETAS)
    assert resultado == {"adjuntos": 0, "constancias": 0, "omitidos": 0, "archivos": [], "errores": []}

    # Pasando un doble explícito, tampoco debe recibir ninguna llamada.
    servicio = FakeServicioGmail()
    resultado2 = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)
    assert resultado2 == resultado
    assert servicio.llamadas_list == []
    assert servicio.llamadas_get == []


# -----------------------------------------------------------------------------
# Extracción de constancia -> forma exacta del JSON
# -----------------------------------------------------------------------------
def test_constancia_html_produce_json_con_forma_exacta():
    html = _html_constancia(
        solicitud="100001", fecha="15/06/2026", para="NOMBRE DEL BENEFICIARIO",
        cuenta="003-1234567890-4134", monto="1,234.50",
    )
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_simple(html))
    almacen = FakeAlmacenDrive()
    config = _config()

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    assert resultado["constancias"] == 1
    assert len(almacen.llamadas_subir) == 1

    subida = almacen.llamadas_subir[0]
    assert subida["carpeta_id"] == "constancias-id"
    assert subida["nombre"] == "cons_4134.json"

    datos = json.loads(subida["contenido"])
    assert len(datos) == 1
    assert datos[0]["fecha"] == "15/06/2026"
    assert datos[0]["monto"] == 1234.50
    assert isinstance(datos[0]["monto"], float)
    assert datos[0]["para"] == "NOMBRE DEL BENEFICIARIO"
    assert datos[0]["cuenta"] == "4134"


# -----------------------------------------------------------------------------
# Deduplicación por número de solicitud
# -----------------------------------------------------------------------------
def test_dos_correos_mismo_numero_solicitud_cuentan_como_uno():
    html = _html_constancia(solicitud="100001", monto="500.00")
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_simple(html))
    servicio.agregar("msg-2", _payload_simple(html))  # p.ej. un reenvío del mismo correo
    almacen = FakeAlmacenDrive()
    config = _config()

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["constancias"] == 1
    datos = json.loads(almacen.llamadas_subir[0]["contenido"])
    assert len(datos) == 1


# -----------------------------------------------------------------------------
# Cuenta no configurada
# -----------------------------------------------------------------------------
def test_constancia_de_cuenta_no_configurada_no_entra():
    html = _html_constancia(cuenta="003-1234567890-9999")  # 9999 no está en config
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_simple(html))
    almacen = FakeAlmacenDrive()
    config = _config(numeros_cuenta=("4134",))

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["constancias"] == 0
    assert almacen.llamadas_subir == []


# -----------------------------------------------------------------------------
# HTML anidado en parts dentro de parts
# -----------------------------------------------------------------------------
def test_html_anidado_en_parts_dentro_de_parts_se_encuentra():
    html = _html_constancia(solicitud="200002")
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_anidado(html))
    almacen = FakeAlmacenDrive()
    config = _config()

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    assert resultado["constancias"] == 1


# -----------------------------------------------------------------------------
# Regla de tipo desconocido (ni implementado, ni declarado como pendiente):
# el mecanismo de saltar-con-advertencia sigue vivo para el próximo tipo que
# se agregue a config.yaml sin código todavía.
# -----------------------------------------------------------------------------
def test_regla_de_tipo_desconocido_se_salta_con_advertencia_sin_consultar_gmail(caplog):
    reglas = [
        {
            "nombre": "algo-nuevo", "tipo": "tipo_que_no_existe", "destino": "EECC",
            "consulta": "subject:(lo que sea) has:attachment",
        },
    ]
    config = _config(reglas=reglas)
    servicio = FakeServicioGmail()
    almacen = FakeAlmacenDrive()

    with caplog.at_level(logging.WARNING, logger="procesar.correo_gmail"):
        resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["omitidos"] == 1
    assert resultado["adjuntos"] == 0
    assert resultado["constancias"] == 0
    assert servicio.llamadas_attachments == []
    assert servicio.llamadas_list == []  # ni siquiera se consulta Gmail para esta regla
    assert any(
        "algo-nuevo" in m and "tipo_que_no_existe" in m and "desconocido" in m for m in caplog.messages
    )


# -----------------------------------------------------------------------------
# Regla de tipo 'adjunto': adjunto anidado se encuentra y se sube
# -----------------------------------------------------------------------------
def test_adjunto_anidado_en_parts_dentro_de_parts_se_encuentra_y_se_sube():
    contenido_pdf = b"%PDF-1.4 contenido de prueba, no es un pdf real"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto("estado.pdf", contenido_pdf, attachment_id="attach-1", anidado=True))
    servicio.agregar_adjunto("attach-1", contenido_pdf)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    assert resultado["adjuntos"] == 1
    assert resultado["omitidos"] == 0
    assert len(almacen.llamadas_subir) == 1

    subida = almacen.llamadas_subir[0]
    assert subida["carpeta_id"] == CARPETAS["EECC"]
    assert subida["nombre"] == "estado.pdf"
    assert subida["contenido"] == contenido_pdf
    assert subida["mimetype"] == "application/pdf"

    assert resultado["archivos"][0]["destino"] == "EECC"
    assert resultado["archivos"][0]["regla"] == "eecc-interbank"
    assert resultado["archivos"][0]["archivo"] == "estado.pdf"
    assert resultado["archivos"][0]["mensaje"] == "msg-1"
    assert resultado["archivos"][0]["id"] in almacen._archivos  # id real que devolvió AlmacenDrive.subir()


# -----------------------------------------------------------------------------
# Adjunto con 'body.data' inline (adjuntos chicos), sin attachmentId
# -----------------------------------------------------------------------------
def test_adjunto_con_data_inline_sin_attachment_id_se_sube_sin_llamar_a_attachments():
    contenido = b"contenido chico, viene inline"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto("chico.pdf", contenido, inline=True))
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    assert resultado["adjuntos"] == 1
    assert almacen.llamadas_subir[0]["contenido"] == contenido
    assert servicio.llamadas_attachments == []  # no hizo falta pedirlo aparte


# -----------------------------------------------------------------------------
# Filtro por extensión
# -----------------------------------------------------------------------------
def test_adjunto_con_extension_no_permitida_se_omite_y_no_se_sube():
    contenido_zip = b"PK\x03\x04 contenido zip de prueba"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto("comprimido.zip", contenido_zip, mime_type="application/zip"))
    servicio.agregar_adjunto("attach-1", contenido_zip)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto(extensiones=(".pdf",))], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    assert resultado["adjuntos"] == 0
    assert resultado["omitidos"] == 1
    assert almacen.llamadas_subir == []


def test_adjunto_extension_no_distingue_mayusculas():
    contenido_pdf = b"%PDF-1.4 contenido de prueba"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto("ESTADO.PDF", contenido_pdf))
    servicio.agregar_adjunto("attach-1", contenido_pdf)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto(extensiones=(".pdf",))], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["adjuntos"] == 1
    assert almacen.llamadas_subir[0]["nombre"] == "ESTADO.PDF"


# -----------------------------------------------------------------------------
# Idempotencia: correr dos veces no duplica nada en Drive
# -----------------------------------------------------------------------------
def test_adjunto_idempotente_segunda_corrida_no_vuelve_a_subir():
    contenido_pdf = b"%PDF-1.4 contenido de prueba"
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    servicio1 = FakeServicioGmail()
    servicio1.agregar("msg-1", _payload_con_adjunto("estado.pdf", contenido_pdf))
    servicio1.agregar_adjunto("attach-1", contenido_pdf)
    resultado1 = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio1)
    assert resultado1["adjuntos"] == 1
    assert len(almacen.llamadas_subir) == 1

    # Segunda corrida: mismo correo (por ejemplo, sigue calzando con la
    # consulta porque todavía está dentro de dias_atras). subir() NO debe
    # llamarse de nuevo.
    servicio2 = FakeServicioGmail()
    servicio2.agregar("msg-1", _payload_con_adjunto("estado.pdf", contenido_pdf))
    servicio2.agregar_adjunto("attach-1", contenido_pdf)
    resultado2 = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio2)

    assert resultado2["adjuntos"] == 0
    assert resultado2["omitidos"] == 1
    assert resultado2["errores"] == []
    assert len(almacen.llamadas_subir) == 1  # sigue en 1: no se volvió a llamar a subir()


# -----------------------------------------------------------------------------
# Nombre de adjunto malicioso: no puede escapar de la carpeta que le toca
# -----------------------------------------------------------------------------
def test_nombre_de_adjunto_con_path_traversal_se_sanea():
    contenido = b"contenido cualquiera"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto("../../evil.pdf", contenido))
    servicio.agregar_adjunto("attach-1", contenido)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    assert len(almacen.llamadas_subir) == 1
    nombre_subido = almacen.llamadas_subir[0]["nombre"]
    assert "/" not in nombre_subido
    assert "\\" not in nombre_subido
    assert ".." not in nombre_subido
    assert nombre_subido.endswith("evil.pdf")


def test_nombre_de_adjunto_con_caracteres_de_control_se_sanea():
    contenido = b"contenido cualquiera"
    nombre_crudo = "estado\x00\x0a\x1f.pdf"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto(nombre_crudo, contenido))
    servicio.agregar_adjunto("attach-1", contenido)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    nombre_subido = almacen.llamadas_subir[0]["nombre"]
    assert "\x00" not in nombre_subido
    assert "\x0a" not in nombre_subido
    assert "\x1f" not in nombre_subido
    assert nombre_subido == "estado.pdf"


def test_sanear_nombre_archivo_directo():
    """Prueba unitaria de _sanear_nombre_archivo, incluido el caso donde el
    nombre queda vacío después de sanear (solo caracteres de control, o
    solo separadores/puntos dobles): usa el fallback con el id del mensaje,
    en vez de tumbar la corrida con un nombre inservible."""
    assert correo_gmail._sanear_nombre_archivo("estado.pdf", "msg-1") == "estado.pdf"
    assert correo_gmail._sanear_nombre_archivo("../../evil.pdf", "msg-1") == "evil.pdf"
    assert correo_gmail._sanear_nombre_archivo("", "msg-1") == "adjunto_sin_nombre_msg-1"
    assert correo_gmail._sanear_nombre_archivo(None, "msg-2") == "adjunto_sin_nombre_msg-2"
    assert correo_gmail._sanear_nombre_archivo("\x00\x01\x02", "msg-3") == "adjunto_sin_nombre_msg-3"
    assert correo_gmail._sanear_nombre_archivo("..", "msg-4") == "adjunto_sin_nombre_msg-4"


# -----------------------------------------------------------------------------
# Nombre de adjunto vacío no tumba la corrida
# -----------------------------------------------------------------------------
def test_nombre_de_adjunto_vacio_no_tumba_la_corrida():
    """Un adjunto cuyo nombre sanea a vacío (aquí: solo caracteres de
    control, sin extensión reconocible) no revienta la corrida: cae a
    'omitidos' -el nombre de respaldo no tiene extensión, así que nunca
    calza con 'extensiones'- y el resto de los mensajes se procesa igual."""
    contenido_malo = b"nombre inservible"
    contenido_bueno = b"%PDF-1.4 este si tiene nombre util"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-malo", _payload_con_adjunto("\x00\x01\x02", contenido_malo, attachment_id="attach-malo"))
    servicio.agregar_adjunto("attach-malo", contenido_malo)
    servicio.agregar("msg-bueno", _payload_con_adjunto("estado.pdf", contenido_bueno, attachment_id="attach-bueno"))
    servicio.agregar_adjunto("attach-bueno", contenido_bueno)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["errores"] == []
    assert resultado["omitidos"] == 1
    assert resultado["adjuntos"] == 1
    assert len(almacen.llamadas_subir) == 1
    assert almacen.llamadas_subir[0]["nombre"] == "estado.pdf"


# -----------------------------------------------------------------------------
# dry_run
# -----------------------------------------------------------------------------
def test_adjunto_dry_run_no_llama_a_subir():
    contenido = b"contenido cualquiera"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto("estado.pdf", contenido))
    servicio.agregar_adjunto("attach-1", contenido)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio, dry_run=True)

    assert almacen.llamadas_subir == []
    assert resultado["adjuntos"] == 1  # se reporta lo que habría subido
    assert resultado["archivos"][0]["archivo"] == "estado.pdf"
    assert "id" not in resultado["archivos"][0]
    assert servicio.llamadas_attachments == []  # dry-run no baja el contenido, no hace falta


# -----------------------------------------------------------------------------
# Un mensaje que revienta se anota en errores y los siguientes se procesan
# -----------------------------------------------------------------------------
def test_adjunto_mensaje_que_revienta_se_anota_en_errores_y_siguientes_se_procesan():
    contenido_bueno = b"%PDF-1.4 contenido bueno"
    servicio = FakeServicioGmail()
    servicio.fallar_en_get("msg-api-error", RuntimeError("500 backend error"))
    servicio.agregar("msg-bueno", _payload_con_adjunto("estado.pdf", contenido_bueno, attachment_id="attach-bueno"))
    servicio.agregar_adjunto("attach-bueno", contenido_bueno)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["adjuntos"] == 1
    assert len(resultado["errores"]) == 1
    assert "msg-api-error" in resultado["errores"][0]
    assert almacen.llamadas_subir[0]["contenido"] == contenido_bueno


def test_adjunto_falla_al_bajar_contenido_se_anota_en_errores_y_no_tumba_la_corrida():
    contenido_bueno = b"%PDF-1.4 contenido bueno"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-falla", _payload_con_adjunto("falla.pdf", b"", attachment_id="attach-falla"))
    servicio.fallar_en_attachment("attach-falla", RuntimeError("adjunto no disponible"))
    servicio.agregar("msg-bueno", _payload_con_adjunto("estado.pdf", contenido_bueno, attachment_id="attach-bueno"))
    servicio.agregar_adjunto("attach-bueno", contenido_bueno)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["adjuntos"] == 1
    assert len(resultado["errores"]) == 1
    assert "msg-falla" in resultado["errores"][0]
    assert len(almacen.llamadas_subir) == 1
    assert almacen.llamadas_subir[0]["contenido"] == contenido_bueno


# -----------------------------------------------------------------------------
# Destino inexistente en carpetas: error reportado, no excepción
# -----------------------------------------------------------------------------
def test_adjunto_con_destino_inexistente_en_carpetas_se_reporta_como_error():
    contenido = b"contenido cualquiera"
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_con_adjunto("estado.pdf", contenido))
    servicio.agregar_adjunto("attach-1", contenido)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto(destino="NO_EXISTE")], numeros_cuenta=None)

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["adjuntos"] == 0
    assert len(resultado["errores"]) == 1
    assert "NO_EXISTE" in resultado["errores"][0]
    assert almacen.llamadas_subir == []
    assert servicio.llamadas_list == []  # ni siquiera se consultó Gmail: no hay dónde subir


# -----------------------------------------------------------------------------
# Ningún cuerpo de correo llega a Drive (tampoco con adjuntos de por medio)
# -----------------------------------------------------------------------------
def test_adjunto_ningun_cuerpo_de_correo_llega_a_drive(caplog):
    marca = "MARCA_DISTINTIVA_QUE_NUNCA_DEBE_GUARDARSE_EN_DRIVE_NI_EN_EL_LOG"
    contenido_pdf = "%PDF-1.4 contenido real del adjunto, sin la marca".encode("utf-8")
    servicio = FakeServicioGmail()
    payload = _payload_con_adjunto("estado.pdf", contenido_pdf)
    # Inserta la marca en el cuerpo (texto plano / HTML) del mensaje, para
    # confirmar que nunca sale de ahí.
    payload["parts"][0]["parts"][0]["body"]["data"] = _b64url(f"cuerpo con {marca}")
    payload["parts"][0]["parts"][1]["body"]["data"] = _b64url(f"<html><body>{marca}</body></html>")
    servicio.agregar("msg-1", payload)
    servicio.agregar_adjunto("attach-1", contenido_pdf)
    almacen = FakeAlmacenDrive()
    config = _config(reglas=[_regla_adjunto()], numeros_cuenta=None)

    with caplog.at_level(logging.DEBUG):
        resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["adjuntos"] == 1
    assert len(almacen.llamadas_subir) == 1
    # Lo subido son EXACTAMENTE los bytes del adjunto, nunca el cuerpo.
    assert almacen.llamadas_subir[0]["contenido"] == contenido_pdf

    for registro in caplog.records:
        assert marca not in registro.getMessage()


# -----------------------------------------------------------------------------
# Errores por mensaje: no tumban la corrida
# -----------------------------------------------------------------------------
def test_mensaje_que_revienta_se_anota_en_errores_y_siguientes_se_procesan():
    html_ok = _html_constancia(solicitud="300003")
    servicio = FakeServicioGmail()

    # 1) HTML sin los campos obligatorios (el regex no calza).
    servicio.agregar("msg-sin-campos", _payload_simple("<html><body>correo irrelevante</body></html>"))
    # 2) Error de la API de Gmail al pedir el mensaje.
    servicio.fallar_en_get("msg-api-error", RuntimeError("500 backend error"))
    # 3) base64 corrupto: longitud no reparable con relleno (5 caracteres,
    #    resto 1 mod 4, matemáticamente inválido con o sin '=').
    servicio.agregar("msg-b64-corrupto", {"mimeType": "text/html", "body": {"data": "QQQQQ"}})
    # 4) Mensaje válido: debe procesarse igual pese a los fallos anteriores.
    servicio.agregar("msg-bueno", _payload_simple(html_ok))

    almacen = FakeAlmacenDrive()
    config = _config()

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["constancias"] == 1
    assert len(resultado["errores"]) == 3
    errores_texto = " | ".join(resultado["errores"])
    assert "msg-sin-campos" in errores_texto
    assert "msg-api-error" in errores_texto
    assert "msg-b64-corrupto" in errores_texto

    datos = json.loads(almacen.llamadas_subir[0]["contenido"])
    assert len(datos) == 1
    assert datos[0]["fecha"] == "15/06/2026"


# -----------------------------------------------------------------------------
# dry_run
# -----------------------------------------------------------------------------
def test_dry_run_no_llama_a_subir():
    html = _html_constancia(solicitud="400004")
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_simple(html))
    almacen = FakeAlmacenDrive()
    config = _config()

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio, dry_run=True)

    assert almacen.llamadas_subir == []
    assert resultado["constancias"] == 1  # se reporta lo que habría subido
    assert resultado["archivos"][0]["archivo"] == "cons_4134.json"
    assert "id" not in resultado["archivos"][0]


# -----------------------------------------------------------------------------
# Fusión con lo que ya había en Drive
# -----------------------------------------------------------------------------
def test_fusion_conserva_viejas_y_nuevas_sin_duplicar():
    viejo = [
        {"fecha": "01/06/2026", "monto": 100.0, "para": "PROVEEDOR VIEJO", "cuenta": "4134", "numero_solicitud": "900001"},
    ]
    almacen = FakeAlmacenDrive()
    almacen.agregar_archivo("constancias-id", "cons_4134.json", json.dumps(viejo).encode("utf-8"))

    html_nuevo = _html_constancia(solicitud="900002", fecha="02/06/2026", monto="200.00", para="PROVEEDOR NUEVO")
    html_repetido = _html_constancia(solicitud="900001", fecha="01/06/2026", monto="100.00", para="PROVEEDOR VIEJO")
    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_simple(html_nuevo))
    servicio.agregar("msg-2", _payload_simple(html_repetido))  # ya estaba en cons_4134.json
    config = _config()

    resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert len(almacen.llamadas_subir) == 1
    subida = almacen.llamadas_subir[0]
    # subir() nunca sobrescribe: el resultado sube versionado, cons_4134.json
    # queda intacto en Drive.
    assert subida["nombre"] == "cons_4134 v2.json"

    datos = json.loads(subida["contenido"])
    assert len(datos) == 2  # 900001 fusionado (no duplicado) + 900002 nuevo
    solicitudes = {d["numero_solicitud"] for d in datos}
    assert solicitudes == {"900001", "900002"}
    montos_por_solicitud = {d["numero_solicitud"]: d["monto"] for d in datos}
    assert montos_por_solicitud["900002"] == 200.00


# -----------------------------------------------------------------------------
# Ningún cuerpo de correo se guarda
# -----------------------------------------------------------------------------
def test_ningun_cuerpo_de_correo_se_guarda(caplog):
    marca = "MARCA_DISTINTIVA_QUE_NUNCA_DEBE_GUARDARSE_EN_DRIVE_NI_EN_EL_LOG"
    html = _html_constancia(solicitud="500005", marca=marca)
    assert marca in html  # confirma que el fixture sí trae la marca

    servicio = FakeServicioGmail()
    servicio.agregar("msg-1", _payload_simple(html))
    almacen = FakeAlmacenDrive()
    config = _config()

    with caplog.at_level(logging.DEBUG):
        resultado = correo_gmail.descargar(config, almacen, CARPETAS, servicio=servicio)

    assert resultado["constancias"] == 1
    assert len(almacen.llamadas_subir) == 1

    # Nada de lo subido a Drive contiene la marca, ni siquiera una etiqueta
    # HTML cruda del cuerpo original: solo los campos ya extraídos.
    for subida in almacen.llamadas_subir:
        contenido_texto = subida["contenido"].decode("utf-8")
        assert marca not in contenido_texto
        assert "<div>" not in contenido_texto
        assert "<html>" not in contenido_texto

    # Tampoco quedó en ningún mensaje de log de la corrida.
    for registro in caplog.records:
        assert marca not in registro.getMessage()
