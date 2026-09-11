import asyncio
from copy import deepcopy
from decimal import Decimal
import time
import pytest
from servicios import paquetes as p
from servicios import paquetes_shopify as shop
from servicios import paquetes_cotizacion as quotes
from servicios import tiendanube_shipping as tn
from tests.test_paquetes import datos


@pytest.fixture
def checkout(monkeypatch,datos):
    cfg,cat=datos
    monkeypatch.setattr(p,"cargar_catalogo",lambda _:cat)
    monkeypatch.setattr(p,"cargar_configuracion",lambda _:cfg)
    config={"cliente_id":"JACKS","dominio":"jacks.myshopify.com","nacional":{"habilitado":True,"politica":"real"},"internacional":{"habilitado":True,"politica":"real"}}
    monkeypatch.setattr(shop,"config_callback",lambda _:config)
    captured=[]
    def cotizar(cliente,plan,origin,destination,value,policy):
        captured.append((cliente,plan,origin,destination,value))
        return {"encontrado":True,"opciones":[{"courier":"dhl","codigo_servicio":"dhl","servicio":"DHL Express",
            "precio_tauro_ars":"12500.50","precio_comprador_ars":str(p.precio_comprador("12500.50",policy,value))}]}
    monkeypatch.setattr(quotes,"cotizar_plan",cotizar)
    payload={"rate":{"currency":"ARS","origin":{"country":"AR","postal_code":"1000"},
        "destination":{"country":"US","postal_code":"33101","city":"Miami"},
        "items":[{"variant_id":10,"quantity":2,"price":100000,"grams":1}]}}
    return config,payload,captured


def test_checkout_usa_cajas_guardadas_y_centavos_no_peso_del_payload(checkout):
    _,payload,seen=checkout
    result=shop._rates(payload,"token")
    assert result["rates"][0]["total_price"]=="1250050"
    assert seen[0][1]["cajas_total"]==2
    assert seen[0][1]["peso_total_kg"]==.9
    assert seen[0][4]==Decimal("2000")
    assert "precio_tauro" not in str(result) and "costo" not in str(result)


def test_envio_gratis_no_pierde_precio_merchant(checkout):
    cfg,payload,seen=checkout;cfg["internacional"]["politica"]="gratis"
    assert shop._rates(payload,"token")["rates"][0]["total_price"]=="0"
    assert seen


def test_servicio_desactivado_no_llama_courier(checkout):
    cfg,payload,seen=checkout;cfg["internacional"]["habilitado"]=False
    assert shop._rates(payload,"token")=={"rates":[]}
    assert not seen


def test_no_acepta_moneda_no_configurada(checkout):
    _,payload,seen=checkout;payload["rate"]["currency"]="USD"
    with pytest.raises(p.PaqueteError):shop._rates(payload,"token")
    assert not seen


def test_token_desconocido_no_llega_a_cotizar(checkout,monkeypatch):
    _,payload,seen=checkout;monkeypatch.setattr(shop,"config_callback",lambda _:None)
    with pytest.raises(shop.CheckoutNoDisponible):shop._rates(payload,"token")
    assert not seen


def test_timeout_acotado_liberacion_del_worker(monkeypatch):
    monkeypatch.setattr(shop,"CALLBACK_SECONDS",.02)
    monkeypatch.setattr(shop,"_rates",lambda *_:(time.sleep(.08),{"rates":[]})[1])
    async def run():
        with pytest.raises(shop.CheckoutNoDisponible):await shop.cotizar_callback({},"t")
        await asyncio.sleep(.1)
    asyncio.run(run())
    assert shop._SLOTS.acquire(blocking=False)
    shop._SLOTS.release()


def test_tiendanube_usa_mismo_plan_y_respeta_limites(datos,monkeypatch):
    cfg,cat=datos
    cat=[{**r,"plataforma":"tiendanube","tienda_dominio":"123.tiendanube","external_variant_id":str(r["id"])} for r in cat]
    monkeypatch.setattr(p,"cargar_catalogo",lambda _:cat)
    monkeypatch.setattr(p,"cargar_configuracion",lambda _:cfg)
    data={"store_id":123,"currency":"ARS","origin":{"country":"AR","postal_code":"1000"},
        "destination":{"country":"AR","postal_code":"2000"},"items":[{"variant_id":2,"quantity":4,"price":1000}]}
    request=tn._quote_request(data,"JACKS",packaging={"cliente_id":"JACKS","usar_paquetes":True,"nacional":{"habilitado":True}})
    assert len(request.packages)==1
    assert request.packages[0].weight_kg==Decimal(".3")
    assert request.packages[0].length_cm==15


def test_direccion_real_y_cpa_no_se_reemplaza_por_referencia():
    assert quotes.direccion({"pais":"AR","cp":"C1425ABC"},"origen")["cp"]=="1425"
    with pytest.raises(p.PaqueteError):quotes.direccion({"pais":"US","cp":"","city":"Miami"},"destino")


def test_no_hay_tarifa_nacional_si_no_hay_operador(datos,monkeypatch):
    from servicios import carrier_adapter
    monkeypatch.setattr(carrier_adapter,"registered_adapters",lambda:())
    plan=p.planificar([{"producto_id":1,"cantidad":1}],*datos)
    r=quotes.cotizar_plan("JACKS",plan,{"pais":"AR","cp":"1000"},{"pais":"AR","cp":"2000"},1000,{"politica":"gratis"})
    assert not r["encontrado"] and r["opciones"]==[]


def test_callback_url_no_acepta_otro_host_ni_query():
    base="https://taurosolutions.ar"
    assert shop._token_url(base+"/integraciones/paquetes/shopify/"+"a"*43,base)=="a"*43
    assert not shop._token_url(base+".evil/integraciones/paquetes/shopify/"+"a"*43,base)
    assert not shop._token_url(base+"/integraciones/paquetes/shopify/"+"a"*43+"?q=x",base)
