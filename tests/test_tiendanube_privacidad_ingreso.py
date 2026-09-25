import asyncio
import hashlib
import hmac
import json

import pytest

from endpoints import integraciones
from servicios import tiendanube_app


class Request:
    def __init__(self, body, *, firma_valida=True):
        self.cuerpo = json.dumps(body).encode()
        self.headers = {
            "x-linkedstore-hmac-sha256": hmac.new(
                b"secret", self.cuerpo, hashlib.sha256,
            ).hexdigest() if firma_valida else "incorrecta",
            "x-linkedstore-event-id": "delivery-1",
        }

    async def body(self):
        return self.cuerpo


@pytest.fixture
def cola(monkeypatch):
    monkeypatch.setenv("TIENDANUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(tiendanube_app, "app_configurada", lambda: True)
    guardados = []
    monkeypatch.setattr(
        tiendanube_app,
        "encolar_webhook",
        lambda identificador, datos: guardados.append(datos) or True,
    )
    monkeypatch.setattr(tiendanube_app, "lanzar_procesamiento_eventos", lambda: None)
    return guardados


@pytest.mark.parametrize("tipo,evento,datos", [
    ("app-store-redact", "app/store_redact", {"store_id": 123}),
    ("customer-redact", "customer/redact", {
        "store_id": 123, "customer": {"id": 4}, "orders_to_redact": [8],
    }),
    ("customers-data-request", "customers/data_request", {
        "store_id": 123, "customer": {"id": 4}, "data_request": {"id": 9},
        "orders_requested": [8],
    }),
])
def test_acepta_contrato_oficial_sin_event_con_firma_original(cola, tipo, evento, datos):
    respuesta = asyncio.run(integraciones.tiendanube_privacidad_webhook(Request(datos), tipo))
    assert respuesta["encolado"] is True
    assert cola == [{**datos, "event": evento}]


@pytest.mark.parametrize("tipo,evento,datos", [
    ("store-redact", "app/store_redact", {"store_id": 123}),
    ("customers-redact", "customer/redact", {
        "store_id": 123, "customer": {"id": 4}, "orders_to_redact": [8],
    }),
])
def test_aliases_de_ruta_previos_persisten_evento_oficial(cola, tipo, evento, datos):
    respuesta = asyncio.run(
        integraciones.tiendanube_privacidad_webhook(Request(datos), tipo)
    )
    assert respuesta["encolado"] is True
    assert cola == [{**datos, "event": evento}]


@pytest.mark.parametrize("tipo,datos", [
    ("app-store-redact", {"store_id": 123, "event": "order/created", "id": 8}),
    ("app-store-redact", {"store_id": 123, "id": 8}),
    ("app-store-redact", {"store_id": 123, "customer": {"id": 4}, "orders_to_redact": []}),
    ("customer-redact", {"store_id": 123, "customer": {"id": 4},
                          "orders_requested": [8], "data_request": {"id": 9}}),
    ("customer-redact", {"store_id": 123, "customer": [], "orders_to_redact": [8]}),
    ("customers-data-request", {"store_id": 123, "customer": {"id": 4},
                                "orders_to_redact": [8]}),
])
def test_no_reinterpreta_otros_cuerpos_firmados_como_borrados(cola, tipo, datos):
    respuesta = asyncio.run(integraciones.tiendanube_privacidad_webhook(Request(datos), tipo))
    assert respuesta.status_code == 400
    assert cola == []


def test_privacidad_requiere_hmac_antes_de_encolar(cola):
    respuesta = asyncio.run(integraciones.tiendanube_privacidad_webhook(
        Request({"store_id": 123}, firma_valida=False), "app-store-redact",
    ))
    assert respuesta.status_code == 401
    assert cola == []


def test_privacidad_no_ackea_si_no_hay_commit(cola, monkeypatch):
    def fallo(*_):
        raise RuntimeError("db caída")
    monkeypatch.setattr(tiendanube_app, "encolar_webhook", fallo)
    respuesta = asyncio.run(integraciones.tiendanube_privacidad_webhook(
        Request({"store_id": 123}), "app-store-redact",
    ))
    assert respuesta.status_code == 503


def test_seis_eventos_api_no_prueban_configuracion_partners(monkeypatch):
    monkeypatch.delenv("TIENDANUBE_PRIVACY_WEBHOOKS_CONFIRMED", raising=False)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: pytest.fail("no habilitar DB"))
    assert tiendanube_app.confirmar_webhooks(
        "123", tiendanube_app.WEBHOOKS_API_REQUERIDOS,
        expected_generation="gen-1",
    ) is False
    with pytest.raises(tiendanube_app.TiendanubeWebhookError, match="PRIVACY_PARTNERS"):
        tiendanube_app.reactivar("123")


def test_registro_api_nunca_intenta_crear_avisos_partners(monkeypatch):
    destino = "https://taurosolutions.ar/integraciones/tiendanube/webhook"
    existentes = [
        {"event": evento, "url": destino}
        for evento in tiendanube_app.WEBHOOKS_API_REQUERIDOS
    ]

    class Response:
        status_code = 200

        def json(self):
            return existentes

    def api(_store, _token, metodo, _path, **_kwargs):
        assert metodo == "GET", "Los seis existentes bastan; privacidad no usa POST"
        return Response()

    monkeypatch.setenv("BASE_URL", "https://taurosolutions.ar")
    monkeypatch.setattr(tiendanube_app, "_api", api)
    monkeypatch.setattr(
        tiendanube_app, "exigir_generacion_oauth", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "servicios.tiendanube_labels.labels_execution_ready",
        lambda: False,
    )
    assert set(tiendanube_app.registrar_webhooks(
        "123", "token", expected_generation="gen-1",
    )) == set(
        tiendanube_app.WEBHOOKS_API_REQUERIDOS
    )
    assert tiendanube_app.EVENTOS_PRIVACIDAD.isdisjoint(
        tiendanube_app.WEBHOOKS_API_REQUERIDOS
    )
    assert tiendanube_app.EVENTOS_PRIVACIDAD.issubset(
        tiendanube_app.EVENTOS_ACEPTADOS
    )
