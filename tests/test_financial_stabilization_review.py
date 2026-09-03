"""Independent acceptance checks; only the explicitly supplied local test DB."""
import importlib.util
import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from servicios import facturacion_clientes as fc
from servicios import cuenta_corriente as cc
from servicios import pricing
from servicios.estados_envio import ESTADOS_VALIDOS
import psycopg2
from psycopg2 import sql


SOURCE = Path(__file__).resolve().parent / "test_pagos_documentales_postgres.py"
SPEC = importlib.util.spec_from_file_location("review_db_fixture", SOURCE)
DB_FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DB_FIXTURE)


@pytest.fixture
def cuenta_db(monkeypatch):
    if not DB_FIXTURE.DATABASE_URL:
        pytest.skip("requires an explicitly isolated TAURO_TEST_DATABASE_URL")
    yield from DB_FIXTURE.cuenta_db.__wrapped__(monkeypatch)


def test_review_fresh_schema_accepts_all_canonical_states_and_rejects_unknown(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email) VALUES ('STATES','states@example.invalid')")
            for state in ESTADOS_VALIDOS:
                cur.execute(
                    "INSERT INTO solicitudes_guia (cliente_id,estado,producto_alias,destino_pais,dest_nombre,dest_direccion,dest_ciudad,dest_zip) "
                    "VALUES ('STATES',%s,'TEST','AR','Test','Test','Test','1000')", (state,),
                )
    with pytest.raises(psycopg2.errors.CheckViolation) as captured:
        with cuenta_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO solicitudes_guia (cliente_id,estado,producto_alias,destino_pais,dest_nombre,dest_direccion,dest_ciudad,dest_zip) "
                    "VALUES ('STATES','UNKNOWN','TEST','AR','Test','Test','Test','1000')",
                )
    assert captured.value.diag.constraint_name == "ck_solicitudes_guia_estado"


def test_review_concurrent_same_cargo_has_one_invoice_and_no_extra_debt(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email) VALUES ('REVIEW','review@example.invalid')")
            cur.execute("INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito) VALUES ('REVIEW',CURRENT_DATE,100,'ACTIVO','NACIONAL') RETURNING id")
            envio_id = cur.fetchone()["id"]
    barrier = Barrier(2)

    def invoice(number):
        barrier.wait(timeout=5)
        try:
            return fc.crear_factura_cliente(
                cliente_id="REVIEW", tipo="FC", punto_venta=1, numero=number,
                cae="", fecha_emision="2026-09-03", subtotal="100", iva="0",
                pdf=b"%PDF-1.4\n%%EOF\n", pdf_nombre="test.pdf",
                seleccion=[f"E:{envio_id}"], created_by="independent-review",
            )
        except fc.FacturacionClienteError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoice, [801, 802]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, fc.FacturacionClienteError) for result in results) == 1
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM facturas_cliente")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT COUNT(*) AS n FROM facturas_cliente_items")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT SUM(monto_ars) AS total FROM envios")
            assert cur.fetchone()["total"] == Decimal("100.00")


def test_review_legacy_write_rejected_before_database(monkeypatch):
    def forbidden():
        pytest.fail("Legacy invoice input reached the database")
    monkeypatch.setattr(cc, "get_conn", forbidden)
    with pytest.raises((ValueError, TypeError)):
        cc.registrar_envio(
            cliente_id="REVIEW", fecha="2026-09-03", monto_ars="100",
            nro_fc="0001-00000123", ambito="NACIONAL",
        )


def test_review_database_trigger_alone_serializes_duplicate_billing(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email) VALUES ('TRIGGER','trigger@example.invalid')")
            cur.execute("INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito) VALUES ('TRIGGER',CURRENT_DATE,100,'ACTIVO','NACIONAL') RETURNING id")
            envio_id = cur.fetchone()["id"]
    barrier = Barrier(2)

    def insert(number):
        try:
            with cuenta_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '5s'")
                    cur.execute(
                        "INSERT INTO facturas_cliente (cliente_id,tipo,punto_venta,numero,fecha_emision,subtotal,iva,total,pdf,created_by) "
                        "VALUES ('TRIGGER','FC',1,%s,CURRENT_DATE,100,0,100,%s,'review') RETURNING id",
                        (number, psycopg2.Binary(b"%PDF-1.4\n%%EOF\n")),
                    )
                    factura_id = cur.fetchone()["id"]
                    barrier.wait(timeout=5)
                    cur.execute(
                        "INSERT INTO facturas_cliente_items (factura_id,envio_id,descripcion,monto) VALUES (%s,%s,'Test',100)",
                        (factura_id, envio_id),
                    )
            return "committed"
        except psycopg2.Error as exc:
            assert not isinstance(exc, psycopg2.errors.LockNotAvailable)
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(insert, [901, 902]))
    assert sorted(results) == ["committed", "conflict"]
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM facturas_cliente")
            assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("row", [None, {"markup_pct": None, "markup_tipo": "PCT", "markup_valor": None}])
def test_review_missing_margin_fails_closed(monkeypatch, row):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): pass
        def fetchone(self): return row
    class Connection(Cursor):
        def cursor(self): return Cursor()
    monkeypatch.setattr(pricing, "get_conn", Connection)
    with pytest.raises(ValueError):
        pricing.get_pricing_config("REVIEW")


def test_review_configured_legacy_zero_is_not_twenty_five(monkeypatch):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): pass
        def fetchone(self): return {"markup_pct": 0, "markup_tipo": None, "markup_valor": None}
    class Connection(Cursor):
        def cursor(self): return Cursor()
    monkeypatch.setattr(pricing, "get_conn", Connection)
    assert pricing.get_pricing_config("REVIEW") == {"tipo": "PCT", "valor": 0.0}


def test_review_unknown_database_messages_are_not_exposed_or_masked():
    from servicios.conflictos_db import mensaje_conflicto_db
    unknown = psycopg2.errors.RaiseException("internal credential diagnostic QA_ONLY")
    assert mensaje_conflicto_db(unknown) is None
    assert mensaje_conflicto_db(psycopg2.errors.UniqueViolation("unknown invariant")) is None


@pytest.mark.parametrize("legacy_fields", [
    {"nro_fc": "0001-00000123"},
    {"factura_pdf": SimpleNamespace(read=lambda: None, filename="invoice.pdf")},
])
def test_review_old_manual_form_never_silently_discards_invoice(monkeypatch, legacy_fields):
    from endpoints import admin

    def forbidden(**kwargs):
        pytest.fail("Old invoice form silently created an undocumented debit")

    monkeypatch.setattr(admin, "_is_auth", lambda token: True)
    monkeypatch.setattr(admin, "registrar_envio", forbidden)
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(admin.templates, "TemplateResponse", lambda **kwargs: SimpleNamespace(**kwargs))
    result = asyncio.run(admin.admin_envio_nuevo(
        request=SimpleNamespace(), cliente_id="REVIEW", **legacy_fields,
        fecha="2026-09-03", monto_ars="100", ambito="NACIONAL",
        idempotency_key="r" * 43, descripcion="Test", tracking="",
        estado="ACTIVO", admin_token="test",
    ))
    assert result.context["flash_error"]


def test_review_missing_exchange_rate_never_invents_1450(monkeypatch):
    from servicios import cotizador
    monkeypatch.delenv("COTIZACION_DOLAR_ARS", raising=False)

    def disconnected():
        raise RuntimeError("test database unavailable")

    monkeypatch.setattr(cotizador, "get_conn", disconnected)
    with pytest.raises(cotizador.DolarNoConfigurado):
        cotizador.dolar_ars()


def test_review_schema_replay_preserves_legacy_invoice_and_validates_readiness(cuenta_db):
    from core import database
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email) VALUES ('HISTORY','history@example.invalid')")
            # Only this disposable fixture: simulate a row created before the
            # read-only migration, without loosening any production control.
            cur.execute("ALTER TABLE envios DISABLE TRIGGER trg_proteger_fc_legacy_envios")
            cur.execute(
                "INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito,nro_fc,factura_pdf,factura_nombre) "
                "VALUES ('HISTORY',CURRENT_DATE,123.45,'ACTIVO','INTERNACIONAL','FC-OLD',%s,'old.pdf') RETURNING id",
                (psycopg2.Binary(b"%PDF-1.4 OLD"),),
            )
            envio_id = cur.fetchone()["id"]
            cur.execute("ALTER TABLE envios ENABLE TRIGGER trg_proteger_fc_legacy_envios")
            schema_sql = (SOURCE.parent.parent / "sql/schema.sql").read_text()
            cur.execute(schema_sql)
            cur.execute(schema_sql)
            assert all(database._verificar_readiness_contable(cur).values())
            cur.execute("SELECT nro_fc,factura_pdf,factura_nombre,monto_ars FROM envios WHERE id=%s", (envio_id,))
            row = cur.fetchone()
            assert row["nro_fc"] == "FC-OLD"
            assert bytes(row["factura_pdf"]) == b"%PDF-1.4 OLD"
            assert row["factura_nombre"] == "old.pdf"
            assert row["monto_ars"] == Decimal("123.45")
    with pytest.raises(psycopg2.errors.RaiseException):
        with cuenta_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE envios SET nro_fc='CHANGED' WHERE id=%s", (envio_id,))


@pytest.mark.parametrize("kind,value", [("UNKNOWN", 10), ("PCT", -1), ("FIJO_ARS", -1), ("MULTIPLICADOR", 0.5)])
def test_review_invalid_margin_is_never_silently_repaired_to_zero(monkeypatch, kind, value):
    with pytest.raises(ValueError):
        pricing.normalizar_pricing(kind, value)


def test_review_dollar_job_does_not_invent_reference_or_send_false_alert(monkeypatch):
    from servicios import dolar_oficial
    monkeypatch.delenv("COTIZACION_DOLAR_ARS", raising=False)
    monkeypatch.setattr(dolar_oficial, "auto_activo", lambda: True)
    monkeypatch.setattr(dolar_oficial, "consultar_dolar_oficial", lambda: 5000.0)
    monkeypatch.setattr(dolar_oficial, "_valor_actual", lambda: None)

    def forbidden(*args, **kwargs):
        pytest.fail("No configured reference: must not alert or update using an invented 1450")

    monkeypatch.setattr(dolar_oficial, "_avisar_salto", forbidden)
    monkeypatch.setattr(dolar_oficial, "get_conn", forbidden)
    assert dolar_oficial.actualizar_dolar_auto()["ok"] is False


@pytest.mark.parametrize("known_rule", [True, False])
def test_review_commit_failure_rolls_back_entire_invoice(cuenta_db, known_rule):
    message = (
        "Los ítems no coinciden con el total de la factura cliente"
        if known_rule else "unknown internal diagnostic QA_ONLY"
    )
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email) VALUES ('COMMIT','commit@example.invalid')")
            cur.execute("INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito) VALUES ('COMMIT',CURRENT_DATE,100,'ACTIVO','NACIONAL') RETURNING id")
            envio_id = cur.fetchone()["id"]
            cur.execute(sql.SQL(
                "CREATE FUNCTION review_commit_failure() RETURNS TRIGGER AS $$ "
                "BEGIN RAISE EXCEPTION {}; END; $$ LANGUAGE plpgsql"
            ).format(sql.Literal(message)))
            cur.execute(
                "CREATE CONSTRAINT TRIGGER review_commit_failure "
                "AFTER INSERT ON facturas_cliente DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION review_commit_failure()"
            )
    expected = fc.FacturacionConflictoError if known_rule else psycopg2.errors.RaiseException
    with pytest.raises(expected):
        fc.crear_factura_cliente(
            cliente_id="COMMIT", tipo="FC", punto_venta=1, numero=1001,
            cae="", fecha_emision="2026-09-03", subtotal="100", iva="0",
            pdf=b"%PDF-1.4\n%%EOF\n", pdf_nombre="test.pdf",
            seleccion=[f"E:{envio_id}"], created_by="review",
        )
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            for table in ("facturas_cliente", "facturas_cliente_items"):
                cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(sql.Identifier(table)))
                assert cur.fetchone()["n"] == 0
            cur.execute("SELECT SUM(monto_ars) AS total FROM envios")
            assert cur.fetchone()["total"] == Decimal("100.00")


def test_review_duplicate_document_number_is_domain_conflict(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email) VALUES ('NUMBER','number@example.invalid')")
            cur.execute(
                "INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito) "
                "VALUES ('NUMBER',CURRENT_DATE,100,'ACTIVO','NACIONAL'), "
                "('NUMBER',CURRENT_DATE,100,'ACTIVO','NACIONAL') RETURNING id"
            )
            ids = [row["id"] for row in cur.fetchall()]
    params = dict(
        cliente_id="NUMBER", tipo="FC", punto_venta=1, numero=2001,
        cae="", fecha_emision="2026-09-03", subtotal="100", iva="0",
        pdf=b"%PDF-1.4\n%%EOF\n", pdf_nombre="test.pdf", created_by="review",
    )
    fc.crear_factura_cliente(**params, seleccion=[f"E:{ids[0]}"])
    with pytest.raises(fc.FacturacionConflictoError, match="Ya existe una factura"):
        fc.crear_factura_cliente(**params, seleccion=[f"E:{ids[1]}"])
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM facturas_cliente")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT COUNT(*) AS n FROM facturas_cliente_items")
            assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("value,enabled", [(None, False), (-1, False), (0, True), (14500, True)])
def test_review_courier_permission_requires_valid_price_not_positive_margin(monkeypatch, value, enabled):
    from servicios import configuracion_couriers_cliente as config
    monkeypatch.setattr(config, "estado_integracion", lambda courier: {
        "operativa": True, "estado": "test", "detalle": "test",
    })
    matrix = config._armar_matriz(
        {"cliente_id": "TEST", "activo": True, "markup_pct": None},
        [{"courier": "dhl", "puede_cotizar": True, "puede_emitir": True,
          "puede_recolectar": False, "markup_tipo": "FIJO_ARS", "markup_valor": value}],
    )
    courier = next(item for item in matrix["couriers"] if item["id"] == "dhl")
    assert courier["puede_cotizar"] is enabled
    assert courier["puede_emitir"] is enabled


def test_review_admin_unknown_database_failure_uses_technical_handler(monkeypatch):
    from endpoints import admin
    error = psycopg2.errors.RaiseException("private SQL detail QA_ONLY")

    def broken(**kwargs):
        raise error

    monkeypatch.setattr(admin, "_is_auth", lambda token: True)
    monkeypatch.setattr(admin, "registrar_envio", broken)
    with pytest.raises(psycopg2.errors.RaiseException) as caught:
        asyncio.run(admin.admin_envio_nuevo(
            request=SimpleNamespace(), cliente_id="REVIEW", fecha="2026-09-03",
            monto_ars="100", ambito="NACIONAL", idempotency_key="r" * 43,
            descripcion="Test", tracking="", estado="ACTIVO", admin_token="test",
        ))
    assert caught.value is error


@pytest.mark.parametrize("kind", [psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected])
def test_review_concurrent_database_conflicts_have_safe_retry_message(kind):
    from servicios.conflictos_db import mensaje_conflicto_db
    result = mensaje_conflicto_db(kind("private SQL detail QA_ONLY"))
    assert "volvé a intentar" in result
    assert "QA_ONLY" not in result


@pytest.mark.parametrize("table,column,value", [
    ("envios", "estado", "PAGADO"),
    ("pagos", "estado", "DESCONOCIDO"),
])
def test_review_financial_states_reject_unknown_values(cuenta_db, table, column, value):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,email) VALUES ('CHECK','check@example.invalid')")
    with pytest.raises(psycopg2.errors.CheckViolation) as caught:
        with cuenta_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL(
                    "INSERT INTO {} (cliente_id,fecha,monto_ars,{}) "
                    "VALUES ('CHECK',CURRENT_DATE,100,%s)"
                ).format(sql.Identifier(table), sql.Identifier(column)), (value,))
    assert caught.value.diag.constraint_name == f"ck_{table}_estado"
