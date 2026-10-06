"""Persistencia y rollback del precio web DHL en PostgreSQL aislado."""
from decimal import Decimal
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios import precios_web_dhl as precios
from test_conciliacion_couriers_postgres import DATABASE_URL, conciliacion_db


pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    monkeypatch.setattr(precios, "get_conn", conciliacion_db)
    return conciliacion_db


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/admin/precios-web",
        "query_string": b"",
        "headers": [(b"x-request-id", b"pricing-dhl-postgres-test")],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    })


def _config(db) -> dict[str, str]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT parametro, valor FROM config WHERE parametro = ANY(%s)",
            (list(precios.PARAMETROS_RESERVADOS),),
        )
        return {fila["parametro"]: fila["valor"] for fila in cur.fetchall()}


def test_guardar_dos_porcentajes_cambia_cotizacion_y_no_toca_legacy(db):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO config (parametro, valor) VALUES (%s, %s), (%s, %s)
            ON CONFLICT (parametro) DO UPDATE SET valor = EXCLUDED.valor
            """,
            (
                precios.PARAMETRO_FIJO_ARS_LEGACY,
                "135000",
                precios.PARAMETRO_MARKUP_PCT_LEGACY,
                "100000",
            ),
        )

    precios.guardar_configuracion_dhl(
        request=_request(), modo="PCT", markup_pct="10", margen_fijo_ars="",
    )
    regla_10 = precios.pricing_publico_dhl()
    cotizacion_10 = precios.calcular_precio_publico_dhl(
        {"costo": "100", "moneda": "USD"}, "1000", regla_10,
    )
    precios.guardar_configuracion_dhl(
        request=_request(), modo="PCT", markup_pct="30", margen_fijo_ars="",
    )
    regla_30 = precios.pricing_publico_dhl()
    cotizacion_30 = precios.calcular_precio_publico_dhl(
        {"costo": "100", "moneda": "USD"}, "1000", regla_30,
    )

    assert cotizacion_10["precio_ars"] == 110000
    assert cotizacion_30["precio_ars"] == 130000
    assert _config(db)[precios.PARAMETRO_FIJO_ARS_LEGACY] == "135000"
    assert _config(db)[precios.PARAMETRO_MARKUP_PCT_LEGACY] == "100000"


def test_fijo_cero_explicito_persiste_y_audita(db):
    resultado = precios.guardar_configuracion_dhl(
        request=_request(),
        modo="FIJO_ARS",
        markup_pct="valor inactivo inválido",
        margen_fijo_ars=Decimal("0"),
    )
    assert resultado["modo"] == "FIJO_ARS"
    assert resultado["margen_fijo_ars"] == Decimal("0")
    assert _config(db)[precios.PARAMETRO_FIJO_ARS] == "0"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor_type, actor_ref, metadata
              FROM security_audit
             WHERE event = 'admin.configurar_precio_web_dhl'
            """
        )
        auditoria = cur.fetchone()
    assert auditoria["actor_type"] == "admin"
    assert auditoria["actor_ref"] == "WEB:DHL"
    assert auditoria["metadata"]["despues"]["modo"] == "FIJO_ARS"


def test_fallo_de_auditoria_revierte_modo_y_valor(db, monkeypatch):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO config (parametro, valor) VALUES (%s, %s), (%s, %s)",
            (
                precios.PARAMETRO_MODO,
                "PCT",
                precios.PARAMETRO_MARKUP_PCT,
                "10",
            ),
        )

    def fallar_auditoria(*_args, **_kwargs):
        raise RuntimeError("auditoría indisponible")

    monkeypatch.setattr(
        "servicios.auditoria.registrar_desde_request_con_cursor",
        fallar_auditoria,
    )
    with pytest.raises(RuntimeError, match="auditoría indisponible"):
        precios.guardar_configuracion_dhl(
            request=_request(),
            modo="PCT",
            markup_pct="25",
            margen_fijo_ars="",
        )

    config = _config(db)
    assert config[precios.PARAMETRO_MODO] == "PCT"
    assert config[precios.PARAMETRO_MARKUP_PCT] == "10"
