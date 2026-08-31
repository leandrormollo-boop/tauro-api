import asyncio
from decimal import Decimal

import pytest

from servicios.carrier_adapter import OperationState, QuoteResult
from servicios.tiendanube_shipping import (
    ShippingAuthenticationError,
    ShippingContractError,
    ShippingUnavailableError,
    cotizar_callback,
    hash_callback_token,
)


TOKEN = "callback-super-secreto"


def _payload(*, mixed=False, dimensions=True):
    def item(name, price, free=False):
        value = {
            "id": name,
            "name": name,
            "quantity": 1,
            "grams": 1000,
            "price": price,
            "free_shipping": free,
        }
        if dimensions:
            value["dimensions"] = {"width": 10, "height": 20, "depth": 30}
        return value

    items = [item("pago", 10000)]
    if mixed:
        items.append(item("gratis", 20000, True))
    return {
        "cart_id": "cart-1",
        "store_id": 123456,
        "currency": "ARS",
        "total_price": sum(i["price"] for i in items),
        "origin": {"country": "AR", "postal_code": "1425"},
        "destination": {"country": "AR", "postal_code": "2000"},
        "items": items,
    }


def _installation(_store_id):
    return {"cliente_id": "CLIENTE-1", "estado": "active"}


def _config(_store_id):
    return {
        "activa": True,
        "callback_token_hash": hash_callback_token(TOKEN),
    }


class FakeAdapter:
    carrier_id = "oca"

    def __init__(self):
        self.requests = []

    def quote(self, request):
        self.requests.append(request)
        price = Decimal("12000") if len(request.packages) == 2 else Decimal("7000")
        return (
            QuoteResult(
                state=OperationState.COTIZADO,
                carrier_id="oca",
                quote_id=f"quote-{len(self.requests)}",
                service_code="oca-domicilio",
                service_name="OCA domicilio",
                carrier_cost=price - Decimal("1000"),
                carrier_currency="ARS",
                customer_price=price,
                currency="ARS",
                estimated_days=3,
            ),
        )


class NoRateAdapter(FakeAdapter):
    def quote(self, request):
        self.requests.append(request)
        return (QuoteResult(OperationState.SIN_TARIFA, "oca"),)


class DownAdapter(FakeAdapter):
    def quote(self, request):
        raise RuntimeError("timeout")


def test_callback_rechaza_token_incorrecto_antes_de_cotizar():
    with pytest.raises(ShippingAuthenticationError):
        cotizar_callback(
            _payload(),
            "otro-token",
            installation_loader=_installation,
            config_loader=_config,
            adapters=[FakeAdapter()],
        )


def test_no_inventa_tarifa_si_no_hay_adapter_nacional():
    with pytest.raises(ShippingUnavailableError):
        cotizar_callback(
            _payload(),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[],
        )


def test_respuesta_sin_cobertura_es_error_de_negocio_y_no_indisponibilidad():
    with pytest.raises(ShippingContractError, match="cobertura"):
        cotizar_callback(
            _payload(),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[NoRateAdapter()],
        )


def test_caida_del_operador_sigue_siendo_indisponibilidad():
    with pytest.raises(ShippingUnavailableError):
        cotizar_callback(
            _payload(),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[DownAdapter()],
        )


class _JsonRequest:
    async def json(self):
        return _payload()


def test_endpoint_mapea_negocio_a_422_y_caida_a_503(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "cotizar_callback",
        lambda *_: (_ for _ in ()).throw(ShippingContractError("sin cobertura")),
    )
    business = asyncio.run(endpoint.rates("token", _JsonRequest()))
    assert business.status_code == 422

    monkeypatch.setattr(
        endpoint,
        "cotizar_callback",
        lambda *_: (_ for _ in ()).throw(ShippingUnavailableError("timeout")),
    )
    unavailable = asyncio.run(endpoint.rates("token", _JsonRequest()))
    assert unavailable.status_code == 503


def test_rechaza_producto_sin_peso_y_medidas():
    with pytest.raises(ShippingContractError, match="dimensiones"):
        cotizar_callback(
            _payload(dimensions=False),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[FakeAdapter()],
        )


def test_devuelve_solo_precio_final_sin_costo_ni_margen():
    adapter = FakeAdapter()
    response = cotizar_callback(
        _payload(),
        TOKEN,
        installation_loader=_installation,
        config_loader=_config,
        adapters=[adapter],
    )

    rate = response["rates"][0]
    assert rate["code"] == "tauro_nacional_domicilio"
    assert rate["price"] == 7000.0
    assert rate["price_merchant"] == 7000.0
    assert rate["currency"] == "ARS"
    assert rate["accepts_cod"] is False
    assert rate["reference"].startswith("tauro:oca:quote-")
    assert "carrier_cost" not in rate
    assert "margin" not in rate


def test_carrito_mixto_cotiza_total_al_merchant_y_solo_pago_al_comprador():
    adapter = FakeAdapter()
    response = cotizar_callback(
        _payload(mixed=True),
        TOKEN,
        installation_loader=_installation,
        config_loader=_config,
        adapters=[adapter],
    )

    assert len(adapter.requests) == 2
    assert len(adapter.requests[0].packages) == 2
    assert len(adapter.requests[1].packages) == 1
    rate = response["rates"][0]
    assert rate["price"] == 7000.0
    assert rate["price_merchant"] == 12000.0
