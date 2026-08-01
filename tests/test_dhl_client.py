"""
Tests del cliente DHL (core/dhl_client.py) — armados ANTES de tener las
credenciales, para que el día que se carguen en Railway no descubramos los
errores contra la API en vivo.

Salieron de una auditoría contra el OpenAPI oficial de MyDHL API 3.3.1
(https://developer.dhl.com/sites/default/files/2026-07/dpdhl-express-api-3.3.1.yaml).
Los tres bugs que atrapan estaban todos en producción latente:

  1. plannedShippingDate es required:true y el código lo mandaba en None y lo
     filtraba. El 100% de las cotizaciones habría vuelto 400 — y como el
     comparador pinta la tarjeta "sin_tarifa", se habría leído como "DHL no
     tiene cobertura para esa ruta" en vez de "el cliente está roto".
  2. Se tomaba products[0] a ciegas. GET /rates no garantiza el orden: podía
     cotizar un producto premium (perdés la venta) o uno más barato que el que
     después despachás (la factura de DHL viene más alta que lo que cobraste).
  3. Faltaba el header x-version, el único obligatorio.
"""
import os
import sys
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dhl_client import DHLClient  # noqa: E402

# Los 12 query params que el OpenAPI 3.3.1 marca required:true para GET /rates.
OBLIGATORIOS = {
    "accountNumber",
    "originCountryCode",
    "originCityName",
    "destinationCountryCode",
    "destinationCityName",
    "weight",
    "length",
    "width",
    "height",
    "plannedShippingDate",
    "isCustomsDeclarable",
    "unitOfMeasurement",
}

ORIGEN = {"country": "AR", "city": "BUENOS AIRES", "postal_code": "1043"}
DESTINO = {"country": "US", "city": "MIAMI", "postal_code": "33131"}
PAQUETE = {"peso_kg": 1.4, "largo": 33, "ancho": 33, "alto": 22}


def _producto(codigo, precio, nombre=None, acuerdo=False):
    return {
        "productCode": codigo,
        "productName": nombre or f"DHL {codigo}",
        "isCustomerAgreement": acuerdo,
        "totalPrice": [{"price": precio, "priceCurrency": "USD"}],
        "deliveryCapabilities": {"totalTransitDays": "3"},
    }


def _cliente():
    with mock.patch.dict(
        os.environ,
        {
            "DHL_API_KEY": "k",
            "DHL_API_SECRET": "s",
            "DHL_ACCOUNT_NUMBER": "123456789",
            "DHL_ENVIRONMENT": "sandbox",
        },
    ):
        return DHLClient()


def _llamar(productos, capturar=None):
    """Ejecuta get_rates() con una respuesta falsa y devuelve (resultado, kwargs)."""
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"products": productos}
    with mock.patch("core.dhl_client.requests.get", return_value=resp) as get:
        out = _cliente().get_rates(ORIGEN, DESTINO, PAQUETE)
    return out, get.call_args.kwargs


# ── Lo que rompía toda llamada ───────────────────────────────

def test_manda_todos_los_parametros_obligatorios():
    _, kw = _llamar([_producto("P", 120.0)])
    faltan = OBLIGATORIOS - set(kw["params"])
    assert not faltan, f"faltan parámetros required:true del OpenAPI: {sorted(faltan)}"


def test_la_fecha_de_envio_va_y_es_futura_y_sin_hora():
    _, kw = _llamar([_producto("P", 120.0)])
    fecha = kw["params"]["plannedShippingDate"]
    assert "T" not in fecha, f"la spec pide YYYY-MM-DD, no ISO con hora: {fecha}"
    assert date.fromisoformat(fecha) > date.today(), (
        f"DHL no cotiza contra una fecha pasada: {fecha}"
    )


def test_manda_el_header_de_version():
    _, kw = _llamar([_producto("P", 120.0)])
    assert kw["headers"].get("x-version") == DHLClient.API_VERSION


# ── Lo que costaba plata ─────────────────────────────────────

def test_elige_por_codigo_y_no_el_primero_de_la_lista():
    """
    Express 9:00 (K) primero y más caro; el nuestro es Worldwide (P).
    Tomar el [0] cotizaba 310 en vez de 120 y perdíamos contra FedEx.
    """
    out, _ = _llamar([_producto("K", 310.0), _producto("P", 120.0)])
    assert out["encontrado"]
    assert out["costo"] == 120.0, "agarró el primero de la lista en vez del P"


def test_ignora_los_productos_que_requieren_acuerdo_previo():
    """
    Un isCustomerAgreement más barato NO se puede vender: se publicaría ese
    precio y después se despacharía como Worldwide, con la factura más alta.
    """
    out, _ = _llamar(
        [_producto("P", 95.0, acuerdo=True), _producto("P", 120.0)]
    )
    assert out["costo"] == 120.0


def test_avisa_cuando_la_ruta_no_tiene_nuestro_producto():
    out, _ = _llamar([_producto("K", 310.0)])
    assert not out["encontrado"]
    assert "K" in out["error"], "el error tiene que decir qué productos sí hay"


# ── Que no tumbe el comparador ───────────────────────────────

def test_sin_credenciales_no_llama_a_la_api():
    with mock.patch.dict(os.environ, {}, clear=True):
        out = DHLClient().get_rates(ORIGEN, DESTINO, PAQUETE)
    assert not out["encontrado"]


def test_un_error_de_dhl_no_lanza_excepcion():
    resp = mock.Mock(status_code=400, text="Bad request: missing parameter")
    with mock.patch("core.dhl_client.requests.get", return_value=resp):
        out = _cliente().get_rates(ORIGEN, DESTINO, PAQUETE)
    assert not out["encontrado"]
    assert "Bad request" in out["error"]
