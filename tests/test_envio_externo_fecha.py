"""Envío realizado cargado tarde: el envío y su cargo quedan con la fecha real."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import pytest

from servicios import cotizador, cuenta_corriente, solicitudes_guia


RAIZ = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
AR = ZoneInfo("America/Argentina/Buenos_Aires")


def _hoy() -> date:
    return datetime.now(AR).date()


def test_valida_fecha_futura_y_demasiado_vieja():
    hoy = _hoy()
    assert solicitudes_guia.validar_fecha_envio_externo(hoy) == ""
    assert solicitudes_guia.validar_fecha_envio_externo(hoy - timedelta(days=50)) == ""
    assert "futura" in solicitudes_guia.validar_fecha_envio_externo(hoy + timedelta(days=1))
    assert "más de un año" in solicitudes_guia.validar_fecha_envio_externo(
        hoy - timedelta(days=401)
    )


def test_formulario_ofrece_fecha_opcional():
    plantilla = (RAIZ / "templates/admin/envio_realizado_form.html").read_text(encoding="utf-8")
    assert 'type="date" name="fecha_envio"' in plantilla
    assert "required" not in plantilla.split('name="fecha_envio"')[1].split(">")[0]


@pytest.fixture
def envio_db(monkeypatch):
    if not DATABASE_URL:
        pytest.skip("requiere TAURO_TEST_DATABASE_URL aislada")

    schema = f"test_envio_externo_fecha_{uuid.uuid4().hex}"
    schema_sql = (RAIZ / "sql/schema.sql").read_text(encoding="utf-8")
    admin = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def conexion():
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
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

        monkeypatch.setattr(solicitudes_guia, "get_conn", conexion)
        monkeypatch.setattr(cuenta_corriente, "get_conn", conexion)
        monkeypatch.setattr(cotizador, "dolar_ars", lambda: 1500.0)
        with conexion() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clientes (cliente_id,email) "
                    "VALUES ('WAIMAO','waimao-fecha@example.invalid')"
                )
        yield conexion
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _cargar(**extra):
    base = dict(
        cliente_id="WAIMAO", dest_nombre="Fernando Architector", dest_ciudad="CABA",
        destino_pais="AR", producto="Muestras de tela", cantidad=1, peso_kg=12.5,
        tracking=f"99{uuid.uuid4().int % 10**8:08d}", precio_tauro_ars=756755.0,
        courier="DHL", origen_pais="BD", costo_courier_estimado_ars=415923.6,
    )
    base.update(extra)
    return solicitudes_guia.cargar_envio_externo(**base)


def test_envio_tardio_queda_con_fecha_real(envio_db):
    fecha = _hoy() - timedelta(days=26)
    resultado = _cargar(fecha_envio=fecha)
    assert resultado["ok"], resultado
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fecha, monto_ars, estado FROM envios WHERE solicitud_id=%s",
                (resultado["solicitud_id"],),
            )
            cargo = cur.fetchone()
            cur.execute(
                "SELECT created_at, guia_generada_at, estado FROM solicitudes_guia WHERE id=%s",
                (resultado["solicitud_id"],),
            )
            solicitud = cur.fetchone()
    assert cargo["fecha"] == fecha
    assert cargo["estado"] == "ACTIVO"
    assert float(cargo["monto_ars"]) == 756755.0
    assert solicitud["created_at"].astimezone(AR).date() == fecha
    assert solicitud["guia_generada_at"].astimezone(AR).date() == fecha
    assert solicitud["estado"] == "GUIA_LISTA"


def test_sin_fecha_sigue_siendo_hoy(envio_db):
    resultado = _cargar()
    assert resultado["ok"], resultado
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT fecha FROM envios WHERE solicitud_id=%s",
                        (resultado["solicitud_id"],))
            assert cur.fetchone()["fecha"] == _hoy()


def test_fecha_futura_no_crea_nada(envio_db):
    resultado = _cargar(fecha_envio=_hoy() + timedelta(days=3))
    assert not resultado["ok"]
    assert "futura" in resultado["error"]
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM solicitudes_guia")
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT COUNT(*) AS n FROM envios")
            assert cur.fetchone()["n"] == 0
