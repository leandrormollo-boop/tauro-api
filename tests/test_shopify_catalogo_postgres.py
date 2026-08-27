"""Regresiones de orden temporal del catálogo sobre PostgreSQL real.

Se ejecutan en la auditoría de release con ``TAURO_TEST_DATABASE_URL``. El
schema temporal evita tocar datos locales o productivos; sin PostgreSQL el
archivo se omite y los tests unitarios siguen funcionando normalmente.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from servicios import catalogo


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


@pytest.fixture
def catalogo_db(monkeypatch):
    schema = f"test_catalogo_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")

    admin = psycopg2.connect(DATABASE_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)
            cur.execute(
                "INSERT INTO clientes (cliente_id, email) VALUES (%s, %s)",
                ("SMOKE", f"smoke-{uuid.uuid4().hex}@example.invalid"),
            )

        @contextmanager
        def get_conn_aislada():
            conn = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{schema}"')
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        monkeypatch.setattr(catalogo, "get_conn", get_conn_aislada)
        yield get_conn_aislada
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _upsert(variante: str, producto: str, source: str, observed: str, stock: int, run: str):
    return catalogo.upsert_producto_importado(
        "SMOKE",
        variante,
        variante,
        tienda_dominio="smoke.myshopify.com",
        external_product_id=producto,
        external_variant_id=variante,
        stock_controlado=True,
        stock_disponible=stock,
        source_updated_at=source,
        source_observed_at=observed,
        sync_run_id=run,
        inventario_completo=True,
    )


def _estado(get_conn_aislada, variante: str) -> dict:
    with get_conn_aislada() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sync_activo, stock_disponible, sync_run_id,
                       source_updated_at, source_deleted_at, source_observed_at
                  FROM productos
                 WHERE cliente_id='SMOKE' AND external_variant_id=%s
                """,
                (variante,),
            )
            return dict(cur.fetchone())


def test_relojes_impiden_resucitar_stock_viejo(catalogo_db):
    get_conn_aislada = catalogo_db

    # Una ausencia observada en T13 vence un webhook atrasado de T12, pero no
    # una observación realmente posterior de T14.
    variante_ausente = "gid://shopify/ProductVariant/101"
    producto_ausente = "gid://shopify/Product/100"
    _upsert(variante_ausente, producto_ausente, "2026-08-27T10:00:00Z", "2026-08-27T10:00:00Z", 1, "T10")
    assert catalogo.desactivar_ausentes_shopify(
        "SMOKE", "smoke.myshopify.com", "otro-run", "2026-08-27T13:00:00Z"
    ) == 1
    # Aunque ya está inactivo, el barrido T15 debe avanzar el reloj. Así un
    # webhook T14 demorado no puede resucitarlo después.
    assert catalogo.desactivar_ausentes_shopify(
        "SMOKE", "smoke.myshopify.com", "otro-run", "2026-08-27T15:00:00Z"
    ) == 0
    _upsert(variante_ausente, producto_ausente, "2026-08-27T14:00:00Z", "2026-08-27T14:00:00Z", 99, "T14")
    assert _estado(get_conn_aislada, variante_ausente)["sync_activo"] is False
    _upsert(variante_ausente, producto_ausente, "2026-08-27T16:00:00Z", "2026-08-27T16:00:00Z", 4, "T16")
    assert _estado(get_conn_aislada, variante_ausente)["stock_disponible"] == 4

    # Un delete explícito T13 no puede revivirse con mutación T12 aunque la
    # consulta se procese en T14; exige una mutación fuente posterior al delete.
    variante_delete = "gid://shopify/ProductVariant/201"
    producto_delete = "gid://shopify/Product/200"
    _upsert(variante_delete, producto_delete, "2026-08-27T10:00:00Z", "2026-08-27T10:00:00Z", 1, "T10")
    catalogo.desactivar_producto_shopify(
        "SMOKE", "smoke.myshopify.com", producto_delete, "2026-08-27T13:00:00Z"
    )
    _upsert(variante_delete, producto_delete, "2026-08-27T12:00:00Z", "2026-08-27T14:00:00Z", 99, "T12")
    assert _estado(get_conn_aislada, variante_delete)["sync_activo"] is False
    _upsert(variante_delete, producto_delete, "2026-08-27T14:00:00Z", "2026-08-27T14:00:00Z", 4, "T14")
    assert _estado(get_conn_aislada, variante_delete)["stock_disponible"] == 4

    # Un full sync más viejo nunca archiva una actualización webhook más nueva.
    variante_nueva = "gid://shopify/ProductVariant/301"
    _upsert(variante_nueva, "gid://shopify/Product/300", "2026-08-27T12:00:00Z", "2026-08-27T12:00:00Z", 2, "T12")
    catalogo.desactivar_ausentes_shopify(
        "SMOKE", "smoke.myshopify.com", "run-viejo", "2026-08-27T11:00:00Z"
    )
    assert _estado(get_conn_aislada, variante_nueva)["sync_activo"] is True
