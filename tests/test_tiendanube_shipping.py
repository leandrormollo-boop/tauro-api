import asyncio
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from servicios import tiendanube_rate_quotes, tiendanube_shipping
from servicios.carrier_adapter import OperationState, QuoteResult
from servicios.tiendanube_shipping import (
    ShippingAuthenticationError,
    ShippingContractError,
    ShippingUnavailableError,
    _add_business_days,
    _holiday_calendar,
    cotizar_callback,
    hash_callback_token,
)


TOKEN = "callback-super-secreto"


@pytest.fixture(autouse=True)
def _freeze_quotes_without_database(monkeypatch):
    """Las pruebas del callback verifican contrato; PostgreSQL tiene suite propia."""
    monkeypatch.setattr(
        tiendanube_shipping,
        "guardar_snapshot",
        tiendanube_rate_quotes.construir_snapshot,
    )


def _payload(
    *,
    mixed=False,
    dimensions=True,
    all_free=False,
    additional_cost=0,
    carrier_id="77",
    option_id="88",
):
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

    items = [item("pago", 10000, all_free)]
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
        "carrier": {
            "id": carrier_id,
            "name": "Tauro Solutions Ar",
            "options": [
                {
                    "id": option_id,
                    "code": "tauro_nacional_domicilio",
                    "additional_cost": {
                        "amount": additional_cost,
                        "currency": "ARS",
                    },
                    "additional_days": 0,
                    "allow_free_shipping": False,
                }
            ],
        },
    }


def _installation(_store_id):
    return {"cliente_id": "CLIENTE-1", "estado": "active"}


def _config(_store_id):
    return {
        "activa": True,
        "callback_token_hash": hash_callback_token(TOKEN),
        "carrier_id": "77",
        "carrier_option_id": "88",
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
    """Doble mínimo de Request: el endpoint lee el cuerpo acotado, no .json()."""

    def __init__(self, payload=None):
        import json as _json
        self._body = _json.dumps(_payload() if payload is None else payload).encode("utf-8")
        self.headers = {"content-length": str(len(self._body))}

    async def json(self):
        import json as _json
        return _json.loads(self._body)

    async def stream(self):
        yield self._body


def test_ruta_rates_no_expone_secreto_en_el_path():
    from endpoints import tiendanube_shipping as endpoint

    rate_route = next(
        route for route in endpoint.router.routes
        if getattr(route, "name", "") == "rates"
    )

    assert rate_route.path.endswith("/shipping/rates")
    assert "callback_token" not in rate_route.path


def test_ruta_rates_asgi_exige_una_unica_query_de_autenticacion(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import tiendanube_shipping as endpoint

    token = "token-prueba-largo-12345678901234567890"
    monkeypatch.setattr(
        endpoint,
        "cotizar_callback",
        lambda payload, callback_token: {
            "rates": [],
            "store_id": payload["store_id"],
            "token_ok": callback_token == token,
        },
    )
    app = FastAPI()
    app.include_router(endpoint.router)

    with TestClient(app) as client:
        accepted = client.post(
            f"/integraciones/tiendanube/shipping/rates?callback_token={token}",
            json={"store_id": "123"},
        )
        missing = client.post(
            "/integraciones/tiendanube/shipping/rates",
            json={"store_id": "123"},
        )
        duplicate = client.post(
            "/integraciones/tiendanube/shipping/rates"
            f"?callback_token={token}&callback_token={token}",
            json={"store_id": "123"},
        )
        extra = client.post(
            "/integraciones/tiendanube/shipping/rates"
            f"?callback_token={token}&otro=1",
            json={"store_id": "123"},
        )

    assert accepted.status_code == 200
    assert accepted.json()["token_ok"] is True
    assert missing.status_code == 422
    assert duplicate.status_code == 401
    assert extra.status_code == 401


def test_endpoint_mapea_negocio_a_422_y_caida_a_503(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "cotizar_callback",
        lambda *_: (_ for _ in ()).throw(ShippingContractError("sin cobertura")),
    )
    business = asyncio.run(
        endpoint.rates(_JsonRequest(), callback_token="token-prueba-largo-1234567890")
    )
    assert business.status_code == 422

    monkeypatch.setattr(
        endpoint,
        "cotizar_callback",
        lambda *_: (_ for _ in ()).throw(ShippingUnavailableError("timeout")),
    )
    unavailable = asyncio.run(
        endpoint.rates(_JsonRequest(), callback_token="token-prueba-largo-1234567890")
    )
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


def test_rechaza_paquete_que_supera_limite_contractual(monkeypatch):
    monkeypatch.setenv("TAURO_NACIONAL_MAX_PACKAGE_WEIGHT_KG", "0.5")

    with pytest.raises(ShippingContractError, match="límites contractuales"):
        cotizar_callback(
            _payload(),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[FakeAdapter()],
        )


def test_shipping_habilitado_falla_cerrado_sin_limites(monkeypatch):
    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")

    with pytest.raises(ShippingUnavailableError, match="límites contractuales"):
        cotizar_callback(
            _payload(),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[FakeAdapter()],
        )


def test_fecha_habil_salta_fin_de_semana_y_feriado():
    timezone = ZoneInfo("America/Argentina/Buenos_Aires")
    friday = datetime(2026, 9, 4, 10, tzinfo=timezone)
    holidays = _holiday_calendar("2026-09-07")

    result = _add_business_days(friday, 1, holidays)

    assert result.date() == date(2026, 9, 8)


def test_shipping_habilitado_rechaza_calendario_de_otro_anio(monkeypatch):
    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")

    with pytest.raises(ShippingUnavailableError, match="año en curso"):
        _holiday_calendar("2000-01-01")


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
    assert rate["reference"].startswith("tauro:oca:tnq_")
    assert "carrier_cost" not in rate
    assert "margin" not in rate


def test_envio_gratis_total_devuelve_el_precio_completo_a_tiendanube():
    response = cotizar_callback(
        _payload(all_free=True),
        TOKEN,
        installation_loader=_installation,
        config_loader=_config,
        adapters=[FakeAdapter()],
    )

    rate = response["rates"][0]
    assert rate["price"] == 7000.0
    assert rate["price_merchant"] == 7000.0


def test_costo_adicional_se_congela_pero_no_se_suma_en_la_respuesta():
    captured = {}

    def save(*args, **kwargs):
        snapshot = tiendanube_rate_quotes.construir_snapshot(*args, **kwargs)
        captured.update(snapshot)
        return snapshot

    response = cotizar_callback(
        _payload(additional_cost=1500),
        TOKEN,
        installation_loader=_installation,
        config_loader=_config,
        adapters=[FakeAdapter()],
        snapshot_saver=save,
    )

    rate = response["rates"][0]
    assert rate["price"] == 7000.0
    assert rate["price_merchant"] == 7000.0
    assert captured["platform_additional_cost"] == "1500"
    assert captured["expected_consumer_price"] == "8500"
    assert captured["platform_option_id"] == "88"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_payload(carrier_id="otro-carrier"), "otro carrier"),
        (_payload(option_id="otra-opcion"), "otra opción"),
    ],
)
def test_rechaza_carrier_u_opcion_que_no_coinciden_con_la_configuracion(
    payload, message
):
    with pytest.raises(ShippingAuthenticationError, match=message):
        cotizar_callback(
            payload,
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[FakeAdapter()],
        )


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


def test_deadline_global_descarta_cotizacion_tardia(monkeypatch):
    from servicios import tiendanube_shipping

    clock = iter((0.0, 0.0, 4.1))
    monkeypatch.setattr(
        tiendanube_shipping.time, "monotonic", lambda: next(clock)
    )

    with pytest.raises(ShippingUnavailableError, match="tarifas nacionales"):
        cotizar_callback(
            _payload(),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[FakeAdapter()],
        )


def test_fallo_al_congelar_tarifa_impide_publicar_referencia():
    def fail(*_args, **_kwargs):
        raise tiendanube_rate_quotes.RateQuoteSnapshotError("db caída")

    with pytest.raises(ShippingUnavailableError, match="congelar"):
        cotizar_callback(
            _payload(),
            TOKEN,
            installation_loader=_installation,
            config_loader=_config,
            adapters=[FakeAdapter()],
            snapshot_saver=fail,
        )
