"""Extracción de comprobantes con el modelo Claude, para PDF y foto.

Se usa cuando no hay XML UBL de SUNAT disponible: comprobantes en papel,
recibos de servicios, PDFs escaneados o fotos tomadas por WhatsApp. A
diferencia de `extractores.xml_ubl`, esto tiene costo por llamada y no es
determinista — por eso el resultado siempre trae `confianza` < 1.0 y puede
venir con advertencias de truncamiento.

El prompt porta literalmente las reglas de extracción de cantidad/unidad y de
total_linea ya afinadas en `sconcha-app/src/lib/factura-parser.ts` contra
facturas peruanas reales (el caso "CJ*6", la normalización a kg/L, los ítems
indivisibles, el descuento aplicado en total_linea). No se reinventan porque
son reglas ganadas contra documentos reales, no un diseño desde cero.

Este skill tiene que poder instalarse en cualquier empresa cambiando solo
`config.yaml` — por eso nada específico de SCONCHA (razones sociales propias,
modelo, effort) vive hardcodeado aquí. Todo eso entra por el parámetro
opcional `config` de `extraer()`.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib

import anthropic

from esquema import ComprobanteExtraido, ESQUEMA_JSON, ItemExtraido

# Defaults si `config` no trae 'modelo' / 'esfuerzo', o si se llama sin config
# (por ejemplo desde un test). El valor real de negocio sale de config.yaml.
_MODELO_DEFECTO = "claude-opus-5"
_ESFUERZO_DEFECTO = "low"

# El thinking adaptativo de Opus 5 viene encendido por defecto y max_tokens
# limita thinking + texto juntos: con un JSON de comprobante (con ítems) 16000
# da margen de sobra sin encarecer cada llamada innecesariamente.
_MAX_TOKENS = 16000

_MEDIA_TYPE_POR_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_EXTENSIONES_NO_SOPORTADAS = {".heic", ".heif"}


class ErrorModeloClaude(Exception):
    """Error base para fallos al extraer un comprobante con el modelo Claude."""


class ClaveApiFaltanteError(ErrorModeloClaude):
    """No hay ANTHROPIC_API_KEY configurada en el entorno."""


class FormatoNoSoportadoError(ErrorModeloClaude):
    """El formato del archivo no es aceptado por la API (ej. HEIC/HEIF)."""


class RespuestaRechazadaError(ErrorModeloClaude):
    """El modelo se negó a procesar el comprobante (`stop_reason == 'refusal'`)."""


def extraer(ruta: pathlib.Path, tipo: str, config: dict | None = None) -> ComprobanteExtraido:
    """Extrae un comprobante con el modelo Claude a partir de un PDF o una foto.

    `tipo` es `'pdf'` o `'imagen'`. `config` es opcional (default `None`, para
    no romper el contrato original `extraer(ruta, tipo)`) y puede traer:
    - `config['empresas']`: lista de dicts `{'razon_social': ..., 'ruc': ...}`
      con las razones sociales propias del negocio, para que el prompt sepa
      reconocerlas del lado cliente. Sin esto, el modelo igual extrae
      `cliente_ruc` / `cliente_razon_social` tal cual aparecen en el
      documento — quien orqueste hace la asignación de empresa a partir de
      ahí, así que no es indispensable.
    - `config['modelo']`: nombre del modelo (default `claude-opus-5`).
    - `config['esfuerzo']`: `effort` de structured outputs (default `low`).

    Lanza `ErrorModeloClaude` (o una de sus subclases) ante cualquier fallo
    irrecuperable — no hay resultado parcial razonable que devolver si la
    llamada a la API falla del todo o la respuesta no se puede interpretar.
    """
    if tipo not in ("pdf", "imagen"):
        raise ValueError(f"tipo debe ser 'pdf' o 'imagen', recibido: '{tipo}'")

    ruta = pathlib.Path(ruta)
    config = config or {}
    empresas = config.get("empresas")
    modelo = config.get("modelo") or _MODELO_DEFECTO
    esfuerzo = config.get("esfuerzo") or _ESFUERZO_DEFECTO

    if tipo == "imagen" and ruta.suffix.lower() in _EXTENSIONES_NO_SOPORTADAS:
        raise FormatoNoSoportadoError(
            f"El archivo '{ruta.name}' es HEIC/HEIF, formato no soportado por la API de Anthropic "
            "(acepta JPEG, PNG, GIF o WebP). Debe derivarse a revisión manual o convertirse antes de "
            "reintentar la extracción."
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ClaveApiFaltanteError(
            "No se encontró la variable de entorno ANTHROPIC_API_KEY. Configúrala en PowerShell con:\n"
            "  [Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', 'tu-clave', 'User')\n"
            "y abre una terminal nueva para que la tome."
        )

    bloque_documento = _construir_bloque_documento(ruta, tipo)

    system = [
        {"type": "text", "text": _construir_prompt(empresas), "cache_control": {"type": "ephemeral"}},
    ]
    mensajes = [
        {
            "role": "user",
            "content": [
                bloque_documento,
                {
                    "type": "text",
                    "text": "Extrae la información estructurada de este comprobante en JSON, "
                    "siguiendo exactamente las reglas del system prompt.",
                },
            ],
        }
    ]

    client = anthropic.Anthropic()

    try:
        respuesta = client.messages.create(
            model=modelo,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=mensajes,
            output_config={"effort": esfuerzo, "format": {"type": "json_schema", "schema": ESQUEMA_JSON}},
        )
    except anthropic.NotFoundError as exc:
        raise ErrorModeloClaude(
            f"No se encontró el modelo '{modelo}'. Verifica el nombre del modelo configurado. "
            f"Detalle: {exc}"
        ) from exc
    except anthropic.RateLimitError as exc:
        raise ErrorModeloClaude(
            f"Se alcanzó el límite de tasa de la API de Anthropic; reintenta más tarde. Detalle: {exc}"
        ) from exc
    except anthropic.APIStatusError as exc:
        raise ErrorModeloClaude(
            f"La API de Anthropic devolvió un error (status {exc.status_code}). Detalle: {exc}"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise ErrorModeloClaude(
            f"No se pudo conectar con la API de Anthropic. Verifica tu conexión a internet. Detalle: {exc}"
        ) from exc

    return _procesar_respuesta(respuesta, tipo)


def _construir_prompt(empresas: list[dict] | None) -> str:
    """Arma el prompt del sistema.

    Determinista a propósito: para el mismo `config` (misma lista de
    empresas, mismo orden lógico) tiene que producir exactamente la misma
    cadena de bytes en cada llamada, o se invalida el `cache_control` del
    bloque de system y se paga el prompt completo en cada documento en vez
    de una vez por corrida. Por eso las empresas se ordenan por RUC antes de
    listarlas, sin depender del orden en que vengan en `config.yaml` o de
    iterar un `set`/`dict` no determinista.

    Sin `empresas` (None o vacío) se omite por completo la frase de razones
    sociales propias — nunca se inventa una lista por defecto. El skill tiene
    que poder instalarse en cualquier empresa con solo cambiar `config.yaml`;
    el modelo igual extrae `cliente_ruc` / `cliente_razon_social` tal cual
    aparecen en el documento, y quien orqueste hace la asignación de empresa.
    """
    bullets_proveedor_cliente = [
        "- PROVEEDOR = la empresa que EMITE el comprobante. Aparece en el encabezado/parte superior del "
        "documento (nombre comercial, RUC, dirección fiscal). NO es el cliente/comprador.",
        "- El CLIENTE o COMPRADOR (marcado como \"CLIENTE\", \"CLIE\", \"SEÑOR(ES)\", \"ADQUIRIENTE\") NO "
        "es el proveedor. Extrae también sus datos en cliente_ruc y cliente_razon_social — con eso el "
        "sistema identifica solo a cuál de nuestras empresas pertenece el gasto, así que estos dos campos "
        "son tan importantes como los del proveedor.",
    ]
    if empresas:
        ordenadas = sorted(empresas, key=lambda empresa: str(empresa.get("ruc", "")))
        lista = ", ".join(
            f"{empresa.get('razon_social', '')} (RUC {empresa.get('ruc', '')})" for empresa in ordenadas
        )
        bullets_proveedor_cliente.append(
            "- Nuestras razones sociales propias, si aparecen, van del lado CLIENTE y nunca del lado "
            f"proveedor: {lista}."
        )
    bullets_proveedor_cliente.append(
        "- El RUC del proveedor es el que aparece arriba junto al nombre de la empresa emisora, NO el que "
        "aparece junto a \"CLIE:\" o \"RUC CLIENTE\"."
    )
    seccion_proveedor_cliente = "PROVEEDOR Y CLIENTE:\n" + "\n".join(bullets_proveedor_cliente)

    return f"""Eres un asistente experto en comprobantes de pago peruanos (facturas, boletas, \
recibos por honorarios, recibos de servicios, notas de crédito y notas de débito). Vas a recibir un \
documento (PDF o foto) de un comprobante y debes devolver JSON estructurado con la información extraída.

{seccion_proveedor_cliente}

TIPO DE DOCUMENTO:
- "factura" (comprobante con IGV discriminado, serie F###), "boleta" (serie B###), "recibo_honorarios" \
(recibo por honorarios profesionales, cuarta categoría), "recibo_servicio" (recibo de un servicio público \
o similar: luz, agua, internet, telefonía), "nota_credito" (anula o corrige un comprobante anterior), \
"nota_debito" (incrementa el valor de un comprobante anterior), "guia_remision" (traslado de bienes, sin \
importes de venta), "otro" si no calza en ninguno de los anteriores o no se puede determinar.
- Si es nota de crédito o nota de débito, busca a qué comprobante hace referencia (suele decir "Doc. que \
modifica" o similar) y ponlo en documento_referencia con el mismo formato serie-número del original.

CANTIDAD Y UNIDAD (REGLA CRÍTICA — el error más común):
- "cantidad" debe ser la cantidad TOTAL en la unidad base (kg, L o unid), NUNCA el número de \
envases/latas/botellas.
- Si un ítem se vende en envases que contienen un peso o volumen, MULTIPLICA:
  cantidad_total = (número de envases comprados) × (tamaño de cada envase).
- Ejemplo clave: "SALSA HOISIN 2.268KG CJ*6" con 6 UN (6 latas de 2.268 kg c/u)
  → cantidad: 13.608, unidad: "kg"  (NO cantidad 6)
- Normaliza a kg/L: "500gr" → 0.5 kg ; "250ml" → 0.25 L.
  Ejemplo: "Salsa Sriracha 500gr" con 1 UN → cantidad: 0.5, unidad: "kg"
- Tamaño en la misma descripción de un solo envase: "AZUCAR BLANCA X5KG" con 1 UN
  → cantidad: 5, unidad: "kg"
- Marcadores como "CJ*6", "X12", "PACK 6" indican cuántas piezas trae cada envase;
  combínalos con las unidades compradas para obtener el total.
- Ítems indivisibles (cajas de cartón, bolsas, cinta, etiquetas): deja cantidad =
  número de piezas, unidad: "unid". Ej: 21 cajas → cantidad: 21, unidad: "unid".

total_linea Y PRECIO:
- "total_linea" = importe FINAL de la línea YA con el descuento aplicado (la cifra de
  importe a la derecha de cada renglón).
  Ejemplo: 6 × 35.00 con 10% de descuento → total_linea: 189.00 (no 210.00)
- Si hay columna de descuento (Dcto %), aplícalo: total_linea = cantidad_envases × precio − descuento.
- "precio_unitario": déjalo null si no estás seguro; el sistema deriva el costo con total_linea / cantidad.
- Los precios son en soles peruanos, sin símbolo, salvo que el documento indique explícitamente USD.
- Las fechas vienen en formato dd/mm/yyyy en la factura — conviértelas a YYYY-MM-DD

DETRACCIÓN:
- Busca la leyenda típica "Operación sujeta al Sistema de Pago de Obligaciones Tributarias (SPOT)" o \
"Operación sujeta a detracción". Extrae detraccion_pct (número, ej. 12 para 12%, o 4 para 4%) y \
detraccion_monto (el importe en soles depositado). Si el documento trae un código del bien/servicio \
detraído, ponlo en detraccion_codigo.
- Si no hay ninguna mención de detracción, deja los tres campos en null.

RETENCIÓN E ICBPER:
- "retencion": monto retenido si el documento menciona un régimen de retención de IGV (leyenda "Sujeto a \
retención" o similar). Si no aparece, null.
- "icbper": Impuesto al Consumo de Bolsas Plásticas, suele aparecer como una línea o cargo adicional de \
S/ 0.50 por bolsa. Si no aparece, null.

CONDICIÓN Y MONEDA:
- "condicion": "contado" o "credito" según lo indicado en el documento (busca "Contado"/"Crédito", forma \
de pago o cuotas). Si no se menciona, null.
- "moneda": "PEN" (soles) o "USD" (dólares) según el documento. Si no se indica, asume "PEN".
- "tipo_cambio": si el documento está en USD y muestra un tipo de cambio, extráelo; si no, null.

CONFIANZA:
- Agrega tu propia estimación en "confianza" (número entre 0 y 1) de qué tan segura está la extracción \
completa. Usa valores bajos (0.3-0.5) si el documento está borroso, cortado o con campos ambiguos; \
valores altos (0.9-1.0) si es claro y completo.

GENERAL:
- Si un campo no es legible o no aparece, usa null.
- Los códigos de barras al inicio de cada línea NO van en la descripción del ítem.
- Devuelve el JSON siguiendo exactamente el esquema indicado."""


def _construir_bloque_documento(ruta: pathlib.Path, tipo: str) -> dict:
    datos_b64 = base64.b64encode(ruta.read_bytes()).decode("ascii")
    if tipo == "pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": datos_b64},
        }
    media_type = _MEDIA_TYPE_POR_EXTENSION.get(ruta.suffix.lower(), "image/jpeg")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": datos_b64},
    }


def _procesar_respuesta(respuesta, tipo: str) -> ComprobanteExtraido:
    if respuesta.stop_reason == "refusal":
        raise RespuestaRechazadaError("El modelo Claude rechazó procesar este comprobante.")

    advertencias: list[str] = []
    truncado = respuesta.stop_reason == "max_tokens"
    if truncado:
        advertencias.append(
            "La respuesta del modelo se truncó por max_tokens; los datos pueden estar incompletos."
        )

    texto = next((bloque.text for bloque in respuesta.content if bloque.type == "text"), None)
    if texto is None:
        raise ErrorModeloClaude("La respuesta del modelo no incluyó ningún bloque de texto.")

    try:
        datos = json.loads(texto)
    except json.JSONDecodeError as exc:
        # Con structured outputs esto no debería pasar salvo que la respuesta
        # se haya cortado a mitad de camino (max_tokens) — se distingue el
        # caso para que quien orqueste sepa si el remedio es reintentar con
        # más tokens o si hay un problema real con la respuesta del modelo.
        if truncado:
            raise ErrorModeloClaude(
                "La respuesta del modelo se truncó por max_tokens y el JSON quedó incompleto, no se pudo "
                f"parsear. Detalle: {exc}"
            ) from exc
        raise ErrorModeloClaude(
            f"El modelo devolvió JSON inválido pese al structured output. Detalle: {exc}"
        ) from exc

    origen = "pdf" if tipo == "pdf" else "foto"
    confianza_defecto = 0.8 if tipo == "pdf" else 0.6
    confianza = datos.get("confianza")
    if not isinstance(confianza, (int, float)):
        confianza = confianza_defecto

    items = [
        ItemExtraido(
            orden=item.get("orden", posicion),
            descripcion=item.get("descripcion") or "",
            cantidad=item.get("cantidad"),
            unidad=item.get("unidad"),
            precio_unitario=item.get("precio_unitario"),
            total_linea=item.get("total_linea"),
        )
        for posicion, item in enumerate(datos.get("items", []), start=1)
    ]

    return ComprobanteExtraido(
        origen=origen,
        confianza=float(confianza),
        proveedor_ruc=datos.get("proveedor_ruc"),
        proveedor_razon_social=datos.get("proveedor_razon_social"),
        cliente_ruc=datos.get("cliente_ruc"),
        cliente_razon_social=datos.get("cliente_razon_social"),
        tipo_documento=datos.get("tipo_documento"),
        serie_numero=datos.get("serie_numero"),
        fecha_emision=datos.get("fecha_emision"),
        fecha_vencimiento=datos.get("fecha_vencimiento"),
        condicion=datos.get("condicion"),
        moneda=datos.get("moneda") or "PEN",
        tipo_cambio=datos.get("tipo_cambio"),
        subtotal=datos.get("subtotal"),
        igv=datos.get("igv"),
        icbper=datos.get("icbper"),
        descuento_global=datos.get("descuento_global"),
        total=datos.get("total"),
        detraccion_pct=datos.get("detraccion_pct"),
        detraccion_monto=datos.get("detraccion_monto"),
        detraccion_codigo=datos.get("detraccion_codigo"),
        retencion=datos.get("retencion"),
        documento_referencia=datos.get("documento_referencia"),
        items=items,
        advertencias=advertencias,
    )
