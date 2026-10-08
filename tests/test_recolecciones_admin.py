"""Servicio de pickups ADMIN sobre PostgreSQL aislado, sin APIs reales."""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import psycopg2
import psycopg2.extras
import pytest
from psycopg2.extras import Json

from servicios import recolecciones as rec
from servicios import recolecciones_admin as admin_rec


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


def _proximo_habil() -> str:
    fecha = date.today() + timedelta(days=1)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha.isoformat()


@pytest.fixture
def db(monkeypatch):
    schema = f"test_recoleccion_admin_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    raiz = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
    )
    raiz.autocommit = True
    try:
        with raiz.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def get_conn():
            conn = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
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

        monkeypatch.setattr(admin_rec, "get_conn", get_conn)
        monkeypatch.setattr(rec, "get_conn", get_conn)
        monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
        monkeypatch.setattr(
            admin_rec,
            "_courier_habilitado_para_retiro",
            lambda _courier: (True, ""),
        )
        yield get_conn
    finally:
        with raiz.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        raiz.close()


def _crear_guia(db, *, estado="GUIA_LISTA", test=False, tracking="JD014600006838"):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clientes (cliente_id, email, nombre)
            VALUES ('CLIENTE_PICKUP', 'pickup@example.invalid', 'Cliente Pickup')
            ON CONFLICT (cliente_id) DO NOTHING
            """
        )
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, estado, producto_alias, cantidad,
                remitente_nombre, remitente_contacto, remitente_telefono,
                remitente_direccion, remitente_ciudad, remitente_estado,
                remitente_zip, remitente_pais, ambito,
                destino_pais, dest_nombre, dest_direccion, dest_ciudad,
                dest_zip, peso_kg, largo_cm, ancho_cm, alto_cm,
                valor_declarado_usd, courier, tracking, bultos, test
            ) VALUES (
                'CLIENTE_PICKUP', %s, 'Repuestos', 2,
                'Fabrica Origen SA', 'Ana Origen', '+54 11 5555 0101',
                'Calle Original 123', 'Buenos Aires', 'CABA',
                '1001', 'AR', 'INTERNACIONAL',
                'US', 'Destino', '1 Ocean Dr', 'Miami', '33139',
                5.5, 30, 20, 10, 80, 'DHL', %s, %s, %s
            ) RETURNING id
            """,
            (
                estado,
                tracking,
                Json(
                    [
                        {
                            "producto_alias": "Repuesto A",
                            "cantidad": 2,
                            "peso_kg": 2.75,
                            "largo_cm": 30,
                            "ancho_cm": 20,
                            "alto_cm": 10,
                            "unidades_aduana": 1,
                            "valor_unitario_usd": 40,
                        }
                    ]
                ),
                test,
            ),
        )
        return int(cur.fetchone()["id"])


def _programar(solicitud_id, *, clave="pickup_admin_00000000000000000001", **cambios):
    datos = {
        "solicitud_id": solicitud_id,
        "fecha": _proximo_habil(),
        "ready_time": "09:00",
        "close_time": "17:00",
        "instrucciones": "Tocar timbre en recepción",
        "actor": "admin@example.invalid",
        "idempotency_key": clave,
    }
    datos.update(cambios)
    return admin_rec.programar_recoleccion_admin(**datos)


def test_contexto_deriva_origen_y_cajas_de_la_guia_sin_llamar_api(db, monkeypatch):
    solicitud_id = _crear_guia(db)
    api = mock.Mock()
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)

    contexto = admin_rec.contexto_recoleccion_admin(solicitud_id)

    assert contexto["habilitada"] is True
    assert contexto["cliente_id"] == "CLIENTE_PICKUP"
    assert contexto["courier"] == "DHL"
    assert contexto["origen"]["calle"] == "Calle Original 123"
    assert contexto["origen"]["nombre"] == "Ana Origen"
    assert contexto["cajas"]["bultos"] == 2
    assert contexto["cajas"]["peso_kg"] == 5.5
    api.create_pickup.assert_not_called()


def test_programa_con_snapshot_original_audita_actor_y_reintento_es_estable(
    db, monkeypatch
):
    solicitud_id = _crear_guia(db)
    api = mock.Mock()
    api.create_pickup.return_value = {
        "encontrado": True,
        "confirmation_code": "PU-ADMIN-1",
        "ubicacion": "BUE",
        # El adapter no puede reemplazar la clave durable de replay.
        "message_reference": "referencia-externa-distinta",
    }
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)

    primera = _programar(solicitud_id)
    repetida = _programar(solicitud_id)

    assert primera["ok"] is True and primera["duplicado"] is False
    assert repetida == {
        "id": primera["id"],
        "solicitud_id": solicitud_id,
        "duplicado": True,
        "estado": "AGENDADA",
        "ok": True,
        "confirmation_code": "PU-ADMIN-1",
    }
    api.create_pickup.assert_called_once()
    payload = api.create_pickup.call_args.args[0]
    assert payload["origen"]["calle"] == "Calle Original 123"
    assert payload["origen"]["nombre"] == "Ana Origen"
    assert payload["bultos"] == 2
    assert payload["peso_kg"] == 5.5
    assert payload["message_reference"].startswith("tauro-admin-pick-")

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor_type, actor_ref, metadata
            FROM security_audit
            WHERE event='admin.programar_recoleccion'
            ORDER BY id
            """
        )
        auditoria = cur.fetchall()
        cur.execute(
            "SELECT courier_message_reference FROM recolecciones WHERE id=%s",
            (primera["id"],),
        )
        referencia_guardada = cur.fetchone()["courier_message_reference"]
    assert referencia_guardada == payload["message_reference"]
    assert [fila["metadata"]["resultado"] for fila in auditoria] == ["AGENDADA"]
    assert all(fila["actor_type"] == "admin" for fila in auditoria)
    assert all(fila["actor_ref"] == "admin@example.invalid" for fila in auditoria)


def test_reutilizar_token_con_otra_ventana_no_llama_otra_vez(db, monkeypatch):
    solicitud_id = _crear_guia(db)
    api = mock.Mock()
    api.create_pickup.return_value = {
        "encontrado": True,
        "confirmation_code": "PU-ADMIN-2",
    }
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)
    assert _programar(solicitud_id)["ok"] is True

    salida = _programar(solicitud_id, close_time="18:00")

    assert salida["ok"] is False
    assert "otros datos" in salida["error"]
    api.create_pickup.assert_called_once()


@pytest.mark.parametrize(
    ("cambios", "error"),
    [
        ({"test": True}, "prueba"),
        ({"estado": "SOLICITADO", "tracking": ""}, "emitida"),
        ({"estado": "CANCELADO"}, "emitida"),
    ],
)
def test_rechaza_solicitudes_que_no_son_guias_reales_aptas(
    db, monkeypatch, cambios, error
):
    solicitud_id = _crear_guia(db, **cambios)
    api = mock.Mock()
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)

    salida = _programar(solicitud_id)

    assert salida["ok"] is False
    assert error in salida["error"].lower()
    api.create_pickup.assert_not_called()


def test_timeout_conserva_reserva_y_reintento_no_duplica_visita(db, monkeypatch):
    solicitud_id = _crear_guia(db)
    api = mock.Mock()
    api.create_pickup.side_effect = TimeoutError("sin respuesta")
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)

    primera = _programar(solicitud_id)
    repetida = _programar(solicitud_id)

    assert primera["ok"] is False and primera["incierto"] is True
    assert repetida["ok"] is False and repetida["incierto"] is True
    assert repetida["duplicado"] is True
    api.create_pickup.assert_called_once()
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT estado FROM recolecciones WHERE solicitud_id=%s",
            (solicitud_id,),
        )
        assert cur.fetchone()["estado"] == "VERIFICAR_COURIER"


def test_cuenta_suspendida_falla_antes_del_courier(db, monkeypatch):
    solicitud_id = _crear_guia(db)
    api = mock.Mock()
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE clientes SET activo=FALSE WHERE cliente_id='CLIENTE_PICKUP'"
        )
    suspendida = _programar(solicitud_id)
    assert suspendida["ok"] is False and "suspendida" in suspendida["error"]
    api.create_pickup.assert_not_called()


def test_retiro_para_hoy_usa_la_validacion_comun_y_deja_decidir_al_courier(
    db, monkeypatch
):
    solicitud_id = _crear_guia(db)
    api = mock.Mock()
    api.create_pickup.return_value = {
        "encontrado": True,
        "confirmation_code": "PU-HOY-1",
    }
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)
    # Hace determinista el caso incluso si la suite corre durante un fin de
    # semana: el servicio ADMIN debe delegar la política de fecha al validador
    # común, no agregar su propio bloqueo de mismo día.
    validador_fecha = mock.Mock(return_value=None)
    monkeypatch.setattr(rec, "_dias_habiles_validos", validador_fecha)

    mismo_dia = _programar(
        solicitud_id,
        fecha=date.today().isoformat(),
        clave="pickup_admin_00000000000000000021",
    )
    assert mismo_dia["ok"] is True
    validador_fecha.assert_called_once_with(date.today().isoformat())
    assert api.create_pickup.call_args.args[0]["fecha"] == date.today().isoformat()


def test_dos_tokens_concurrentes_reservan_una_sola_visita(db, monkeypatch):
    solicitud_id = _crear_guia(db)
    api_entro = threading.Event()
    liberar_api = threading.Event()
    llamadas = []

    class Api:
        def create_pickup(self, payload):
            llamadas.append(payload)
            api_entro.set()
            assert liberar_api.wait(timeout=5)
            return {"encontrado": True, "confirmation_code": "PU-RACE-1"}

    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: Api())
    with ThreadPoolExecutor(max_workers=2) as executor:
        primera = executor.submit(
            _programar,
            solicitud_id,
            clave="pickup_admin_00000000000000000011",
        )
        assert api_entro.wait(timeout=5)
        segunda = executor.submit(
            _programar,
            solicitud_id,
            clave="pickup_admin_00000000000000000012",
        )
        salida_2 = segunda.result(timeout=5)
        liberar_api.set()
        salida_1 = primera.result(timeout=5)

    assert salida_1["ok"] is True
    assert salida_2["ok"] is False
    assert salida_2["recoleccion_conflicto_id"] == salida_1["id"]
    assert len(llamadas) == 1
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cantidad FROM recolecciones")
        assert cur.fetchone()["cantidad"] == 1


def test_conflicto_de_otro_envio_en_mismo_origen_fecha_es_veraz(db, monkeypatch):
    solicitud_1 = _crear_guia(db, tracking="JD014600006839")
    solicitud_2 = _crear_guia(db, tracking="JD014600006840")
    api = mock.Mock()
    api.create_pickup.return_value = {
        "encontrado": True,
        "confirmation_code": "PU-ORIGEN-1",
    }
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)
    primera = _programar(
        solicitud_1,
        clave="pickup_admin_00000000000000000031",
    )

    segunda = _programar(
        solicitud_2,
        clave="pickup_admin_00000000000000000032",
    )

    assert primera["ok"] is True
    assert segunda["ok"] is False
    assert segunda["recoleccion_conflicto_id"] == primera["id"]
    assert "ese origen, courier y fecha" in segunda["error"]
    assert "esa guía" not in segunda["error"]
    api.create_pickup.assert_called_once()


def test_rechazo_definitivo_libera_reserva_sin_ocultar_auditoria(db, monkeypatch):
    solicitud_id = _crear_guia(db)
    api = mock.Mock()
    api.create_pickup.return_value = {
        "encontrado": False,
        "incierto": False,
        "error": "Fuera de cobertura",
    }
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)

    salida = _programar(solicitud_id)

    assert salida == {"ok": False, "error": "Fuera de cobertura"}
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cantidad FROM recolecciones")
        assert cur.fetchone()["cantidad"] == 0
        cur.execute(
            """
            SELECT metadata->>'resultado' AS resultado
            FROM security_audit
            WHERE event='admin.programar_recoleccion'
            ORDER BY id DESC LIMIT 1
            """
        )
        assert cur.fetchone()["resultado"] == "RECHAZADA"
