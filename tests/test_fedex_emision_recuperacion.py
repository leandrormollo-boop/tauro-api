"""Clasificación fail-closed de respuestas de emisión FedEx."""

from core.fedex_client import FedExClient


ENVIO = {
    "shipper": {
        "nombre": "Origen", "empresa": "Origen SA", "telefono": "111111",
        "calle": "Calle 1", "ciudad": "CABA", "estado": "C",
        "zip": "1000", "pais": "AR",
    },
    "recipient": {
        "nombre": "Destino", "telefono": "222222", "calle": "Street 1",
        "ciudad": "Miami", "estado": "FL", "zip": "33101", "pais": "US",
    },
    "package": {"peso_kg": 1, "largo": 10, "ancho": 10, "alto": 10},
    "commodity": {
        "descripcion": "Merchandise", "hs_code": "640399", "cantidad": 1,
        "valor_unitario_usd": 10, "pais_origen": "AR",
    },
}


class Respuesta:
    def __init__(self, status, datos):
        self.status_code = status
        self._datos = datos
        self.content = b"{}"

    def json(self):
        return self._datos


def cliente():
    instancia = FedExClient()
    instancia.api_key = "KEY-TEST"
    instancia.secret_key = "SECRET-TEST"
    instancia.account_number = "CUENTA-TEST"
    return instancia


def test_sin_credenciales_no_crea_un_falso_intento_incierto():
    instancia = cliente()
    instancia.api_key = None

    resultado = instancia.create_shipment(ENVIO)

    assert not resultado["encontrado"]
    assert resultado["rechazo_confirmado"] is True
    assert "Credenciales FedEx" in resultado["error"]


def test_rechazo_4xx_fedex_permite_liberar_reserva(monkeypatch):
    monkeypatch.setattr(
        FedExClient,
        "_request_with_retry",
        lambda *_args, **_kwargs: Respuesta(
            400, {"errors": [{"code": "INVALID", "message": "Dato inválido"}]},
        ),
    )

    resultado = cliente().create_shipment(ENVIO)

    assert not resultado["encontrado"]
    assert resultado["rechazo_confirmado"] is True
    assert resultado["incierto"] is False


def test_timeout_fedex_obliga_a_conciliar(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise TimeoutError("respuesta perdida")

    monkeypatch.setattr(FedExClient, "_request_with_retry", timeout)

    resultado = cliente().create_shipment(ENVIO)

    assert not resultado["encontrado"]
    assert resultado["incierto"] is True


def test_respuesta_exitosa_sin_tracking_obliga_a_conciliar(monkeypatch):
    monkeypatch.setattr(
        FedExClient,
        "_request_with_retry",
        lambda *_args, **_kwargs: Respuesta(
            200, {"output": {"transactionShipments": [{}]}},
        ),
    )

    resultado = cliente().create_shipment(ENVIO)

    assert not resultado["encontrado"]
    assert resultado["incierto"] is True


def test_respuestas_408_y_409_fedex_no_habilitan_reemision(monkeypatch):
    for status in (408,409,500,502):
        monkeypatch.setattr(FedExClient,'_request_with_retry',
                            lambda *a,**k:Respuesta(status,{'errors':[{'code':'ERROR'}]}))
        resultado=cliente().create_shipment(ENVIO)
        assert resultado['incierto'] is True
        assert resultado['rechazo_confirmado'] is False
