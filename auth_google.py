"""Autenticación OAuth de usuario con Google (Drive, Sheets y Gmail de solo
lectura).

Usa el flujo de "aplicación instalada" (InstalledAppFlow), NO una cuenta de
servicio: las hojas y archivos quedan asociados a la cuenta de Gmail del
negocio (ver config.yaml -> cuenta_google), que es la que el dueño ve y
administra directamente desde su navegador.

Archivos que este módulo lee o escribe en el disco, junto a este script:

- credenciales.json: secreto de cliente OAuth descargado de Google Cloud
  Console (ver ONBOARDING.md, paso 2). Lo entrega el dueño del proyecto de
  Google Cloud. Este módulo solo lo LEE, nunca lo genera.
- token.json: token de acceso/actualización que se genera la primera vez
  que alguien completa el consentimiento en el navegador. Se reutiliza y se
  refresca automáticamente en corridas siguientes; solo se vuelve a pedir
  consentimiento si el refresh token deja de ser válido (por ejemplo, si la
  app de Google Cloud quedó en modo "Testing" y pasaron más de 7 días: ver
  la advertencia en ONBOARDING.md).

Ambos archivos están en .gitignore. Este módulo nunca imprime ni registra
en el log su contenido.
"""
from __future__ import annotations

import logging
import pathlib

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

logger = logging.getLogger("procesar.auth_google")

# Ámbitos mínimos necesarios: leer/escribir en Sheets y en Drive (para mover
# comprobantes dentro de las carpetas del negocio y obtener sus enlaces), y
# leer Gmail (para la Fase 4, conciliación bancaria: bajar del correo del
# negocio los estados de cuenta y las constancias de transferencia).
#
# gmail.readonly se agrega AHORA, aunque todavía no se use, porque token.json
# todavía no existe: mientras no exista, el dueño da su consentimiento en el
# navegador UNA sola vez por todos los ámbitos juntos. Si este ámbito se
# agregara después de la primera autorización, habría que repetir todo el
# trámite del navegador para volver a pedir consentimiento. Es de SOLO
# LECTURA: este módulo nunca escribe, responde ni borra correo.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
]

RUTA_MODULO = pathlib.Path(__file__).resolve().parent
RUTA_CREDENCIALES_POR_DEFECTO = RUTA_MODULO / "credenciales.json"
RUTA_TOKEN_POR_DEFECTO = RUTA_MODULO / "token.json"

MENSAJE_FALTA_CREDENCIALES = (
    "No se encontró '{ruta}'.\n"
    "Falta el secreto de cliente OAuth de Google Cloud. Sigue el paso 1 de "
    "ONBOARDING.md: crea (o reutiliza) un proyecto en Google Cloud Console, "
    "habilita 'Google Drive API', 'Google Sheets API' y 'Gmail API', crea "
    "credenciales OAuth de tipo 'Aplicación de escritorio' y descarga el JSON. "
    "Guárdalo exactamente como 'credenciales.json' en la raíz del proyecto "
    "(junto a procesar.py). Este archivo nunca debe subirse a un "
    "repositorio: ya está incluido en .gitignore."
)


class ErrorAutenticacion(RuntimeError):
    """Error claro para problemas de autenticación con Google."""


def _obtener_credenciales(
    ruta_credenciales: pathlib.Path = RUTA_CREDENCIALES_POR_DEFECTO,
    ruta_token: pathlib.Path = RUTA_TOKEN_POR_DEFECTO,
) -> Credentials:
    """Devuelve credenciales OAuth válidas, refrescando o pidiendo consentimiento
    solo cuando hace falta.

    Nunca imprime ni registra el contenido de las credenciales o el token.
    """
    credenciales: Credentials | None = None

    if ruta_token.exists():
        try:
            credenciales = Credentials.from_authorized_user_file(
                str(ruta_token), SCOPES
            )
        except (ValueError, OSError) as exc:
            logger.warning(
                "No se pudo leer token.json (%s); se pedirá autorización de nuevo.",
                type(exc).__name__,
            )
            credenciales = None

    if credenciales and credenciales.valid:
        return credenciales

    if credenciales and credenciales.expired and credenciales.refresh_token:
        try:
            credenciales.refresh(Request())
            _guardar_token(credenciales, ruta_token)
            return credenciales
        except Exception as exc:  # refresh_token revocado o expirado
            logger.warning(
                "No se pudo refrescar el token existente (%s). Posible causa: "
                "la aplicación de Google Cloud quedó en modo 'Testing' y el "
                "permiso caducó a los 7 días (ver ONBOARDING.md). Se pedirá "
                "autorización de nuevo.",
                type(exc).__name__,
            )

    if not ruta_credenciales.exists():
        raise ErrorAutenticacion(
            MENSAJE_FALTA_CREDENCIALES.format(ruta=ruta_credenciales)
        )

    flujo = InstalledAppFlow.from_client_secrets_file(str(ruta_credenciales), SCOPES)
    credenciales = flujo.run_local_server(port=0)
    _guardar_token(credenciales, ruta_token)
    return credenciales


def _guardar_token(credenciales: Credentials, ruta_token: pathlib.Path) -> None:
    ruta_token.write_text(credenciales.to_json(), encoding="utf-8")
    logger.debug("Token de Google guardado (contenido no registrado).")


def servicio_sheets(
    ruta_credenciales: pathlib.Path = RUTA_CREDENCIALES_POR_DEFECTO,
    ruta_token: pathlib.Path = RUTA_TOKEN_POR_DEFECTO,
) -> Resource:
    """Devuelve un recurso de la API de Google Sheets (v4) autenticado."""
    credenciales = _obtener_credenciales(ruta_credenciales, ruta_token)
    return build("sheets", "v4", credentials=credenciales)


def servicio_drive(
    ruta_credenciales: pathlib.Path = RUTA_CREDENCIALES_POR_DEFECTO,
    ruta_token: pathlib.Path = RUTA_TOKEN_POR_DEFECTO,
) -> Resource:
    """Devuelve un recurso de la API de Google Drive (v3) autenticado."""
    credenciales = _obtener_credenciales(ruta_credenciales, ruta_token)
    return build("drive", "v3", credentials=credenciales)


def servicio_gmail(
    ruta_credenciales: pathlib.Path = RUTA_CREDENCIALES_POR_DEFECTO,
    ruta_token: pathlib.Path = RUTA_TOKEN_POR_DEFECTO,
) -> Resource:
    """Devuelve un recurso de la API de Gmail (v1) autenticado, de solo lectura."""
    credenciales = _obtener_credenciales(ruta_credenciales, ruta_token)
    return build("gmail", "v1", credentials=credenciales)
