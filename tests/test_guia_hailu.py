"""
LA GUÍA REAL DE HAILU (WAIMAO), punta a punta contra el formato de DHL.

Leandro (05/08) pasó una guía real y pidió: "probá cargando todo.
Comparala con la web de DHL — debe estar igual, el formato respetarlo."

  HAILU - MULTIMODA-DHL · 8 camisas · caja 48×47×20 · 3,9 kg
  Shipper: Yiwu Hailu Garment · JEFF JANG · YIWU 322009 · CN
           Tax ID 91330782MA2DCHET04
  Receiver: COMERCIO EXCELENTE NORTE SUR · Victoria Jacobo · MX
            ID CEN040218L96 · CP 11540 · MEXICO
  Invoice: 60% POLYESTER 40% COTTON · 8 pcs · tejido plano

Este test arma exactamente ese envío con el flujo del portal y verifica el
cuerpo que sale hacia POST /shipments de DHL: los mismos campos que pide su
web (companyName + fullName separados, registrationNumbers del shipper,
caja con sus medidas reales, descripción con composición textil).
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dhl_client import DHLClient  # noqa: E402
from servicios.api_b2b import _piezas_del_catalogo  # noqa: E402

CAJA_HAILU = {
    "peso_kg": 3.9, "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20,
    # Una caja física contiene ocho unidades comerciales. DHL debe recibir
    # 1 package, pero la invoice tiene que declarar 8 PCS a USD 15 cada una.
    "cantidad": 1, "unidades_aduana": 8,
    "descripcion_en": "8 SHIRTS 60% POLYESTER 40% COTTON WOVEN",
    "valor_unitario_usd": 15.0, "hs_code": "6205.30", "pais_origen": "CN",
}

SHIPPER = {
    "empresa": "Yiwu Hailu Garment", "nombre": "JEFF JANG",
    "telefono": "15057802211", "direccion": "YIWU CITY ZHEJIANG PROVINCE",
    "ciudad": "YIWU", "zip": "322009", "pais": "CN",
    "documento": "91330782MA2DCHET04",
}
RECEIVER = {
    "empresa": "COMERCIO EXCELENTE NORTE SUR", "nombre": "Victoria Jacobo",
    "telefono": "+525518661024",
    "direccion": "Calle Lafontaine #92, Col. Polanco II Seccion",
    "ciudad": "MEXICO", "estado": "Distrito Federal", "zip": "11540",
    "pais": "MX", "documento": "CEN040218L96",
}


def _cuerpo_dhl():
    datos = {"shipper": SHIPPER, "recipient": RECEIVER,
             "bultos": [CAJA_HAILU], "tax_paga": "DESTINATARIO"}
    resp = mock.Mock(status_code=201)
    resp.json.return_value = {"shipmentTrackingNumber": "111", "documents": []}
    with mock.patch.dict(os.environ, {
        "DHL_API_KEY": "k", "DHL_API_SECRET": "s",
        "DHL_ACCOUNT_NUMBER_EXPO": "741622792",
        "DHL_ACCOUNT_NUMBER_IMPO": "730089966"}, clear=True), \
         mock.patch("core.dhl_client.requests.post", return_value=resp) as post:
        DHLClient().create_shipment(datos)
    return post.call_args.kwargs["json"]


def test_el_portal_acepta_la_caja_tal_como_viene_en_la_guia():
    """Sin catálogo: 48×47×20, 3,9 kg, '8 camisas'. Como en Boxfly."""
    piezas, det, err = _piezas_del_catalogo("WAIMAO", [CAJA_HAILU])
    assert err is None, err
    assert piezas[0] == {"peso_kg": 3.9, "largo_cm": 48.0,
                         "ancho_cm": 47.0, "alto_cm": 20.0,
                         "valor_declarado_caja_usd": 120.0}
    assert det[0]["descripcion_en"].startswith("8 SHIRTS 60% POLYESTER")
    assert det[0]["cantidad"] == 1
    assert det[0]["unidades_aduana"] == 8


def test_empresa_y_contacto_van_separados_como_en_la_web_de_dhl():
    c = _cuerpo_dhl()
    sh = c["customerDetails"]["shipperDetails"]["contactInformation"]
    assert sh["companyName"] == "Yiwu Hailu Garment"
    assert sh["fullName"] == "JEFF JANG"
    rc = c["customerDetails"]["receiverDetails"]["contactInformation"]
    assert rc["companyName"] == "COMERCIO EXCELENTE NORTE SUR"
    assert rc["fullName"] == "Victoria Jacobo"


def test_el_tax_id_del_shipper_viaja_a_dhl():
    reg = c = _cuerpo_dhl()["customerDetails"]["shipperDetails"]["registrationNumbers"]
    assert reg[0]["number"] == "91330782MA2DCHET04"
    assert reg[0]["issuerCountryCode"] == "CN"


def test_la_caja_llega_con_sus_medidas_reales():
    pk = _cuerpo_dhl()["content"]["packages"]
    assert len(pk) == 1
    assert pk[0]["weight"] == 3.9
    assert pk[0]["dimensions"] == {"length": 48.0, "width": 47.0, "height": 20.0}


def test_la_invoice_lleva_composicion_pais_y_valor():
    contenido = _cuerpo_dhl()["content"]
    li = contenido["exportDeclaration"]["lineItems"][0]
    assert "60% POLYESTER 40% COTTON" in li["description"]
    assert li["manufacturerCountry"] == "CN"
    assert li["price"] == 15.0
    assert li["quantity"]["value"] == 8
    assert contenido["declaredValue"] == 120.0


def test_cn_a_mx_usa_la_cuenta_de_impo():
    """
    Regla de Leandro (05/08): "va por la cuenta de IMPO lo que es terceros
    países". EXPO es sólo para lo que SALE de Argentina; China → México va
    por la de importación.
    """
    assert _cuerpo_dhl()["accounts"][0]["number"] == "730089966"
