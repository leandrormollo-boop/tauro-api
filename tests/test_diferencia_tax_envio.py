"""La diferencia y el TAX viven dentro del mismo envío, sin doble cargo."""

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from endpoints import admin
from servicios import conciliacion_couriers, cuenta_corriente


ROOT = Path(__file__).resolve().parents[1]


def test_schema_separa_diferencia_de_flete_y_tax_sin_cambiar_el_total():
    schema = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")

    assert "diferencia_flete_ars" in schema
    assert "tax_cliente_ars" in schema
    assert "ck_conciliacion_componentes" in schema
    assert "ajuste_cliente_ars\n            - diferencia_flete_ars\n            - tax_cliente_ars" in schema


def test_portal_muestra_inicial_diferencia_tax_y_total_en_el_mismo_envio():
    listado = (ROOT / "templates" / "portal" / "envios.html").read_text(
        encoding="utf-8"
    )
    detalle = (ROOT / "templates" / "portal" / "envio_detalle.html").read_text(
        encoding="utf-8"
    )
    consultas = (ROOT / "servicios" / "solicitudes_guia.py").read_text(
        encoding="utf-8"
    )

    assert "Precio del envío" in listado
    assert "envio-price-extra diferencia" in listado
    assert "envio-price-extra tax" in listado
    assert "Total final" in listado
    assert "Costo adicional de flete" in detalle
    assert "Impuesto adicional del envío" in detalle
    assert consultas.count("AS diferencia_flete_ars") == 3
    assert consultas.count("AS tax_cliente_ars") == 3


def test_casillero_tax_admin_agrega_linea_impuesto_al_tracking(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)

    async def leer_pdf(_archivo):
        return b"%PDF-1.4 evidencia"

    monkeypatch.setattr(cuenta_corriente, "leer_comprobante_con_tope", leer_pdf)
    monkeypatch.setattr(
        conciliacion_couriers,
        "parsear_lineas_factura_texto",
        lambda *_a, **_k: [{
            "linea_numero": 1,
            "tracking": "ABC123",
            "importe": Decimal("10000"),
            "moneda": "ARS",
            "tipo_cambio_ars": Decimal("1"),
            "concepto_tipo": "FLETE",
        }],
    )
    registradas = []
    monkeypatch.setattr(
        conciliacion_couriers,
        "registrar_factura_courier",
        lambda **datos: registradas.append(datos) or {"id": 91},
    )
    monkeypatch.setattr(
        conciliacion_couriers, "matchear_items_exactos", lambda *_a, **_k: {}
    )

    class Request:
        async def form(self):
            return {
                "courier": "DHL",
                "tipo_documento": "FC",
                "numero": "FC-91",
                "moneda": "ARS",
                "tipo_cambio_ars": "1",
                "subtotal": "10.000",
                "impuestos": "1.250,50",
                "total": "11.250,50",
                "lineas": "ABC123;10000;FLETE",
                "tax_tracking": "ABC123",
                "tax_importe": "1.250,50",
                "archivo_pdf": SimpleNamespace(filename="fc-91.pdf"),
            }

    respuesta = asyncio.run(
        admin.admin_factura_courier_post(Request(), admin_token="ok")
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].endswith("/facturas/91?ok=cargada")
    assert len(registradas) == 1
    tax = registradas[0]["items"][1]
    assert tax["linea_numero"] == 2
    assert tax["tracking"] == "ABC123"
    assert tax["importe"] == Decimal("1250.50")
    assert tax["concepto_tipo"] == "IMPUESTO"
    assert tax["datos_crudos"]["origen"] == "casillero_tax_admin"


def test_formulario_admin_exhibe_casilleros_tax_y_evitar_duplicados():
    html = (ROOT / "templates" / "admin" / "factura_courier_form.html").read_text(
        encoding="utf-8"
    )

    assert 'name="tax_tracking"' in html
    assert 'name="tax_importe"' in html
    assert "dejá estos casilleros vacíos" in html
