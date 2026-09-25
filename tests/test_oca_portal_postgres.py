"""Portal OCA con PostgreSQL real aislado y transporte OCA simulado."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import pytest
from test_conciliacion_couriers_postgres import conciliacion_db, DATABASE_URL
from servicios import oca_portal as oca, solicitudes_guia as sg, cuenta_corriente as cc
from servicios import configuracion_couriers_cliente as permisos
from servicios.carrier_adapter import (
    QuoteResult,
    ShipmentResult,
    OperationState,
    validate_quote_request,
)
from servicios.oca_adapter import OCAConfig

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="requiere PostgreSQL aislado")


class Adapter:
    emissions = 0
    timeout = False
    pdf_fail = False
    cost = Decimal("100")

    def __init__(self, config, pricing_loader=None):
        pass

    def quote(self, request):
        validate_quote_request(request, "oca")
        return (
            QuoteResult(
                state=OperationState.COTIZADO,
                carrier_id="oca",
                quote_id="oca-fixture",
                carrier_cost=self.cost,
                customer_price=self.cost * Decimal("1.25"),
            ),
        )

    def create_shipment(self, *args, **kwargs):
        type(self).emissions += 1
        if self.timeout:
            raise TimeoutError()
        return ShipmentResult(
            state=OperationState.EMITIDO,
            carrier_id="oca",
            external_id="order:42",
            tracking="1217400000000333771",
        )

    def get_label(self, *args):
        if self.pdf_fail:
            raise TimeoutError()
        return ShipmentResult(
            state=OperationState.ETIQUETA_LISTA,
            carrier_id="oca",
            label_pdf=b"%PDF-fixture",
        )


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    from servicios import direcciones
    for module in (oca, sg, cc, permisos, direcciones):
        monkeypatch.setattr(module, "get_conn", conciliacion_db)
    monkeypatch.setattr(sg, "_avisar_tienda_origen", lambda *a: None)
    config = OCAConfig.from_env(
        {
            "OCA_ENVIRONMENT": "production",
            "OCA_CUIT": "20-12345678-6",
            "OCA_CUENTA": "123456/001",
            "OCA_OPERATIVA": "472095",
            "OCA_CENTRO_COSTO": "1",
            "OCA_USUARIO": "fixture@example.invalid",
            "OCA_PASSWORD": "fixture",
            "OCA_ORIGIN_MODE": "domicilio",
            "OCA_DESTINATION_MODE": "domicilio",
            "OCA_OPERATIVA_SEGURO_CONFIRMADO": "true",
            "OCA_CONFIRM_WITHDRAWAL": "true",
            "OCA_ADAPTER_ENABLED": "true",
            "OCA_UAT_APPROVED": "true",
            "OCA_PRODUCTION_APPROVED": "true",
            "OCA_FULFILLMENT_ENABLED": "true",
            "OCA_FULFILLMENT_UAT_APPROVED": "true",
        }
    )
    monkeypatch.setattr(oca, "config_productiva", lambda: config)
    monkeypatch.setattr(oca, "OCAAdapter", Adapter)
    Adapter.emissions = 0
    Adapter.timeout = False
    Adapter.pdf_fail = False
    Adapter.cost = Decimal("100")
    with conciliacion_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO clientes(cliente_id,nombre,email,activo) VALUES ('OCA-TEST','Prueba','qa@example.invalid',TRUE),('OTRO','Otro','otro@example.invalid',TRUE)"
        )
        cur.execute(
            "INSERT INTO cliente_courier_config(cliente_id,courier,puede_cotizar,puede_emitir,markup_tipo,markup_valor) VALUES ('OCA-TEST','oca',TRUE,TRUE,'PCT',25)"
        )
    return conciliacion_db


def datos():
    form = {
        "cantidad_bultos": "2",
        "peso_kg": "1",
        "largo_cm": "10",
        "ancho_cm": "10",
        "alto_cm": "10",
        "valor_declarado_ars": "1000",
        "destino_apellido": "Prueba",
    }
    for prefix in ("origen", "destino"):
        form.update(
            {
                prefix + "_provincia": "C",
                prefix + "_localidad": "Capital Federal",
                prefix + "_cp": "1211",
                prefix + "_calle": "Venezuela",
                prefix + "_numero": "3446",
                prefix + "_nombre": "TEST",
                prefix + "_email": "qa@example.invalid",
            }
        )
    return form


def nueva():
    return oca.confirmar(oca.cotizar("OCA-TEST", datos()), "OCA-TEST")


def test_circuito_y_doble_click_un_cargo_nacional(db):
    quote = oca.cotizar("OCA-TEST", datos())
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: oca.confirmar(quote, "OCA-TEST"), range(2)))
    assert ids[0] == ids[1]
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda _: sg.emitir_guia_como_cliente(ids[0], "OCA-TEST"), range(2)
            )
        )
    assert any(r.get("ok") for r in results)
    assert Adapter.emissions == 1
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM envios WHERE solicitud_id=%s", (ids[0],))
        rows = cur.fetchall()
        assert (
            len(rows) == 1
            and rows[0]["monto_ars"] == 125
            and rows[0]["ambito"] == "NACIONAL"
        )
        cur.execute(
            "SELECT * FROM envio_cotizacion_snapshots WHERE solicitud_id=%s", (ids[0],)
        )
        assert cur.fetchone()["costo_courier_estimado_ars"] == 100


def test_ajeno_sin_permiso_y_limite(db):
    ident = nueva()
    assert not sg.emitir_guia_como_cliente(ident, "OTRO")["ok"]
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE clientes SET tope_deuda_ars=100 WHERE cliente_id='OCA-TEST'"
        )
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE clientes SET tope_deuda_ars=NULL WHERE cliente_id='OCA-TEST'"
        )
        cur.execute(
            "UPDATE cliente_courier_config SET puede_emitir=FALSE WHERE cliente_id='OCA-TEST'"
        )
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    assert Adapter.emissions == 0


def test_timeout_bloquea_reemision(db):
    ident = nueva()
    Adapter.timeout = True
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    assert sg.obtener_solicitud(ident)["estado"] == "VERIFICAR_COURIER"
    assert Adapter.emissions == 1


def test_pdf_se_recupera_sin_emitir(db):
    ident = nueva()
    Adapter.pdf_fail = True
    assert sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    Adapter.pdf_fail = False
    assert oca.recuperar_etiqueta(sg.obtener_solicitud(ident))["ok"]
    assert Adapter.emissions == 1


def test_tarifa_vencida_y_cambio_costo(db):
    ident = nueva()
    Adapter.cost = Decimal("101")
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    Adapter.cost = Decimal("100")
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE oca_portal_cotizaciones SET expires_at=NOW()-INTERVAL '1 minute'"
        )
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    assert Adapter.emissions == 0


def test_no_hereda_pricing_internacional(db):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cliente_courier_config SET markup_tipo=NULL,markup_valor=NULL WHERE cliente_id='OCA-TEST'"
        )
    with pytest.raises(oca.OCAPortalError):
        oca.cotizar("OCA-TEST", datos())


def test_solicitud_inmutable_y_precio_manipulado(db):
    ident = nueva()
    with pytest.raises(ValueError):
        sg.editar_solicitud_pre_emision(ident, {"dest_direccion": "Otra 123"})
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE solicitudes_guia SET precio_tauro_ars=1 WHERE id=%s", (ident,)
        )
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    assert Adapter.emissions == 0


def test_http_portal_auth_propiedad_y_precio_privado(db, monkeypatch):
    import asyncio, httpx
    from fastapi import FastAPI
    from endpoints import portal_oca as ep
    from servicios import auth

    monkeypatch.setattr(auth, "get_conn", db)
    app = FastAPI()
    app.include_router(ep.router)
    from endpoints.portal_cliente import router as portal_router

    app.include_router(portal_router)

    @app.middleware("http")
    async def nonce(request, call_next):
        request.state.csp_nonce = "test"
        return await call_next(request)

    token = auth.generar_token("qa@example.invalid", "OCA-TEST")
    # Existing base-template helpers read the isolated DB too.
    from core import database

    monkeypatch.setattr(database, "get_conn", db)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/portal/oca/nuevo")).status_code == 303
            assert (await client.get("/portal/agenda")).status_code == 303
            client.cookies.set("token", token)
            r = await client.get("/portal/oca/nuevo")
            assert r.status_code == 200 and "Ver tarifa" in r.text
            r = await client.post(
                "/portal/oca/cotizar", data={**datos(), "precio_ars": "1"}
            )
            assert r.status_code == 303
            url = r.headers["location"]
            r = await client.get(url)
            assert r.status_code == 200 and "Guardar solicitud" in r.text
            assert "costo_ars" not in r.text and "config_hash" not in r.text
            edited = await client.get(url + "/editar")
            assert edited.status_code == 200
            from html.parser import HTMLParser
            fields = {}
            class Inputs(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    attrs = dict(attrs)
                    if tag == 'input' and 'name' in attrs:
                        fields[attrs['name']] = attrs.get('value', '')
            Inputs().feed(edited.text)
            for key in ('origen_nombre', 'origen_calle', 'destino_apellido', 'destino_cp'):
                assert fields[key] == datos()[key]
            assert 'costo_ars' not in edited.text and 'config_hash' not in edited.text
            a = await client.post(url + "/confirmar")
            b = await client.post(url + "/confirmar")
            assert a.headers["location"] == b.headers["location"]
            detail = await client.get(a.headers["location"])
            assert detail.status_code == 200 and "Emitir guía ahora" in detail.text
            client.cookies.set(
                "token", auth.generar_token("otro@example.invalid", "OTRO")
            )
            assert (await client.get(url)).status_code == 303
            assert (await client.get(url + "/editar")).status_code == 303

    asyncio.run(run())


def test_limite_con_dos_solicitudes_concurrentes(db):
    ids = [nueva(), nueva()]
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE clientes SET tope_deuda_ars=200 WHERE cliente_id='OCA-TEST'"
        )
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda ident: sg.emitir_guia_como_cliente(ident, "OCA-TEST"), ids)
        )
    assert sum(bool(r["ok"]) for r in results) == 1
    assert Adapter.emissions == 1


def test_cambio_config_y_cuenta_desactivada_no_emiten(db, monkeypatch):
    ident = nueva()
    config = oca.config_productiva()
    monkeypatch.setattr(
        oca, "config_productiva", lambda: replace(config, cost_center="2")
    )
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    monkeypatch.setattr(oca, "config_productiva", lambda: config)
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE clientes SET activo=FALSE WHERE cliente_id='OCA-TEST'")
    assert not sg.emitir_guia_como_cliente(ident, "OCA-TEST")["ok"]
    assert Adapter.emissions == 0
