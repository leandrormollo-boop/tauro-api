"""Concurrencia, restart y replay de los outboxes sobre PostgreSQL aislado."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import uuid

import psycopg2
import psycopg2.extras
import pytest


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


@pytest.fixture
def ecommerce_db(monkeypatch):
    from servicios import ecommerce_outbox, integraciones_tienda

    schema = f"test_ecommerce_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    admin = psycopg2.connect(DATABASE_URL)
    admin.set_client_encoding("UTF8")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def conexion():
            conn = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            conn.set_client_encoding("UTF8")
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

        monkeypatch.setattr(ecommerce_outbox, "get_conn", conexion)
        monkeypatch.setattr(integraciones_tienda, "get_conn", conexion)
        monkeypatch.setattr(integraciones_tienda, "_tablas_listas", False)
        integraciones_tienda._ensure_tablas()
        yield conexion
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _base(conexion, plataforma="shopify"):
    dominio = (
        "piloto.myshopify.com" if plataforma == "shopify"
        else "123.tiendanube"
    )
    with conexion() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO clientes(cliente_id,email,nombre) VALUES ('PILOTO','p@example.invalid','Piloto')"
        )
        cur.execute(
            """
            INSERT INTO tiendas_conectadas(cliente_id,plataforma,dominio,secreto)
            VALUES ('PILOTO',%s,%s,%s) RETURNING id
            """,
            (plataforma, dominio, "oauth:shopify-app" if plataforma == "shopify" else "oauth:tn"),
        )
        tienda_id = int(cur.fetchone()["id"])
        if plataforma == "shopify":
            cur.execute(
                """
                INSERT INTO shopify_instalaciones
                    (dominio,access_token,cliente_id,app_client_id,
                     install_generation,webhooks_ready)
                VALUES (%s,'token','PILOTO','app','gen-1',TRUE)
                """,
                (dominio,),
            )
        else:
            cur.execute(
                """
                INSERT INTO tiendanube_instalaciones
                    (store_id,access_token,cliente_id,install_generation,
                     webhooks_ready,estado)
                VALUES ('123','token','PILOTO','gen-1',TRUE,'ACTIVA')
                """
            )
    return tienda_id, dominio


def _pedido(direccion="Calle 1"):
    return {
        "pedido_externo_id": "ORDER-1",
        "numero": "#1",
        "destinatario": {"direccion": direccion, "pais": "US"},
        "items": [{"sku": "SKU", "cantidad": 1}],
        "valor_total": "10",
        "moneda": "USD",
    }


def test_pedido_y_outbox_atomicos_replay_y_update(ecommerce_db):
    from servicios import integraciones_tienda

    tienda_id, dominio = _base(ecommerce_db)
    assert integraciones_tienda.guardar_pedido(
        "PILOTO", tienda_id, "shopify", _pedido(),
        dominio_verificado=dominio, install_generation_verificada="gen-1",
    ) is True
    assert integraciones_tienda.guardar_pedido(
        "PILOTO", tienda_id, "shopify", _pedido(),
        dominio_verificado=dominio, install_generation_verificada="gen-1",
    ) is False
    assert integraciones_tienda.guardar_pedido(
        "PILOTO", tienda_id, "shopify", _pedido("Calle 2"),
        dominio_verificado=dominio, install_generation_verificada="gen-1",
    ) is False

    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM pedidos_tienda")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM solicitud_automatica_outbox")
        assert cur.fetchone()["n"] == 2


def test_claim_skip_locked_y_restart_stale(ecommerce_db):
    from servicios import ecommerce_outbox, integraciones_tienda

    tienda_id, dominio = _base(ecommerce_db)
    integraciones_tienda.guardar_pedido(
        "PILOTO", tienda_id, "shopify", _pedido(),
        dominio_verificado=dominio, install_generation_verificada="gen-1",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _n: ecommerce_outbox._claim_pedido(), range(2)))
    assert sum(claim is not None for claim in claims) == 1
    primero = next(claim for claim in claims if claim)

    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE solicitud_automatica_outbox
               SET claimed_at=NOW()-INTERVAL '11 minutes'
             WHERE id=%s
            """,
            (primero["id"],),
        )
    recuperado = ecommerce_outbox._claim_pedido()
    assert recuperado["id"] == primero["id"]
    assert recuperado["claim_id"] != primero["claim_id"]
    assert recuperado["intentos"] == 2


def test_fulfillment_outbox_comparte_commit_y_deduplica(ecommerce_db, monkeypatch):
    from servicios import ecommerce_outbox, shopify_app

    tienda_id, dominio = _base(ecommerce_db)
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pedidos_tienda
                (cliente_id,tienda_id,plataforma,pedido_externo_id,estado)
            VALUES ('PILOTO',%s,'shopify','ORDER-1','CONVERTIDO')
            RETURNING id
            """,
            (tienda_id,),
        )
        pedido_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO solicitudes_guia
                (cliente_id,producto_alias,destino_pais,dest_nombre,
                 dest_direccion,dest_ciudad,dest_zip,origen_plataforma,
                 origen_dominio,origen_pedido_externo_id)
            VALUES ('PILOTO','SKU','US','Dest','Calle','Miami','33101',
                    'shopify',%s,'ORDER-1') RETURNING id
            """,
            (dominio,),
        )
        solicitud_id = int(cur.fetchone()["id"])
        cur.execute(
            "UPDATE pedidos_tienda SET solicitud_id=%s WHERE id=%s",
            (solicitud_id, pedido_id),
        )

    with pytest.raises(RuntimeError):
        with ecommerce_db() as conn, conn.cursor() as cur:
            assert ecommerce_outbox.encolar_fulfillment_con_cursor(
                cur, solicitud_id, "TRACK-1", "DHL",
            )
            raise RuntimeError("simular caída antes del commit")
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tienda_fulfillment_outbox")
        assert cur.fetchone()["n"] == 0

    with ecommerce_db() as conn, conn.cursor() as cur:
        assert ecommerce_outbox.encolar_fulfillment_con_cursor(
            cur, solicitud_id, "TRACK-1", "DHL",
        )
        assert not ecommerce_outbox.encolar_fulfillment_con_cursor(
            cur, solicitud_id, "TRACK-1", "DHL",
        )
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tienda_fulfillment_outbox")
        assert cur.fetchone()["n"] == 1

    # Un restart durante PROCESANDO no puede repetir la mutación a ciegas.
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tienda_fulfillment_outbox
               SET estado='PROCESANDO', claim_id='worker-caido',
                   claimed_at=NOW()-INTERVAL '11 minutes'
            """
        )
    recuperado = ecommerce_outbox._claim_fulfillment()
    assert recuperado["estado_anterior"] == "PROCESANDO"
    conciliaciones = []
    monkeypatch.setattr(
        shopify_app,
        "marcar_enviado_resultado",
        lambda *_args, **kwargs: (
            conciliaciones.append(kwargs.get("solo_reconciliar")) or "COMPLETADO"
        ),
    )
    assert ecommerce_outbox._ejecutar_fulfillment_bajo_lock(recuperado) == "COMPLETADO"
    assert conciliaciones == [True]


def test_tracking_sin_pedido_queda_durable_en_revision_manual(ecommerce_db):
    from servicios import ecommerce_outbox

    _tienda_id, dominio = _base(ecommerce_db)
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia
                (cliente_id,producto_alias,destino_pais,dest_nombre,
                 dest_direccion,dest_ciudad,dest_zip,origen_plataforma,
                 origen_dominio,origen_pedido_externo_id)
            VALUES ('PILOTO','SKU','US','Dest','Calle','Miami','33101',
                    'shopify',%s,'ORDER-AUSENTE') RETURNING id
            """,
            (dominio,),
        )
        solicitud_id = int(cur.fetchone()["id"])
        assert ecommerce_outbox.encolar_fulfillment_con_cursor(
            cur, solicitud_id, "TRACK-SIN-PEDIDO", "DHL",
        )
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pedido_id, estado, ultimo_error_codigo
              FROM tienda_fulfillment_outbox
             WHERE solicitud_id=%s
            """,
            (solicitud_id,),
        )
        job = cur.fetchone()
        assert job["pedido_id"] is None
        assert job["estado"] == "MANUAL_REVIEW"
        assert job["ultimo_error_codigo"] == "PEDIDO_NO_VINCULADO"


def test_cancelacion_emitida_bloquea_y_crea_obligacion_sin_tocar_cargo(ecommerce_db):
    from servicios import integraciones_tienda

    tienda_id, _dominio = _base(ecommerce_db, plataforma="tiendanube")
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia
                (cliente_id,producto_alias,destino_pais,dest_nombre,
                 dest_direccion,dest_ciudad,dest_zip,estado,tracking,
                 guia_generada_at,courier)
            VALUES ('PILOTO','SKU','AR','Dest','Calle','CABA','1000',
                    'GUIA_LISTA','TRACK-EMITIDO',NOW(),'OCA') RETURNING id
            """
        )
        solicitud_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO pedidos_tienda
                (cliente_id,tienda_id,plataforma,pedido_externo_id,estado,solicitud_id)
            VALUES ('PILOTO',%s,'tiendanube','ORDER-1','CONVERTIDO',%s)
            """,
            (tienda_id, solicitud_id),
        )
        cur.execute(
            """
            INSERT INTO envios(cliente_id,fecha,monto_ars,estado,tracking,solicitud_id)
            VALUES ('PILOTO',CURRENT_DATE,1234,'ACTIVO','TRACK-EMITIDO',%s)
            """,
            (solicitud_id,),
        )

    assert integraciones_tienda.cancelar_pedido_externo(
        tienda_id, "ORDER-1",
    ) is False
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pedidos_tienda WHERE pedido_externo_id='ORDER-1'")
        pedido = cur.fetchone()
        assert pedido["estado"] == "CANCELACION_MANUAL"
        assert pedido["automatismos_bloqueados"] is True
        cur.execute("SELECT * FROM tienda_cancelacion_obligaciones")
        assert cur.fetchone()["estado"] == "MANUAL_REVIEW"
        cur.execute("SELECT estado,monto_ars FROM envios WHERE solicitud_id=%s", (solicitud_id,))
        cargo = cur.fetchone()
        assert cargo["estado"] == "ACTIVO"
        assert str(cargo["monto_ars"]) == "1234.00"
        cur.execute("SELECT estado,tracking FROM solicitudes_guia WHERE id=%s", (solicitud_id,))
        solicitud = cur.fetchone()
        assert solicitud["estado"] == "GUIA_LISTA"
        assert solicitud["tracking"] == "TRACK-EMITIDO"


def test_cancelacion_convertida_pre_emision_es_local_e_historica(ecommerce_db):
    from servicios import integraciones_tienda

    tienda_id, _dominio = _base(ecommerce_db, plataforma="tiendanube")
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia
                (cliente_id,producto_alias,destino_pais,dest_nombre,
                 dest_direccion,dest_ciudad,dest_zip,estado,courier)
            VALUES ('PILOTO','SKU','AR','Dest','Calle','CABA','1000',
                    'SOLICITADO','OCA') RETURNING id
            """
        )
        solicitud_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO pedidos_tienda
                (cliente_id,tienda_id,plataforma,pedido_externo_id,estado,solicitud_id)
            VALUES ('PILOTO',%s,'tiendanube','ORDER-1','CONVERTIDO',%s)
            """,
            (tienda_id, solicitud_id),
        )

    assert integraciones_tienda.cancelar_pedido_externo(
        tienda_id, "ORDER-1",
    ) is True
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT estado FROM solicitudes_guia WHERE id=%s", (solicitud_id,))
        assert cur.fetchone()["estado"] == "CANCELADO"
        cur.execute("SELECT estado,automatismos_bloqueados FROM pedidos_tienda")
        pedido = cur.fetchone()
        assert pedido["estado"] == "CANCELADO"
        assert pedido["automatismos_bloqueados"] is True
        cur.execute("SELECT count(*) AS n FROM tienda_cancelacion_obligaciones")
        assert cur.fetchone()["n"] == 0


def test_cancelacion_reconcilia_solicitud_creada_aun_no_vinculada(ecommerce_db):
    from servicios import integraciones_tienda

    tienda_id, dominio = _base(ecommerce_db, plataforma="tiendanube")
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pedidos_tienda
                (cliente_id,tienda_id,plataforma,pedido_externo_id,estado)
            VALUES ('PILOTO',%s,'tiendanube','ORDER-VENTANA','PENDIENTE')
            RETURNING id
            """,
            (tienda_id,),
        )
        pedido_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO solicitudes_guia
                (cliente_id,producto_alias,destino_pais,dest_nombre,
                 dest_direccion,dest_ciudad,dest_zip,estado,origen_plataforma,
                 origen_dominio,origen_pedido_externo_id)
            VALUES ('PILOTO','SKU','AR','Dest','Calle','CABA','1000',
                    'SOLICITADO','tiendanube',%s,'ORDER-VENTANA') RETURNING id
            """,
            (dominio,),
        )
        solicitud_id = int(cur.fetchone()["id"])

    assert integraciones_tienda.cancelar_pedido_externo(
        tienda_id, "ORDER-VENTANA",
    ) is True
    with ecommerce_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT estado,solicitud_id FROM pedidos_tienda WHERE id=%s",
            (pedido_id,),
        )
        pedido = cur.fetchone()
        assert pedido["estado"] == "CANCELADO"
        assert pedido["solicitud_id"] == solicitud_id
        cur.execute("SELECT estado FROM solicitudes_guia WHERE id=%s", (solicitud_id,))
        assert cur.fetchone()["estado"] == "CANCELADO"
