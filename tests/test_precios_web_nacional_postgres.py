"""Persistencia y atomicidad del precio web OCA sobre PostgreSQL aislado."""
from decimal import Decimal

import pytest
from starlette.requests import Request

from servicios import precios_web_nacional as precios
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
        "headers": [(b"x-request-id", b"pricing-postgres-test")],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    })


def _config(db) -> dict[str, str]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT parametro, valor FROM config "
            "WHERE parametro IN (%s, %s) ORDER BY parametro",
            (precios.PARAMETRO_HABILITADO, precios.PARAMETRO_MARKUP_PCT),
        )
        return {fila["parametro"]: fila["valor"] for fila in cur.fetchall()}


def test_guardado_real_persiste_cero_y_auditoria_en_la_misma_transaccion(db):
    resultado = precios.guardar_configuracion_oca(
        request=_request(),
        habilitada=True,
        markup_pct=Decimal("0"),
    )

    assert resultado["publicable"] is True
    assert resultado["markup_pct"] == Decimal("0")
    assert _config(db) == {
        precios.PARAMETRO_HABILITADO: "1",
        precios.PARAMETRO_MARKUP_PCT: "0",
    }
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor_type, actor_ref, method, path, status_code, metadata
              FROM security_audit
             WHERE event = 'admin.configurar_precio_web_oca'
            """
        )
        auditoria = cur.fetchone()

    assert auditoria["actor_type"] == "admin"
    assert auditoria["actor_ref"] == "WEB:OCA"
    assert auditoria["method"] == "POST"
    assert auditoria["path"] == "/admin/precios-web"
    assert auditoria["status_code"] == 303
    assert auditoria["metadata"] == {
        "antes": {"habilitada": False, "markup_pct": None},
        "despues": {"habilitada": True, "markup_pct": "0"},
    }


def test_fallo_de_auditoria_revierte_el_precio_web(db, monkeypatch):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO config (parametro, valor) VALUES (%s, %s), (%s, %s)",
            (
                precios.PARAMETRO_HABILITADO,
                "0",
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
        precios.guardar_configuracion_oca(
            request=_request(),
            habilitada=True,
            markup_pct="25",
        )

    assert _config(db) == {
        precios.PARAMETRO_HABILITADO: "0",
        precios.PARAMETRO_MARKUP_PCT: "10",
    }
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM security_audit "
            "WHERE event = 'admin.configurar_precio_web_oca'"
        )
        assert cur.fetchone()["n"] == 0
