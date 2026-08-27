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


def test_normaliza_cpa_argentino_para_rates_y_direcciones():
    origen = dict(ORIGEN, postal_code="C1043ABC")
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"products": [_producto("P", 120.0)]}
    with mock.patch("core.dhl_client.requests.get", return_value=resp) as get:
        _cliente().get_rates(origen, DESTINO, PAQUETE)

    assert get.call_args.kwargs["params"]["originPostalCode"] == "1043"
    direccion = DHLClient._direccion({
        "pais": "AR", "zip": "C1043ABC", "ciudad": "CABA", "estado": "C",
    })
    assert direccion["postalCode"] == "1043"
    assert "provinceCode" not in direccion
    assert "provinceCode" not in DHLClient._direccion({
        "pais": "CN", "zip": "322000", "ciudad": "YIWU", "estado": "Zhejiang",
    })
    assert DHLClient._direccion({
        "pais": "US", "zip": "10118", "ciudad": "NEW YORK", "estado": "ny",
    })["provinceCode"] == "NY"


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


def test_elige_billc_aunque_basec_venga_primero():
    producto = _producto("P", 1)
    producto["totalPrice"] = [
        {"price": 80, "priceCurrency": "USD", "currencyType": "BASEC"},
        {"price": 125, "priceCurrency": "USD", "currencyType": "BILLC"},
    ]

    out, _ = _llamar([producto])

    assert out["encontrado"] and out["costo"] == 125


def test_precio_billc_cero_o_no_finito_falla_cerrado():
    for invalido in (0, -1, "NaN"):
        producto = _producto("P", 1)
        producto["totalPrice"] = [{
            "price": invalido, "priceCurrency": "USD", "currencyType": "BILLC",
        }]

        out, _ = _llamar([producto])

        assert not out["encontrado"] and "inválido" in out["error"]


def test_avisa_cuando_la_ruta_no_tiene_nuestro_producto():
    out, _ = _llamar([_producto("K", 310.0)])
    assert not out["encontrado"]
    assert "K" in out["error"], "el error tiene que decir qué productos sí hay"


# ── Que no tumbe el comparador ───────────────────────────────

def test_sin_credenciales_no_llama_a_la_api():
    with mock.patch.dict(os.environ, {}, clear=True):
        out = DHLClient().get_rates(ORIGEN, DESTINO, PAQUETE)
    assert not out["encontrado"]


# ── Expo vs impo: una cuenta por sentido ─────────────────────

def _cliente_con(env):
    base = {
        "DHL_API_KEY": "k",
        "DHL_API_SECRET": "s",
        "DHL_ACCOUNT_NUMBER": "741622792",      # EXPO
        "DHL_ENVIRONMENT": "sandbox",
    }
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=True):
        return DHLClient()


def _cuenta_usada(cli, origen, destino):
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"products": [_producto("P", 120.0)]}
    with mock.patch("core.dhl_client.requests.get", return_value=resp) as get:
        out = cli.get_rates(origen, destino, PAQUETE)
    if not get.called:
        return None, out
    return get.call_args.kwargs["params"]["accountNumber"], out


AR = {"country": "AR", "city": "BUENOS AIRES", "postal_code": "1043"}
US = {"country": "US", "city": "MIAMI", "postal_code": "33131"}


def test_una_exportacion_usa_la_cuenta_de_expo():
    cli = _cliente_con({"DHL_ACCOUNT_NUMBER_IMPORT": "730089966"})
    cuenta, _ = _cuenta_usada(cli, AR, US)
    assert cuenta == "741622792"


def test_una_importacion_usa_la_cuenta_de_impo():
    """
    Cada cuenta tiene su propia grilla negociada. Cotizar una importación con
    la cuenta de exportación NO da error en DHL: devuelve el precio del otro
    sentido y después la factura no coincide.
    """
    cli = _cliente_con({"DHL_ACCOUNT_NUMBER_IMPORT": "730089966"})
    cuenta, _ = _cuenta_usada(cli, US, AR)
    assert cuenta == "730089966"


def test_sin_cuenta_de_impo_no_cotiza_una_importacion():
    """Antes que publicar un precio con la grilla equivocada, no cotiza."""
    cli = _cliente_con({})  # sin DHL_ACCOUNT_NUMBER_IMPORT
    cuenta, out = _cuenta_usada(cli, US, AR)
    assert cuenta is None, "no tendría que haber llamado a DHL"
    assert not out["encontrado"]
    assert "IMPO" in out["error"]


def test_un_envio_nacional_usa_la_de_expo():
    cli = _cliente_con({"DHL_ACCOUNT_NUMBER_IMPORT": "730089966"})
    cuenta, _ = _cuenta_usada(cli, AR, {"country": "AR", "city": "CORDOBA"})
    assert cuenta == "741622792"


def test_un_error_de_dhl_no_lanza_excepcion():
    resp = mock.Mock(status_code=400, text="Bad request: missing parameter")
    with mock.patch("core.dhl_client.requests.get", return_value=resp):
        out = _cliente().get_rates(ORIGEN, DESTINO, PAQUETE)
    assert not out["encontrado"]
    assert "HTTP 400" in out["error"]
    assert "missing parameter" not in out["error"]


# ── Multi-bulto: POST /rates ─────────────────────────────────

BULTOS = [
    {"peso_kg": 1.4, "largo_cm": 33, "ancho_cm": 33, "alto_cm": 22},
    {"peso_kg": 3.0, "largo_cm": 40, "ancho_cm": 30, "alto_cm": 20},
]


def _post(origen=None, destino=None, bultos=None, env=None):
    base = {"DHL_API_KEY": "k", "DHL_API_SECRET": "s",
            "DHL_ACCOUNT_NUMBER_EXPO": "741622792",
            "DHL_ACCOUNT_NUMBER_IMPO": "730089966"}
    base.update(env or {})
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"products": [_producto("P", 240.0)]}
    with mock.patch.dict(os.environ, base, clear=True), \
         mock.patch("core.dhl_client.requests.post", return_value=resp) as post:
        out = DHLClient().get_rates(
            origen or ORIGEN, destino or DESTINO, paquetes=bultos or BULTOS)
    return out, (post.call_args.kwargs if post.called else None)


def test_varias_cajas_van_por_post_con_una_entrada_cada_una():
    """
    Sin campo de cantidad: 2 cajas son 2 elementos. Cada una con SUS medidas,
    porque cada una paga su propio peso volumétrico.
    """
    out, kw = _post()
    assert out["encontrado"]
    pk = kw["json"]["packages"]
    assert len(pk) == 2
    assert pk[0]["weight"] == 1.4
    assert pk[0]["dimensions"] == {"length": 33.0, "width": 33.0, "height": 22.0}
    assert pk[1]["dimensions"]["length"] == 40.0


def test_la_cuenta_va_en_accounts_no_en_la_raiz():
    """
    En el GET era un query param suelto; en el POST es un array. Y la raíz
    tiene additionalProperties:false, así que un `accountNumber` de más es
    rechazo directo.
    """
    _, kw = _post()
    body = kw["json"]
    assert body["accounts"] == [{"typeCode": "shipper", "number": "741622792"}]
    assert "accountNumber" not in body


def test_una_importacion_multibulto_usa_la_cuenta_de_impo():
    _, kw = _post(origen=DESTINO, destino={**ORIGEN, "country": "AR"})
    assert kw["json"]["accounts"][0]["number"] == "730089966"


def test_isCustomsDeclarable_es_booleano_no_string():
    """En el querystring del GET viajaba como el string "true"."""
    _, kw = _post()
    assert kw["json"]["isCustomsDeclarable"] is True


def test_la_fecha_lleva_hora_y_no_espacio_antes_de_GMT():
    _, kw = _post()
    f = kw["json"]["plannedShippingDateAndTime"]
    assert " GMT" not in f, f"la spec de /rates lo ejemplifica sin espacio: {f}"
    assert f.endswith("GMT-03:00")
    assert len(f) <= 29, "maxLength 29"


def test_manda_el_header_de_version_en_el_post():
    _, kw = _post()
    assert kw["headers"]["x-version"] == DHLClient.API_VERSION


def test_sin_ciudad_de_destino_ni_llama():
    """cityName tiene minLength 1: mandarlo vacío es un 400 garantizado."""
    out, kw = _post(destino={"country": "US", "postal_code": "33101", "city": ""})
    assert not out["encontrado"]
    assert kw is None, "no tendría que haber llamado a DHL"
    assert "ciudad" in out["error"].lower()


def test_una_sola_caja_sigue_yendo_por_el_GET():
    """El camino probado en producción no se toca."""
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"products": [_producto("P", 120.0)]}
    with mock.patch.dict(os.environ, {
        "DHL_API_KEY": "k", "DHL_API_SECRET": "s",
        "DHL_ACCOUNT_NUMBER_EXPO": "741622792"}, clear=True), \
         mock.patch("core.dhl_client.requests.get", return_value=resp) as get, \
         mock.patch("core.dhl_client.requests.post") as post:
        out = DHLClient().get_rates(ORIGEN, DESTINO, paquetes=[BULTOS[0]])
    assert out["encontrado"]
    assert get.called and not post.called


# ── create_shipment: las reglas de negocio de Leandro (01/08) ──

SHIPPER_CN = {
    "nombre": "Shenzhen Trading Co", "pais": "CN", "ciudad": "SHENZHEN",
    "city": "SHENZHEN", "postal_code": "518000", "zip": "518000",
    "direccion": "Main Rd 1", "telefono": "+86 755 1234567",
    "documento": "30-71234567-9",
}
RECEIVER_AR = {
    "nombre": "WAIMAO", "pais": "AR", "ciudad": "BUENOS AIRES",
    "zip": "1043", "direccion": "Corrientes 1234", "telefono": "+54 11 1234 5678",
}
BULTO_IMPO = {
    "peso_kg": 2.0, "largo_cm": 30, "ancho_cm": 20, "alto_cm": 10,
    "unidades": 1, "valor_unitario_usd": 150, "hs_code": "6109.10",
    "descripcion_en": "Cotton T-shirts",
}


def _emitir(extra=None):
    """Devuelve el body que se le manda a DHL, sin llamar a nadie."""
    datos = {"shipper": SHIPPER_CN, "recipient": RECEIVER_AR,
             "bultos": [BULTO_IMPO], **(extra or {})}
    resp = mock.Mock(status_code=201)
    resp.json.return_value = {"shipmentTrackingNumber": "1234567890", "documents": []}
    with mock.patch.dict(os.environ, {
        "DHL_API_KEY": "k", "DHL_API_SECRET": "s",
        "DHL_ACCOUNT_NUMBER_EXPO": "741622792",
        "DHL_ACCOUNT_NUMBER_IMPO": "730089966"}, clear=True), \
         mock.patch("core.dhl_client.requests.post", return_value=resp) as post:
        DHLClient().create_shipment(datos)
    return post.call_args.kwargs["json"]


def test_el_incoterm_sale_de_quien_paga_los_impuestos():
    """
    Estaba fijo en DAP: si el cliente elegía hacerse cargo, la guía salía
    igual con los impuestos a cargo del que recibe.
    """
    assert _emitir({"tax_paga": "DESTINATARIO"})["content"]["incoterm"] == "DAP"
    assert _emitir({"tax_paga": "CLIENTE"})["content"]["incoterm"] == "DDP"


def test_el_pais_de_fabricacion_es_el_del_origen_del_envio():
    """
    Regla de Leandro: sale de China → CN. El "AR" fijo era una declaración
    FALSA ante la aduana en cualquier importación — justo lo que hace WAIMAO.
    """
    li = _emitir()["content"]["exportDeclaration"]["lineItems"][0]
    assert li["manufacturerCountry"] == "CN", (
        "declara Argentina como origen de una importación de China"
    )


def test_manda_el_cuit_del_cliente_como_exportador():
    """El CUIT estaba en la base y nunca se le mandaba al courier."""
    reg = _emitir()["customerDetails"]["shipperDetails"].get("registrationNumbers")
    assert reg, "no viaja el CUIT del exportador"
    assert reg[0]["number"] == "30712345679", "el CUIT tiene que ir sin guiones"


def test_sin_cuit_no_manda_el_bloque_vacio():
    """Un registrationNumbers vacío hace que DHL rechace el envío entero."""
    sin_doc = {**SHIPPER_CN, "documento": ""}
    datos = {"shipper": sin_doc, "recipient": RECEIVER_AR, "bultos": [BULTO_IMPO]}
    resp = mock.Mock(status_code=201)
    resp.json.return_value = {"shipmentTrackingNumber": "1", "documents": []}
    with mock.patch.dict(os.environ, {
        "DHL_API_KEY": "k", "DHL_API_SECRET": "s",
        "DHL_ACCOUNT_NUMBER_IMPO": "730089966"}, clear=True), \
         mock.patch("core.dhl_client.requests.post", return_value=resp) as post:
        DHLClient().create_shipment(datos)
    ship = post.call_args.kwargs["json"]["customerDetails"]["shipperDetails"]
    assert "registrationNumbers" not in ship


def test_una_importacion_se_emite_con_la_cuenta_de_impo():
    assert _emitir()["accounts"][0]["number"] == "730089966"
