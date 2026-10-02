"""Asistente de invoice: extracción, lectura (modelo mockeado) y post-proceso.

Todas las fixtures son sintéticas y anónimas. Ningún test sale a la red.
"""

import io
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import ChainableUndefined

from endpoints import portal_cliente as pc
from servicios import invoice_asistente as ia


class _Respuesta:
    def __init__(self, data):
        self.output_text = json.dumps(data)
        self.id = "resp_test"


class ModeloFalso:
    """Imita client.responses.create y guarda lo que recibió."""

    def __init__(self, data):
        self.data = data
        self.llamadas = []
        self.responses = self

    def create(self, **kwargs):
        self.llamadas.append(kwargs)
        return _Respuesta(self.data)

    def texto_enviado(self):
        partes = self.llamadas[0]["input"][0]["content"]
        return "\n".join(p.get("text", "") for p in partes)


def _item(**extra):
    base = {
        "descripcion_original": "", "descripcion_en": "", "hs_code": None, "cantidad": None,
        "valor_unitario": None, "moneda": None, "pais_origen": None, "peso_neto_kg": None,
        "fuente_linea": "",
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _sin_ia(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("INVOICE_ASISTENTE_IA", raising=False)
    monkeypatch.delenv("INVOICE_ASISTENTE_ENABLED", raising=False)


def _xlsx(filas):
    import openpyxl

    libro = openpyxl.Workbook()
    hoja = libro.active
    hoja.title = "Packing"
    for fila in filas:
        hoja.append(fila)
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


# (1) Packing list xlsx leído por encabezados, sin IA.
def test_packing_list_xlsx_por_encabezados_sin_ia():
    contenido = _xlsx([
        ["PACKING LIST DEMO"],
        [],
        ["TIPOLOGIA", "SKU", "NOMBRE", "COLOR", "COMPOSICION", "CANTIDAD", "VALOR"],
        ["REMERA", "SKU-001", "Cotton knitted t-shirts", "Negro", "100% algodón", 12, 8.5],
        ["BUZO", "SKU-002", "Buzo frisa", "Gris", "80% algodón", 5, "19,90"],
        ["", "", "TOTAL", "", "", None, None],
    ])
    r = ia.leer_invoice("packing_demo.xlsx", contenido, "", "AR")
    assert r["fuente"] == "encabezados" and r["tipo"] == "xlsx"
    assert len(r["items"]) == 2
    remera, buzo = r["items"]
    assert remera["descripcion_en"] == "Cotton knitted t-shirts"
    assert (remera["cantidad"], remera["valor_unitario"], remera["valor_total"]) == (12, 8.5, 102.0)
    assert (buzo["cantidad"], buzo["valor_unitario"], buzo["valor_total"]) == (5, 19.9, 99.5)
    assert remera["hs_code"] == "6109.10" and remera["hs_origen"] == "sugerido"
    assert remera["pais_origen"] == "AR" and remera["pais_origen_tipo"] == "asumido"
    assert any("sin IA" in a for a in r["avisos"])


# (2) Texto de factura E argentina: coma decimal y redondeo a centavos.
FACTURA_E = """FACTURA E  N° 00001-00000099
Código  Producto  Cantidad  Precio Unit.  Subtotal
ARACA - Aros Acacia 3,000000 70,000000 210,00
BOLSA - Bolsa de tela 216,000000 0,001000 0,22
Total USD 210,22"""


def test_factura_e_texto_redondeo_y_total_documento():
    modelo = ModeloFalso({"total_declarado_documento": 210.22, "items": [
        _item(descripcion_original="ARACA - Aros Acacia", descripcion_en="Wooden earrings (acacia)",
              cantidad=3, valor_unitario=70, moneda="USD",
              fuente_linea="ARACA - Aros Acacia 3,000000 70,000000 210,00"),
        _item(descripcion_original="BOLSA - Bolsa de tela", descripcion_en="Fabric bags",
              cantidad=216, valor_unitario=0.001, moneda="USD",
              fuente_linea="BOLSA - Bolsa de tela 216,000000 0,001000 0,22"),
    ]})
    r = ia.leer_invoice(None, None, FACTURA_E, "AR", client=modelo)
    assert "ARACA - Aros Acacia 3,000000 70,000000 210,00" in modelo.texto_enviado()
    llamada = modelo.llamadas[0]
    assert llamada["text"]["format"]["strict"] is True and llamada["store"] is False
    aros, bolsa = r["items"]
    assert aros["valor_total"] == 210.0
    assert bolsa["valor_total"] == 0.22  # 216 × 0,001 = 0,216
    assert any("210,216" in a and "210,22" in a and "0,004" in a and "redondeo" in a for a in r["avisos"])


def test_numeros_argentinos_y_diferencia_exacta_del_documento():
    assert ia.parsear_numero("3,000000") == 3
    assert ia.parsear_numero("70,000000") == 70
    assert ia.parsear_numero("1.234,56") == ia.parsear_numero("1,234.56") == ia.parsear_numero("USD 1234.56")
    data = {"total_declarado_documento": 5825.84, "items": [
        _item(descripcion_en="Glass beads", hs_code="7018.10", cantidad=1, valor_unitario=5825.836)]}
    r = ia.procesar(data, "AR", "ia")
    assert any("5.825,836" in a and "5.825,84" in a and "por redondeo" in a for a in r["avisos"])


def test_texto_sin_ia_no_inventa_y_avisa():
    with pytest.raises(ia.AsistenteNoDisponible, match="cargá los artículos a mano"):
        ia.leer_invoice(None, None, FACTURA_E, "AR")


# (3) HS escrito en la fuente: se respeta tal cual.
def test_hs_escrito_se_respeta_con_modelo_y_sin_modelo():
    csv_demo = ("Description;Qty;Unit price;HS code;Country of origin\n"
                "Glass beads for jewellery;100;0,35;7018.10.00.00;CZ\n").encode()
    modelo = ModeloFalso({"total_declarado_documento": None, "items": [
        _item(descripcion_original="Glass beads for jewellery", descripcion_en="Glass beads for jewellery",
              hs_code="7018.10.00.00", cantidad=100, valor_unitario=0.35, moneda="USD", pais_origen="CZ")]})
    con_ia = ia.leer_invoice("invoice.csv", csv_demo, "", "AR", client=modelo)
    sin_ia = ia.leer_invoice("invoice.csv", csv_demo, "", "AR")
    for r in (con_ia, sin_ia):
        item = r["items"][0]
        assert item["hs_code"] == "7018.10.00.00" and item["hs_origen"] == "documento"
        assert item["pais_origen"] == "CZ" and item["pais_origen_tipo"] == "documento"
        assert item["valor_total"] == 35.0


# (4) Sin HS: sugerido por el buscador local y marcado.
def test_hs_faltante_se_sugiere_y_queda_marcado(monkeypatch):
    llamadas = []
    from servicios import hs_code

    original = hs_code.sugerir_hs

    def espia(descripcion, detalle=""):
        llamadas.append(descripcion)
        return original(descripcion, detalle)

    monkeypatch.setattr(hs_code, "sugerir_hs", espia)
    data = {"total_declarado_documento": None, "items": [
        _item(descripcion_en="Fishing reels", cantidad=2, valor_unitario=40, moneda="EUR"),
        _item(descripcion_en="", cantidad=0, valor_unitario=-1),
    ]}
    r = ia.procesar(data, "AR", "ia")
    reel, vacio = r["items"]
    assert llamadas == ["Fishing reels"]
    assert reel["hs_code"] == "9507.30" and reel["hs_origen"] == "sugerido"
    assert reel["pais_origen"] == "AR" and reel["pais_origen_tipo"] == "asumido"
    avisos = " ".join(r["avisos"])
    assert "EUR" in avisos and "No lo convertimos" in avisos
    assert "no tiene descripción" in avisos and "mayor a 0" in avisos
    assert vacio["hs_code"] is None


def test_descripcion_larga_y_cantidad_no_entera():
    data = {"total_declarado_documento": None, "items": [
        _item(descripcion_en="Handmade ceramic decorative bowls " * 4, hs_code="6913.90",
              cantidad=2.5, valor_unitario=10, moneda="USD", pais_origen="AR")]}
    r = ia.procesar(data, "AR", "ia")
    assert len(r["items"][0]["descripcion_en"]) <= 75
    avisos = " ".join(r["avisos"])
    assert "recortamos" in avisos and "no es un número entero" in avisos


# (5) Tipos no permitidos y archivos de más de 10 MB.
def test_tipo_no_permitido_y_archivo_grande():
    with pytest.raises(ia.InvoiceAsistenteError, match="Tipo de archivo no permitido"):
        ia.leer_invoice("contrato.docx", b"PK\x03\x04", "", "AR")
    with pytest.raises(ia.InvoiceAsistenteError, match="10 MB"):
        ia.leer_invoice("grande.csv", b"a" * (ia.MAX_BYTES + 1), "", "AR")
    with pytest.raises(ia.InvoiceAsistenteError, match="Subí un archivo o pegá"):
        ia.leer_invoice(None, None, "   ", "AR")


def test_imagen_va_al_modelo_con_vision():
    modelo = ModeloFalso({"total_declarado_documento": None, "items": []})
    ia.leer_invoice("foto.png", b"\x89PNG demo", "", "AR", client=modelo)
    partes = modelo.llamadas[0]["input"][0]["content"]
    assert any(p["type"] == "input_image" and p["image_url"].startswith("data:image/png;base64,") for p in partes)
    with pytest.raises(ia.AsistenteNoDisponible):
        ia.leer_invoice("foto.png", b"\x89PNG demo", "", "AR")


# Endpoint y template.
def _app():
    app = FastAPI()
    app.include_router(pc.router)
    app.dependency_overrides[pc.cliente_actual] = lambda: "DEMO"
    return app


def _render(monkeypatch):
    env = pc.templates.env.overlay(undefined=ChainableUndefined)
    env.globals.update(saldo_menu=lambda *a: None, ayuda=lambda: {},
                       pendientes_menu=lambda *a: {"envios": 0, "tienda": 0})

    class _Request:
        class state:
            csp_nonce = "n"
        url = type("U", (), {"path": "/portal/envios/nuevo"})()
        query_params = {}
        cookies = {}

    return env.get_template("portal/envio_nuevo.html").render(
        request=_Request(), ambito="internacional", filas=[{}], form={}, paises_destino=[("AR", "Argentina")])


# (6) Flag apagado: 404 y el bloque no se renderiza.
def test_flag_apagado_endpoint_404_y_sin_bloque(monkeypatch):
    with TestClient(_app()) as client:
        r = client.post("/portal/api/invoice/leer", data={"texto": FACTURA_E})
    assert r.status_code == 404
    html = _render(monkeypatch)
    assert "data-invoice-asistente" not in html and "invoice-asistente.js" not in html


def test_flag_encendido_bloque_en_cada_caja_y_endpoint(monkeypatch):
    monkeypatch.setenv("INVOICE_ASISTENTE_ENABLED", "true")
    monkeypatch.setattr(pc, "check_rate", lambda *a, **k: True)
    html = _render(monkeypatch)
    caja = html[html.index("shipment-invoice-card"):html.index('class="invoice-items"')]
    assert "Cargar invoice desde archivo o texto" in caja and "Leer invoice" in caja
    assert "invoice-asistente.js" in html
    with TestClient(_app()) as client:
        csv_demo = "Description,Qty,Unit price\nFishing reels,2,40\n".encode()
        ok = client.post("/portal/api/invoice/leer", data={"pais_origen_envio": "AR"},
                         files={"archivo": ("demo.csv", csv_demo, "text/csv")})
        malo = client.post("/portal/api/invoice/leer", files={"archivo": ("x.exe", b"MZ", "application/octet-stream")})
        sin_ia = client.post("/portal/api/invoice/leer", data={"texto": FACTURA_E})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "private, no-store"
    assert ok.json()["items"][0]["valor_total"] == 80.0
    assert malo.status_code == 422 and "no permitido" in malo.json()["error"]
    assert sin_ia.status_code == 503 and "a mano" in sin_ia.json()["error"]


def test_rate_limit_por_cliente(monkeypatch):
    monkeypatch.setenv("INVOICE_ASISTENTE_ENABLED", "true")
    claves = []
    monkeypatch.setattr(pc, "check_rate", lambda clave, **k: claves.append((clave, k)) or False)
    with TestClient(_app()) as client:
        r = client.post("/portal/api/invoice/leer", data={"texto": "x"})
    assert r.status_code == 429
    assert claves == [("invoice_leer:DEMO", {"max_attempts": 20, "window_seconds": 3600})]


def test_flag_por_defecto_apagado():
    assert os.getenv("INVOICE_ASISTENTE_ENABLED") is None
    assert ia.habilitado() is False
