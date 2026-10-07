"""Regresiones PostgreSQL para la reserva de retiros por origen real.

Los couriers, permisos y fuentes de guia son dobles. La exclusion mutua,
los snapshots y las carreras se ejercitan contra el schema canonico real.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import psycopg2
import pytest
from psycopg2.extras import Json

from servicios import recolecciones as rec
from test_conciliacion_couriers_postgres import DATABASE_URL, conciliacion_db


pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)

CLIENTE = "WAIMAO"
ESTADOS_ABIERTOS = (
    "AGENDANDO", "AGENDADA", "CANCELANDO", "VERIFICAR_COURIER",
)


def _dias_habiles(cantidad: int = 2) -> list[str]:
    dias = []
    actual = date.today() + timedelta(days=1)
    while len(dias) < cantidad:
        if actual.weekday() < 5:
            dias.append(actual.isoformat())
        actual += timedelta(days=1)
    return dias


class _CourierStub:
    def __init__(self, respuestas=None):
        self._lock = threading.Lock()
        self._respuestas = list(respuestas or [])
        self.llamadas = []

    def create_pickup(self, payload):
        with self._lock:
            self.llamadas.append(payload)
            numero = len(self.llamadas)
            respuesta = self._respuestas.pop(0) if self._respuestas else None
        if isinstance(respuesta, BaseException):
            raise respuesta
        return respuesta or {
            "encontrado": True,
            "confirmation_code": f"DHL-PICK-{numero}",
            "ubicacion": "origen",
            "message_reference": f"pickup-ref-{numero}",
        }


@pytest.fixture
def entorno(conciliacion_db, monkeypatch):
    fuentes = {}
    api_actual = {"valor": _CourierStub()}

    with conciliacion_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clientes (cliente_id, email, nombre)
            VALUES (%s, %s, %s)
            """,
            (CLIENTE, "pickup-origin@example.invalid", CLIENTE),
        )

    def obtener_solicitud(solicitud_id, cliente_id):
        if cliente_id != CLIENTE:
            return None
        return fuentes.get(int(solicitud_id))

    monkeypatch.setattr(rec, "get_conn", conciliacion_db)
    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "cliente_puede_recolectar", lambda *_args: True)
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api_actual["valor"])
    monkeypatch.setattr(
        "servicios.configuracion_couriers_cliente.estado_integracion",
        lambda _courier: {"operativa": True},
    )
    monkeypatch.setattr(
        "servicios.solicitudes_guia.obtener_solicitud_de_cliente",
        obtener_solicitud,
    )

    return {
        "db": conciliacion_db,
        "fuentes": fuentes,
        "api_actual": api_actual,
    }


def _agregar_guia(entorno, *, origen: dict, sufijo: str | None = None) -> int:
    sufijo = sufijo or uuid.uuid4().hex[:10]
    pais = origen["pais"].strip().upper()
    destino = "US" if pais == "AR" else "AR"
    tracking = f"DHL-{sufijo}-{uuid.uuid4().hex[:8]}"
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, estado, producto_alias, cantidad, courier,
                tracking, ambito, destino_pais, dest_nombre, dest_direccion,
                dest_ciudad, dest_zip
            ) VALUES (
                %s, 'GUIA_LISTA', 'CARGA', 1, 'DHL', %s, 'INTERNACIONAL',
                %s, 'Destino', 'Destination 1', 'Miami', '33101'
            )
            RETURNING id
            """,
            (CLIENTE, tracking, destino),
        )
        solicitud_id = int(cur.fetchone()["id"])

    entorno["fuentes"][solicitud_id] = {
        "id": solicitud_id,
        "cliente_id": CLIENTE,
        "estado": "GUIA_LISTA",
        "courier": "DHL",
        "tracking": tracking,
        "ambito": "INTERNACIONAL",
        "remitente_nombre": "Proveedor",
        "remitente_contacto": "Contacto",
        "remitente_telefono": "+00 123",
        "remitente_direccion": origen["calle"],
        "remitente_ciudad": origen["ciudad"],
        "remitente_estado": origen["estado"],
        "remitente_zip": origen["zip"],
        "remitente_pais": origen["pais"],
        "destino_pais": destino,
        "bultos": [{
            "cantidad": 1,
            "unidades_aduana": 1,
            "peso_kg": 2,
            "largo_cm": 30,
            "ancho_cm": 20,
            "alto_cm": 10,
            "valor_unitario_usd": 10,
        }],
    }
    return solicitud_id


def _pedir(solicitud_id: int, fecha: str) -> dict:
    return rec.crear(
        CLIENTE,
        fecha,
        "09:00",
        "17:00",
        999,
        999,
        courier="FEDEX",
        solicitud_id=solicitud_id,
    )


def _origen_ar(**cambios):
    origen = {
        "pais": "AR",
        "estado": "C",
        "ciudad": "CABA",
        "zip": "1043",
        "calle": "Av. Corrientes 1234",
    }
    origen.update(cambios)
    return origen


def _origen_cn(**cambios):
    origen = {
        "pais": "CN",
        "estado": "Zhejiang",
        "ciudad": "Yiwu",
        "zip": "322000",
        "calle": "88 Fabric Road",
    }
    origen.update(cambios)
    return origen


def test_misma_fecha_admite_ar_y_cn_y_congela_el_origen_enviado(entorno):
    fecha = _dias_habiles(1)[0]
    solicitud_ar = _agregar_guia(entorno, origen=_origen_ar(), sufijo="AR")
    solicitud_cn = _agregar_guia(entorno, origen=_origen_cn(), sufijo="CN")

    salidas = [_pedir(solicitud_ar, fecha), _pedir(solicitud_cn, fecha)]

    assert [salida["ok"] for salida in salidas] == [True, True]
    api = entorno["api_actual"]["valor"]
    assert len(api.llamadas) == 2
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT solicitud_id, origen_retiro, origen_clave
            FROM recolecciones
            ORDER BY solicitud_id
            """
        )
        filas = {int(f["solicitud_id"]): f for f in cur.fetchall()}

    for solicitud_id, payload in zip(
        (solicitud_ar, solicitud_cn), api.llamadas, strict=True,
    ):
        snapshot_enviado = {
            campo: payload["origen"][campo]
            for campo in ("pais", "estado", "ciudad", "zip", "calle")
        }
        assert filas[solicitud_id]["origen_retiro"] == snapshot_enviado
        assert filas[solicitud_id]["origen_clave"]


def test_mismo_origen_con_variantes_normalizadas_bloquea_antes_del_courier(entorno):
    fecha = _dias_habiles(1)[0]
    solicitud_1 = _agregar_guia(entorno, origen=_origen_ar(), sufijo="NORMAL-1")
    solicitud_2 = _agregar_guia(
        entorno,
        origen=_origen_ar(
            pais=" ar ",
            estado=" c ",
            ciudad="  caba  ",
            zip=" 1043 ",
            calle="  AV CORRIÉNTES-1234 ",
        ),
        sufijo="NORMAL-2",
    )

    primera = _pedir(solicitud_1, fecha)
    segunda = _pedir(solicitud_2, fecha)

    assert primera["ok"] is True
    assert segunda["ok"] is False
    assert segunda["recoleccion_conflicto_id"] == primera["id"]
    assert "ese origen" in segunda["error"]
    assert len(entorno["api_actual"]["valor"].llamadas) == 1


def test_la_misma_guia_sigue_bloqueada_aunque_cambie_el_dia(entorno):
    fecha_1, fecha_2 = _dias_habiles(2)
    solicitud = _agregar_guia(entorno, origen=_origen_cn(), sufijo="MISMA-GUIA")

    primera = _pedir(solicitud, fecha_1)
    segunda = _pedir(solicitud, fecha_2)

    assert primera["ok"] is True
    assert segunda["ok"] is False
    assert segunda["recoleccion_conflicto_id"] == primera["id"]
    assert "esa guía" in segunda["error"]
    assert len(entorno["api_actual"]["valor"].llamadas) == 1


def test_timeout_reserva_solo_su_origen_hasta_conciliar(entorno):
    fecha = _dias_habiles(1)[0]
    solicitud_ar = _agregar_guia(entorno, origen=_origen_ar(), sufijo="TIMEOUT")
    solicitud_ar_2 = _agregar_guia(entorno, origen=_origen_ar(), sufijo="RETRY")
    solicitud_cn = _agregar_guia(entorno, origen=_origen_cn(), sufijo="OTRO-ORIGEN")
    api = _CourierStub([TimeoutError("respuesta perdida")])
    entorno["api_actual"]["valor"] = api

    incierta = _pedir(solicitud_ar, fecha)
    repetida = _pedir(solicitud_ar_2, fecha)
    otro_origen = _pedir(solicitud_cn, fecha)

    assert incierta["ok"] is False and incierta["incierto"] is True
    assert repetida["ok"] is False
    assert repetida["recoleccion_conflicto_id"]
    assert otro_origen["ok"] is True
    assert len(api.llamadas) == 2
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT solicitud_id, estado, origen_retiro->>'pais' AS pais
            FROM recolecciones
            ORDER BY id
            """
        )
        filas = cur.fetchall()
    assert filas == [
        {"solicitud_id": solicitud_ar, "estado": "VERIFICAR_COURIER", "pais": "AR"},
        {"solicitud_id": solicitud_cn, "estado": "AGENDADA", "pais": "CN"},
    ]


def test_historia_sin_origen_no_cambia_y_no_bloquea_un_retiro_nuevo(entorno):
    fecha = _dias_habiles(1)[0]
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recolecciones (
                cliente_id, courier, fecha, direccion, estado, confirmation_code
            ) VALUES (%s, 'DHL', %s, 'dato histórico', 'COMPLETADA', 'HIST-001')
            RETURNING id
            """,
            (CLIENTE, fecha),
        )
        historica_id = int(cur.fetchone()["id"])

    solicitud = _agregar_guia(entorno, origen=_origen_ar(), sufijo="POST-HIST")
    salida = _pedir(solicitud, fecha)

    assert salida["ok"] is True
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT estado, confirmation_code, origen_retiro, origen_clave
            FROM recolecciones WHERE id=%s
            """,
            (historica_id,),
        )
        historica = cur.fetchone()
    assert historica["estado"] == "COMPLETADA"
    assert historica["confirmation_code"] == "HIST-001"
    assert historica["origen_retiro"] == {"_legacy_desconocido": True}
    assert historica["origen_clave"] == f"legacy-desconocido:{historica_id}"


def test_retiro_abierto_sin_origen_falla_cerrado_y_conserva_confirmacion(entorno):
    fecha = _dias_habiles(1)[0]
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recolecciones (
                cliente_id, courier, fecha, direccion, estado, confirmation_code
            ) VALUES (%s, 'DHL', %s, 'dato legacy', 'AGENDADA', 'LEGACY-001')
            RETURNING id
            """,
            (CLIENTE, fecha),
        )
        legacy_id = int(cur.fetchone()["id"])
    solicitud = _agregar_guia(entorno, origen=_origen_cn(), sufijo="LEGACY-GUARD")

    salida = _pedir(solicitud, fecha)

    assert salida["ok"] is False
    assert salida["recoleccion_conflicto_id"] == legacy_id
    assert "verificar el origen" in salida["error"]
    assert entorno["api_actual"]["valor"].llamadas == []
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT confirmation_code FROM recolecciones WHERE id=%s",
            (legacy_id,),
        )
        assert cur.fetchone()["confirmation_code"] == "LEGACY-001"


def test_carrera_del_mismo_origen_hace_una_sola_llamada(entorno):
    fecha = _dias_habiles(1)[0]
    solicitudes = [
        _agregar_guia(entorno, origen=_origen_ar(), sufijo=f"RACE-SAME-{i}")
        for i in range(2)
    ]
    barrera = threading.Barrier(2)

    def competir(solicitud_id):
        barrera.wait()
        return _pedir(solicitud_id, fecha)

    with ThreadPoolExecutor(max_workers=2) as pool:
        salidas = list(pool.map(competir, solicitudes))

    assert sorted(salida["ok"] for salida in salidas) == [False, True]
    assert len(entorno["api_actual"]["valor"].llamadas) == 1
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cantidad FROM recolecciones WHERE estado = ANY(%s)",
            (list(ESTADOS_ABIERTOS),),
        )
        assert cur.fetchone()["cantidad"] == 1


def test_carrera_de_origenes_distintos_hace_dos_llamadas(entorno):
    fecha = _dias_habiles(1)[0]
    solicitudes = [
        _agregar_guia(entorno, origen=_origen_ar(), sufijo="RACE-AR"),
        _agregar_guia(entorno, origen=_origen_cn(), sufijo="RACE-CN"),
    ]
    barrera = threading.Barrier(2)

    def competir(solicitud_id):
        barrera.wait()
        return _pedir(solicitud_id, fecha)

    with ThreadPoolExecutor(max_workers=2) as pool:
        salidas = list(pool.map(competir, solicitudes))

    assert [salida["ok"] for salida in salidas] == [True, True]
    assert len(entorno["api_actual"]["valor"].llamadas) == 2


def _insertar_retiro_crudo(cur, *, fecha: str, origen: dict | None):
    direccion = (
        f"{origen['calle']}, {origen['ciudad']}" if origen
        else "dirección legacy sin snapshot"
    )
    cur.execute(
        """
        INSERT INTO recolecciones (
            cliente_id, courier, fecha, ready_time, close_time, bultos,
            peso_kg, direccion, estado, origen_retiro
        ) VALUES (%s, 'DHL', %s, '09:00', '17:00', 1, 2, %s,
                  'AGENDANDO', %s)
        RETURNING id
        """,
        (CLIENTE, fecha, direccion, Json(origen) if origen else None),
    )
    return int(cur.fetchone()["id"])


def _esperar_advisory_lock(db, pid: int, timeout: float = 3.0):
    limite = time.monotonic() + timeout
    with db() as conn, conn.cursor() as cur:
        while time.monotonic() < limite:
            cur.execute(
                """
                SELECT wait_event_type, wait_event, state
                FROM pg_stat_activity
                WHERE pid=%s
                """,
                (pid,),
            )
            estado = cur.fetchone()
            if estado and estado["wait_event_type"] == "Lock":
                return estado
            time.sleep(0.01)
    return None


@pytest.mark.parametrize("primero", ["legacy", "conocido"])
def test_carrera_legacy_desconocido_y_origen_conocido_falla_cerrado(
    entorno, primero,
):
    fecha = _dias_habiles(1)[0]
    origen = _origen_ar()
    origen_primero = None if primero == "legacy" else origen
    origen_segundo = origen if primero == "legacy" else None
    segunda_inicio = threading.Event()
    segunda_pid = {}

    primera_cm = entorno["db"]()
    primera_conn = primera_cm.__enter__()
    pool = ThreadPoolExecutor(max_workers=1)
    futuro = None
    try:
        with primera_conn.cursor() as cur:
            primera_id = _insertar_retiro_crudo(
                cur, fecha=fecha, origen=origen_primero,
            )

        def insertar_segunda():
            with entorno["db"]() as conn, conn.cursor() as cur:
                cur.execute("SELECT pg_backend_pid() AS pid")
                segunda_pid["valor"] = int(cur.fetchone()["pid"])
                segunda_inicio.set()
                try:
                    _insertar_retiro_crudo(
                        cur, fecha=fecha, origen=origen_segundo,
                    )
                except psycopg2.IntegrityError as exc:
                    resultado = {
                        "pgcode": exc.pgcode,
                        "constraint": exc.diag.constraint_name,
                    }
                    conn.rollback()
                    return resultado
                return {"insertada": True}

        futuro = pool.submit(insertar_segunda)
        assert segunda_inicio.wait(timeout=2)
        espera = _esperar_advisory_lock(
            entorno["db"], segunda_pid["valor"],
        )
        assert espera is not None
        assert espera["wait_event"].lower() == "advisory"
        assert futuro.done() is False

        primera_conn.commit()
        resultado = futuro.result(timeout=3)
    finally:
        if not primera_conn.closed:
            primera_conn.rollback()
        primera_cm.__exit__(None, None, None)
        pool.shutdown(wait=True)

    assert resultado == {
        "pgcode": "23505",
        "constraint": "uq_recoleccion_origen_pendiente_v3",
    }
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, origen_retiro
            FROM recolecciones
            WHERE cliente_id=%s AND fecha=%s AND courier='DHL'
            """,
            (CLIENTE, fecha),
        )
        filas = cur.fetchall()
    assert len(filas) == 1
    assert filas[0]["id"] == primera_id
    if primero == "legacy":
        assert filas[0]["origen_retiro"] == {"_legacy_desconocido": True}
    else:
        assert filas[0]["origen_retiro"] == origen


def test_un_23505_de_primary_key_no_se_presenta_como_retiro_existente(entorno):
    fecha = _dias_habiles(1)[0]
    id_colision = 7001
    with entorno["db"]() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recolecciones (
                id, cliente_id, courier, fecha, direccion, estado, confirmation_code
            ) VALUES (%s, %s, 'DHL', %s, 'histórico', 'COMPLETADA', 'OLD-7001')
            """,
            (id_colision, CLIENTE, fecha),
        )
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('recolecciones', 'id'), %s, false)",
            (id_colision,),
        )
    solicitud = _agregar_guia(entorno, origen=_origen_ar(), sufijo="PK")

    salida = _pedir(solicitud, fecha)

    assert salida["ok"] is False
    assert salida["error_codigo"] == "RESERVA_NO_REGISTRADA"
    assert "No pudimos registrar" in salida["error"]
    assert "Ya hay" not in salida["error"]
    assert "recoleccion_conflicto_id" not in salida
    assert entorno["api_actual"]["valor"].llamadas == []
