"""Regresión del SQL real: un cursor simulado no detecta los % de psycopg2."""

from decimal import Decimal

import pytest

from servicios import monitoreo_guias_reemplazadas as monitoreo
from test_conciliacion_couriers_postgres import (
    DATABASE_URL,
    _crear_cargo_activo,
    _crear_solicitud,
    conciliacion_db,
)


pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="requiere TAURO_TEST_DATABASE_URL aislada",
)
NOTA_LEGACY = "Cancelación confirmada: DHL no registró movimientos en siete días."


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    monkeypatch.setattr(monitoreo, "get_conn", conciliacion_db)
    return conciliacion_db


def crear_control(db, sufijo, *, riesgo="VIGILAR", nota=None,
                  antiguedad=8, dias_desde_consulta=None, alerta=False):
    solicitud = _crear_solicitud(db, sufijo=sufijo)
    _crear_cargo_activo(db, solicitud, monto="1234.56")
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET estado='CANCELADO' WHERE id=%s", (solicitud,))
        cur.execute("UPDATE envios SET estado='CANCELADO' WHERE solicitud_id=%s", (solicitud,))
        cur.execute("""
            INSERT INTO solicitudes_guia_reemisiones (
                cliente_id, solicitud_anterior_id, operacion, tracking_anterior,
                estado, riesgo_estado, riesgo_resuelto_nota, completed_at,
                tracking_anterior_consultado_at, alerta_movimiento_at
            ) VALUES (
                %s, %s, 'CANCELACION', %s, 'EMITIDA', %s, %s,
                NOW() - (%s * INTERVAL '1 day'),
                NOW() - (%s * INTERVAL '1 day'),
                CASE WHEN %s THEN NOW() ELSE NULL END
            ) RETURNING id
        """, (f"CLIENTE_{sufijo}", solicitud, f"TRACK-{sufijo}", riesgo, nota,
              antiguedad, dias_desde_consulta, alerta))
        return int(cur.fetchone()["id"])


class DHLFicticio:
    def __init__(self, eventos=None):
        self.eventos = eventos
        self.consultas = []

    def _error_configuracion(self):
        return None

    def track(self, tracking):
        self.consultas.append(tracking)
        if self.eventos is None:
            return {"encontrado": False, "http_status": 404}
        return {"encontrado": True, "eventos": self.eventos}


def test_lote_vacio_ejecuta_sql_parametrizado_real(db, monkeypatch):
    dhl = DHLFicticio()
    monkeypatch.setattr(monitoreo, "DHLClient", lambda: dhl)

    resultado = monitoreo.actualizar_trackings_reemplazados_dhl(limite=5)

    assert resultado["ok"] is True
    assert resultado["candidatos"] == resultado["consultados"] == 0
    assert dhl.consultas == []


def test_lote_respeta_patron_limite_y_ventanas_con_postgres(db, monkeypatch):
    crear_control(db, "ANTIGUA", riesgo="CERRADA", nota=NOTA_LEGACY, antiguedad=40)
    crear_control(db, "VIGILAR", antiguedad=8)
    crear_control(db, "RECIENTE", antiguedad=3)
    crear_control(db, "BACKOFF", antiguedad=20, dias_desde_consulta=1)
    crear_control(db, "MANUAL", riesgo="CERRADA", nota="Caso verificado por el administrador")
    crear_control(db, "CON_ALERTA", riesgo="CERRADA", nota=NOTA_LEGACY, alerta=True)
    dhl = DHLFicticio()
    monkeypatch.setattr(monitoreo, "DHLClient", lambda: dhl)

    primero = monitoreo.actualizar_trackings_reemplazados_dhl(limite=1)
    segundo = monitoreo.actualizar_trackings_reemplazados_dhl(limite=10)
    tercero = monitoreo.actualizar_trackings_reemplazados_dhl(limite=10)

    assert primero["candidatos"] == primero["sin_movimiento"] == 1
    assert segundo["candidatos"] == segundo["sin_movimiento"] == 1
    assert tercero["candidatos"] == 0
    assert dhl.consultas == ["TRACK-ANTIGUA", "TRACK-VIGILAR"]
    assert all(r["ok"] and r["errores"] == 0 for r in (primero, segundo, tercero))


@pytest.mark.parametrize("nota,alerta,elegible", [
    (NOTA_LEGACY, False, True),
    ("Cancelación confirmada: DHL no registró", False, True),
    ("Cierre manual verificado", False, False),
    (NOTA_LEGACY, True, False),
])
def test_candidato_conserva_semantica_del_like(db, nota, alerta, elegible):
    ident = crear_control(db, "PATRON", riesgo="CERRADA", nota=nota, alerta=alerta)

    candidato = monitoreo._candidato(ident)

    assert bool(candidato) is elegible
    if candidato:
        assert candidato["id"] == ident


@pytest.mark.parametrize("con_movimiento", [False, True])
def test_actualizacion_legacy_ejecuta_sql_y_audita_sin_reactivar_cargo(db, con_movimiento):
    ident = crear_control(db, "ACTUALIZAR", riesgo="CERRADA", nota=NOTA_LEGACY)
    eventos = [{"typeCode": "PU", "description": "Shipment picked up"}] if con_movimiento else []
    dhl = DHLFicticio(eventos)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM envios ORDER BY id")
        cargos_antes = cur.fetchall()

    resultado = monitoreo.actualizar_tracking_reemplazado_dhl(
        ident, cliente_dhl=dhl, control_programado=True,
    )

    assert resultado["ok"] and resultado["guardado"]
    assert resultado["movimiento"] is con_movimiento
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM solicitudes_guia_reemisiones WHERE id=%s", (ident,))
        control = cur.fetchone()
        assert control["riesgo_estado"] == ("ALERTA_MOVIMIENTO" if con_movimiento else "VIGILAR")
        assert control["tracking_anterior_consultado_at"] is not None
        if con_movimiento:
            assert control["tracking_anterior_estado_courier"] == "PU"
            assert control["alerta_movimiento_at"] is not None
        else:
            assert control["riesgo_resuelto_nota"] is None
            assert control["riesgo_resuelto_at"] is None
        cur.execute("SELECT event FROM security_audit ORDER BY id")
        assert [fila["event"] for fila in cur.fetchall()] == [
            "dhl.guia_reemplazada_con_movimiento" if con_movimiento
            else "dhl.guia_descartada_sin_movimientos_observados"
        ]
        cur.execute("SELECT * FROM envios ORDER BY id")
        assert cur.fetchall() == cargos_antes
        assert cargos_antes[0]["estado"] == "CANCELADO"
        assert Decimal(str(cargos_antes[0]["monto_ars"])) == Decimal("1234.56")
