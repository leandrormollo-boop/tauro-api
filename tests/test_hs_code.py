import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from servicios import hs_code as hs
from modelos.producto import ProductoNuevo
from endpoints import portal_cliente as pc


@pytest.mark.parametrize('description,code', [
    ('Cotton knitted t-shirts', '610910'),
    ('Remeras de algodón tejido de punto', '610910'),
    ('Fishing reels', '950730'), ('reel de pesca', '950730'),
    ('Smartphones', '851713'), ('Leather handbags', '420221'),
    ('Baterías de litio recargables', '850760'),
    ('camisas de poliester tejido plano para hombre', '620530'),
])
def test_candidates_from_real_reference_not_invented(description, code):
    result = hs.sugerir_hs(description)
    assert result['candidates'][0]['code'] == code
    assert result['requires_confirmation'] is True
    assert result['edition'] == 'HS2022'
    assert all(len(r['code']) == 6 for r in result['candidates'])


@pytest.mark.parametrize('description', ['ropa', 'mercadería', '100% cotton', 'zzzzqqqvvvv'])
def test_generic_or_unknown_description_does_not_invent_code(description):
    result = hs.sugerir_hs(description)
    assert result['candidates'] == []
    assert result['status'] == 'needs_details'
    assert result['questions']


def test_questions_for_missing_composition_and_construction():
    result = hs.sugerir_hs('Shirts')
    assert any('composición' in q for q in result['questions'])
    assert any('punto' in q for q in result['questions'])
    assert any('hombre' in q for q in result['questions'])


def test_added_details_narrow_results_without_mutating_input():
    result = hs.sugerir_hs('T-shirts', '100% cotton knitted')
    assert result['candidates'][0]['code'] == '610910'
    assert result['questions'] == []


def test_reference_has_complete_unique_hs6_and_attribution():
    data, codes, _, _ = hs._indice()
    assert len(codes) == 5612
    assert data['source_sha256']
    assert data['source_url'].startswith('https://www.usitc.gov/')
    assert 'publicdomain' in data['source_license']
    assert '010121' in codes
    assert not any(c.startswith(('98', '99')) for c in codes)


@pytest.mark.parametrize('code,expected', [('610910','6109.10'), ('6109.10','6109.10'),
    ('42022100','4202.21.00'), ('4202.21.00','4202.21.00'), ('6109100012','6109.10.00.12')])
def test_hs_does_not_pad_or_drop_national_digits(code, expected):
    assert hs.formato_hs(code) == expected
    product = ProductoNuevo(alias_interno='DEMO', nombre_invoice='Cotton t-shirt', hs_code=code,
        largo_cm=1, ancho_cm=1, alto_cm=1, peso_kg=1, valor_usd_default=1)
    assert product.hs_code == expected


@pytest.mark.parametrize('code', ['61','61091','6109.1.0','foo61091000','６１０９１０','6109.10.00junk'])
def test_invalid_codes_rejected(code):
    with pytest.raises(ValueError): hs.formato_hs(code)


def _request(raw):
    async def receive(): return {'type':'http.request','body':raw, 'more_body':False}
    return Request({'type':'http','method':'POST','path':'/portal/api/hs-code','headers':[]}, receive=receive)


def test_api_private_no_store_and_no_other_customer_data(monkeypatch):
    monkeypatch.setattr(pc, 'check_rate', lambda *a, **k: True)
    result = asyncio.run(pc.api_hs_code(_request(b'{"descripcion":"Fishing reels"}'), cliente='DEMO'))
    data = json.loads(result.body)
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'private, no-store'
    assert data['candidates'][0]['code'] == '950730'
    assert not any(k in data for k in ('cliente', 'precio', 'aprobado'))


@pytest.mark.parametrize('body,status', [(b'[]',422),(b'broken',422), (b'{"descripcion":123}',422),
    (json.dumps({'descripcion':'a'*1001}).encode(),422), (b'x'*8193,413)])
def test_api_invalid_bounded_payload(monkeypatch, body, status):
    monkeypatch.setattr(pc, 'check_rate', lambda *a, **k: True)
    result = asyncio.run(pc.api_hs_code(_request(body), cliente='DEMO'))
    assert result.status_code == status
    assert 'candidates' not in json.loads(result.body)


def test_api_rate_limit(monkeypatch):
    monkeypatch.setattr(pc, 'check_rate', lambda *a, **k: False)
    result = asyncio.run(pc.api_hs_code(_request(b'{}'), cliente='DEMO'))
    assert result.status_code == 429 and result.headers['retry-after'] == '60'


def test_api_requires_client_session():
    app = FastAPI(); app.include_router(pc.router)
    with TestClient(app, follow_redirects=False) as client:
        response = client.post('/portal/api/hs-code', json={'descripcion':'Fishing reels'})
    assert response.status_code in (302,303,401,403)


def test_reference_failure_graceful(monkeypatch):
    monkeypatch.setattr(pc, 'check_rate', lambda *a, **k: True)
    def unavailable(*args): raise OSError('unavailable')
    monkeypatch.setattr(hs, 'sugerir_hs', unavailable)
    result = asyncio.run(pc.api_hs_code(_request(b'{"descripcion":"Fishing reels"}'), cliente='DEMO'))
    assert result.status_code == 503
    assert 'manualmente' in json.loads(result.body)['error']
