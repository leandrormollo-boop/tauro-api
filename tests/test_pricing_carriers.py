"""
Cada courier tiene su propio margen (Leandro, 01/08/2026: "tenemos que
entender que cada COURIER FEDEX UPS o DHL tienen su markup diferente").

Lo que se protege acá es el ORDEN DE PRIORIDAD, que es donde esto se rompe
sin dar error: si el admin edita un margen en /admin/config y el código
sigue leyendo la variable de entorno, la pantalla muestra una perilla que no
mueve nada. Eso ya pasaba con WEB_MARKUP_PCT: la fila existía en la tabla
desde el día uno y nadie la leía.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios.carriers import _markup_de, _desc_fedex  # noqa: E402


def test_el_admin_le_gana_a_la_variable_de_entorno():
    """Lo que Leandro edita en /admin/config manda."""
    with mock.patch.dict(os.environ, {"WEB_MARKUP_PCT_DHL": "99"}, clear=True):
        assert _markup_de("dhl", 20.0, {"WEB_MARKUP_PCT_DHL": 25.0}) == 25.0


def test_sin_fila_en_config_vale_la_variable_de_entorno():
    with mock.patch.dict(os.environ, {"WEB_MARKUP_PCT_DHL": "30"}, clear=True):
        assert _markup_de("dhl", 20.0, {}) == 30.0


def test_sin_nada_propio_cae_al_general_de_config():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _markup_de("ups", 20.0, {"WEB_MARKUP_PCT": 18.0}) == 18.0


def test_cada_courier_puede_tener_el_suyo():
    """El punto de todo esto: tres couriers, tres márgenes distintos."""
    config = {
        "WEB_MARKUP_PCT": 20.0,
        "WEB_MARKUP_PCT_FEDEX": 35.0,
        "WEB_MARKUP_PCT_DHL": 20.0,
        "WEB_MARKUP_PCT_UPS": 28.0,
    }
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _markup_de("fedex", 20.0, config) == 35.0
        assert _markup_de("dhl", 20.0, config) == 20.0
        assert _markup_de("ups", 20.0, config) == 28.0


def test_un_valor_roto_no_tumba_el_cotizador():
    """Un margen mal tipeado tiene que caer al general, no explotar."""
    with mock.patch.dict(os.environ, {"WEB_MARKUP_PCT_UPS": "veinte"}, clear=True):
        assert _markup_de("ups", 20.0, {"WEB_MARKUP_PCT": 22.0}) == 22.0


def test_el_descuento_de_fedex_tambien_se_edita_desde_el_admin():
    """
    Si el margen de FedEx viviera en el admin y su descuento en Railway, la
    pantalla mostraría una perilla que no hace nada.
    """
    with mock.patch.dict(os.environ, {"WEB_DESC_FEDEX_PCT": "90"}, clear=True):
        assert _desc_fedex({"WEB_DESC_FEDEX_PCT": 88.0}) == 88.0
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _desc_fedex({}) == 88.0


# ── Las dos capas de precio: web vs portal ───────────────────

def _carriers_falsos(costo_usd=130.18):
    """CARRIERS con un solo courier activo que siempre cotiza lo mismo."""
    class Falso:
        def get_rates(self, o, d, p):
            return {"encontrado": True, "costo": costo_usd, "moneda": "USD",
                    "servicio": "EXPRESS_WORLDWIDE", "dias_estimados": "2"}
    return [{
        "id": "dhl", "nombre": "DHL Express", "servicio": "Express Worldwide",
        "logo": "/x.svg", "requisitos": ("FAKE_KEY",), "cliente": Falso,
    }]


def _cotizar(fn, **kw):
    from servicios import carriers as C
    with mock.patch.dict(os.environ, {"FAKE_KEY": "1"}, clear=True), \
         mock.patch.object(C, "CARRIERS", _carriers_falsos()), \
         mock.patch.object(C, "_pricing_configurado", return_value={}):
        return fn(**kw)


ENVIO = dict(origen={"country": "AR"}, destino={"country": "US"},
             paquete={"peso_kg": 1.4}, dolar=1450.0)


def test_el_portal_cobra_el_monto_fijo_del_cliente():
    """
    WAIMAO: costo + $100.000. Costo USD 130,18 × 1450 = ARS 188.761.
    Precio final = 288.761. Nada de markup de la web acá.
    """
    from servicios.carriers import cotizar_carriers_cliente
    r = _cotizar(cotizar_carriers_cliente, **ENVIO,
                 pricing_cliente={"tipo": "FIJO_ARS", "valor": 100000.0})[0]
    assert r["precio_ars"] == round(130.18 * 1450) + 100000


def test_cada_cliente_su_precio_con_el_mismo_costo():
    """Prete Rosso $11.000 y Melcior $14.000 sobre el MISMO envío."""
    from servicios.carriers import cotizar_carriers_cliente
    base = round(130.18 * 1450)
    for valor in (11000.0, 14000.0):
        r = _cotizar(cotizar_carriers_cliente, **ENVIO,
                     pricing_cliente={"tipo": "FIJO_ARS", "valor": valor})[0]
        assert r["precio_ars"] == base + valor


def test_el_portal_no_devuelve_el_costo_ni_el_margen():
    """
    LA REGLA: "NO puede ver el costo nuestro" (Leandro, 01/08/2026).
    aplicar_pricing devuelve markup_valor y markup_pct_equivalente; si alguno
    se cuela, el cliente despeja nuestro costo con una resta.
    """
    from servicios.carriers import cotizar_carriers_cliente
    r = _cotizar(cotizar_carriers_cliente, **ENVIO,
                 pricing_cliente={"tipo": "FIJO_ARS", "valor": 100000.0})[0]
    filtradas = [k for k in r if k.startswith(("costo", "margen", "markup"))]
    assert not filtradas, f"el portal filtra el costo de TAURO: {filtradas}"


def test_la_web_y_el_portal_dan_precios_distintos():
    """
    El punto de haber partido las capas: mismo costo, dos negocios.
    Si dieran igual, alguien enchufó el cotizador equivocado.
    """
    from servicios.carriers import cotizar_carriers, cotizar_carriers_cliente
    web = _cotizar(cotizar_carriers, **ENVIO, markup_pct=20.0)[0]
    portal = _cotizar(cotizar_carriers_cliente, **ENVIO,
                      pricing_cliente={"tipo": "FIJO_ARS", "valor": 100000.0})[0]
    assert web["precio_ars"] != portal["precio_ars"]
