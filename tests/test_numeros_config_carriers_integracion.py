"""Compatibilidad de lectura con perillas historicas guardadas en config."""

import pytest

from servicios import carriers


class _CursorConfigFalso:
    def __init__(self, filas):
        self._filas = filas

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params=None):
        return None

    def fetchall(self):
        return list(self._filas)


class _ConexionConfigFalsa:
    def __init__(self, filas):
        self._filas = filas

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _CursorConfigFalso(self._filas)


@pytest.mark.parametrize("texto", ["100.000", "100,000"])
def test_carrier_lee_margen_fijo_historico_como_cien_mil(monkeypatch, texto):
    filas = [{"parametro": "WEB_MARGEN_FIJO_DHL_ARS", "valor": texto}]
    monkeypatch.setattr(carriers, "get_conn", lambda: _ConexionConfigFalsa(filas))

    valores = carriers._pricing_configurado()

    assert valores["WEB_MARGEN_FIJO_DHL_ARS"] == 100_000.0


@pytest.mark.parametrize("texto", ["5,5", "5.5"])
def test_carrier_lee_porcentaje_historico_con_coma_o_punto(monkeypatch, texto):
    filas = [{"parametro": "WEB_MARKUP_PCT_DHL", "valor": texto}]
    monkeypatch.setattr(carriers, "get_conn", lambda: _ConexionConfigFalsa(filas))

    valores = carriers._pricing_configurado()

    assert valores["WEB_MARKUP_PCT_DHL"] == 5.5
