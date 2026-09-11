from decimal import Decimal
import pytest
from servicios import paquetes as p, paquetes_pedidos as orders, paquetes_cotizacion as q
from tests.test_paquetes import datos


@pytest.fixture
def pedido(monkeypatch,datos):
    from servicios import direcciones,solicitud_automatica,solicitudes_guia,integraciones_tienda,api_b2b,cotizador
    cfg,cat=datos
    for product in cat:product.update(activo=True,nombre_invoice="Fishing tackle",hs_code="95073000",pais_origen_tienda="CN",valor_usd_default=20)
    cfg["combinaciones"]=[{"id":1,"version":1,"nombre":"Kit","paquete_id":2,"prioridad":0,"activo":True,
        "contenido":[{"producto_id":1,"cantidad":1},{"producto_id":2,"cantidad":2}]}]
    plan=p.planificar(cfg["combinaciones"][0]["contenido"],cfg,cat)
    monkeypatch.setattr(q,"plan_tienda",lambda *_:(plan,cat))
    monkeypatch.setattr(q,"guardar_plan_pedido",lambda *_:None)
    monkeypatch.setattr(direcciones,"obtener_remitente_para_envio",lambda *_:{"pais":"AR","cp":"1425","ciudad":"CABA","direccion":"Demo 1"})
    monkeypatch.setattr(solicitud_automatica,"_motivo",lambda pid,msg:{"ok":False,"motivo":msg})
    monkeypatch.setattr(cotizador,"dolar_ars",lambda:1000)
    monkeypatch.setattr(api_b2b,"cotizar_couriers_cliente",lambda *a,**kw:{"opciones":[{"id":"dhl","precio_ars":15000,"servicio":"DHL Express"}]})
    captures=[]
    monkeypatch.setattr(solicitudes_guia,"crear_solicitud_guia",lambda **kw:(captures.append(kw),{"id":55})[1])
    monkeypatch.setattr(integraciones_tienda,"marcar_convertido",lambda *a,**kw:None)
    ped={"id":1,"cliente_id":"JACKS","numero":"#100","origen_plataforma":"shopify","origen_dominio":"jacks.myshopify.com",
        "origen_pedido_externo_id":"100","items":[],"flete_cobrado":0,"moneda":"ARS",
        "flete_detalle":[{"codigo":"tauro_internacional_dhl_dhl"}],
        "destinatario":{"pais":"US","cp":"33101","ciudad":"Miami","nombre":"Prueba","direccion":"Demo 2"}}
    return ped,plan,cat,captures


def test_venta_conserva_cajas_invoice_y_cobro_tauro_aunque_comprador_tenga_gratis(pedido):
    ped,plan,cat,seen=pedido
    result=orders.crear_desde_pedido(ped)
    assert result["ok"]
    req=seen[0]
    assert req["cantidad"]==1 and req["courier"]=="DHL"
    assert req["peso_kg"]==.55
    assert len(req["bultos"][0]["items_invoice"])==2
    assert req["bultos"][0]["embalaje"]["paquete_id"]==2
    assert req["valor_declarado_usd"]==60
    assert req["precio_tauro_ars"]==15000 and req["precio_cliente_final_ars"]==0
    assert len(req["idempotency_key_hash"])==64
    assert req["origen_pedido_externo_id"]=="100"


def test_nacional_conserva_plan_sin_inventar_emision(pedido):
    ped,plan,cat,seen=pedido;ped["destinatario"].update(pais="AR",cp="2000",ciudad="Rosario")
    result=orders.crear_desde_pedido(ped)
    assert not result["ok"] and "1 caja" in result["motivo"] and not seen


def test_no_sustituye_courier_elegido(pedido):
    ped,_,_,seen=pedido;ped["flete_detalle"][0]["codigo"]="tauro_internacional_ups_ups"
    assert not orders.crear_desde_pedido(ped)["ok"] and not seen


def test_sin_aduana_no_inventa_valores_para_crear_guia(pedido):
    ped,plan,cat,seen=pedido;cat[0]["valor_usd_default"]=0
    assert not orders.crear_desde_pedido(ped)["ok"] and not seen
    cat[0].update(hs_code="",pais_origen_tienda="",activo=False)
    form=orders.bultos_invoice(plan,cat,para_revision=True)
    assert len(form)==1 and len(form[0]["items_invoice"])==2
    assert form[0]["items_invoice"][0]["valor_unitario_usd"]==0
    assert form[0]["items_invoice"][0]["hs_code"]==""
    assert form[0]["embalaje"]["paquete_id"]==plan["bultos"][0]["paquete_id"]
