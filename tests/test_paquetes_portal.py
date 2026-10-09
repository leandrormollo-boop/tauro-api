from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from endpoints import paquetes as ep


@pytest.fixture
def web(monkeypatch):
    app=FastAPI();app.include_router(ep.router)
    app.dependency_overrides[ep.cliente_actual]=lambda:"CLIENTE"
    return TestClient(app)


def test_csrf_se_valida_en_handler(web,monkeypatch):
    monkeypatch.setattr(ep.pkg,"guardar_paquete",lambda *a:pytest.fail("No debe guardar"))
    assert web.post('/portal/paquetes/embalajes',json={},headers={"Origin":"https://evil.example"}).status_code==403


def test_tope_payload_y_objeto_json(web):
    assert web.post('/portal/paquetes/embalajes',content='x'*131073).status_code==413
    assert web.post('/portal/paquetes/embalajes',json=[]).status_code==422


def test_api_exige_credenciales(web):
    assert web.get('/api/paquetes').status_code==403
    assert web.post('/api/paquetes/cotizar',json={}).status_code==403


def test_guardar_toma_cliente_de_sesion_no_de_body(web,monkeypatch):
    seen=[]
    monkeypatch.setattr(ep.pkg,"guardar_paquete",lambda customer,data:(seen.append(customer),{"id":1})[1])
    assert web.post('/portal/paquetes/embalajes',json={"cliente_id":"OTRO"}).json()=={"ok":True,"id":1}
    assert seen==["CLIENTE"]


def test_shopify_v1_no_expone_callback_de_tarifas(web):
    resp=web.post('/integraciones/paquetes/shopify/test',json={})
    assert resp.status_code==404
