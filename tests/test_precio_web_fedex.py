"""
Precio de vidriera de FedEx: lista − 90% + $10.000.

Regla de Leandro (06/08/2026), textual: "FedEx (los precios de la web,
aplicarle un 90% de descuento y sumarle un markup de $10.000) es referencial
para que vean competidores".

Antes existía sólo el descuento, así que el precio salía `lista − 88%` a secas
y el markup fijo no llegaba nunca a la vidriera.

Los tres modelos de precio de la web NO se pisan y por eso hay un test de cada
uno: ganancia fija en ARS (DHL, corta antes que todo), descuento + adicional
(FedEx), y markup % (el resto).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios.carriers import _adicional_de, _desc_fedex, _precios  # noqa: E402

DOLAR = 1520.0


@pytest.fixture(autouse=True)
def _sin_env(monkeypatch):
    """Los tests miden los defaults del código, no lo que haya en el .env."""
    for k in ("WEB_DESC_FEDEX_PCT", "WEB_ADICIONAL_FEDEX_ARS",
              "WEB_MARGEN_FIJO_DHL_ARS", "WEB_MARKUP_PCT"):
        monkeypatch.delenv(k, raising=False)


def test_default_fedex_es_90_y_10000():
    assert _desc_fedex({}) == 90.0
    assert _adicional_de("fedex", {}) == 10000.0


def test_el_adicional_es_solo_de_fedex():
    """DHL va con ganancia fija y UPS con markup %: sumarles $10.000 sería un bug."""
    assert _adicional_de("dhl", {}) == 0.0
    assert _adicional_de("ups", {}) == 0.0


def test_precio_web_fedex_es_lista_menos_90_mas_10000():
    res = {"costo": 269.57, "costo_lista": 269.57, "moneda": "USD"}
    p = _precios(res, DOLAR, markup_pct=20, descuento_pct=90, adicional_ars=10000)

    lista = p["precio_lista_ars"]
    assert p["precio_ars"] == round(lista * 0.10) + 10000
    assert p["descuento_pct"] == 90
    # La lista tachada NO lleva el adicional: es la tarifa del courier.
    assert lista == round(269.57 * DOLAR)


def test_el_adicional_no_puede_perderse_en_el_redondeo():
    """Con una tarifa chica, los $10.000 son casi todo el precio."""
    res = {"costo": 10.0, "costo_lista": 10.0, "moneda": "USD"}
    p = _precios(res, DOLAR, markup_pct=20, descuento_pct=90, adicional_ars=10000)
    assert p["precio_ars"] == round(10.0 * DOLAR * 0.10) + 10000
    assert p["precio_ars"] > 10000


def test_la_ganancia_fija_corta_antes_y_no_recibe_el_adicional():
    """DHL: precio = costo + $135.000, sin descuento y sin adicional encima."""
    res = {"costo": 100.0, "costo_lista": 300.0, "moneda": "USD"}
    p = _precios(res, DOLAR, markup_pct=20, descuento_pct=90,
                 margen_fijo_ars=135000, adicional_ars=10000)
    assert p["precio_ars"] == round(100.0 * DOLAR) + 135000
    assert "descuento_pct" not in p


def test_sin_descuento_el_adicional_no_se_aplica():
    """Rama de markup %: el adicional es exclusivo del modelo con descuento."""
    res = {"costo": 100.0, "costo_lista": 100.0, "moneda": "USD"}
    p = _precios(res, DOLAR, markup_pct=20, descuento_pct=0, adicional_ars=10000)
    assert p["precio_ars"] == round(100.0 * DOLAR * 1.20)


def test_el_piso_de_seguridad_ve_el_precio_con_el_adicional(monkeypatch):
    """
    El piso tiene que evaluar lo que el cliente PAGA, no el subtotal. Si mirara
    el precio antes de sumar los $10.000, se activaría de más y pisaría un
    precio que ya estaba bien.
    """
    monkeypatch.setenv("FEDEX_ENVIRONMENT", "production")
    monkeypatch.setenv("WEB_MARGEN_MINIMO_PCT", "15")

    # costo 30 USD; lista 300 USD → -90% = 30 USD, + $10.000 ARS = 55.600 ARS.
    # El piso es 30 USD × 1520 × 1,15 = 52.440 → NO debe activarse.
    res = {"costo": 30.0, "costo_lista": 300.0, "moneda": "USD"}
    p = _precios(res, DOLAR, markup_pct=20, descuento_pct=90, adicional_ars=10000)
    assert p["precio_ars"] == round(300.0 * DOLAR * 0.10) + 10000

    # Sin el adicional el mismo caso sí cae bajo el piso y se corrige.
    p2 = _precios(res, DOLAR, markup_pct=20, descuento_pct=90, adicional_ars=0)
    assert p2["precio_ars"] == round(round(30.0 * DOLAR) * 1.15)
