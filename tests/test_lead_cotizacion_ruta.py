"""La ruta cotizada debe sobrevivir hasta pantalla, base y email."""
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest
from pydantic import ValidationError

import main
from servicios import cotizador, leads


class _Cursor:
    def __init__(self):
        self.respuestas = iter([None, {"id": 7}])
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.ejecutadas.append((sql, tuple(params or ())))

    def fetchone(self):
        return next(self.respuestas)


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _conexion(cursor):
    @contextmanager
    def get_conn():
        yield _Conn(cursor)

    return get_conn


def test_cotizador_rapido_canoniza_nombres_antes_de_decidir_ambito(monkeypatch):
    def no_llamar(*_args, **_kwargs):
        raise AssertionError("AR→AR no debe consultar ningún courier")

    monkeypatch.setattr(
        "servicios.carriers.cotizar_carriers_cliente", no_llamar,
    )

    with pytest.raises(ValueError, match="Andreani y OCA"):
        cotizador.cotizar_referencia_couriers(
            "TEST", "Argentina", "argentina", 1, 10, 10, 10, 100,
        )


def test_lead_guarda_el_par_exacto_y_no_inventa_argentina(monkeypatch):
    cursor = _Cursor()
    monkeypatch.setattr(leads, "_tabla_lista", True)
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    class _ThreadSinEnviar:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self):
            return None

    monkeypatch.setattr(threading, "Thread", _ThreadSinEnviar)

    resultado = leads.guardar_lead(
        "cliente@example.com", "China", "India", 5.5,
        [{
            "estado": "cotizado", "nombre": "DHL", "servicio": "Express",
            "dias_estimados": "4", "precio_ars": 100000,
            "precio_usd": 80,
        }],
    )

    assert resultado == {"ok": True}
    sql, params = cursor.ejecutadas[-1]
    assert "email, origen, destino, peso_kg" in sql
    assert params[:4] == ("cliente@example.com", "CN", "IN", 5.5)


def test_lead_nacional_falla_antes_de_base_o_email(monkeypatch):
    monkeypatch.setattr(
        leads, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("no debe tocar la base")),
    )

    resultado = leads.guardar_lead(
        "cliente@example.com", "Argentina", "AR", 1,
        [{"estado": "cotizado"}],
    )
    assert resultado["ok"] is False
    assert "nacionales" in resultado["error"]


def test_contrato_web_y_schema_transportan_origen():
    raiz = Path(__file__).resolve().parent.parent
    widget = (raiz / "web" / "components" / "02-quote-widget.jsx").read_text(
        encoding="utf-8"
    )
    schema = (raiz / "sql" / "schema.sql").read_text(encoding="utf-8")

    with pytest.raises(ValidationError):
        main.LeadCotizacionRequest(email="cliente@example.com", destino="US")
    assert "{result.origen} → {result.destino}" in widget
    assert "origen={result.origen_pais}" in widget
    assert "email: email.trim(),\n          origen,\n          destino" in widget
    assert "ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS origen TEXT" in schema
    assert "Buenos Aires →" not in widget
