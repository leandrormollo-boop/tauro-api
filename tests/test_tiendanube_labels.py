import asyncio
import inspect
import json

import pytest

from servicios import tiendanube_labels
from servicios.tiendanube_labels import (
    LabelsAuthenticationError,
    LabelsBlockedError,
    LabelsConflictError,
    PersistResult,
    recibir_cancel,
    recibir_generate,
)


TOKEN = "label-callback-super-secreto-123456"


def _config(_token):
    return {"store_id": "123456", "activa": True}


def _generate_payload(*, address="Calle 1"):
    return [
        {
            "id": "label-1",
            "fulfillment_order_info": {
                "id": "ffo-1",
                "recipient": {"name": "Comprador", "address": address},
                "shipping": {"option": "domicilio"},
            },
        }
    ]


def _cancel_payload():
    return {
        "labels": [
            {"fulfillment_order_id": "ffo-1", "label_id": "label-1"}
        ]
    }


class MemoryRepository:
    def __init__(self):
        self.operations = {}

    def persist(self, operations, *, state):
        created = 0
        replayed = 0
        for operation in operations:
            key = (operation.store_id, operation.label_id, operation.operation)
            existing = self.operations.get(key)
            if existing and existing.fingerprint != operation.fingerprint:
                raise LabelsConflictError("payload distinto")
            if existing:
                replayed += 1
            else:
                self.operations[key] = operation
                created += 1
        self.last_state = state
        return PersistResult(created=created, replayed=replayed, state=state)


def test_generate_persiste_antes_de_fallar_cerrado_y_reintento_es_idempotente():
    repository = MemoryRepository()

    with pytest.raises(LabelsBlockedError):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=_config,
        )

    assert repository.last_state == "BLOQUEADA_SIN_ADAPTER"
    assert len(repository.operations) == 1
    blocked = next(iter(repository.operations.values()))
    assert blocked.payload_complete is False
    assert blocked.payload == {
        "id": "label-1",
        "fulfillment_order_info": {"id": "ffo-1"},
    }
    assert "Comprador" not in json.dumps(blocked.payload)

    with pytest.raises(LabelsBlockedError):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=_config,
        )
    assert len(repository.operations) == 1


def test_generate_solo_acepta_despues_de_persistir_si_worker_fuera_habilitado():
    repository = MemoryRepository()

    result = recibir_generate(
        _generate_payload(),
        TOKEN,
        repository=repository,
        config_loader=_config,
        execution_ready=lambda: True,
    )

    assert result == PersistResult(created=1, replayed=0, state="PENDIENTE")
    assert len(repository.operations) == 1


def test_mismo_store_y_label_con_payload_distinto_es_conflicto():
    repository = MemoryRepository()
    recibir_generate(
        _generate_payload(),
        TOKEN,
        repository=repository,
        config_loader=_config,
        execution_ready=lambda: True,
    )

    with pytest.raises(LabelsConflictError):
        recibir_generate(
            _generate_payload(address="Otra calle"),
            TOKEN,
            repository=repository,
            config_loader=_config,
            execution_ready=lambda: True,
        )


def test_cancel_persiste_pero_nunca_aprueba_sin_cancelacion_real():
    repository = MemoryRepository()

    with pytest.raises(LabelsBlockedError):
        recibir_cancel(
            _cancel_payload(),
            TOKEN,
            repository=repository,
            config_loader=_config,
        )

    key = ("123456", "label-1", "CANCEL")
    assert key in repository.operations
    assert repository.last_state == "BLOQUEADA_SIN_CANCELACION"


def test_token_invalido_no_persiste():
    repository = MemoryRepository()

    with pytest.raises(LabelsAuthenticationError):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=lambda _token: None,
        )
    assert repository.operations == {}


def test_outbox_tiene_clave_idempotente_y_fk_de_redaccion():
    source = inspect.getsource(tiendanube_labels._ensure_tables)
    normalized = " ".join(source.split())

    assert "UNIQUE (store_id, label_id, operacion)" in normalized
    assert "PRIMARY KEY (store_id, label_id)" in normalized
    assert "ON DELETE CASCADE" in normalized


class _Request:
    def __init__(self, payload, *, content_length=None, raw_body=None):
        self._body = (
            bytes(raw_body)
            if raw_body is not None
            else json.dumps(payload).encode("utf-8")
        )
        self.headers = {
            "content-length": str(
                len(self._body) if content_length is None else content_length
            )
        }

    async def body(self):
        return self._body

    async def stream(self):
        yield self._body


def test_endpoint_generate_devuelve_202_solo_si_servicio_acepta(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    called = []
    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda payload, token: called.append((payload, token)),
    )

    response = asyncio.run(endpoint.generate_labels(TOKEN, _Request(_generate_payload())))

    assert response.status_code == 202
    assert called and called[0][1] == TOKEN


def test_endpoint_generate_bloqueado_devuelve_503(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda *_: (_ for _ in ()).throw(LabelsBlockedError("bloqueado")),
    )

    response = asyncio.run(endpoint.generate_labels(TOKEN, _Request(_generate_payload())))
    assert response.status_code == 503


def test_endpoint_rechaza_content_length_excesivo_con_413(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda *_: pytest.fail("No debe procesar un body anunciado como excesivo"),
    )

    response = asyncio.run(
        endpoint.generate_labels(
            TOKEN,
            _Request(
                _generate_payload(),
                content_length=endpoint._MAX_LABEL_CALLBACK_BYTES + 1,
            ),
        )
    )
    assert response.status_code == 413


def test_endpoint_corta_stream_excesivo_aunque_header_mienta(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda *_: pytest.fail("No debe procesar un body realmente excesivo"),
    )
    response = asyncio.run(
        endpoint.generate_labels(
            TOKEN,
            _Request(
                None,
                content_length=1,
                raw_body=b"x" * (endpoint._MAX_LABEL_CALLBACK_BYTES + 1),
            ),
        )
    )
    assert response.status_code == 413


def test_endpoint_cancel_no_devuelve_2xx_aunque_servicio_retornara(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(endpoint, "recibir_cancel", lambda *_: None)

    response = asyncio.run(endpoint.cancel_labels(TOKEN, _Request(_cancel_payload())))
    assert response.status_code == 503


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload


def test_registro_nuevo_incluye_callback_labels_con_secreto_distinto(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: True)
    monkeypatch.setattr(tiendanube_shipping, "configuracion", lambda _store: None)
    tokens = iter(("rate-token", "labels-token"))
    monkeypatch.setattr(
        tiendanube_shipping.secrets, "token_urlsafe", lambda _size: next(tokens)
    )
    calls = []

    def fake_api(store_id, access_token, method, path, payload=None):
        calls.append((store_id, method, path, payload))
        if method == "GET" and path == "shipping_carriers":
            return _Response(200, [])
        if method == "POST" and path == "shipping_carriers":
            return _Response(201, {"id": 77})
        return _Response(201, {"id": 88})

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    carrier_payload = next(
        call[3]
        for call in calls
        if call[1:3] == ("POST", "shipping_carriers")
    )
    assert carrier_payload["callback_url"].endswith("/rates/rate-token")
    assert carrier_payload["callback_labels_url"].endswith(
        "/labels/labels-token"
    )
    assert saved[0][1]["label_token"] == "labels-token"
    assert result["labels_callback_registered"] is True


def test_registro_existente_agrega_labels_sin_rotar_callback_rates(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: True)
    rate_token = "rate-token-existente-12345678901234567890"
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "store_id": "123",
            "activa": True,
            "callback_token_hash": tiendanube_shipping.hash_callback_token(
                rate_token
            ),
            "label_callback_token_hash": None,
            "carrier_id": "77",
            "carrier_option_id": "88",
        },
    )
    monkeypatch.setattr(
        tiendanube_shipping.secrets,
        "token_urlsafe",
        lambda _size: "labels-token",
    )
    calls = []
    def fake_api(*args):
        calls.append(args)
        if args[2] == "GET" and args[3].endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
                "active": True,
            }])
        if args[2] == "GET":
            return _Response(200, {
                "id": 77,
                "active": True,
                "callback_url": (
                    "https://api.tauro.test/integraciones/tiendanube/"
                    f"shipping/rates/{rate_token}"
                ),
            })
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_label_callback_token",
        lambda store, token: saved.append((store, token)),
    )
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    update = next(call for call in calls if call[2] == "PUT")
    assert update[3] == "shipping_carriers/77"
    assert set(update[4]) == {"callback_labels_url"}
    assert "/labels/labels-token" in update[4]["callback_labels_url"]
    assert saved == [("123", "labels-token")]
    assert result["existing"] is True


def test_registro_omite_labels_mientras_worker_esta_bloqueado(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    monkeypatch.setattr(tiendanube_shipping, "configuracion", lambda _store: None)
    monkeypatch.setattr(
        tiendanube_shipping.secrets,
        "token_urlsafe",
        lambda _size: "rate-token",
    )
    calls = []

    def fake_api(store_id, access_token, method, path, payload=None):
        calls.append((store_id, method, path, payload))
        if method == "GET":
            return _Response(200, [])
        if path == "shipping_carriers":
            return _Response(201, {"id": 77})
        return _Response(201, {"id": 88})

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    carrier_payload = next(
        call[3]
        for call in calls
        if call[1:3] == ("POST", "shipping_carriers")
    )
    assert "callback_labels_url" not in carrier_payload
    assert saved[0][1]["label_token"] is None
    assert result["labels_callback_registered"] is False


def test_registro_existente_elimina_callback_labels_si_worker_no_esta_listo(
    monkeypatch,
):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    rate_token = "rate-token-existente-12345678901234567890"
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "store_id": "123",
            "activa": True,
            "callback_token_hash": tiendanube_shipping.hash_callback_token(
                rate_token
            ),
            "label_callback_token_hash": "hash-labels",
            "carrier_id": "77",
            "carrier_option_id": "88",
        },
    )
    calls = []

    def fake_api(*args):
        calls.append(args)
        if args[2] == "GET" and args[3].endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
                "active": True,
            }])
        if args[2] == "GET":
            return _Response(
                200,
                {
                    "id": 77,
                    "active": True,
                    "callback_url": (
                        "https://api.tauro.test/integraciones/tiendanube/"
                        f"shipping/rates/{rate_token}"
                    ),
                    "callback_labels_url": (
                        "https://api.tauro.test/integraciones/tiendanube/"
                        "shipping/labels/labels-token-remoto"
                    ),
                },
            )
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)
    cleaned = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_limpiar_label_callback_token",
        lambda store: cleaned.append(store),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    update = next(call for call in calls if call[2] == "PUT")
    assert update[4] == {"callback_labels_url": None}
    assert cleaned == ["123"]
    assert result["labels_callback_registered"] is False


def test_reinstalacion_reactiva_carrier_inactivo_sin_crear_otro(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    rate_token = "rate-token-existente-12345678901234567890"
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "activa": False,
            "callback_token_hash": tiendanube_shipping.hash_callback_token(
                rate_token
            ),
            "carrier_id": "77",
            "carrier_option_id": "88",
            "label_callback_token_hash": None,
        },
    )
    calls = []

    def fake_api(*args):
        calls.append(args)
        if args[2] == "GET" and args[3].endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
                "active": True,
            }])
        if args[2] == "GET":
            return _Response(200, {
                "id": 77,
                "active": False,
                "callback_url": (
                    "https://api.tauro.test/integraciones/tiendanube/"
                    f"shipping/rates/{rate_token}"
                ),
            })
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    activated = []
    monkeypatch.setattr(
        tiendanube_shipping, "reactivar", lambda store: activated.append(store)
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    assert not [call for call in calls if call[2] == "POST"]
    update = next(call for call in calls if call[2] == "PUT")
    assert update[4] == {"active": True}
    assert activated == ["123"]
    assert result["existing"] is True


def test_reconcilia_carrier_remoto_tras_fallo_db_sin_duplicar(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    monkeypatch.setattr(tiendanube_shipping, "configuracion", lambda _store: None)
    rate_token = "rate-token-remoto-12345678901234567890"
    calls = []

    def fake_api(*args):
        calls.append(args)
        method, path = args[2], args[3]
        if method == "GET" and path == "shipping_carriers":
            return _Response(200, [{
                "id": 77,
                "name": "TAURO Nacional",
                "active": True,
                "callback_url": (
                    "https://api.tauro.test/integraciones/tiendanube/"
                    f"shipping/rates/{rate_token}"
                ),
            }])
        if method == "GET" and path.endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
            }])
        if method == "GET":
            return _Response(200, {"id": 77, "active": True})
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    assert not [call for call in calls if call[2] == "POST"]
    assert saved[0][0][1] == rate_token
    assert saved[0][0][2:] == ("77", "88")
    assert result["existing"] is True


def test_config_local_repara_callback_y_opcion_remota_sin_duplicar_carrier(
    monkeypatch,
):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "activa": True,
            "callback_token_hash": "hash-local-desactualizado",
            "label_callback_token_hash": None,
            "carrier_id": "77",
            "carrier_option_id": "88",
        },
    )
    monkeypatch.setattr(
        tiendanube_shipping.secrets,
        "token_urlsafe",
        lambda _size: "rate-token-nuevo-12345678901234567890",
    )
    calls = []

    def fake_api(*args):
        calls.append(args)
        method, path = args[2], args[3]
        if method == "GET" and path.endswith("/options"):
            return _Response(200, [])
        if method == "GET":
            return _Response(200, {
                "id": 77,
                "active": True,
                "callback_url": (
                    "https://otra.example/integraciones/tiendanube/"
                    "shipping/rates/token-ajeno"
                ),
            })
        if method == "POST" and path.endswith("/options"):
            return _Response(201, {
                "id": 99,
                "code": "tauro_nacional_domicilio",
                "active": True,
            })
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    assert not [
        call
        for call in calls
        if call[2] == "POST" and call[3] == "shipping_carriers"
    ]
    assert any(
        call[2] == "PUT"
        and call[3] == "shipping_carriers/77"
        and "callback_url" in call[4]
        for call in calls
    )
    assert saved[0][0][1] == "rate-token-nuevo-12345678901234567890"
    assert saved[0][0][3] == "99"
    assert result["option_id"] == "99"
