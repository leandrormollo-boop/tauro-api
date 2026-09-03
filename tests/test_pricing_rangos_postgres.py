"""Release acceptance against an explicitly isolated PostgreSQL database."""
import importlib.util
import json
from pathlib import Path

import pytest
from starlette.requests import Request

from endpoints import admin
from servicios import pricing

SOURCE = Path(__file__).with_name("test_pagos_documentales_postgres.py")
SPEC = importlib.util.spec_from_file_location("rangos_db_fixture", SOURCE)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


@pytest.fixture
def db(monkeypatch):
    if not FIXTURE.DATABASE_URL:
        pytest.skip("requiere TAURO_TEST_DATABASE_URL aislada")
    yield from FIXTURE.cuenta_db.__wrapped__(monkeypatch)


def test_migracion_y_guardado_no_alteran_historia(db, monkeypatch):
    schema = (SOURCE.parents[1] / "sql/schema.sql").read_text(encoding="utf-8")
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email,markup_tipo,markup_valor,markup_pct) VALUES ('TEST','test@example.invalid','FIJO_ARS',14000,25),('OTRO','otro@example.invalid','FIJO_ARS',20000,25)")
            cur.execute("INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito) VALUES ('TEST',CURRENT_DATE,114000,'ACTIVO','INTERNACIONAL')")
            cur.execute("SELECT * FROM envios")
            historia = cur.fetchall()
            cur.execute("ALTER TABLE clientes DROP COLUMN pricing_rangos_internacional, DROP COLUMN pricing_rangos_nacional, DROP COLUMN perfil_comercial")
            cur.execute(schema)
            cur.execute(schema)
            cur.execute("SELECT markup_valor,pricing_rangos_internacional,pricing_rangos_nacional FROM clientes WHERE cliente_id='TEST'")
            anterior = cur.fetchone()
            assert anterior["markup_valor"] == 14000
            assert anterior["pricing_rangos_internacional"] == anterior["pricing_rangos_nacional"] == []
    monkeypatch.setattr(admin, "get_conn", db)
    monkeypatch.setattr(pricing, "get_conn", db)
    monkeypatch.setattr(admin, "_is_auth", lambda _:True)
    request = Request({"type":"http", "method":"POST", "path":"/admin/clientes/TEST/tarifas",
                       "headers":[], "client":("127.0.0.1",1234), "scheme":"http", "server":("test",80)})
    international = [dict(desde=0,hasta=None,tipo="PCT",valor=15,minimo=20000)]
    national = [dict(desde=0,hasta=None,tipo="FIJO_ARS",valor=2000,minimo=0)]
    response = admin.admin_tarifas_guardar(request, "TEST", internacional=json.dumps(international),
        nacional=json.dumps(national),perfil="ECOMMERCE",accion="guardar",costo_prueba="",
        ambito_prueba="internacional",admin_token="test")
    assert response.status_code == 303
    assert pricing.aplicar_pricing(costo_ars=100000,costo_usd=100,dolar=1000,
        pricing=pricing.get_pricing_config("TEST"))["precio_final_ars"] == 120000
    assert pricing.aplicar_pricing(costo_ars=10000,costo_usd=10,dolar=1000,
        pricing=pricing.get_pricing_nacional_estricto("TEST"))["precio_final_ars"] == 12000
    assert pricing.get_pricing_config("OTRO") == {"tipo":"FIJO_ARS","valor":20000}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(schema)
            cur.execute("SELECT * FROM envios")
            assert cur.fetchall() == historia
            cur.execute("SELECT pricing_rangos_internacional FROM clientes WHERE cliente_id='TEST'")
            assert cur.fetchone()["pricing_rangos_internacional"][0]["minimo"] == "20000"
            cur.execute("SELECT COUNT(*) AS n FROM security_audit WHERE event='admin.tarifas_rangos'")
            assert cur.fetchone()["n"] == 1
