from urllib.parse import urlparse, parse_qs

import pytest

from servicios.ubicaciones_cotizador import buscar_ubicaciones
from test_cotizador_unificado import web, configurar, datos, render_quote


def test_caba_cotiza_con_referencia_explicita_del_usuario():
    r = buscar_ubicaciones('AR', 'CABA', 'city', 'B')
    assert r['automatic']['postal_code'] == '1000'
    assert r['automatic']['province'] == 'C'
    assert r['automatic']['reference'] is True


@pytest.mark.parametrize('country,code,city', [('AR','1000','Buenos Aires'),('US','33101','Miami'),('US','90210','Beverly Hills')])
def test_codigo_postal_identifica_ciudad(country, code, city):
    assert buscar_ubicaciones(country, code, 'postal')['automatic']['city'] == city


def test_no_elige_una_localidad_arbitraria_ni_un_postal_parcial():
    assert buscar_ubicaciones('AR', '1900', 'postal')['automatic'] is None
    assert len(buscar_ubicaciones('AR', 'La Plata')['suggestions']) > 1
    assert buscar_ubicaciones('US', 'Springfield')['automatic'] is None
    assert buscar_ubicaciones('US', '331', 'postal')['automatic'] is None
    assert buscar_ubicaciones('AR', 'La Plata', province='B')['automatic']['postal_code'] == '1900'


def test_wilde_en_ambos_sentidos_y_prefijos_solo_sugieren():
    for query, mode in [('Wilde', 'city'), ('1875', 'postal'), ('B1875ABC', 'postal')]:
        option = buscar_ubicaciones('AR', query, mode)['automatic']
        assert (option['city'], option['postal_code'], option['province']) == ('Wilde', '1875', 'B')
    assert buscar_ubicaciones('AR', 'Wil')['automatic'] is None
    assert buscar_ubicaciones('AR', '187', 'postal')['automatic'] is None
    assert buscar_ubicaciones('AR', 'Wilde inexistente')['automatic'] is None
    # The exact reference ranks above a different city with a shared prefix.
    assert buscar_ubicaciones('AR', 'Wilde')['suggestions'][0]['city'] == 'Wilde'


def test_normaliza_pais_acentos_y_cpa_sin_inventar():
    assert buscar_ubicaciones('Argentina','Córdoba',province='X')['automatic']['postal_code'] == '5000'
    assert buscar_ubicaciones('AR','C1000AAA','postal')['automatic']['city'] == 'Buenos Aires'
    assert buscar_ubicaciones('GB','London')['automatic']['postal_code'] == 'EC1A 1BB'
    assert not buscar_ubicaciones('AR', 'NoExisteCiudadXX')['suggestions']
    assert not buscar_ubicaciones('../etc', '1000', 'postal')['suggestions']
    assert not buscar_ubicaciones('AR', "' OR 1=1;--")['suggestions']


def test_api_autenticada_y_limitada(web, monkeypatch):
    from servicios import rate_limit
    r = web.get('/portal/cotizar/ubicaciones?pais=AR&q=CABA')
    assert r.status_code == 200 and '1000' in r.text
    monkeypatch.setattr(rate_limit, 'check_rate', lambda *a, **k: False)
    assert web.get('/portal/cotizar/ubicaciones?pais=AR&q=CABA').status_code == 429


def test_referencia_nacional_no_se_transfiere_como_domicilio(web, monkeypatch):
    configurar(monkeypatch)
    r = web.post('/portal/cotizar', data={**datos(), 'ambito':'nacional', 'origen_referencia':'1'})
    from html import unescape
    import re
    href = re.search(r'href="(/portal/oca/nuevo\?[^\"]+)"', r.text).group(1)
    params = parse_qs(urlparse(unescape(href)).query)
    assert 'origen_cp' not in params and 'origen_localidad' not in params
    assert params['destino_cp'] == ['5000']
    assert params['peso_kg'] == ['5.5']


def test_referencia_internacional_no_se_transfiere_como_domicilio():
    html = render_quote(form={'origen_pais':'AR','destino_pais':'US','origen_ciudad':'Buenos Aires',
       'origen_cp_internacional':'1000','origen_referencia':'1', 'destino_ciudad_internacional':'Miami',
       'destino_cp_internacional':'33101','valor_declarado_usd':'100','bultos':[{}]},
       opciones=[{'carrier_id':'dhl','carrier_nombre':'DHL','servicio':'Express','precio_final_ars':20000}])
    assert 'origen_ciudad=&amp;origen_cp=&amp;' in html
    assert 'destino_ciudad=Miami&amp;destino_cp=33101' in html


def test_geo_endpoint_requiere_sesion():
    from fastapi import FastAPI
    from endpoints import portal_cliente
    from test_cotizador_unificado import TestClient
    app = FastAPI(); app.include_router(portal_cliente.router)
    with TestClient(app) as client:
        assert client.get('/portal/cotizar/ubicaciones?pais=AR&q=CABA').status_code in {302,303,401}
