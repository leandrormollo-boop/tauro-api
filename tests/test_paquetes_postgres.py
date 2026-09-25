"""Persistencia/aislamiento reales. Sólo una base de pruebas explícita."""
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest
from servicios import paquetes as pkg
from servicios import paquetes_cotizacion as q
from servicios import paquetes_shopify as shop

URL=os.getenv("TAURO_TEST_DATABASE_URL","")
pytestmark=pytest.mark.skipif(not URL,reason="requiere PostgreSQL aislado")


@pytest.fixture
def db(monkeypatch):
    schema="test_paquetes_"+uuid.uuid4().hex
    root=Path(__file__).resolve().parents[1]
    admin=psycopg2.connect(URL);admin.autocommit=True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute((root/"sql/schema.sql").read_text(encoding="utf-8"))
        cur.execute((root/"sql/paquetes.sql").read_text(encoding="utf-8"))
        cur.execute("INSERT INTO clientes(cliente_id,email) VALUES ('A','a@example.invalid'),('B','b@example.invalid')")
        cur.execute("""INSERT INTO productos(cliente_id,alias_interno,nombre_invoice,hs_code,largo_cm,ancho_cm,alto_cm,peso_kg,activo)
            VALUES ('A','REEL','Fishing reel','95073000',15,15,10,.35,true),('B','REEL','Fishing reel','95073000',15,15,10,.35,true)""")
    @contextmanager
    def conn():
        c=psycopg2.connect(URL,cursor_factory=RealDictCursor)
        try:
            with c.cursor() as cur:cur.execute(f'SET search_path TO "{schema}"')
            yield c;c.commit()
        except Exception:c.rollback();raise
        finally:c.close()
    for mod in (pkg,q,shop):monkeypatch.setattr(mod,"get_conn",conn)
    yield conn
    with admin.cursor() as cur:cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
    admin.close()


BOX=dict(nombre="Caja reel",largo_cm=15,ancho_cm=15,alto_cm=10,tara_kg=".08",proteccion_kg=".02",max_kg=5)


def test_persistencia_y_aislamiento(db):
    bid=pkg.guardar_paquete("A",BOX)["id"]
    assert pkg.cargar_configuracion("B")["paquetes"]==[]
    assert pkg.cargar_configuracion("A")["paquetes"][0]["id"]==bid
    with pytest.raises(pkg.PaqueteError):pkg.archivar("B","paquete",bid)
    assert pkg.cargar_configuracion("A")["paquetes"][0]["activo"]


def test_fk_impide_asociar_caja_o_producto_de_otro_cliente(db):
    a=pkg.guardar_paquete("A",BOX)["id"]
    b=pkg.guardar_paquete("B",BOX)["id"]
    for product,box in [(1,b),(2,a)]:
        with pytest.raises(pkg.PaqueteError):pkg.guardar_asociacion("A",dict(producto_id=product,paquete_id=box,peso_neto_kg=".35",unidades_por_caja=1,confirmado=True))
    with pytest.raises(psycopg2.IntegrityError):
        with db() as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO paquetes_productos VALUES ('A',1,%s,.35,1,1)",(b,))


def test_edicion_no_cambia_medidas_ya_asociadas(db):
    bid=pkg.guardar_paquete("A",BOX)["id"]
    pkg.guardar_asociacion("A",dict(producto_id=1,paquete_id=bid,peso_neto_kg=".35",unidades_por_caja=1,confirmado=True))
    with pytest.raises(pkg.PaqueteError,match="asociados"):pkg.guardar_paquete("A",{**BOX,"largo_cm":30},bid)
    cfg=pkg.cargar_configuracion("A")
    assert pkg.planificar([{"producto_id":1,"cantidad":2}],cfg,pkg.cargar_catalogo("A"))["cajas_total"]==2


def test_version_y_archivo_no_borran_historia(db):
    bid=pkg.guardar_paquete("A",BOX)["id"]
    pkg.guardar_paquete("A",{**BOX,"largo_cm":16},bid)
    pkg.archivar("A","paquete",bid)
    data=pkg.cargar_configuracion("A")["paquetes"][0]
    assert data["version"]==3 and not data["activo"]
    pkg.guardar_paquete("A",BOX)


def test_confirmacion_fisica_obligatoria(db):
    bid=pkg.guardar_paquete("A",BOX)["id"]
    with pytest.raises(pkg.PaqueteError,match="físicamente"):
        pkg.guardar_asociacion("A",dict(producto_id=1,paquete_id=bid,peso_neto_kg=".35",unidades_por_caja=1))


def test_token_callback_ligado_a_owner_generacion_y_webhooks(db,monkeypatch):
    from servicios import integraciones_tienda
    monkeypatch.setattr(integraciones_tienda,"_ensure_tablas",lambda:None)
    with db() as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO tiendas_conectadas(cliente_id,plataforma,dominio,secreto,activa)
            VALUES ('A','shopify','a.myshopify.com','oauth:shopify-app',true);
            INSERT INTO shopify_instalaciones(dominio,cliente_id,access_token,install_generation,webhooks_ready,scopes)
            VALUES ('a.myshopify.com','A','encrypted-test','g1',true,'read_orders,write_shipping')""")
        cur.execute("""INSERT INTO paquetes_tiendas(cliente_id,plataforma,dominio,usar_paquetes,callback_token_hash,install_generation,checkout_activo)
            VALUES ('A','shopify','a.myshopify.com',true,%s,'g1',true)""",(shop.hash_token("a"*43),))
    assert shop.config_callback("a"*43)["cliente_id"]=="A"
    assert q.tiendas("A")[0]["checkout_activo"] is True
    assert shop.config_callback("b"*43) is None
    with db() as conn,conn.cursor() as cur:cur.execute("UPDATE shopify_instalaciones SET install_generation='g2'")
    assert shop.config_callback("a"*43) is None
    assert q.tiendas("A")[0]["checkout_activo"] is False
    with db() as conn,conn.cursor() as cur:cur.execute("UPDATE shopify_instalaciones SET install_generation='g1',webhooks_ready=false")
    assert shop.config_callback("a"*43) is None
    assert q.tiendas("A")[0]["checkout_activo"] is False
    with db() as conn,conn.cursor() as cur:cur.execute("UPDATE shopify_instalaciones SET webhooks_ready=true,scopes='read_orders'")
    assert shop.config_callback("a"*43) is None
    assert q.tiendas("A")[0]["checkout_activo"] is False


def test_migracion_replicada_en_schema_base():
    root=Path(__file__).resolve().parents[1]
    assert (root/"sql/paquetes.sql").read_text(encoding="utf-8").strip() in (root/"sql/schema.sql").read_text(encoding="utf-8")


def test_activar_reconcilia_servicio_sin_duplicarlo(db,monkeypatch):
    from servicios import shopify_app
    with db() as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO tiendas_conectadas(cliente_id,plataforma,dominio,secreto,activa)
            VALUES ('A','shopify','a.myshopify.com','oauth:shopify-app',true);
            INSERT INTO shopify_instalaciones(dominio,cliente_id,access_token,install_generation,webhooks_ready,scopes)
            VALUES ('a.myshopify.com','A','test','g1',true,'write_shipping');
            INSERT INTO paquetes_tiendas(cliente_id,plataforma,dominio,usar_paquetes,nacional)
            VALUES ('A','shopify','a.myshopify.com',true,'{"habilitado":true,"politica":"real"}')""")
    monkeypatch.setattr(q,"tienda_propia",lambda *_:{"id":1,"plataforma":"shopify","dominio":"a.myshopify.com"})
    monkeypatch.setattr(shopify_app,"instalacion",lambda *_:{"cliente_id":"A","access_token":"test","webhooks_ready":True,"scopes":"write_shipping","install_generation":"g1"})
    monkeypatch.setattr(shopify_app,"_base_url",lambda:"https://taurosolutions.ar")
    remote=[];created=[]
    def api(domain,token,query,variables=None):
        if "query PaquetesCarriers" in query:
            return {"carrierServices":{"edges":[{"node":r} for r in remote],"pageInfo":{"hasNextPage":False}}}
        created.append(variables)
        remote.append({"id":"gid://shopify/DeliveryCarrierService/1",**variables["input"]})
        return {"carrierServiceCreate":{"carrierService":remote[0],"userErrors":[]}}
    monkeypatch.setattr(shopify_app,"_graphql",api)
    assert shop.activar("A",1)["ok"]
    assert shop.activar("A",1)["ok"]
    assert len(created)==1
    token=remote[0]["callbackUrl"].rsplit('/',1)[1]
    assert shop.config_callback(token)["cliente_id"]=="A"
