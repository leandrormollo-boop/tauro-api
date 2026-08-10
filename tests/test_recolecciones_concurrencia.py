"""Idempotencia y migración de recolecciones, sin APIs reales."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

from servicios import recolecciones as rec


class _DBCancelacion:
    def __init__(self):
        self.estado = "AGENDADA"
        self.rec = {
            "id": 7,
            "cliente_id": "WAIMAO",
            "estado": "AGENDADA",
            "courier": "DHL",
            "confirmation_code": "PU-123",
            "fecha": date(2026, 8, 12),
            "ubicacion": "",
        }

    @contextmanager
    def conexion(self):
        db = self

        class Cursor:
            siguiente = None

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def execute(self, sql, params=None):
                limpio = " ".join(sql.split())
                if "SET estado='CANCELANDO'" in limpio and "RETURNING" in limpio:
                    if db.estado == "AGENDADA":
                        db.estado = "CANCELANDO"
                        self.siguiente = dict(db.rec, estado="CANCELANDO")
                    else:
                        self.siguiente = None
                elif "SET estado='CANCELADA'" in limpio:
                    if db.estado == "CANCELANDO":
                        db.estado = "CANCELADA"
                elif "SET estado='VERIFICAR_COURIER'" in limpio:
                    if db.estado == "CANCELANDO":
                        db.estado = "VERIFICAR_COURIER"
                elif "SET estado='AGENDADA'" in limpio:
                    if db.estado == "CANCELANDO":
                        db.estado = "AGENDADA"

            def fetchone(self):
                return self.siguiente

        class Conn:
            def cursor(self):
                return Cursor()

        yield Conn()


def test_dos_cancelaciones_solo_llaman_una_vez_al_courier(monkeypatch):
    db = _DBCancelacion()
    api = mock.Mock()
    api.cancel_pickup.return_value = {"ok": True}
    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "get_conn", db.conexion)
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)

    primera = rec.cancelar(7, "WAIMAO")
    segunda = rec.cancelar(7, "WAIMAO")

    assert primera == {"ok": True}
    assert not segunda["ok"]
    assert db.estado == "CANCELADA"
    api.cancel_pickup.assert_called_once()


def test_cancelacion_sin_confirmacion_queda_para_conciliar(monkeypatch):
    db = _DBCancelacion()
    api = mock.Mock()
    api.cancel_pickup.return_value = {"ok": False, "error": "respuesta incierta"}
    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "get_conn", db.conexion)
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: api)

    salida = rec.cancelar(7, "WAIMAO")

    assert not salida["ok"] and salida["incierto"]
    assert db.estado == "VERIFICAR_COURIER"
    assert "no programes otro retiro" in salida["error"]


def test_indices_viven_en_schema_y_el_request_no_hace_ddl():
    raiz = Path(__file__).resolve().parents[1]
    schema = (raiz / "sql" / "schema.sql").read_text(encoding="utf-8")
    servicio = (raiz / "servicios" / "recolecciones.py").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS recolecciones" in schema
    assert "uq_recoleccion_cliente_fecha_abierta_v2" in schema
    assert "uq_recoleccion_solicitud_abierta_v2" in schema
    cuerpo_ensure = servicio.split("def _ensure_tabla", 1)[1].split(
        "def _dias_habiles_validos", 1
    )[0]
    assert "DROP INDEX" not in cuerpo_ensure
    assert "CREATE TABLE" not in cuerpo_ensure


def test_conciliacion_exige_codigo_para_confirmar_retiro_activo(monkeypatch):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, _sql, _params=None): pass
        def fetchone(self): return {"confirmation_code": ""}

    class Conn:
        def cursor(self): return Cursor()

    @contextmanager
    def conexion(): yield Conn()

    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "get_conn", conexion)

    salida = rec.resolver_verificacion(7, "AGENDADA", "")

    assert not salida["ok"] and "código" in salida["error"]


def test_conciliacion_activa_actualiza_solo_desde_verificar(monkeypatch):
    consultas = []

    class Cursor:
        respuestas = iter([
            {"confirmation_code": "", "estado": "VERIFICAR_COURIER"},
            {"id": 7},
        ])
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, params=None):
            consultas.append((" ".join(sql.split()), params))
        def fetchone(self): return next(self.respuestas)

    class Conn:
        def cursor(self): return Cursor()

    @contextmanager
    def conexion(): yield Conn()

    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "get_conn", conexion)

    salida = rec.resolver_verificacion(7, "AGENDADA", "PU-REAL-7")

    assert salida == {"ok": True, "estado": "AGENDADA"}
    update = consultas[1]
    assert "WHERE id=%s AND estado=%s" in update[0]
    assert update[1] == ("AGENDADA", "PU-REAL-7", 7, "VERIFICAR_COURIER")
