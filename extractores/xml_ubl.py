"""Parser determinista de comprobantes electrónicos SUNAT en formato UBL 2.1.

Es la vía preferente frente al modelo Claude: SUNAT ya validó estos XML antes de
aceptarlos, así que leer los campos por XPath es exacto y no cuesta nada (vs. una
llamada al modelo). Por eso `origen='xml'` siempre trae `confianza=1.0`.

Filosofía defensiva: un comprobante real casi nunca trae todos los campos
opcionales (detracción, ICBPER, vencimiento, forma de pago...). Cada extracción
de campo está aislada — si una ruta XPath no existe o el archivo viene raro,
se agrega una advertencia y se sigue, nunca se lanza una excepción por un dato
faltante. Solo un XML realmente ilegible (mal formado, o un .zip sin XML)
devuelve un `ComprobanteExtraido` vacío con advertencias.
"""
from __future__ import annotations

import pathlib
import zipfile

from lxml import etree

from esquema import ComprobanteExtraido, ItemExtraido

NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"

_NSMAP = {"cbc": NS_CBC, "cac": NS_CAC}

# InvoiceTypeCode (catálogo 01 de SUNAT) solo aplica a la raíz Invoice; NC y ND
# se distinguen por su propia raíz, no por un código dentro del documento.
_TIPO_DOCUMENTO_POR_CODIGO = {
    "01": "factura",
    "03": "boleta",
}

_TIPO_DOCUMENTO_POR_RAIZ = {
    "CreditNote": "nota_credito",
    "DebitNote": "nota_debito",
}

# Catálogo 03 de SUNAT (unidades UN/ECE) reducido a nuestro propio vocabulario.
_UNIDADES_UNECE = {
    "KGM": "kg",
    "GRM": "g",
    "LTR": "L",
    "MLT": "mL",
    "NIU": "unid",
    "ZZ": "unid",
    "C62": "unid",
    "BX": "caja",
    "PK": "paq",
}

_LINEA_POR_RAIZ = {
    "Invoice": "cac:InvoiceLine",
    "CreditNote": "cac:CreditNoteLine",
    "DebitNote": "cac:DebitNoteLine",
}

_CANTIDAD_POR_RAIZ = {
    "Invoice": "cbc:InvoicedQuantity",
    "CreditNote": "cbc:CreditedQuantity",
    "DebitNote": "cbc:DebitedQuantity",
}

# Valores que son etiquetas de bloque (no códigos de detracción) y que hay que
# descartar cuando se busca el código de bien/servicio de forma tolerante.
_ETIQUETAS_NO_SON_CODIGO_DETRACCION = {"detraccion", "deposito en cuenta"}


def extraer(ruta: pathlib.Path) -> ComprobanteExtraido:
    """Parsea un XML UBL 2.1 de SUNAT (factura, boleta, NC o ND).

    Acepta tanto un `.xml` suelto como el `.zip` con el que SUNAT suele
    entregarlo (el zip trae también la CDR — constancia de recepción — cuyo
    nombre empieza con "R-"; esa no es el comprobante y se ignora).
    """
    advertencias: list[str] = []
    contenido = _leer_contenido_xml(ruta, advertencias)
    if contenido is None:
        return ComprobanteExtraido(origen="xml", confianza=1.0, advertencias=advertencias)

    try:
        root = etree.fromstring(contenido)
    except etree.XMLSyntaxError as exc:
        advertencias.append(f"XML malformado, no se pudo parsear: {exc}")
        return ComprobanteExtraido(origen="xml", confianza=1.0, advertencias=advertencias)

    raiz = etree.QName(root).localname
    if raiz not in _LINEA_POR_RAIZ:
        advertencias.append(f"Raíz XML desconocida para UBL SUNAT: '{raiz}'")
        return ComprobanteExtraido(origen="xml", confianza=1.0, advertencias=advertencias)

    comp = ComprobanteExtraido(origen="xml", confianza=1.0)

    comp.serie_numero = _texto(root, "cbc:ID", advertencias, "serie-número (cbc:ID)")
    comp.fecha_emision = _texto(root, "cbc:IssueDate", advertencias, "fecha de emisión (cbc:IssueDate)")
    comp.fecha_vencimiento = _texto(
        root, "cbc:DueDate", advertencias, "fecha de vencimiento (cbc:DueDate)", opcional=True
    )
    moneda = _texto(root, "cbc:DocumentCurrencyCode", advertencias, "moneda (cbc:DocumentCurrencyCode)")
    comp.moneda = moneda or "PEN"

    comp.tipo_documento = _tipo_documento(root, raiz, advertencias)

    if raiz in ("CreditNote", "DebitNote"):
        comp.documento_referencia = _texto(
            root,
            "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
            advertencias,
            "documento de referencia (BillingReference)",
        )

    comp.proveedor_ruc = _texto(
        root,
        "cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID",
        advertencias,
        "RUC del proveedor",
    )
    comp.proveedor_razon_social = _texto(
        root,
        "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName",
        advertencias,
        "razón social del proveedor",
    )

    comp.cliente_ruc = _texto(
        root,
        "cac:AccountingCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID",
        advertencias,
        "RUC del cliente",
    )
    comp.cliente_razon_social = _texto(
        root,
        "cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName",
        advertencias,
        "razón social del cliente",
    )

    comp.total = _numero(root, "cac:LegalMonetaryTotal/cbc:PayableAmount", advertencias, "total (PayableAmount)")
    comp.subtotal = _numero(
        root, "cac:LegalMonetaryTotal/cbc:LineExtensionAmount", advertencias, "subtotal (LineExtensionAmount)"
    )
    comp.descuento_global = _numero(
        root,
        "cac:LegalMonetaryTotal/cbc:AllowanceTotalAmount",
        advertencias,
        "descuento global (AllowanceTotalAmount)",
        opcional=True,
    )

    _extraer_impuestos(root, comp, advertencias)
    _extraer_condicion_y_detraccion(root, comp, advertencias)
    comp.items = _extraer_items(root, raiz, advertencias)

    comp.advertencias = advertencias
    return comp


def _leer_contenido_xml(ruta: pathlib.Path, advertencias: list[str]) -> bytes | None:
    ruta = pathlib.Path(ruta)
    try:
        if ruta.suffix.lower() == ".zip":
            with zipfile.ZipFile(ruta) as zf:
                nombre_xml = next(
                    (
                        nombre
                        for nombre in zf.namelist()
                        if nombre.lower().endswith(".xml")
                        and not pathlib.Path(nombre).name.upper().startswith("R-")
                    ),
                    None,
                )
                if nombre_xml is None:
                    advertencias.append("El .zip no contiene un XML de comprobante (solo CDR o vacío)")
                    return None
                contenido = zf.read(nombre_xml)
        else:
            contenido = ruta.read_bytes()
    except (OSError, zipfile.BadZipFile) as exc:
        advertencias.append(f"No se pudo leer el archivo '{ruta}': {exc}")
        return None

    # SUNAT a veces entrega el XML con BOM UTF-8 al inicio; lxml no lo acepta
    # como primer byte de un documento que declara encoding en su cabecera.
    if contenido.startswith(b"\xef\xbb\xbf"):
        contenido = contenido[3:]
    return contenido


def _texto(
    nodo, xpath: str, advertencias: list[str], descripcion: str, opcional: bool = False
) -> str | None:
    """Busca un valor de texto por XPath. Nunca lanza: registra advertencia si falta."""
    try:
        encontrados = nodo.xpath(xpath, namespaces=_NSMAP)
    except etree.XPathEvalError as exc:
        advertencias.append(f"XPath inválido para {descripcion}: {exc}")
        return None
    if not encontrados:
        if not opcional:
            advertencias.append(f"No se encontró {descripcion}")
        return None
    valor = encontrados[0].text
    if valor is None or not valor.strip():
        if not opcional:
            advertencias.append(f"Campo vacío para {descripcion}")
        return None
    return valor.strip()


def _numero(
    nodo, xpath: str, advertencias: list[str], descripcion: str, opcional: bool = False
) -> float | None:
    texto = _texto(nodo, xpath, advertencias, descripcion, opcional=opcional)
    if texto is None:
        return None
    try:
        return float(texto)
    except ValueError:
        advertencias.append(f"Valor no numérico para {descripcion}: '{texto}'")
        return None


def _tipo_documento(root, raiz: str, advertencias: list[str]) -> str | None:
    if raiz != "Invoice":
        return _TIPO_DOCUMENTO_POR_RAIZ[raiz]
    codigo = _texto(root, "cbc:InvoiceTypeCode", advertencias, "tipo de documento (InvoiceTypeCode)")
    if codigo is None:
        return None
    codigo_base = codigo.strip()[:2]
    tipo = _TIPO_DOCUMENTO_POR_CODIGO.get(codigo_base)
    if tipo is None:
        advertencias.append(f"Código de tipo de documento no reconocido: '{codigo}'")
    return tipo


def _extraer_impuestos(root, comp: ComprobanteExtraido, advertencias: list[str]) -> None:
    subtotales = root.xpath("cac:TaxTotal/cac:TaxSubtotal", namespaces=_NSMAP)
    if not subtotales:
        advertencias.append("No se encontró desglose de impuestos (TaxTotal/TaxSubtotal)")
        return
    for subtotal in subtotales:
        codigo = _texto(
            subtotal,
            "cac:TaxCategory/cac:TaxScheme/cbc:ID",
            advertencias,
            "código de tributo (TaxScheme)",
            opcional=True,
        )
        monto = _numero(subtotal, "cbc:TaxAmount", advertencias, "monto de tributo", opcional=True)
        if codigo == "1000":
            comp.igv = monto
        elif codigo == "7152":
            comp.icbper = monto


def _extraer_condicion_y_detraccion(root, comp: ComprobanteExtraido, advertencias: list[str]) -> None:
    terminos = root.xpath("cac:PaymentTerms", namespaces=_NSMAP)
    if not terminos:
        advertencias.append("No se encontraron condiciones de pago (PaymentTerms)")
    for termino in terminos:
        id_termino = _texto(termino, "cbc:ID", advertencias, "ID de PaymentTerms", opcional=True)
        if id_termino == "FormaPago":
            valor = _texto(termino, "cbc:PaymentMeansID", advertencias, "forma de pago", opcional=True)
            if valor:
                comp.condicion = _normalizar_condicion(valor)
        elif id_termino == "Detraccion":
            comp.detraccion_pct = _numero(
                termino, "cbc:PaymentPercent", advertencias, "porcentaje de detracción", opcional=True
            )
            comp.detraccion_monto = _numero(
                termino, "cbc:Amount", advertencias, "monto de detracción", opcional=True
            )

    if comp.detraccion_pct is not None or comp.detraccion_monto is not None:
        comp.detraccion_codigo = _buscar_codigo_detraccion(root, advertencias)


def _normalizar_condicion(valor: str) -> str:
    normalizado = valor.strip().lower()
    if normalizado.startswith("cred"):
        return "credito"
    if normalizado.startswith("cont"):
        return "contado"
    return normalizado


def _buscar_codigo_detraccion(root, advertencias: list[str]) -> str | None:
    """Busca el código del bien/servicio sujeto a detracción de forma tolerante.

    No todos los proveedores de facturación electrónica lo ubican en el mismo
    sitio dentro de `cac:PaymentMeans`. Se prueban las rutas más comunes en
    orden; conviene validar esto contra un XML real de SUNAT con detracción.
    """
    candidatos = [
        "cac:PaymentMeans[cbc:ID='Detraccion']/cbc:PaymentMeansCode",
        "cac:PaymentMeans/cbc:PaymentMeansCode",
        "cac:PaymentMeans/cbc:ID",
        "cac:PaymentMeans/cbc:PaymentMeansID",
    ]
    for xpath in candidatos:
        valor = _texto(root, xpath, advertencias, "código de detracción", opcional=True)
        if valor and valor.strip().lower() not in _ETIQUETAS_NO_SON_CODIGO_DETRACCION:
            return valor
    advertencias.append("No se pudo determinar el código de detracción")
    return None


def _extraer_items(root, raiz: str, advertencias: list[str]) -> list[ItemExtraido]:
    xpath_linea = _LINEA_POR_RAIZ[raiz]
    xpath_cantidad = _CANTIDAD_POR_RAIZ[raiz]
    lineas = root.xpath(xpath_linea, namespaces=_NSMAP)
    if not lineas:
        advertencias.append(f"No se encontraron ítems ({xpath_linea})")
        return []

    items: list[ItemExtraido] = []
    for posicion, linea in enumerate(lineas, start=1):
        orden_texto = _texto(linea, "cbc:ID", advertencias, f"orden del ítem #{posicion}", opcional=True)
        orden = posicion
        if orden_texto:
            try:
                orden = int(orden_texto)
            except ValueError:
                advertencias.append(
                    f"Orden de ítem no numérico: '{orden_texto}', se usa la posición {posicion}"
                )

        descripcion = (
            _texto(linea, "cac:Item/cbc:Description", advertencias, f"descripción del ítem #{posicion}")
            or ""
        )

        cantidad, unidad = _extraer_cantidad_y_unidad(linea, xpath_cantidad, posicion, advertencias)

        precio_unitario = _numero(
            linea,
            "cac:Price/cbc:PriceAmount",
            advertencias,
            f"precio unitario del ítem #{posicion}",
            opcional=True,
        )
        total_linea = _numero(
            linea, "cbc:LineExtensionAmount", advertencias, f"total de línea del ítem #{posicion}"
        )

        items.append(
            ItemExtraido(
                orden=orden,
                descripcion=descripcion,
                cantidad=cantidad,
                unidad=unidad,
                precio_unitario=precio_unitario,
                total_linea=total_linea,
            )
        )

    return items


def _extraer_cantidad_y_unidad(
    linea, xpath_cantidad: str, posicion: int, advertencias: list[str]
) -> tuple[float | None, str | None]:
    nodos = linea.xpath(xpath_cantidad, namespaces=_NSMAP)
    if not nodos:
        advertencias.append(f"No se encontró cantidad ({xpath_cantidad}) en ítem #{posicion}")
        return None, None

    nodo = nodos[0]
    cantidad = None
    texto_cantidad = (nodo.text or "").strip()
    if texto_cantidad:
        try:
            cantidad = float(texto_cantidad)
        except ValueError:
            advertencias.append(f"Cantidad no numérica en ítem #{posicion}: '{texto_cantidad}'")
    else:
        advertencias.append(f"Cantidad vacía en ítem #{posicion}")

    unidad = None
    codigo_unidad = nodo.get("unitCode")
    if codigo_unidad:
        unidad = _UNIDADES_UNECE.get(codigo_unidad, codigo_unidad.lower())
    else:
        advertencias.append(f"No se encontró unitCode en ítem #{posicion}")

    return cantidad, unidad
