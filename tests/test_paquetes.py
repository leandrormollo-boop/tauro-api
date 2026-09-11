from copy import deepcopy
from decimal import Decimal
import pytest
from servicios import paquetes as p


@pytest.fixture
def datos():
    catalogo = [{"id":1,"alias_interno":"REEL","sync_activo":True,"plataforma":"shopify",
        "tienda_dominio":"jacks.myshopify.com","external_variant_id":"gid://shopify/ProductVariant/10"},
        {"id":2,"alias_interno":"SENUELO","sync_activo":True,"plataforma":"shopify",
        "tienda_dominio":"jacks.myshopify.com","external_variant_id":"gid://shopify/ProductVariant/20"}]
    box = {"id":1,"version":1,"activo":True,"nombre":"Reel","largo_cm":15,"ancho_cm":15,
        "alto_cm":10,"tara_kg":Decimal(".080"),"proteccion_kg":Decimal(".020"),"max_kg":5}
    config = {"paquetes":[box,{**box,"id":2,"nombre":"Combinada","largo_cm":25,"ancho_cm":20,"alto_cm":15}],
        "asociaciones":[{"producto_id":1,"paquete_id":1,"peso_neto_kg":Decimal(".350"),"unidades_por_caja":1,"version":1},
            {"producto_id":2,"paquete_id":1,"peso_neto_kg":Decimal(".050"),"unidades_por_caja":4,"version":1}],
        "combinaciones":[]}
    return config,catalogo


def test_reel_usa_medidas_externas_y_suma_tara_y_proteccion(datos):
    plan=p.planificar([{"producto_id":1,"cantidad":1}],*datos)
    assert plan["peso_total_kg"] == .450
    assert plan["peso_facturable_kg"] == .450
    assert plan["cajas_total"] == 1
    assert plan["bultos"][0]["largo_cm"] == 15


def test_varias_unidades_usan_capacidad_confirmada_y_caja_parcial(datos):
    plan=p.planificar([{"producto_id":2,"cantidad":9}],*datos)
    assert [b["contenido"][0]["cantidad"] for b in plan["bultos"]] == [4,4,1]
    assert [b["peso_kg"] for b in plan["bultos"]] == [.3,.3,.15]
    assert plan["peso_total_kg"] == .75
    assert plan["peso_facturable_kg"] == 1.35


def test_mixto_sin_receta_separa_productos(datos):
    plan=p.planificar([{"producto_id":1,"cantidad":2},{"producto_id":2,"cantidad":2}],*datos)
    assert plan["cajas_total"] == 3
    assert sum(b["peso_kg"] for b in plan["bultos"]) == 1.1


def test_receta_repetida_conserva_todas_las_unidades_y_no_muta_config(datos):
    cfg,cat=datos
    cfg["combinaciones"]=[{"id":1,"version":3,"activo":True,"nombre":"Kit","paquete_id":2,"prioridad":0,
        "contenido":[{"producto_id":1,"cantidad":1},{"producto_id":2,"cantidad":2}]}]
    antes=deepcopy(cfg)
    plan=p.planificar([{"producto_id":1,"cantidad":2},{"producto_id":2,"cantidad":5}],cfg,cat)
    assert plan["cajas_total"] == 3
    assert [b["paquete_id"] for b in plan["bultos"]] == [2,2,1]
    assert [b["combinacion_version"] for b in plan["bultos"]] == [3,3,None]
    assert plan["unidades_total"] == 7
    assert cfg == antes


def test_filas_duplicadas_se_agrupan_antes_de_embalar(datos):
    plan=p.planificar([{"producto_id":2,"cantidad":2},{"producto_id":2,"cantidad":2}],*datos)
    assert plan["cajas_total"]==1
    assert plan["bultos"][0]["contenido"][0]["cantidad"]==4


@pytest.mark.parametrize("value",[0,-1,True,1.5,"NaN","Infinity","1e100","",None])
def test_cantidad_invalida_falla_antes_de_cotizar(datos,value):
    with pytest.raises(p.PaqueteError):
        p.planificar([{"producto_id":1,"cantidad":value}],*datos)


@pytest.mark.parametrize("campo,value",[("largo_cm","NaN"),("max_kg",0),("tara_kg",-1),("largo_cm",301),("alto_cm","1.111")])
def test_validacion_fisica(campo,value):
    d=dict(nombre="Caja",largo_cm=15,ancho_cm=15,alto_cm=10,tara_kg="0,08",proteccion_kg="0,02",max_kg=5)
    d[campo]=value
    with pytest.raises(p.PaqueteError): p.validar_paquete(d)


def test_producto_sin_asociacion_no_usa_medidas_inventadas(datos):
    datos[0]["asociaciones"]=[]
    with pytest.raises(p.PaqueteError,match="Asociá"):p.planificar([{"producto_id":1,"cantidad":1}],*datos)


def test_archivado_y_exceso_de_peso_bloquean(datos):
    datos[0]["paquetes"][0]["activo"]=False
    with pytest.raises(p.PaqueteError,match="archivado"):p.planificar([{"producto_id":1,"cantidad":1}],*datos)
    datos[0]["paquetes"][0]["activo"]=True
    datos[0]["paquetes"][0]["max_kg"]=.4
    with pytest.raises(p.PaqueteError,match="peso máximo"):p.planificar([{"producto_id":1,"cantidad":1}],*datos)


def test_no_mas_de_20_cajas(datos):
    with pytest.raises(p.PaqueteError,match="20 cajas"):p.planificar([{"producto_id":1,"cantidad":21}],*datos)


def test_id_shopify_numerico_y_gid_identifican_la_misma_variante(datos):
    for vid in [10,"gid://shopify/ProductVariant/10"]:
        assert p.resolver_items_tienda([{"variant_id":vid,"quantity":2}],datos[1],"shopify","jacks.myshopify.com")==[{"producto_id":1,"cantidad":2}]


def test_no_cruza_variante_de_otra_tienda_ni_recurre_a_sku_si_variante_desconocida(datos):
    with pytest.raises(p.PaqueteError):p.resolver_items_tienda([{"variant_id":10,"quantity":1}],datos[1],"shopify","otra.myshopify.com")
    with pytest.raises(p.PaqueteError):p.resolver_items_tienda([{"variant_id":999,"sku":"REEL","quantity":1}],datos[1],"shopify","jacks.myshopify.com")


def test_ignora_productos_digitales(datos):
    result=p.resolver_items_tienda([{"requires_shipping":False,"quantity":1},{"variant_id":10,"quantity":1}],datos[1],"shopify","jacks.myshopify.com")
    assert len(result)==1


@pytest.mark.parametrize("cfg,subtotal,expected",[({"politica":"real"},100,"1000.00"),
    ({"politica":"markup","markup_pct":"12.50"},100,"1125.00"),
    ({"politica":"gratis"},100,"0.00"),({"politica":"fijo","precio_fijo_ars":"500"},100,"500.00"),
    ({"politica":"real","gratis_desde_ars":"200"},200,"0.00"),
    ({"politica":"real","gratis_desde_ars":"200"},199,"1000.00")])
def test_politica_comercial_separa_tarifa_de_precio_comprador(cfg,subtotal,expected):
    assert p.precio_comprador("1000.00",cfg,subtotal)==Decimal(expected)


@pytest.mark.parametrize("cfg",[{"politica":"fijo","precio_fijo_ars":0},{"politica":"fijo","precio_fijo_ars":"NaN"},{"politica":"markup","markup_pct":301}])
def test_no_regala_envio_por_config_invalida(cfg):
    with pytest.raises(p.PaqueteError):p.precio_comprador(1000,cfg,100)


def test_no_hay_envio_gratis_sin_tarifa_tauro():
    with pytest.raises(p.PaqueteError):p.precio_comprador(0,{"politica":"gratis"},100)
