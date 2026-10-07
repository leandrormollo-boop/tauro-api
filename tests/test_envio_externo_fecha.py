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


def _guia_historica(conexion, *, cliente="WAIMAO", estado="ENTREGADO", tracking="5377135584"):
    with conexion() as conn:
        with conn.cursor() as cur:
            if cliente != "WAIMAO":
                cur.execute(
                    "INSERT INTO clientes (cliente_id,email) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (cliente, f"{cliente.lower()}@example.invalid"),
                )
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, remitente_pais, destino_pais,
                    dest_nombre, dest_direccion, dest_ciudad, dest_zip, estado,
                    tracking, ambito, courier, visible_cliente, cargo_pendiente,
                    created_at
                ) VALUES (
                    %s, 'Envio historico WAIMAO 2026', 'CN', 'AR', 'BERAJATEX SRL',
                    '', '', '', %s, %s, 'INTERNACIONAL', 'DHL', FALSE, FALSE,
                    '2026-08-29 12:00:00-03'
                ) RETURNING id
                """,
                (cliente, estado, tracking),
            )
            return cur.fetchone()["id"]


def test_completa_guia_existente_sin_cargo(envio_db):
    hoy = _hoy()
    fecha = hoy - timedelta(days=30)
    sid = _guia_historica(envio_db)
    resultado = _cargar(
        tracking="5377135584", precio_tauro_ars=1028160.0,
        costo_courier_estimado_ars=930393.0, producto="Gorras (muestras)",
        fecha_envio=fecha, label_pdf=b"%PDF-1.4\n%%EOF\n",
    )
    assert resultado == {"ok": True, "solicitud_id": sid, "completada": True}
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM solicitudes_guia")
            assert cur.fetchone()["n"] == 1
            cur.execute(
                "SELECT fecha, monto_ars, estado FROM envios WHERE solicitud_id=%s", (sid,)
            )
            cargo = cur.fetchone()
            cur.execute(
                "SELECT precio_tauro_ars, visible_cliente, producto_alias, estado, "
                "cargo_pendiente, label_pdf IS NOT NULL AS tiene_label "
                "FROM solicitudes_guia WHERE id=%s",
                (sid,),
            )
            sol = cur.fetchone()
    assert cargo["fecha"] == fecha
    assert float(cargo["monto_ars"]) == 1028160.0
    assert cargo["estado"] == "ACTIVO"
    assert float(sol["precio_tauro_ars"]) == 1028160.0
    assert sol["visible_cliente"] is True
    assert sol["producto_alias"] == "Gorras (muestras)"
    assert sol["estado"] == "ENTREGADO"
    assert sol["cargo_pendiente"] is False
    assert sol["tiene_label"] is True

    # Una segunda carga ya no completa nada: la guía tiene cargo.
    otra = _cargar(tracking="5377135584", precio_tauro_ars=1.0, fecha_envio=fecha)
    assert not otra["ok"]
    assert "ya está cargado" in otra["error"]


def test_sin_fecha_conserva_la_fecha_original_de_la_guia(envio_db):
    sid = _guia_historica(envio_db, tracking="4164037172")
    resultado = _cargar(tracking="4164037172", precio_tauro_ars=626700.0)
    assert resultado["ok"], resultado
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT fecha FROM envios WHERE solicitud_id=%s", (sid,))
            assert cur.fetchone()["fecha"] == date(2026, 8, 29)


@pytest.mark.parametrize("cliente,estado", [("OTRO", "ENTREGADO"), ("WAIMAO", "CANCELADO")])
def test_no_completa_guia_de_otro_cliente_ni_cancelada(envio_db, cliente, estado):
    _guia_historica(envio_db, cliente=cliente, estado=estado)
    resultado = _cargar(tracking="5377135584", precio_tauro_ars=1028160.0)
    assert not resultado["ok"]
    assert "ya está cargado" in resultado["error"]
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM envios")
            assert cur.fetchone()["n"] == 0
