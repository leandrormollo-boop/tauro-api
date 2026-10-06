"""Las guías FedEx se actualizan solas todos los días hasta la entrega."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

import servicios.tracking_fedex_portal as fedex

RAIZ = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()


def _resultado(codigo, texto, fecha="2026-07-15T10:20:00-05:00", **extra):
    return {
        "latestStatusDetail": {"code": codigo, "statusByLocale": texto},
        "scanEvents": [{"date": fecha}, {"date": "2026-07-14T08:00:00-05:00"}],
        **extra,
    }


def test_entregado_por_codigo_y_fecha_real_de_entrega():
    r = fedex.normalizar_respuesta_fedex(_resultado(
        "DL", "Entregado",
        dateAndTimes=[{"type": "ACTUAL_DELIVERY", "dateTime": "2026-07-16T12:00:00-05:00"}],
    ))
    assert r["ok"] and r["estado"] == "ENTREGADO" and r["estado_courier"] == "DL"
    assert r["evento_at"] == datetime(2026, 7, 16, 17, 0, tzinfo=timezone.utc)


def test_en_transito_usa_el_ultimo_escaneo():
    r = fedex.normalizar_respuesta_fedex(_resultado("IT", "En tránsito"))
    assert r["estado"] == "PROCESO_ENTREGA"
    assert r["descripcion"] == "En tránsito"
    assert r["evento_at"] == datetime(2026, 7, 15, 15, 20, tzinfo=timezone.utc)


def test_excepcion_es_retenido_y_cancelado_o_error_no_cambian_estado():
    assert fedex.normalizar_respuesta_fedex(_resultado("DE", "Excepción de entrega"))["estado"] == "RETENIDO"
    assert fedex.normalizar_respuesta_fedex(_resultado("CA", "Cancelado"))["ok"] is False
    assert fedex.normalizar_respuesta_fedex({"error": {"message": "Tracking not found"}})["ok"] is False
    assert fedex.normalizar_respuesta_fedex({})["ok"] is False


def test_sin_credenciales_o_en_sandbox_no_consulta(monkeypatch):
    class Cliente:
        api_key = secret_key = None
        environment = "production"
    monkeypatch.setattr("core.fedex_client.FedExClient", Cliente)
    assert fedex.actualizar_trackings_diarios_fedex()["motivo"] == "fedex_no_configurado"

    class Sandbox:
        api_key = secret_key = "x"
        environment = "sandbox"
    monkeypatch.setattr("core.fedex_client.FedExClient", Sandbox)
    monkeypatch.delenv("FEDEX_TRACKING_PERMITIR_SANDBOX", raising=False)
    assert fedex.actualizar_trackings_diarios_fedex()["motivo"] == "fedex_sandbox"


def test_el_job_diario_tambien_corre_fedex():
    fuente = (RAIZ / "servicios/tracking_envios.py").read_text(encoding="utf-8")
    assert "actualizar_trackings_fedex_seguro()" in fuente


@pytest.mark.skipif(not DATABASE_URL, reason="requiere TAURO_TEST_DATABASE_URL aislada")
def test_postgres_actualiza_hasta_entregado_y_despues_no_consulta(monkeypatch):
    schema = f"test_fedex_{uuid.uuid4().hex}"
    admin = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute((RAIZ / "sql/schema.sql").read_text(encoding="utf-8"))
            cur.execute("""INSERT INTO clientes (cliente_id, email, nombre)
                           VALUES ('FDX', 'fdx@example.invalid', 'FDX')""")
            cur.execute("""INSERT INTO solicitudes_guia (cliente_id, producto_alias, destino_pais,
                               dest_nombre, dest_direccion, dest_ciudad, dest_zip, courier,
                               tracking, estado, coti_id, precio_tauro_ars)
                           VALUES ('FDX','Lana','US','Cliente','Calle 1','Austin','78701','FEDEX',
                                   '874423560428','DESPACHADO','COTI-FDX',1000) RETURNING id""")
            solicitud_id = int(cur.fetchone()["id"])

        @contextmanager
        def conexion():
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{schema}"')
                yield conn
                conn.commit()
            finally:
                conn.close()

        monkeypatch.setattr(fedex, "get_conn", conexion)

        class Cliente:
            def __init__(self):
                self.pedidos = []

            def track_many(self, numeros):
                self.pedidos.append(list(numeros))
                return {n: _resultado("DL", "Entregado") for n in numeros}

        cliente = Cliente()
        primero = fedex.actualizar_trackings_diarios_fedex(cliente_fedex=cliente)
        with admin.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            # Aunque sea otro día, un entregado ya no se vuelve a consultar.
            cur.execute("UPDATE solicitudes_guia SET tracking_consultado_at = NOW() - INTERVAL '2 days'")
        segundo = fedex.actualizar_trackings_diarios_fedex(cliente_fedex=cliente)

        assert primero["entregados"] == 1 and segundo["candidatos"] == 0
        assert cliente.pedidos == [["874423560428"]]
        with admin.cursor() as cur:
            cur.execute("SELECT estado, tracking_estado, tracking_evento_at FROM solicitudes_guia WHERE id=%s",
                        (solicitud_id,))
            fila = cur.fetchone()
        assert fila["estado"] == "ENTREGADO" and fila["tracking_estado"] == "ENTREGADO"
        assert fila["tracking_evento_at"] is not None
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
