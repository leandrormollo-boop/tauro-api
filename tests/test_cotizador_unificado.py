from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
from unittest.mock import Mock

import pytest
from starlette.requests import Request
from fastapi import FastAPI
import asyncio
from urllib.parse import urlencode


class TestClient:
    __test__ = False
    def __init__(self, app): self.app = app
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, url, **kwargs): return self.request("GET", url, **kwargs)
    def post(self, url, **kwargs): return self.request("POST", url, **kwargs)
    def request(self, method, url, data=None, headers=None, **kwargs):
        parsed = urlparse(url)
        body = urlencode(data or {}, doseq=True).encode()
        messages = []
        async def receive(): return {"type":"http.request", "body":body, "more_body":False}
        async def send(message): messages.append(message)
        scope = {"type":"http", "asgi":{"version":"3.0"}, "http_version":"1.1",
            "method":method, "scheme":"http", "path":parsed.path, "raw_path":parsed.path.encode(),
            "query_string":parsed.query.encode(), "root_path":"",
            "headers":[(b"host",b"testserver"),(b"content-type",b"application/x-www-form-urlencoded")] + [(k.lower().encode(),v.encode()) for k,v in (headers or {}).items()],
            "client":("127.0.0.1",50000), "server":("testserver",80), "state":{"csp_nonce":"qa"}}
        asyncio.run(self.app(scope,receive,send))
        start = next(m for m in messages if m["type"] == "http.response.start")
        text = b"".join(m.get("body",b"") for m in messages if m["type"] == "http.response.body").decode()
        return SimpleNamespace(status_code=start["status"],text=text)

from endpoints import portal_cliente as portal
from servicios import cotizador_portal_nacional as nacional
from servicios.carrier_adapter import QuoteResult, OperationState


def datos():
    return dict(origen_provincia='B', origen_localidad='La Plata', origen_cp='1900',
                destino_provincia='X', destino_localidad='Córdoba', destino_cp='5000',
                modalidad_origen='domicilio', modalidad_destino='domicilio',
                cantidad_bultos='2', peso_kg='5,5', largo_cm='30,5', ancho_cm='20',
                alto_cm='10', valor_declarado_ars='100.000')


def resultado(**changes):
    return QuoteResult(**dict(dict(state=OperationState.COTIZADO, carrier_id='oca',
        quote_id='Q-test', service_code='p2p', service_name='Puerta a puerta',
        carrier_cost=Decimal('12100.00'), carrier_currency='ARS',
        customer_price=Decimal('18150.00'), currency='ARS', estimated_days=3), **changes))


def configurar(monkeypatch, enabled={'oca'}):
    monkeypatch.setattr(nacional, 'configuracion_cotizacion', lambda _: {'couriers_habilitados': enabled})
    adapter = Mock()
    adapter.quote.return_value = (resultado(),)
    factory = Mock(return_value=(None, adapter))
    monkeypatch.setattr(nacional.oca_portal, 'adapter_cliente', factory)
    return factory, adapter


def test_nacional_consulta_cp_y_bultos_sin_direccion_completa(monkeypatch):
    factory, adapter = configurar(monkeypatch)
    quote = nacional.cotizar_referencia_nacional('CLIENTE_A', **datos())
    factory.assert_called_once_with('CLIENTE_A')
    request = adapter.quote.call_args.args[0]
    assert request.customer_id == 'CLIENTE_A'
    assert request.origin['codigo_postal'] == '1900'
    assert request.packages[0].quantity == 2
    assert request.packages[0].weight_kg == Decimal('5.5')
    assert request.declared_value == Decimal('100000')
    op = quote['opciones'][0]
    assert op['precio_final_ars'] == Decimal('18150.00')
    assert op['dias_estimados'] == 3
    assert not any(word in repr(quote) for word in ('carrier_cost', 'markup', 'costo_ars', 'account'))
    url = urlparse(op['continuar_url'])
    fields = parse_qs(url.query)
    assert url.path == '/portal/oca/nuevo'
    assert fields['peso_kg'] == ['5.5']
    assert fields['cantidad_bultos'] == ['2']
    assert not any('precio' in key for key in fields)


def test_nunca_consulta_couriers_no_habilitados(monkeypatch):
    factory, adapter = configurar(monkeypatch, {'dhl'})
    assert nacional.cotizar_referencia_nacional('OTRO_CLIENTE', **datos())['opciones'] == []
    factory.assert_not_called()
    adapter.quote.assert_not_called()


def test_errores_no_filtran_detalles_y_no_hay_efectos_de_emision(monkeypatch):
    factory, adapter = configurar(monkeypatch)
    adapter.quote.side_effect = RuntimeError('SECRET_ACCOUNT_PASSWORD_XML')
    quote = nacional.cotizar_referencia_nacional('A', **datos())
    assert quote['opciones'] == [] and quote['no_disponibles'][0]['id'] == 'oca'
    assert 'SECRET' not in repr(quote)
    adapter.create_shipment.assert_not_called()


@pytest.mark.parametrize('changes', [dict(customer_price=Decimal('1')), dict(customer_price=Decimal('NaN')), dict(currency='USD'), dict(carrier_id='dhl')])
def test_rechaza_tarifas_invalidas(monkeypatch, changes):
    _, adapter = configurar(monkeypatch)
    adapter.quote.return_value = (resultado(**changes),)
    assert not nacional.cotizar_referencia_nacional('A', **datos())['opciones']


def request(method='GET'):
    r = Request({'type':'http', 'method':method, 'path':'/portal/cotizar', 'headers':[],
                 'query_string':b'', 'scheme':'http', 'server':('testserver',80)})
    r.state.csp_nonce='test'
    return r


def render_quote(**context):
    return portal.templates.TemplateResponse(request=request('POST'), name='portal/cotizar.html', context={
        'cliente':'DEMO', 'ambito':'internacional', 'paises_destino':[('AR','Argentina'),('US','Estados Unidos')],
        'paises_origen':[('AR','Argentina'),('US','Estados Unidos')], 'provincias':[('B','Buenos Aires'),('X','Córdoba')],
        'form':{'bultos':[{}]}, 'opciones':[], 'no_disponibles':[], 'error':None,
        'rutas_frecuentes':[], 'cajas_cotizadas':'[]', **context}).body.decode()


def test_render_una_pantalla_dos_ambitos_y_sin_emojis():
    html=render_quote()
    assert 'data-quote-panel="nacional"' in html and 'data-quote-panel="internacional"' in html
    assert '🌐' not in html and '🇦🇷' not in html
    assert 'Paso 1 de 2' not in html and 'quote-route-next' not in html
    assert html.count('id="origen_pais"') == 1
    assert 'data-quote-results' in html


def test_no_inventa_plazo_y_escapa_contenido():
    op={'carrier_id':'oca','carrier_nombre':'OCA','servicio':'<script>bad</script>',
        'carrier_logo':'/static/img/carriers/oca.png','precio_final_ars':Decimal('15000'),
        'dias_estimados':None,'continuar_url':'/portal/oca/nuevo'}
    html=render_quote(ambito='nacional', opciones=[op])
    assert 'A confirmar' in html and 'None días' not in html
    assert '<script>bad</script>' not in html
    assert 'IVA incluido' in html


@pytest.fixture
def web(monkeypatch):
    from servicios import cotizaciones_reseller, rate_limit
    monkeypatch.setattr(cotizaciones_reseller,'cliente_es_reseller',lambda _:False)
    monkeypatch.setattr(portal,'obtener_rutas_frecuentes',lambda _:[])
    monkeypatch.setattr(portal,'_operadores_cliente',lambda *_:[])
    monkeypatch.setattr(rate_limit,'check_rate',lambda *a,**k:True)
    app=FastAPI()
    app.include_router(portal.router)
    app.dependency_overrides[portal.cliente_actual]=lambda:'CLIENTE_AUTENTICADO'
    with TestClient(app) as client:
        yield client


def test_get_nacional_no_redirige_al_form_de_emision(web):
    response=web.get('/portal/cotizar?ambito=nacional',follow_redirects=False)
    assert response.status_code==200
    assert 'data-quote-active="nacional"' in response.text


def test_post_nacional_real_usa_identidad_autenticada(web,monkeypatch):
    factory,adapter=configurar(monkeypatch)
    response=web.post('/portal/cotizar',data={**datos(),'ambito':'nacional','cliente':'INTRUSO'})
    assert response.status_code==200
    factory.assert_called_once_with('CLIENTE_AUTENTICADO')
    assert '18.150' in response.text and '3 días hábiles' in response.text


def test_post_internacional_muestra_ambos_y_una_caida_parcial(web,monkeypatch):
    mock=Mock(return_value={'opciones':[{'carrier_id':'dhl','carrier_nombre':'DHL','carrier_logo':'/dhl.svg',
        'servicio':'Express','precio_final_ars':100000,'dias_estimados':'3'}],
        'no_disponibles':[{'id':'fedex','nombre':'FedEx','motivo':'Sin tarifa para esta ruta.'}],
        'resumen':{'ruta':'AR → US'},'encontrado':True})
    monkeypatch.setattr(portal,'cotizar_referencia_couriers',mock)
    response=web.post('/portal/cotizar',data={'ambito':'internacional','origen_pais':'AR',
        'destino_pais':'US','origen_ciudad':'Buenos Aires','origen_cp_internacional':'1000',
        'destino_ciudad_internacional':'Miami','destino_cp_internacional':'33101',
        'bulto_cantidad':'1','bulto_peso':'1,5','bulto_largo':'20','bulto_ancho':'20',
        'bulto_alto':'20','valor_declarado_usd':'100'})
    assert response.status_code==200
    assert 'Continuar con DHL' in response.text and 'Sin tarifa para esta ruta.' in response.text
    assert mock.call_args.kwargs['cliente']=='CLIENTE_AUTENTICADO'


def test_cotizacion_exige_autenticacion():
    app=FastAPI();app.include_router(portal.router)
    with TestClient(app) as client:
        response=client.get('/portal/cotizar',follow_redirects=False)
    assert response.status_code in (302,303,401)


def test_autoquote_devuelve_solo_resultados_y_respeta_limite(web,monkeypatch):
    from servicios import rate_limit
    configurar(monkeypatch)
    response=web.post('/portal/cotizar',data={**datos(),'ambito':'nacional'},headers={'X-Requested-With':'TauroQuoteWindow'})
    assert response.status_code==200 and 'data-quote-response' in response.text
    assert '<html' not in response.text and 'form-cotizar' not in response.text
    monkeypatch.setattr(rate_limit,'check_rate',lambda *a,**k:False)
    assert web.post('/portal/cotizar',data={'ambito':'nacional'}).status_code==429


def test_no_hay_precio_si_oca_falla_ni_si_permiso_se_revoca(web,monkeypatch):
    factory,adapter=configurar(monkeypatch)
    adapter.quote.side_effect=RuntimeError('SECRET')
    r=web.post('/portal/cotizar',data={**datos(),'ambito':'nacional'})
    assert r.status_code==200 and 'No pudimos consultar su tarifa' in r.text
    assert 'Continuar con OCA' not in r.text and 'SECRET' not in r.text
    configurar(monkeypatch,set())
    r=web.post('/portal/cotizar',data={**datos(),'ambito':'nacional'})
    assert 'Tu cuenta todavía no tiene operadores nacionales' in r.text
    assert 'Continuar con OCA' not in r.text
