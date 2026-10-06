from decimal import Decimal
from types import SimpleNamespace

import pytest

from servicios import precios_web_nacional as precios


class _Cursor:
    def __init__(self, filas=()):
        self.filas = list(filas)
        self.ejecutadas = []

    def execute(self, sql, params=None):
        self.ejecutadas.append((" ".join(sql.split()), params))

    def fetchall(self):
        return list(self.filas)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _db(monkeypatch, filas=()):
    cursor = _Cursor(filas)
    monkeypatch.setattr(precios, "get_conn", lambda: _Conn(cursor))
    return cursor


def test_pricing_publico_exige_activacion_y_markup_explicitos(monkeypatch):
    _db(monkeypatch, [])
    with pytest.raises(precios.PrecioWebNoDisponible) as error:
        precios.pricing_publico_oca()
    assert error.value.motivo == precios.MotivoPrecioWebNoDisponible.CANAL_DESACTIVADO

    _db(monkeypatch, [
        {"parametro": precios.PARAMETRO_HABILITADO, "valor": "1"},
    ])
    with pytest.raises(precios.PrecioWebNoDisponible) as error:
        precios.pricing_publico_oca()
    assert error.value.motivo == precios.MotivoPrecioWebNoDisponible.PRECIO_NO_CONFIGURADO
    assert str(error.value) == "Falta cargar el porcentaje de ganancia de OCA."


def test_pricing_publico_devuelve_decimal_sin_fallback(monkeypatch):
    _db(monkeypatch, [
        {"parametro": precios.PARAMETRO_HABILITADO, "valor": "1"},
        {"parametro": precios.PARAMETRO_MARKUP_PCT, "valor": "20.5"},
    ])
    assert precios.pricing_publico_oca() == {
        "tipo": "PCT",
        "valor": Decimal("20.5"),
        "oca_tarifa_neta_iva_21": True,
    }


def test_markup_cero_numerico_se_conserva():
    assert precios._parsear_markup(Decimal("0")) == Decimal("0")
    assert precios._parsear_markup(0) == Decimal("0")


@pytest.mark.parametrize("valor", ["-1", "300.01", "NaN", "20.12345"])
def test_markup_invalido_falla_cerrado(monkeypatch, valor):
    _db(monkeypatch, [
        {"parametro": precios.PARAMETRO_HABILITADO, "valor": "1"},
        {"parametro": precios.PARAMETRO_MARKUP_PCT, "valor": valor},
    ])
    with pytest.raises(precios.PrecioWebNoDisponible) as error:
        precios.pricing_publico_oca()
    assert error.value.motivo == precios.MotivoPrecioWebNoDisponible.CONFIGURACION_INVALIDA


def test_falla_de_db_tiene_motivo_tecnico_estable(monkeypatch):
    monkeypatch.setattr(
        precios,
        "get_conn",
        lambda: (_ for _ in ()).throw(RuntimeError("detalle privado")),
    )

    with pytest.raises(precios.PrecioWebNoDisponible) as error:
        precios.pricing_publico_oca()

    assert error.value.motivo == precios.MotivoPrecioWebNoDisponible.LECTURA_CONFIG
    assert "detalle privado" not in str(error.value)


def test_guardado_y_auditoria_comparten_transaccion(monkeypatch):
    cursor = _db(monkeypatch, [
        {"parametro": precios.PARAMETRO_HABILITADO, "valor": "0"},
        {"parametro": precios.PARAMETRO_MARKUP_PCT, "valor": "10"},
    ])
    auditorias = []
    monkeypatch.setattr(
        "servicios.auditoria.registrar_desde_request_con_cursor",
        lambda cur, request, **kwargs: auditorias.append((cur, kwargs)),
    )

    resultado = precios.guardar_configuracion_oca(
        request=SimpleNamespace(), habilitada=True, markup_pct="25,5"
    )

    assert resultado["publicable"] is True
    assert resultado["markup_pct"] == Decimal("25.5")
    assert auditorias[0][0] is cursor
    assert auditorias[0][1]["event"] == "admin.configurar_precio_web_oca"
    assert auditorias[0][1]["metadata"] == {
        "antes": {"habilitada": False, "markup_pct": "10"},
        "despues": {"habilitada": True, "markup_pct": "25.5"},
    }
    assert any(
        params == (precios.PARAMETRO_HABILITADO, "1")
        for _, params in cursor.ejecutadas
    )


def test_no_activa_sin_markup(monkeypatch):
    cursor = _db(monkeypatch)
    with pytest.raises(ValueError, match="antes de activar"):
        precios.guardar_configuracion_oca(
            request=SimpleNamespace(), habilitada=True, markup_pct=""
        )
    assert cursor.ejecutadas == []
