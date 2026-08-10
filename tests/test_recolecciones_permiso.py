"""El permiso de recolección es opt-in y se controla en el servidor."""
from contextlib import contextmanager
from datetime import date, timedelta
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servicios.recolecciones as rec


def _proximo_habil() -> str:
    dia = date.today() + timedelta(days=1)
    while dia.weekday() >= 5:
        dia += timedelta(days=1)
    return dia.isoformat()


def test_sin_permiso_no_reserva_ni_llama_al_courier(monkeypatch):
    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "cliente_puede_recolectar", lambda cliente, courier=None: False)
    monkeypatch.setattr(
        rec, "_cliente_pickup",
        lambda courier: (_ for _ in ()).throw(AssertionError("no llamar al courier")),
    )
    monkeypatch.setattr(
        rec, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("no insertar reserva")),
    )

    salida = rec.crear(
        "WAIMAO", _proximo_habil(), "09:00", "17:00", 1, 1,
        courier="DHL",
    )

    assert salida["ok"] is False
    assert "no están habilitadas" in salida["error"]


def test_con_permiso_reserva_y_llama_al_courier(monkeypatch):
    consultas = []

    class Cursor:
        def execute(self, query, params=()):
            consultas.append((" ".join(query.split()), params))

        def fetchone(self):
            return {"id": 71}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

    @contextmanager
    def conexion():
        yield Conn()

    courier = mock.Mock()
    courier.create_pickup.return_value = {
        "encontrado": True,
        "confirmation_code": "DHL-71",
        "ubicacion": "A1",
        "message_reference": "ref-71",
    }

    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "cliente_puede_recolectar", lambda cliente, courier=None: True)
    monkeypatch.setattr(rec, "_cliente_pickup", lambda nombre: courier)
    monkeypatch.setattr(rec, "get_conn", conexion)
    import servicios.direcciones as direcciones
    monkeypatch.setattr(direcciones, "obtener_remitente_para_envio", lambda *args: {
        "nombre": "WAIMAO", "alias": "Waimao SRL", "telefono": "1111",
        "direccion": "Calle 1", "ciudad": "CABA", "estado": "C",
        "cp": "1000", "pais": "AR",
    })

    salida = rec.crear(
        "WAIMAO", _proximo_habil(), "09:00", "17:00", 2, 4,
        courier="DHL",
    )

    assert salida == {"ok": True, "id": 71, "confirmation_code": "DHL-71"}
    courier.create_pickup.assert_called_once()
    payload = courier.create_pickup.call_args.args[0]
    assert payload["message_reference"].startswith("tauro-dhl-pick-")
    assert any("INSERT INTO recolecciones" in query for query, _ in consultas)
    insert = next(item for item in consultas if "INSERT INTO recolecciones" in item[0])
    assert insert[1][-1] == payload["message_reference"]
    assert any("SET estado='AGENDADA'" in query for query, _ in consultas)
