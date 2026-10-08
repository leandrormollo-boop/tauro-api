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


def test_portal_resume_el_total_y_mantiene_el_desglose_en_el_detalle():
    listado = (ROOT / "templates" / "portal" / "envios.html").read_text(
        encoding="utf-8"
    )
    detalle = (ROOT / "templates" / "portal" / "envio_detalle.html").read_text(
        encoding="utf-8"
    )
    consultas = (ROOT / "servicios" / "solicitudes_guia.py").read_text(
        encoding="utf-8"
    )

    # El #41 rediseñó la lista: el total registrado del envío es el costo final.
    assert "Total registrado · ARS" not in listado
    assert "Precio del envío (ARS)" in listado
    assert "Ver desglose" in listado
    assert "envio-price-extra diferencia" in listado
    assert "envio-price-extra tax" in listado
    assert 'id="costo-del-envio"' in detalle
    assert "Costo inicial" in detalle
    assert "Costo adicional de flete" in detalle
    assert "Impuesto adicional del envío" in detalle
    assert "Saldo final del envío" in detalle
    assert consultas.count("AS diferencia_flete_ars") == 3
    assert consultas.count("AS tax_cliente_ars") == 3


def test_cuenta_muestra_tax_como_fila_independiente_y_la_diferencia_en_el_flete():
    servicio = (ROOT / "servicios" / "cuenta_corriente.py").read_text(
        encoding="utf-8"
    )
    plantilla = (ROOT / "templates" / "portal" / "cuenta_movimientos.html").read_text(
        encoding="utf-8"
    )

    assert "'TAX', 'TAX', c.tax_cliente_ars" in servicio
    # La diferencia desglosada va en la fila del flete; sólo queda una fila
    # aparte para lo que la conciliación no desglosa (residuo).
    assert servicio.count("'DIFERENCIA', 'Diferencia de envío'") == 1
    assert "la conciliación no" in servicio
    assert "e.monto_ars + COALESCE(dif.diferencia_ars, 0) AS debe_ars" in servicio
    assert "tipo IN ('FC', 'PENDIENTE_FACTURA') AND diferencia_detalle IS NOT NULL" in servicio
    assert "tipo IN ('DIFERENCIA', 'TAX')" in servicio
    assert "m.tipo == 'TAX'" in plantilla
    assert ">TAX</span>" in plantilla
    assert "Con diferencia" in plantilla
    assert "peso facturado por el courier" in plantilla


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
    assert "dejá estos campos vacíos para no duplicarlo" in html
