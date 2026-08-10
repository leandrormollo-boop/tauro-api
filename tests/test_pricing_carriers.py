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

from servicios.carriers import _markup_de, _desc_fedex, _margen_fijo_de, _precios  # noqa: E402


def test_dhl_ganancia_fija_es_costo_mas_el_monto():
    """Leandro 04/08: DHL va con ganancia fija de $135.000 = costo + 135.000,
    sin markup % encima."""
    r = _precios({"costo": 130, "moneda": "USD"}, dolar=1500,
                 markup_pct=20, margen_fijo_ars=135000)
    assert r["precio_ars"] == 130 * 1500 + 135000   # 330.000


def test_margen_fijo_manda_sobre_el_markup():
    """Con margen fijo seteado, el markup % se ignora para ese courier."""
    con_fijo = _precios({"costo": 100, "moneda": "USD"}, dolar=1000,
                        markup_pct=50, margen_fijo_ars=135000)
    sin_fijo = _precios({"costo": 100, "moneda": "USD"}, dolar=1000, markup_pct=50)
    assert con_fijo["precio_ars"] == 100 * 1000 + 135000   # costo + fijo, no +50%
    assert sin_fijo["precio_ars"] != con_fijo["precio_ars"]


def test_margen_fijo_solo_para_el_courier_seteado():
    config = {"WEB_MARGEN_FIJO_DHL_ARS": 135000.0}
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _margen_fijo_de("dhl", config) == 135000.0
        assert _margen_fijo_de("fedex", config) == 0.0   # FedEx sigue con markup %
        assert _margen_fijo_de("ups", config) == 0.0


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
    with mock.patch.dict(os.environ, {"WEB_DESC_FEDEX_PCT": "85"}, clear=True):
        assert _desc_fedex({"WEB_DESC_FEDEX_PCT": 88.0}) == 88.0
    # Default del código: 90 desde el 06/08/2026 (antes 88).
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _desc_fedex({}) == 90.0


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
    WAIMAO: costo + $95.000. Costo USD 130,18 × 1450 = ARS 188.761.
    Precio final = 283.761. Nada de markup de la web acá.
    """
    from servicios.carriers import cotizar_carriers_cliente
    r = _cotizar(cotizar_carriers_cliente, **ENVIO,
                 pricing_cliente={"tipo": "FIJO_ARS", "valor": 95000.0})[0]
    assert r["precio_ars"] == round(130.18 * 1450) + 95000


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
                 pricing_cliente={"tipo": "FIJO_ARS", "valor": 95000.0})[0]
    filtradas = [k for k in r if k.startswith(("costo", "margen", "markup"))]
    assert not filtradas, f"el portal filtra el costo de TAURO: {filtradas}"


def test_moneda_no_soportada_falla_cerrado_y_no_calcula_precio():
    """EUR 130 nunca puede interpretarse como ARS 130 + el fijo de WAIMAO."""
    from servicios import carriers as C

    class Euro:
        def get_rates(self, _o, _d, _p):
            return {"encontrado": True, "costo": 130, "moneda": "EUR",
                    "servicio": "EXPRESS", "dias_estimados": "2"}

    configuracion = [{
        "id": "dhl", "nombre": "DHL Express", "servicio": "Express",
        "logo": "/d.svg", "requisitos": ("FAKE_KEY",), "cliente": Euro,
    }]
    with mock.patch.dict(os.environ, {"FAKE_KEY": "1"}, clear=True), \
         mock.patch.object(C, "CARRIERS", configuracion):
        costos = C.costos_carriers(
            origen={"country": "CN"}, destino={"country": "AR"},
            paquete={"peso_kg": 1},
        )
        portal = C.cotizar_carriers_cliente(
            origen={"country": "CN"}, destino={"country": "AR"},
            paquete={"peso_kg": 1}, dolar=1500,
            pricing_cliente={"tipo": "FIJO_ARS", "valor": 95000},
        )

    assert costos[0]["estado"] == "sin_tarifa"
    assert portal[0]["estado"] == "sin_tarifa"
    assert "precio_ars" not in portal[0]


def test_costo_cero_negativo_o_nan_falla_antes_del_pricing():
    from servicios import carriers as C

    for invalido in (0, -3, float("nan")):
        class ImporteInvalido:
            def get_rates(self, _o, _d, _p):
                return {"encontrado": True, "costo": invalido, "moneda": "USD",
                        "servicio": "EXPRESS", "dias_estimados": "2"}

        configuracion = [{
            "id": "dhl", "nombre": "DHL Express", "servicio": "Express",
            "logo": "/d.svg", "requisitos": ("FAKE_KEY",),
            "cliente": ImporteInvalido,
        }]
        with mock.patch.dict(os.environ, {"FAKE_KEY": "1"}, clear=True), \
             mock.patch.object(C, "CARRIERS", configuracion):
            salida = C.cotizar_carriers_cliente(
                origen={"country": "CN"}, destino={"country": "AR"},
                paquete={"peso_kg": 1}, dolar=1500,
                pricing_cliente={"tipo": "FIJO_ARS", "valor": 95000},
            )

        assert salida[0]["estado"] == "sin_tarifa"
        assert "precio_ars" not in salida[0]


def test_la_web_y_el_portal_dan_precios_distintos():
    """
    El punto de haber partido las capas: mismo costo, dos negocios.
    Si dieran igual, alguien enchufó el cotizador equivocado.
    """
    from servicios.carriers import cotizar_carriers, cotizar_carriers_cliente
    web = _cotizar(cotizar_carriers, **ENVIO, markup_pct=20.0)[0]
    portal = _cotizar(cotizar_carriers_cliente, **ENVIO,
                      pricing_cliente={"tipo": "FIJO_ARS", "valor": 95000.0})[0]
    assert web["precio_ars"] != portal["precio_ars"]


# ── Multi-bulto: quién sabe cotizar N cajas ──────────────────

def _carriers_mixtos():
    """FedEx sabe multi-bulto; DHL (todavía) no."""
    class ConMulti:
        MULTIBULTO = True
        def get_rates(self, o, d, paquete=None, paquetes=None):
            n = len(paquetes) if paquetes else 1
            return {"encontrado": True, "costo": 100.0 * n, "moneda": "USD",
                    "servicio": "PRIORITY", "dias_estimados": "4"}

    class SinMulti:
        MULTIBULTO = False
        def get_rates(self, o, d, paquete=None, paquetes=None):
            raise AssertionError("no se le puede pedir multi-bulto a este courier")

    return [
        {"id": "fedex", "nombre": "FedEx", "servicio": "Priority",
         "logo": "/f.svg", "requisitos": ("FAKE_KEY",), "cliente": ConMulti},
        {"id": "dhl", "nombre": "DHL Express", "servicio": "Worldwide",
         "logo": "/d.svg", "requisitos": ("FAKE_KEY",), "cliente": SinMulti},
    ]


def _costos(**kw):
    from servicios import carriers as C
    with mock.patch.dict(os.environ, {"FAKE_KEY": "1"}, clear=True), \
         mock.patch.object(C, "CARRIERS", _carriers_mixtos()):
        return {c["id"]: c for c in C.costos_carriers(**kw)}


def test_con_varias_cajas_el_que_no_sabe_queda_afuera():
    """
    Sumar los pesos cotizaría de MENOS: cada caja paga por su propio peso
    volumétrico, y tres cajas grandes y livianas se volverían una chica y
    pesada. Mejor no ofrecer ese courier que dar un precio que no se factura.
    """
    r = _costos(origen={"country": "AR"}, destino={"country": "US"},
                paquete=None, paquetes=[{"peso_kg": 1.4}, {"peso_kg": 3.0}])
    assert r["fedex"]["estado"] == "cotizado"
    assert r["dhl"]["estado"] == "sin_multibulto"
    assert "varias cajas" in r["dhl"]["error"]


def test_con_una_sola_caja_cotizan_los_dos():
    """Una caja no es multi-bulto: nadie queda afuera."""
    class Simple:
        MULTIBULTO = False
        def get_rates(self, o, d, paquete=None, paquetes=None):
            return {"encontrado": True, "costo": 90.0, "moneda": "USD",
                    "servicio": "WORLDWIDE", "dias_estimados": "2"}
    from servicios import carriers as C
    cs = _carriers_mixtos()
    cs[1]["cliente"] = Simple
    with mock.patch.dict(os.environ, {"FAKE_KEY": "1"}, clear=True), \
         mock.patch.object(C, "CARRIERS", cs):
        r = {c["id"]: c for c in C.costos_carriers(
            origen={"country": "AR"}, destino={"country": "US"},
            paquete=None, paquetes=[{"peso_kg": 1.4}])}
    assert r["fedex"]["estado"] == "cotizado"
    assert r["dhl"]["estado"] == "cotizado"


def test_el_camino_de_un_bulto_sigue_igual():
    """La web pública manda `paquete` singular: no se puede haber roto."""
    r = _costos(origen={"country": "AR"}, destino={"country": "US"},
                paquete={"peso_kg": 1.4})
    assert r["fedex"]["estado"] == "cotizado"
    assert r["fedex"]["costo"] == 100.0
