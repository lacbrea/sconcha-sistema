"""Tests de extractores/xml_ubl.py contra XML UBL 2.1 sintéticos de SUNAT.

Los XML se arman a mano como cadenas dentro de este mismo archivo (no se leen
de disco ni se llama a ninguna API). Cubren: factura con detracción, ICBPER y
3 ítems con distintos unitCode; nota de crédito; XML malformado; y el mismo
XML de factura entregado dentro de un .zip (como lo manda SUNAT), junto con
una CDR que debe ignorarse.
"""
import pathlib
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from extractores import xml_ubl

FACTURA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F001-123</cbc:ID>
  <cbc:IssueDate>2026-07-15</cbc:IssueDate>
  <cbc:DueDate>2026-08-14</cbc:DueDate>
  <cbc:InvoiceTypeCode>01</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>PEN</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID>20111111111</cbc:ID>
      </cac:PartyIdentification>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>DISTRIBUIDORA SAC</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID>20612506036</cbc:ID>
      </cac:PartyIdentification>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>INSTITUCION CEVICHERA S.A.C.</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>
  <cac:PaymentMeans>
    <cbc:ID>Detraccion</cbc:ID>
    <cbc:PaymentMeansCode>037</cbc:PaymentMeansCode>
  </cac:PaymentMeans>
  <cac:PaymentTerms>
    <cbc:ID>FormaPago</cbc:ID>
    <cbc:PaymentMeansID>Contado</cbc:PaymentMeansID>
  </cac:PaymentTerms>
  <cac:PaymentTerms>
    <cbc:ID>Detraccion</cbc:ID>
    <cbc:PaymentMeansID>Deposito en cuenta</cbc:PaymentMeansID>
    <cbc:PaymentPercent>12.00</cbc:PaymentPercent>
    <cbc:Amount currencyID="PEN">34.96</cbc:Amount>
  </cac:PaymentTerms>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="PEN">45.29</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxAmount currencyID="PEN">44.29</cbc:TaxAmount>
      <cac:TaxCategory>
        <cac:TaxScheme>
          <cbc:ID>1000</cbc:ID>
          <cbc:Name>IGV</cbc:Name>
        </cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
    <cac:TaxSubtotal>
      <cbc:TaxAmount currencyID="PEN">1.00</cbc:TaxAmount>
      <cac:TaxCategory>
        <cac:TaxScheme>
          <cbc:ID>7152</cbc:ID>
          <cbc:Name>ICBPER</cbc:Name>
        </cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="PEN">246.08</cbc:LineExtensionAmount>
    <cbc:TaxInclusiveAmount currencyID="PEN">291.37</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="PEN">291.37</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="KGM">13.608</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="PEN">136.08</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Description>SALSA HOISIN 2.268KG CJ*6</cbc:Description>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="PEN">10.00</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
  <cac:InvoiceLine>
    <cbc:ID>2</cbc:ID>
    <cbc:InvoicedQuantity unitCode="NIU">5</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="PEN">20.00</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Description>SERVILLETAS PAQUETE X50</cbc:Description>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="PEN">4.00</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
  <cac:InvoiceLine>
    <cbc:ID>3</cbc:ID>
    <cbc:InvoicedQuantity unitCode="BX">2</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="PEN">90.00</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Description>CAJA DE ENVASES DESCARTABLES</cbc:Description>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="PEN">45.00</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
</Invoice>
"""

NOTA_CREDITO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
            xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
            xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>FC01-45</cbc:ID>
  <cbc:IssueDate>2026-07-20</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>PEN</cbc:DocumentCurrencyCode>
  <cac:BillingReference>
    <cac:InvoiceDocumentReference>
      <cbc:ID>F001-100</cbc:ID>
    </cac:InvoiceDocumentReference>
  </cac:BillingReference>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification><cbc:ID>20111111111</cbc:ID></cac:PartyIdentification>
      <cac:PartyLegalEntity><cbc:RegistrationName>DISTRIBUIDORA SAC</cbc:RegistrationName></cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyIdentification><cbc:ID>20612506036</cbc:ID></cac:PartyIdentification>
      <cac:PartyLegalEntity><cbc:RegistrationName>INSTITUCION CEVICHERA S.A.C.</cbc:RegistrationName></cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="PEN">50.00</cbc:LineExtensionAmount>
    <cbc:PayableAmount currencyID="PEN">59.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:TaxTotal>
    <cac:TaxSubtotal>
      <cbc:TaxAmount currencyID="PEN">9.00</cbc:TaxAmount>
      <cac:TaxCategory><cac:TaxScheme><cbc:ID>1000</cbc:ID></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:CreditNoteLine>
    <cbc:ID>1</cbc:ID>
    <cbc:CreditedQuantity unitCode="NIU">1</cbc:CreditedQuantity>
    <cbc:LineExtensionAmount currencyID="PEN">50.00</cbc:LineExtensionAmount>
    <cac:Item><cbc:Description>DEVOLUCION PRODUCTO X</cbc:Description></cac:Item>
  </cac:CreditNoteLine>
</CreditNote>
"""

XML_MALFORMADO = "<Invoice><cbc:ID>F001-999</cbc:ID><cac:AccountingSupplierParty>"


# --- factura -----------------------------------------------------------------

def test_extraer_factura_completa(tmp_path):
    ruta = tmp_path / "factura.xml"
    ruta.write_text(FACTURA_XML, encoding="utf-8")

    comp = xml_ubl.extraer(ruta)

    assert comp.origen == "xml"
    assert comp.confianza == 1.0
    assert comp.serie_numero == "F001-123"
    assert comp.fecha_emision == "2026-07-15"
    assert comp.fecha_vencimiento == "2026-08-14"
    assert comp.moneda == "PEN"
    assert comp.tipo_documento == "factura"
    assert comp.proveedor_ruc == "20111111111"
    assert comp.proveedor_razon_social == "DISTRIBUIDORA SAC"
    assert comp.cliente_ruc == "20612506036"
    assert comp.cliente_razon_social == "INSTITUCION CEVICHERA S.A.C."
    assert comp.subtotal == 246.08
    assert comp.igv == 44.29
    assert comp.icbper == 1.00
    assert comp.total == 291.37
    assert comp.condicion == "contado"
    assert comp.detraccion_pct == 12.00
    assert comp.detraccion_monto == 34.96
    assert comp.detraccion_codigo == "037"
    assert comp.advertencias == []


def test_extraer_factura_items_con_distintas_unidades(tmp_path):
    ruta = tmp_path / "factura.xml"
    ruta.write_text(FACTURA_XML, encoding="utf-8")

    comp = xml_ubl.extraer(ruta)

    assert len(comp.items) == 3

    item_kg = comp.items[0]
    assert item_kg.orden == 1
    assert item_kg.descripcion == "SALSA HOISIN 2.268KG CJ*6"
    assert item_kg.cantidad == 13.608
    assert item_kg.unidad == "kg"
    assert item_kg.precio_unitario == 10.00
    assert item_kg.total_linea == 136.08

    item_unid = comp.items[1]
    assert item_unid.cantidad == 5.0
    assert item_unid.unidad == "unid"  # NIU -> unid

    item_caja = comp.items[2]
    assert item_caja.cantidad == 2.0
    assert item_caja.unidad == "caja"  # BX -> caja


def test_extraer_maneja_bom_utf8(tmp_path):
    ruta = tmp_path / "factura_bom.xml"
    ruta.write_bytes(b"\xef\xbb\xbf" + FACTURA_XML.encode("utf-8"))

    comp = xml_ubl.extraer(ruta)

    assert comp.serie_numero == "F001-123"
    assert comp.total == 291.37


# --- nota de crédito -----------------------------------------------------------------

def test_extraer_nota_credito(tmp_path):
    ruta = tmp_path / "nc.xml"
    ruta.write_text(NOTA_CREDITO_XML, encoding="utf-8")

    comp = xml_ubl.extraer(ruta)

    assert comp.tipo_documento == "nota_credito"
    assert comp.documento_referencia == "F001-100"
    assert comp.serie_numero == "FC01-45"
    assert comp.total == 59.00
    assert len(comp.items) == 1
    assert comp.items[0].unidad == "unid"
    assert comp.items[0].cantidad == 1.0


# --- XML malformado -----------------------------------------------------------------

def test_extraer_xml_malformado_no_lanza_excepcion(tmp_path):
    ruta = tmp_path / "roto.xml"
    ruta.write_text(XML_MALFORMADO, encoding="utf-8")

    comp = xml_ubl.extraer(ruta)

    assert comp.origen == "xml"
    assert comp.confianza == 1.0
    assert comp.total is None
    assert len(comp.advertencias) > 0


def test_extraer_archivo_inexistente_no_lanza_excepcion(tmp_path):
    ruta = tmp_path / "no_existe.xml"

    comp = xml_ubl.extraer(ruta)

    assert comp.origen == "xml"
    assert len(comp.advertencias) > 0


# --- .zip -----------------------------------------------------------------

def test_extraer_desde_zip_ignora_la_cdr(tmp_path):
    ruta_zip = tmp_path / "20111111111-01-F001-123.zip"
    with zipfile.ZipFile(ruta_zip, "w") as zf:
        # La CDR (constancia de recepción) empieza con "R-" y no es el comprobante.
        zf.writestr("R-20111111111-01-F001-123.xml", "<ApplicationResponse>no soy el comprobante</ApplicationResponse>")
        zf.writestr("20111111111-01-F001-123.xml", FACTURA_XML)

    comp = xml_ubl.extraer(ruta_zip)

    assert comp.serie_numero == "F001-123"
    assert comp.total == 291.37
    assert comp.proveedor_ruc == "20111111111"


def test_extraer_desde_zip_sin_xml_de_comprobante(tmp_path):
    ruta_zip = tmp_path / "vacio.zip"
    with zipfile.ZipFile(ruta_zip, "w") as zf:
        zf.writestr("R-20111111111-01-F001-123.xml", "<ApplicationResponse/>")

    comp = xml_ubl.extraer(ruta_zip)

    assert comp.origen == "xml"
    assert len(comp.advertencias) > 0
    assert comp.total is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
