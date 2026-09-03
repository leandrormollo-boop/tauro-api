"""Rastreo DHL diario, persistido y limitado a envíos pendientes."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from core import database
import servicios.tracking_envios as tracking


RAIZ = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()


def _respuesta(*eventos):
    return {
        "encontrado": True,
        "estado_consulta": "SUCCESS",
        "eventos": list(eventos),
    }


def test_normaliza_entregado_por_el_evento_mas_reciente():
    resultado = tracking.normalizar_respuesta_dhl(_respuesta(
        {
            "date": "2026-08-31", "time": "15:00:00",
            "typeCode": "OK", "description": "Shipment delivered",
        },
        {
            "date": "2026-08-30", "time": "08:00:00",
            "typeCode": "DF", "description": "Shipment departed facility",
        },
    ))

    assert resultado == {
        "ok": True,
        "estado": "ENTREGADO",
        "estado_courier": "OK",
        "descripcion": "Shipment delivered",
    }


def test_retenido_no_se_confunde_con_actualizacion_aduanera_normal():
    retenido = tracking.normalizar_respuesta_dhl(_respuesta({
        "date": "2026-08-31", "time": "15:00:00",
        "typeCode": "OH", "description": "Shipment is on hold",
    }))
    liberado = tracking.normalizar_respuesta_dhl(_respuesta({
        "date": "2026-08-31", "time": "16:00:00",
        "typeCode": "RR", "description": "Customs clearance status updated",
        "remarks": [{
            "value": "Shipment has been given a release status by Customs.",
        }],
    }))

    assert retenido["estado"] == "RETENIDO"
    assert liberado["estado"] == "PROCESO_ENTREGA"


def test_success_sin_eventos_no_inventa_estado_logistico():
    resultado = tracking.normalizar_respuesta_dhl({
        "encontrado": True,
        "estado_consulta": "SUCCESS",
        "eventos": [],
    })

    assert resultado["ok"] is False
    assert "no informó eventos" in resultado["error"]


def test_un_envio_finalizado_no_vuelve_a_llamar_dhl(monkeypatch):
    class Cliente:
        def track(self, _tracking):
            raise AssertionError("un entregado no debe consultar DHL")

    monkeypatch.setattr(tracking, "_candidato_dhl", lambda _sid: None)

    resultado = tracking.actualizar_tracking_dhl(91, cliente_dhl=Cliente())

    assert resultado == {"ok": True, "omitido": True, "solicitud_id": 91}


def test_job_diario_filtra_finalizados_y_usa_lock_global(monkeypatch):
    class Cliente:
        def _error_configuracion(self):
            return None

    class Cursor:
        def __init__(self):
            self.one = None
            self.all = []
            self.sql = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params=None):
            compacto = " ".join(sql.split())
            self.sql.append((compacto, params))
            self.one = None
            self.all = []
            if "pg_try_advisory_lock" in compacto:
                self.one = {"adquirido": True}
            elif compacto.startswith("SELECT id FROM solicitudes_guia"):
                self.all = [{"id": 10}, {"id": 20}]

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.all

    cursor = Cursor()

    @contextmanager
    def conexion():
        class Conn:
            def cursor(self):
                return cursor
        yield Conn()

    procesados = []
    monkeypatch.setattr(tracking, "DHLClient", Cliente)
    monkeypatch.setattr(tracking, "get_conn", conexion)
    monkeypatch.setattr(
        tracking,
        "actualizar_tracking_dhl",
        lambda sid, **_kw: (
            procesados.append(sid)
            or {"ok": True, "estado": "PROCESO_ENTREGA"}
        ),
    )

    resultado = tracking.actualizar_trackings_diarios_dhl(limite=50)

    assert procesados == [10, 20]
    assert resultado["consultados"] == 2
    seleccion = next(
        sql for sql, _ in cursor.sql
        if sql.startswith("SELECT id FROM solicitudes_guia")
    )
    assert "estado NOT IN ('CANCELADO', 'ENTREGADO')" in seleccion
    assert "tracking_estado IS DISTINCT FROM 'ENTREGADO'" in seleccion
    assert "tracking_consultado_at AT TIME ZONE" in seleccion
    assert "America/Argentina/Buenos_Aires" in seleccion
    assert any("pg_try_advisory_lock" in sql for sql, _ in cursor.sql)
    assert any("pg_advisory_unlock" in sql for sql, _ in cursor.sql)


def test_schema_scheduler_y_portal_reflejan_el_tracking_diario():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    main = (RAIZ / "main.py").read_text(encoding="utf-8")
    html = (RAIZ / "templates" / "portal" / "envios.html").read_text(
        encoding="utf-8"
    )
    estados = (RAIZ / "servicios" / "estados_envio.py").read_text(
        encoding="utf-8"
    )

    assert "tracking_estado TEXT" in schema
    assert "tracking_consultado_at TIMESTAMPTZ" in schema
    assert "ck_solicitudes_tracking_estado" in schema
    assert "idx_solicitudes_tracking_dhl_pendiente" in schema
    assert "estado NOT IN ('CANCELADO', 'ENTREGADO')" in schema
    assert 'id="tracking_dhl_diario"' in main
    assert 'trigger="cron"' in main
    assert 'DHL_TRACKING_CRON_HOUR' in main
    assert 'target=actualizar_trackings_diarios_seguro' in main
    assert '"ENTREGADO": ("Entregado", "ok")' in estados
    assert '"RETENIDO": ("Retenido", "error")' in estados
    assert "estado_tracking_ui" in html
    assert '"Sin movimientos", "muted"' in estados


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)
def test_postgres_persiste_entrega_y_luego_la_excluye(monkeypatch):
    schema = f"test_tracking_{uuid.uuid4().hex}"
    schema_sql = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    admin = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    admin.set_client_encoding("UTF8")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)
            assert all(database._verificar_readiness_contable(cur).values())

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

        monkeypatch.setattr(tracking, "get_conn", conexion)
        with conexion() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clientes (cliente_id, email, nombre)
                    VALUES ('TRACK_TEST', 'tracking@example.invalid', 'TRACK')
                    """
                )
                cur.execute(
                    """
                    INSERT INTO solicitudes_guia (
                        cliente_id, producto_alias, destino_pais, dest_nombre,
                        dest_direccion, dest_ciudad, dest_zip, courier,
                        tracking, estado, coti_id, precio_tauro_ars
                    ) VALUES (
                        'TRACK_TEST', 'Producto', 'US', 'Destinatario',
                        'Calle 1', 'Miami', '33101', 'DHL', '1234567890',
                        'DESPACHADO', 'COTI-TRACK', 10000
                    ) RETURNING id
                    """
                )
                solicitud_id = int(cur.fetchone()["id"])

        class DHL:
            def __init__(self):
                self.llamadas = 0

            def track(self, _numero):
                self.llamadas += 1
                if self.llamadas == 1:
                    return _respuesta({
                        "date": "2026-08-30", "time": "10:00:00",
                        "typeCode": "DF",
                        "description": "Shipment departed facility",
                    })
                return _respuesta({
                    "date": "2026-08-31", "time": "15:00:00",
                    "typeCode": "OK", "description": "Shipment delivered",
                })

        dhl = DHL()
        primero = tracking.actualizar_tracking_dhl(
            solicitud_id, cliente_dhl=dhl
        )
        segundo = tracking.actualizar_tracking_dhl(
            solicitud_id, cliente_dhl=dhl
        )
        tercero = tracking.actualizar_tracking_dhl(
            solicitud_id, cliente_dhl=dhl
        )

        assert primero["estado"] == "PROCESO_ENTREGA"
        assert segundo["estado"] == "ENTREGADO"
        assert tercero["omitido"] is True
        assert dhl.llamadas == 2
        with conexion() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tracking_estado, tracking_estado_courier,
                           tracking_finalizado_at, tracking_error
                    FROM solicitudes_guia WHERE id = %s
                    """,
                    (solicitud_id,),
                )
                guardado = cur.fetchone()
        assert guardado["tracking_estado"] == "ENTREGADO"
        assert guardado["tracking_estado_courier"] == "OK"
        assert guardado["tracking_finalizado_at"] is not None
        assert guardado["tracking_error"] is None
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
