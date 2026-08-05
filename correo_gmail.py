"""Cuarta vía de entrada del skill: Gmail de SOLO LECTURA (ámbito
gmail.readonly, ver auth_google.SCOPES).

Este módulo solo consulta Gmail; nunca escribe, responde, etiqueta ni borra
correo. Eso queda garantizado en el código, no solo en este comentario:
nunca se importa ni se llama a ningún método de escritura de la API de
Gmail (modify/send/trash/delete no aparecen en este archivo).

Segunda garantía, igual de dura: el cuerpo de ningún correo se guarda jamás
(ni en disco, ni en Drive, ni en el log). El permiso que otorga Google
abarca TODO el buzón —no existe permiso por etiqueta ni por remitente—, así
que acotar es responsabilidad de este código. Concretamente:

- El HTML del cuerpo (`_extraer_html`) vive solo como variable local dentro
  de `_extraer_constancia_de_html` y de quien la llama; nunca se asigna a
  ningún dict que después se registre, se serialice a JSON o se suba a
  Drive.
- Lo único que sobrevive esa función son los CAMPOS ya extraídos por regex
  (fecha, monto, beneficiario, cuenta de cargo, número de solicitud): un
  puñado de strings/números cortos, nunca el HTML completo.
- Ningún logger.* de este módulo recibe el HTML ni el asunto del mensaje;
  solo el id del mensaje (que no es contenido) y, cuando aplica, los campos
  ya extraídos.
- El único archivo que este módulo escribe en disco es un temporal para
  fusionar cons_<cuenta>.json (ver `_fusionar_y_subir`): son campos ya
  extraídos de corridas anteriores, nunca un cuerpo de correo.

Tercera garantía: no hay ningún método para borrar nada, ni en Gmail (nunca
se llama a trash/delete) ni en Drive (AlmacenDrive no lo expone).

Es una librería sin CLI: quien llama (conciliar.py, todavía no escrito por
este agente) resuelve las carpetas de Drive y las pasa ya resueltas en
`carpetas`. Este módulo no resuelve rutas ni crea carpetas.
"""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import re
import tempfile
from typing import TYPE_CHECKING

import auth_google

if TYPE_CHECKING:  # pragma: no cover - solo para type hints, sin efecto en runtime
    from googleapiclient.discovery import Resource

    from almacen_drive import AlmacenDrive

logger = logging.getLogger("procesar.correo_gmail")

# -----------------------------------------------------------------------------
# Patrones de las constancias de transferencia de Interbank. Vienen literales
# de correos REALES (los usaba parse_constancias.py, ahora obsoleto): no se
# reinventan.
# -----------------------------------------------------------------------------
RE_NUMERO_SOLICITUD = re.compile(r"N[uú]mero de solicitud:\s*</strong>\s*(\d+)")
RE_FECHA = re.compile(r"Fecha:</strong>\s*(\d{2}/\d{2}/\d{4})")
RE_PARA = re.compile(r"Para:\s*</td>\s*<td>([^<]+)</td>")
RE_CUENTA_CARGO = re.compile(r"Cuenta de cargo:\s*</td>\s*<td>([^<]+)</td>")
RE_MONTO = re.compile(r"Monto:\s*</td>\s*<td><strong>S/\s*([\d,]+\.\d{2})")

# Tipos de regla declarados en config.yaml que todavía NO se implementan (ver
# ALCANCE): sus consultas son una suposición mientras no llegue correo real.
# Cualquier regla con uno de estos tipos se salta con advertencia, nunca en
# silencio.
TIPOS_NO_IMPLEMENTADOS = frozenset({"adjunto"})
TIPO_CONSTANCIA_INTERBANK = "constancia_interbank"

DIAS_ATRAS_POR_DEFECTO = 45
MAX_MENSAJES_POR_DEFECTO = 200


# =============================================================================
# Extracción del cuerpo (en memoria, nunca guardado)
# =============================================================================
def _decodificar_base64url(datos: str) -> bytes:
    """Decodifica base64 URL-safe tal como lo entrega Gmail en body.data,
    agregando el relleno ('=') si Gmail lo omitió (lo omite seguido)."""
    faltante = (-len(datos)) % 4
    return base64.urlsafe_b64decode(datos + "=" * faltante)


def _extraer_html(payload: dict) -> str | None:
    """Busca la parte text/html del mensaje y devuelve su cuerpo ya
    decodificado como string. Puede estar anidada en 'parts' dentro de
    'parts' (multipart/mixed > multipart/alternative > text/html), así que
    la búsqueda es recursiva.

    El valor que devuelve vive solo en memoria, en la pila de llamadas de
    quien invoque esto: no se asigna a ningún campo que luego se registre o
    se persista. Es la única función de este módulo que toca el cuerpo
    completo del correo.
    """
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    datos = body.get("data")
    if mime == "text/html" and datos:
        return _decodificar_base64url(datos).decode("utf-8", errors="replace")

    for parte in payload.get("parts") or []:
        resultado = _extraer_html(parte)
        if resultado is not None:
            return resultado
    return None


def _extraer_constancia_de_html(html: str) -> dict | None:
    """Aplica los patrones de Interbank al HTML (ya en memoria, nunca
    persistido) y devuelve solo los campos concretos que le importan al
    conciliador. Devuelve None si falta fecha, monto o cuenta de cargo
    (los tres son obligatorios; sin ellos el registro no sirve para
    conciliar y se descarta, tal como pide el alcance).

    'para' (beneficiario) y 'numero_solicitud' no están en esa lista de
    obligatorios: si el regex de beneficiario no calza se deja vacío, y si
    falta el número de solicitud lo resuelve el llamador (usa el id del
    mensaje como llave de deduplicación de respaldo).
    """
    m_fecha = RE_FECHA.search(html)
    m_cuenta = RE_CUENTA_CARGO.search(html)
    m_monto = RE_MONTO.search(html)
    if not (m_fecha and m_cuenta and m_monto):
        return None

    m_solicitud = RE_NUMERO_SOLICITUD.search(html)
    m_para = RE_PARA.search(html)

    return {
        "numero_solicitud": m_solicitud.group(1) if m_solicitud else None,
        "fecha": m_fecha.group(1),
        "monto": float(m_monto.group(1).replace(",", "")),
        "para": (m_para.group(1) if m_para else "").strip(),
        "cuenta_cargo": m_cuenta.group(1).strip(),
    }


# =============================================================================
# Cuentas configuradas y emparejado por sufijo
# =============================================================================
def _numeros_cuenta(config: dict) -> list[str]:
    """Últimos dígitos de cuenta a considerar, sacados de
    config['conciliacion']['empresas'][*]['cuentas'][*]['numero'].

    Si no existe la sección 'conciliacion' (o viene vacía), devuelve lista
    vacía: no hay cuentas con las que emparejar, no es un error de este
    módulo."""
    empresas = ((config.get("conciliacion") or {}).get("empresas")) or []
    numeros: list[str] = []
    for empresa in empresas:
        for cuenta in empresa.get("cuentas") or []:
            numero = cuenta.get("numero")
            if numero:
                numeros.append(str(numero))
    return numeros


def _cuenta_coincidente(cuenta_cargo: str, numeros: list[str]) -> str | None:
    """Devuelve el número de cuenta configurado que es sufijo de
    'cuenta_cargo', o None si ninguno calza. Prueba primero los números más
    largos para que uno corto no se "adelante" si por casualidad también es
    sufijo de otro más largo configurado."""
    cuenta_cargo = (cuenta_cargo or "").strip()
    for numero in sorted(numeros, key=len, reverse=True):
        if cuenta_cargo.endswith(numero):
            return numero
    return None


# =============================================================================
# Listado de mensajes (paginado, con tope global)
# =============================================================================
def _listar_ids_mensajes(servicio, consulta: str, dias_atras: int, limite: int) -> list[str]:
    """Ids de mensajes que matchean 'consulta' (más el filtro newer_than),
    hasta 'limite' ids, paginando con pageToken. 'limite' es el presupuesto
    que le queda a la corrida completa (no a esta regla sola): así
    max_mensajes es un tope global aunque haya varias reglas."""
    if limite <= 0:
        return []
    query = f"{consulta} newer_than:{dias_atras}d".strip()
    ids: list[str] = []
    token = None
    while len(ids) < limite:
        resp = (
            servicio.users()
            .messages()
            .list(userId="me", q=query, maxResults=min(100, limite - len(ids)), pageToken=token)
            .execute()
        )
        mensajes = resp.get("messages") or []
        if not mensajes:
            break
        for m in mensajes:
            ids.append(m["id"])
            if len(ids) >= limite:
                break
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


# =============================================================================
# Fusión con lo que ya había en Drive
# =============================================================================
def _fusionar(existentes: list[dict], nuevas: dict[str, dict]) -> list[dict]:
    """Combina lo que ya había en cons_<cuenta>.json con lo nuevo de esta
    corrida, deduplicando por número de solicitud. Un registro nuevo con el
    mismo número de solicitud que uno viejo reemplaza al viejo (mismos
    datos esperados; si algo cambiara, gana el más reciente).

    Un registro viejo sin 'numero_solicitud' (por ejemplo, escrito por el
    parse_constancias.py ahora obsoleto, que no guardaba ese campo) no se
    puede deduplicar por solicitud: se conserva igual, bajo una llave
    sintética que no colisiona con números de solicitud reales."""
    por_solicitud: dict[str, dict] = {}
    for i, item in enumerate(existentes):
        solicitud = item.get("numero_solicitud")
        llave = str(solicitud) if solicitud else f"__legacy_{i}__"
        por_solicitud[llave] = item
    for solicitud, item in nuevas.items():
        por_solicitud[str(solicitud)] = item

    fusionado = list(por_solicitud.values())
    fusionado.sort(key=lambda c: (c.get("fecha", ""), c.get("monto", 0)))
    return fusionado


def _fusionar_y_subir(
    almacen: "AlmacenDrive",
    carpeta_id: str,
    numero_cuenta: str,
    nuevas: dict[str, dict],
    archivos_existentes: list[dict],
    dry_run: bool,
) -> dict:
    """Regenera cons_<numero_cuenta>.json con lo viejo + lo nuevo y lo sube.

    AlmacenDrive.subir() nunca sobrescribe (siempre CREA), así que si ya
    existe cons_<numero>.json (o una versión posterior, cons_<numero>
    v2.json, v3.json...) el resultado se sube con el siguiente número de
    versión en vez de reemplazar nada. Se versiona -en vez de, por ejemplo,
    borrar y recrear- porque esta clase de almacenamiento no tiene (a
    propósito) ningún método para borrar.
    """
    patron_version = re.compile(rf"^cons_{re.escape(numero_cuenta)}(?: v(\d+))?\.json$")
    mas_reciente: tuple[int, dict] | None = None
    for archivo in archivos_existentes:
        m = patron_version.match(archivo.get("name", ""))
        if not m:
            continue
        version = int(m.group(1)) if m.group(1) else 1
        if mas_reciente is None or version > mas_reciente[0]:
            mas_reciente = (version, archivo)

    existentes: list[dict] = []
    siguiente_version = 1
    if mas_reciente is not None:
        version_actual, archivo_previo = mas_reciente
        siguiente_version = version_actual + 1
        # Único uso legítimo de disco de este módulo: un archivo temporal
        # con JSON YA EXTRAÍDO de corridas anteriores (fecha/monto/para/
        # cuenta/numero_solicitud), nunca un cuerpo de correo. Se borra solo
        # al salir del 'with'.
        with tempfile.TemporaryDirectory(prefix="sconcha_correo_") as carpeta_tmp:
            destino_tmp = pathlib.Path(carpeta_tmp) / archivo_previo["name"]
            almacen.descargar(archivo_previo["id"], destino_tmp)
            existentes = json.loads(destino_tmp.read_text(encoding="utf-8"))

    fusionado = _fusionar(existentes, nuevas)
    nombre_archivo = (
        f"cons_{numero_cuenta}.json" if siguiente_version == 1 else f"cons_{numero_cuenta} v{siguiente_version}.json"
    )

    info = {
        "destino": "CONSTANCIAS",
        "cuenta": numero_cuenta,
        "archivo": nombre_archivo,
        "nuevas": len(nuevas),
        "total": len(fusionado),
    }

    if dry_run:
        logger.info(
            "[DRY-RUN] se subiría '%s' con %d constancia(s) (%d nueva(s))",
            nombre_archivo, len(fusionado), len(nuevas),
        )
        return info

    contenido = json.dumps(fusionado, ensure_ascii=False, indent=2).encode("utf-8")
    file_id = almacen.subir(carpeta_id, nombre_archivo, contenido, mimetype="application/json")
    info["id"] = file_id
    logger.info(
        "cuenta %s: subido '%s' con %d constancia(s) (%d nueva(s))",
        numero_cuenta, nombre_archivo, len(fusionado), len(nuevas),
    )
    return info


# =============================================================================
# Punto de entrada
# =============================================================================
def descargar(
    config: dict,
    almacen: "AlmacenDrive",
    carpetas: dict,
    servicio: "Resource | None" = None,
    dry_run: bool = False,
) -> dict:
    """Consulta Gmail (solo lectura) según config['correo'] y escribe
    cons_<cuenta>.json en Drive con las constancias de transferencia de
    Interbank que emparejan alguna cuenta configurada.

    carpetas: {"EECC": "<id de Drive>", "CONSTANCIAS": "<id>", "BUZON": "<id>"}
    Devuelve {"adjuntos": int, "constancias": int, "omitidos": int,
              "archivos": [ {...} ], "errores": [str]}

    'adjuntos' queda siempre en 0: las reglas tipo:'adjunto' declaradas en
    config.yaml no se implementan todavía (ver ALCANCE) y se saltan con
    advertencia, sumando a 'omitidos'.
    """
    resumen = {"adjuntos": 0, "constancias": 0, "omitidos": 0, "archivos": [], "errores": []}

    correo_cfg = config.get("correo") or {}
    if not correo_cfg.get("habilitado", False):
        logger.info("correo.habilitado es false: no se consulta Gmail.")
        return resumen

    reglas = correo_cfg.get("reglas") or []
    dias_atras = correo_cfg.get("dias_atras", DIAS_ATRAS_POR_DEFECTO)
    max_mensajes = correo_cfg.get("max_mensajes", MAX_MENSAJES_POR_DEFECTO)

    if servicio is None:
        servicio = auth_google.servicio_gmail()

    numeros_cuenta = _numeros_cuenta(config)
    hay_regla_constancia = any(r.get("tipo") == TIPO_CONSTANCIA_INTERBANK for r in reglas)
    if hay_regla_constancia and not numeros_cuenta:
        logger.warning(
            "No hay 'conciliacion.empresas[*].cuentas' en config: no hay "
            "cuentas con las que emparejar constancias; se devuelven 0."
        )

    presupuesto_restante = max_mensajes
    # numero_cuenta -> {numero_solicitud: registro}. Se acumula entre TODAS
    # las reglas antes de fusionar/subir, para no versionar dos veces el
    # mismo cons_<cuenta>.json si hubiera más de una regla de este tipo.
    constancias_por_cuenta: dict[str, dict[str, dict]] = {}
    destino_por_cuenta: dict[str, str] = {}

    for regla in reglas:
        nombre_regla = regla.get("nombre", "(sin nombre)")
        tipo = regla.get("tipo")

        if tipo in TIPOS_NO_IMPLEMENTADOS:
            logger.warning(
                "regla '%s' de tipo '%s': todavía no implementado, se ignora", nombre_regla, tipo,
            )
            resumen["omitidos"] += 1
            continue

        if tipo != TIPO_CONSTANCIA_INTERBANK:
            logger.warning(
                "regla '%s' de tipo '%s' desconocido: se ignora", nombre_regla, tipo,
            )
            resumen["omitidos"] += 1
            continue

        if not numeros_cuenta:
            # Ya se avisó una vez arriba; sin cuentas no hay nada que
            # emparejar, así que ni vale la pena gastar la consulta a Gmail.
            continue

        destino = regla.get("destino") or "CONSTANCIAS"
        carpeta_destino_id = carpetas.get(destino)
        if not carpeta_destino_id:
            motivo = f"regla '{nombre_regla}': no hay carpeta de Drive para destino '{destino}'"
            logger.error(motivo)
            resumen["errores"].append(motivo)
            continue

        if presupuesto_restante <= 0:
            logger.warning(
                "regla '%s': se alcanzó el tope de max_mensajes (%d); se ignora el resto.",
                nombre_regla, max_mensajes,
            )
            continue

        try:
            ids_mensajes = _listar_ids_mensajes(
                servicio, regla.get("consulta", ""), dias_atras, presupuesto_restante,
            )
        except Exception as exc:
            motivo = f"regla '{nombre_regla}': error al listar mensajes: {exc}"
            logger.error(motivo)
            resumen["errores"].append(motivo)
            continue

        presupuesto_restante -= len(ids_mensajes)

        for msg_id in ids_mensajes:
            try:
                mensaje = servicio.users().messages().get(userId="me", id=msg_id, format="full").execute()
                html = _extraer_html(mensaje.get("payload") or {})
                if html is None:
                    raise ValueError("no se encontró parte text/html en el mensaje")
                datos = _extraer_constancia_de_html(html)
                if datos is None:
                    raise ValueError("faltan campos obligatorios (fecha, monto o cuenta de cargo)")
            except Exception as exc:
                # Un mensaje que falla (regex que no calza, base64 corrupto,
                # error de la API) no tumba la corrida: se anota y se sigue,
                # mismo criterio que procesar.py con los archivos del buzón.
                motivo = f"mensaje {msg_id}: {exc}"
                logger.warning(motivo)
                resumen["errores"].append(motivo)
                continue

            numero_cuenta = _cuenta_coincidente(datos["cuenta_cargo"], numeros_cuenta)
            if numero_cuenta is None:
                logger.info(
                    "mensaje %s: cuenta de cargo no coincide con ninguna cuenta configurada; se descarta.",
                    msg_id,
                )
                continue

            # Si el correo no trae número de solicitud (no debería pasar en
            # una constancia real, pero no se puede garantizar), se usa el
            # id del mensaje como llave de deduplicación de respaldo: sigue
            # siendo único por mensaje y no tumba la corrida.
            solicitud = datos["numero_solicitud"] or msg_id
            registro = {
                "fecha": datos["fecha"],
                "monto": datos["monto"],
                "para": datos["para"],
                "cuenta": numero_cuenta,
                "numero_solicitud": solicitud,
            }
            constancias_por_cuenta.setdefault(numero_cuenta, {})[solicitud] = registro
            destino_por_cuenta[numero_cuenta] = carpeta_destino_id
            logger.info(
                "mensaje %s: constancia extraída para cuenta %s (solicitud %s)",
                msg_id, numero_cuenta, solicitud,
            )

    if not constancias_por_cuenta:
        return resumen

    # Un solo listar() por carpeta destino (normalmente una sola,
    # CONSTANCIAS), cacheado, para no repetir la llamada por cada cuenta.
    listados_por_carpeta: dict[str, list[dict]] = {}
    for numero_cuenta, nuevas in constancias_por_cuenta.items():
        carpeta_id = destino_por_cuenta[numero_cuenta]
        if carpeta_id not in listados_por_carpeta:
            listados_por_carpeta[carpeta_id] = almacen.listar(carpeta_id)

        try:
            info = _fusionar_y_subir(
                almacen, carpeta_id, numero_cuenta, nuevas, listados_por_carpeta[carpeta_id], dry_run,
            )
        except Exception as exc:
            motivo = f"cuenta {numero_cuenta}: error al fusionar/subir cons_{numero_cuenta}.json: {exc}"
            logger.error(motivo)
            resumen["errores"].append(motivo)
            continue

        resumen["constancias"] += len(nuevas)
        resumen["archivos"].append(info)

    return resumen
