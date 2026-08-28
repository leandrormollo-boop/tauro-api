import base64
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import dhl_certificacion_sandbox as cert  # noqa: E402


DATOS = {
    "shipper": {
        "nombre": "TAURO Test", "empresa": "TAURO Test",
        "telefono": "1111111111", "email": "test@example.com",
        "calle": "Calle 1", "ciudad": "Buenos Aires",
        "estado": "C", "zip": "1000", "pais": "AR",
        "documento": "20111111112",
    },
    "recipient": {
        "nombre": "DHL Test", "empresa": "DHL Test",
        "telefono": "1222222222", "email": "test@example.com",
        "calle": "1 Test Street", "ciudad": "Miami",
        "estado": "FL", "zip": "33131", "pais": "US",
    },
    "bultos": [{
        "peso_kg": 1, "largo": 10, "ancho": 10, "alto": 10,
        "unidades": 1, "unidades_aduana": 1,
        "valor_unitario_usd": 10, "descripcion_en": "Test merchandise",
        "hs_code": "42022100", "pais_origen": "AR",
    }],
    "tax_paga": "DESTINATARIO",
}


class _Respuesta:
    status_code = 201
    text = "ok"

    def json(self):
        pdf = base64.b64encode(b"%PDF-1.4\ncertificacion").decode("ascii")
        return {
            "shipmentTrackingNumber": "TEST123456",
            "documents": [
                {"typeCode": "label", "content": pdf},
                {"typeCode": "invoice", "content": pdf},
            ],
        }


class _Sesion:
    def post(self, url, json=None, **kwargs):
        assert "/mydhlapi/test/shipments" in url
        assert kwargs["auth"] == ("usuario-test", "secreto-test")
        return _Respuesta()


def _configurar_sandbox(monkeypatch):
    monkeypatch.setenv("DHL_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("DHL_API_KEY", "usuario-test")
    monkeypatch.setenv("DHL_API_SECRET", "secreto-test")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_EXPO", "123456789")


def test_genera_paquete_completo_sin_exponer_credenciales(monkeypatch, tmp_path):
    _configurar_sandbox(monkeypatch)
    monkeypatch.setattr(cert.requests, "Session", _Sesion)

    paquete = cert.generar_paquete(DATOS, tmp_path)

    with zipfile.ZipFile(paquete) as contenido:
        assert set(contenido.namelist()) == {
            "01_codigo_enviado.json",
            "02_respuesta_dhl.json",
            "03_guia_dhl.pdf",
            "04_factura_comercial_dhl.pdf",
            "05_resumen.json",
        }
        codigo = json.loads(contenido.read("01_codigo_enviado.json"))
        serializado = json.dumps(codigo)
        assert "usuario-test" not in serializado
        assert "secreto-test" not in serializado
        assert codigo["body"]["valueAddedServices"] == [{"serviceCode": "PV"}]
        assert codigo["body"]["outputImageProperties"]["imageOptions"][1][
            "typeCode"
        ] == "invoice"
        assert contenido.read("03_guia_dhl.pdf").startswith(b"%PDF")
        assert contenido.read("04_factura_comercial_dhl.pdf").startswith(b"%PDF")


def test_certificacion_bloquea_produccion(monkeypatch, tmp_path):
    monkeypatch.setenv("DHL_ENVIRONMENT", "production")
    monkeypatch.setenv("DHL_API_KEY", "usuario-test")
    monkeypatch.setenv("DHL_API_SECRET", "secreto-test")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_EXPO", "123456789")

    with pytest.raises(RuntimeError, match="nunca emite en producción"):
        cert.generar_paquete(DATOS, tmp_path)
    assert list(tmp_path.iterdir()) == []
