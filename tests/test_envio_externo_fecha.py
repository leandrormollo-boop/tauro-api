"""Envío realizado cargado tarde: el envío y su cargo quedan con la fecha real."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Barrier
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


def test_no_completa_si_hay_cargo_manual_legado_del_tracking(envio_db):
    tracking = "5377-135-584"
    sid = _guia_historica(envio_db, tracking=tracking)
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id, fecha, monto_ars, estado, tracking, ambito
                ) VALUES ('WAIMAO', CURRENT_DATE, 1028160, 'ACTIVO',
                          '5377135584', 'INTERNACIONAL')
                """
            )

    resultado = _cargar(tracking=tracking, precio_tauro_ars=1028160.0)
    assert not resultado["ok"]
    assert "ya está cargado" in resultado["error"]
    estado = _estado_completar(envio_db, sid)
    assert estado["precio_tauro_ars"] is None
    assert estado["visible_cliente"] is False
    assert estado["cargos"] == 0
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM envios")
            assert cur.fetchone()["n"] == 1


def _estado_completar(conexion, solicitud_id):
    with conexion() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT precio_tauro_ars, visible_cliente, cargo_pendiente,
                       label_pdf, producto_alias, created_at, guia_generada_at
                FROM solicitudes_guia
                WHERE id=%s
                """,
                (solicitud_id,),
            )
            solicitud = dict(cur.fetchone())
            cur.execute(
                "SELECT COUNT(*) AS n FROM envios WHERE solicitud_id=%s",
                (solicitud_id,),
            )
            solicitud["cargos"] = cur.fetchone()["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM envio_cotizacion_snapshots "
                "WHERE solicitud_id=%s",
                (solicitud_id,),
            )
            solicitud["snapshots"] = cur.fetchone()["n"]
            return solicitud


def test_fallo_del_cargo_revierte_toda_la_completitud(envio_db, monkeypatch):
    sid = _guia_historica(envio_db, tracking="1234567890")
    cargar_real = cuenta_corriente.cargar_guia_emitida

    def cargar_y_fallar(solicitud_id, *, conexion=None):
        assert cargar_real(solicitud_id, conexion=conexion) is True
        raise RuntimeError("fallo simulado después del INSERT")

    monkeypatch.setattr(cuenta_corriente, "cargar_guia_emitida", cargar_y_fallar)
    resultado = _cargar(
        tracking="1234567890",
        precio_tauro_ars=810000.0,
        costo_courier_estimado_ars=700000.0,
        producto="Muestras",
        label_pdf=b"%PDF-1.4\n%%EOF\n",
        fecha_envio=_hoy() - timedelta(days=20),
    )

    assert not resultado["ok"]
    assert resultado["error"] == (
        "No pudimos completar la guía. Revisá los datos y volvé a intentar."
    )
    estado = _estado_completar(envio_db, sid)
    assert estado["precio_tauro_ars"] is None
    assert estado["visible_cliente"] is False
    assert estado["cargo_pendiente"] is False
    assert estado["label_pdf"] is None
    assert estado["producto_alias"] == "Envio historico WAIMAO 2026"
    assert estado["cargos"] == 0
    assert estado["snapshots"] == 0


def test_fallo_del_fechado_revierte_y_admite_reintento(envio_db, monkeypatch):
    tracking = "2234567890"
    sid = _guia_historica(envio_db, tracking=tracking)
    fecha = _hoy() - timedelta(days=25)
    fechar_real = solicitudes_guia._fechar_envio_externo

    def fechar_y_fallar(solicitud_id, fecha_envio, *, conexion=None):
        assert fechar_real(solicitud_id, fecha_envio, conexion=conexion) is True
        raise RuntimeError("fallo simulado después del fechado")

    monkeypatch.setattr(solicitudes_guia, "_fechar_envio_externo", fechar_y_fallar)
    fallido = _cargar(
        tracking=tracking,
        precio_tauro_ars=720000.0,
        costo_courier_estimado_ars=600000.0,
        label_pdf=b"%PDF-1.4\n%%EOF\n",
        fecha_envio=fecha,
    )
    assert not fallido["ok"]
    estado = _estado_completar(envio_db, sid)
    assert estado["precio_tauro_ars"] is None
    assert estado["visible_cliente"] is False
    assert estado["label_pdf"] is None
    assert estado["cargos"] == 0
    assert estado["snapshots"] == 0
    assert estado["created_at"].astimezone(AR).date() == date(2026, 8, 29)
    assert estado["guia_generada_at"] is None

    monkeypatch.setattr(solicitudes_guia, "_fechar_envio_externo", fechar_real)
    reintento = _cargar(
        tracking=tracking,
        precio_tauro_ars=720000.0,
        costo_courier_estimado_ars=600000.0,
        fecha_envio=fecha,
    )
    assert reintento == {"ok": True, "solicitud_id": sid, "completada": True}
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fecha FROM envios WHERE solicitud_id=%s",
                (sid,),
            )
            assert cur.fetchone()["fecha"] == fecha


@pytest.mark.parametrize("cambio", ["cliente", "tracking"])
def test_revalida_dueno_y_tracking_al_completar(envio_db, monkeypatch, cambio):
    tracking = "3234567890"
    sid = _guia_historica(envio_db, tracking=tracking)
    completar_real = solicitudes_guia._completar_guia_sin_cargo

    def cambiar_antes_de_completar(solicitud_id, **kwargs):
        with envio_db() as conn:
            with conn.cursor() as cur:
                if cambio == "cliente":
                    cur.execute(
                        "INSERT INTO clientes (cliente_id,email) "
                        "VALUES ('OTRO','otro@example.invalid') ON CONFLICT DO NOTHING"
                    )
                    cur.execute(
                        "UPDATE solicitudes_guia SET cliente_id='OTRO' WHERE id=%s",
                        (solicitud_id,),
                    )
                else:
                    cur.execute(
                        "UPDATE solicitudes_guia SET tracking='TRACKING-CAMBIADO' WHERE id=%s",
                        (solicitud_id,),
                    )
        return completar_real(solicitud_id, **kwargs)

    monkeypatch.setattr(
        solicitudes_guia,
        "_completar_guia_sin_cargo",
        cambiar_antes_de_completar,
    )
    resultado = _cargar(tracking=tracking, precio_tauro_ars=650000.0)
    assert not resultado["ok"]
    assert "cambió" in resultado["error"]
    estado = _estado_completar(envio_db, sid)
    assert estado["precio_tauro_ars"] is None
    assert estado["visible_cliente"] is False
    assert estado["cargos"] == 0


def test_dos_completados_concurrentes_generan_un_solo_cargo(envio_db, monkeypatch):
    tracking = "4234567890"
    sid = _guia_historica(envio_db, tracking=tracking)
    completar_real = solicitudes_guia._completar_guia_sin_cargo
    barrera = Barrier(2)

    def completar_a_la_vez(solicitud_id, **kwargs):
        barrera.wait(timeout=5)
        return completar_real(solicitud_id, **kwargs)

    monkeypatch.setattr(
        solicitudes_guia,
        "_completar_guia_sin_cargo",
        completar_a_la_vez,
    )
    fecha = _hoy() - timedelta(days=10)
    with ThreadPoolExecutor(max_workers=2) as ejecutor:
        futuros = [
            ejecutor.submit(
                _cargar,
                tracking=tracking,
                precio_tauro_ars=900000.0,
                costo_courier_estimado_ars=780000.0,
                fecha_envio=fecha,
            )
            for _ in range(2)
        ]
        resultados = [f.result(timeout=10) for f in futuros]

    assert sum(bool(resultado["ok"]) for resultado in resultados) == 1
    assert any("cambió" in resultado["error"] for resultado in resultados if not resultado["ok"])
    with envio_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n, MIN(fecha) AS fecha FROM envios "
                "WHERE solicitud_id=%s",
                (sid,),
            )
            cargo = cur.fetchone()
            assert cargo["n"] == 1
            assert cargo["fecha"] == fecha
