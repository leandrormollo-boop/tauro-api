import pytest

import servicios.pricing as pricing


class _Cursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query, _params):
        return None

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _Cursor(self._row)


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"markup_nac_tipo": None, "markup_nac_valor": None},
        {"markup_nac_tipo": "PCT", "markup_nac_valor": 301},
        {"markup_nac_tipo": "MULTIPLICADOR", "markup_nac_valor": 0.9},
    ],
)
def test_pricing_nacional_estricto_rechaza_fallback_o_valores_invalidos(
    monkeypatch, row
):
    monkeypatch.setattr(pricing, "get_conn", lambda: _Connection(row))

    with pytest.raises(pricing.PricingNacionalNoConfigurado):
        pricing.get_pricing_nacional_estricto("cliente-1")


@pytest.mark.parametrize(
    ("tipo", "valor"),
    [("PCT", 25), ("FIJO_ARS", 14500), ("MULTIPLICADOR", 1.3)],
)
def test_pricing_nacional_estricto_acepta_regla_explicita(
    monkeypatch, tipo, valor
):
    monkeypatch.setattr(
        pricing,
        "get_conn",
        lambda: _Connection(
            {"markup_nac_tipo": tipo, "markup_nac_valor": valor}
        ),
    )

    assert pricing.get_pricing_nacional_estricto("cliente-1") == {
        "tipo": tipo,
        "valor": float(valor),
    }
