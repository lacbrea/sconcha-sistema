"""Esquema de datos compartido para comprobantes extraídos (facturas, boletas, recibos SUNAT).

Este módulo es el contrato entre los extractores (`extractores.xml_ubl`,
`extractores.modelo`) y el resto del sistema (catálogo, registro en Sheets,
proceso principal). Otros módulos importan `ItemExtraido`, `ComprobanteExtraido`
y `ESQUEMA_JSON` — no cambiar nombres ni tipos sin coordinar con esos módulos.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

# Tolerancia para comparar importes en soles. Existe porque el redondeo de
# céntimos entre el total de la factura y la suma de sus líneas casi nunca da
# exacto (cada línea se redondea a 2 decimales por separado, el total no).
_TOLERANCIA_SOLES = 0.10


@dataclass
class ItemExtraido:
    orden: int
    descripcion: str
    cantidad: float | None = None
    unidad: str | None = None          # 'kg'|'g'|'L'|'mL'|'unid'|'paq'|'caja'|None
    precio_unitario: float | None = None
    total_linea: float | None = None


@dataclass
class ComprobanteExtraido:
    origen: str                        # 'xml' | 'pdf' | 'foto'
    confianza: float                   # 0.0..1.0 ; xml siempre 1.0
    proveedor_ruc: str | None = None
    proveedor_razon_social: str | None = None
    cliente_ruc: str | None = None
    cliente_razon_social: str | None = None
    tipo_documento: str | None = None  # factura|boleta|recibo_honorarios|recibo_servicio|nota_credito|nota_debito|guia_remision|otro
    serie_numero: str | None = None
    fecha_emision: str | None = None   # 'YYYY-MM-DD'
    fecha_vencimiento: str | None = None
    condicion: str | None = None       # 'contado'|'credito'
    moneda: str = 'PEN'                # 'PEN'|'USD'
    tipo_cambio: float | None = None
    subtotal: float | None = None
    igv: float | None = None
    icbper: float | None = None
    descuento_global: float | None = None
    total: float | None = None
    detraccion_pct: float | None = None
    detraccion_monto: float | None = None
    detraccion_codigo: str | None = None
    retencion: float | None = None
    documento_referencia: str | None = None
    items: list[ItemExtraido] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)

    def clave(self) -> str:
        """Clave de deduplicación entre comprobantes que puedan repetirse.

        Se arma con RUC + serie-número + total porque es lo único que dos
        capturas distintas del mismo comprobante (ej. el XML de SUNAT y una
        foto tomada por WhatsApp del mismo papel) van a tener idéntico. Si
        falta el RUC, la serie o el total, no hay forma confiable de comparar
        contra otro comprobante — en ese caso se devuelve una clave única
        (con un UUID) para que dos comprobantes incompletos NUNCA se traten
        como duplicados entre sí solo por tener huecos parecidos.
        """
        ruc = (self.proveedor_ruc or '').strip().upper().replace(' ', '')
        serie = (self.serie_numero or '').strip().upper().replace(' ', '')
        if not ruc or not serie or self.total is None:
            return f"SIN_CLAVE|{uuid.uuid4().hex}"
        return f"{ruc}|{serie}|{round(self.total, 2):.2f}"

    def validar(self) -> list[str]:
        """Devuelve una lista de advertencias; nunca lanza excepción.

        Pensado para correr después de cualquier extractor (determinista o
        por modelo) y antes de grabar en el registro: son las señales que le
        dicen a un humano "revisa este comprobante antes de confiar en él".
        """
        advertencias: list[str] = []

        if self.total is None:
            advertencias.append("Falta el total del comprobante")

        if self.items:
            totales_linea = [item.total_linea for item in self.items if item.total_linea is not None]
            if totales_linea and self.subtotal is not None:
                suma_items = sum(totales_linea)
                if not _valores_cercanos(suma_items, self.subtotal):
                    advertencias.append(
                        f"La suma de los ítems (S/ {round(suma_items, 2)}) no cuadra con el "
                        f"subtotal (S/ {round(self.subtotal, 2)})"
                    )

        if self.proveedor_ruc is not None and not _ruc_valido(self.proveedor_ruc):
            advertencias.append(f"El RUC del proveedor '{self.proveedor_ruc}' no tiene 11 dígitos")

        if self.cliente_ruc is not None and not _ruc_valido(self.cliente_ruc):
            advertencias.append(f"El RUC del cliente '{self.cliente_ruc}' no tiene 11 dígitos")

        if self.fecha_emision is not None and not _fecha_valida(self.fecha_emision):
            advertencias.append(f"La fecha de emisión '{self.fecha_emision}' no tiene formato YYYY-MM-DD")

        if self.fecha_vencimiento is not None and not _fecha_valida(self.fecha_vencimiento):
            advertencias.append(f"La fecha de vencimiento '{self.fecha_vencimiento}' no tiene formato YYYY-MM-DD")

        if self.detraccion_pct is not None:
            if self.detraccion_monto is None:
                advertencias.append("Hay porcentaje de detracción pero falta el monto de detracción")
            elif self.total is not None:
                esperado = self.total * self.detraccion_pct / 100
                if not _valores_cercanos(self.detraccion_monto, esperado):
                    advertencias.append(
                        f"El monto de detracción (S/ {round(self.detraccion_monto, 2)}) no coincide "
                        f"con el {self.detraccion_pct}% del total (S/ {round(esperado, 2)})"
                    )

        return advertencias


def _valores_cercanos(a: float, b: float, tolerancia: float = _TOLERANCIA_SOLES) -> bool:
    """Compara dos importes con tolerancia, siempre sobre valores ya redondeados.

    Nunca comparar floats crudos con `==`: 2348.76 y 2348.75999999998 son el
    mismo importe en la práctica pero distintos en punto flotante.
    """
    return abs(round(a, 2) - round(b, 2)) <= tolerancia


def _ruc_valido(ruc: str) -> bool:
    ruc_limpio = ruc.strip()
    return ruc_limpio.isdigit() and len(ruc_limpio) == 11


def _fecha_valida(fecha: str) -> bool:
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# JSON Schema del comprobante para "structured outputs" de la API de Anthropic
# (ver extractores/modelo.py). Reglas del modo estricto: additionalProperties
# en false en todo objeto, y "required" debe listar TODAS las propiedades del
# objeto (incluidas las que pueden venir null) — no se admiten minimum,
# maximum, minLength ni esquemas recursivos.
_ITEM_JSON: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["orden", "descripcion", "cantidad", "unidad", "precio_unitario", "total_linea"],
    "properties": {
        "orden": {"type": "integer"},
        "descripcion": {"type": "string"},
        "cantidad": {"type": ["number", "null"]},
        "unidad": {"type": ["string", "null"]},
        "precio_unitario": {"type": ["number", "null"]},
        "total_linea": {"type": ["number", "null"]},
    },
}

ESQUEMA_JSON: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "proveedor_ruc",
        "proveedor_razon_social",
        "cliente_ruc",
        "cliente_razon_social",
        "tipo_documento",
        "serie_numero",
        "fecha_emision",
        "fecha_vencimiento",
        "condicion",
        "moneda",
        "tipo_cambio",
        "subtotal",
        "igv",
        "icbper",
        "descuento_global",
        "total",
        "detraccion_pct",
        "detraccion_monto",
        "detraccion_codigo",
        "retencion",
        "documento_referencia",
        "confianza",
        "items",
    ],
    "properties": {
        "proveedor_ruc": {"type": ["string", "null"]},
        "proveedor_razon_social": {"type": ["string", "null"]},
        "cliente_ruc": {"type": ["string", "null"]},
        "cliente_razon_social": {"type": ["string", "null"]},
        "tipo_documento": {"type": ["string", "null"]},
        "serie_numero": {"type": ["string", "null"]},
        "fecha_emision": {"type": ["string", "null"]},
        "fecha_vencimiento": {"type": ["string", "null"]},
        "condicion": {"type": ["string", "null"]},
        "moneda": {"type": "string"},
        "tipo_cambio": {"type": ["number", "null"]},
        "subtotal": {"type": ["number", "null"]},
        "igv": {"type": ["number", "null"]},
        "icbper": {"type": ["number", "null"]},
        "descuento_global": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"]},
        "detraccion_pct": {"type": ["number", "null"]},
        "detraccion_monto": {"type": ["number", "null"]},
        "detraccion_codigo": {"type": ["string", "null"]},
        "retencion": {"type": ["number", "null"]},
        "documento_referencia": {"type": ["string", "null"]},
        "confianza": {"type": "number"},
        "items": {"type": "array", "items": _ITEM_JSON},
    },
}
