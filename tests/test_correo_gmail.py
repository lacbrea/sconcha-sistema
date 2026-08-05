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
# Reproduce la cadena real: servicio.users().messages().list(...).execute()
# y servicio.users().messages().get(...).execute(). attachments() existe
# solo para poder comprobar que NUNCA se llama (tipo 'adjunto' no está
# implementado todavía).
# -----------------------------------------------------------------------------
class FakeServicioGmail:
    def __init__(self):
        self._mensajes: dict[str, dict] = {}
        self._orden_ids: list[str] = []
        self._fallos: dict[str, Exception] = {}
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

    def attachments(self, *a, **k):  # nunca debería llamarse en este módulo
        self.llamadas_attachments.append((a, k))
        raise AssertionError("attachments() no debería llamarse: tipo 'adjunto' no está implementado")

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
# Regla de tipo 'adjunto': no implementada todavía
# -----------------------------------------------------------------------------
def test_regla_adjunto_se_salta_con_advertencia_y_suma_omitidos_sin_attachments(caplog):
    reglas = [
        {
            "nombre": "eecc-interbank", "tipo": "adjunto", "destino": "EECC",
            "consulta": "from:interbank.pe subject:(estado de cuenta) has:attachment",
            "extensiones": [".pdf"],
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
        "eecc-interbank" in m and "adjunto" in m and "todavía no implementado" in m for m in caplog.messages
    )


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
