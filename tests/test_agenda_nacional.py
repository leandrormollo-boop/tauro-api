"""Agenda compartida: round-trip HTTP y separación real de cuentas."""
import asyncio
import json
import pytest
import httpx
from fastapi import FastAPI
from servicios import direcciones as dd
from servicios.agenda_nacional import proyectar, datos_guardados
from endpoints import portal_cliente as pc, portal_oca as po
from test_conciliacion_couriers_postgres import conciliacion_db, DATABASE_URL


def test_legacy_no_inventa_calle_numero_ni_apellido():
    row = dict(id=1, tipo='DESTINATARIO', nombre='Ana Pérez', pais='AR', direccion='Calle 12 345, piso 2',
               ciudad='Wilde', estado='Buenos Aires', cp='1875')
    data = proyectar(row)
    assert data['fields']['calle'] == data['fields']['numero'] == data['fields']['apellido'] == ''
    assert data['fields']['provincia'] == 'B'
    assert proyectar({**row, 'pais':'US'}) is None


@pytest.mark.parametrize('changes', [{'numero':'abc'}, {'cp':'C1000ABC','estado':'Buenos Aires'}, {'calle':''}, {'estado':'ZZ'}])
def test_ficha_nacional_rechaza_domicilio_inconsistente(changes):
    fields=dict(nombre='Ana',apellido='Pérez',calle='Mitre',numero='123',piso='2',depto='A',pais='AR',estado='Buenos Aires',cp='1875')
    with pytest.raises(ValueError): datos_guardados(**{**fields, **changes})


@pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL aislado')
def test_agenda_http_roundtrip_y_aislamiento(conciliacion_db, monkeypatch):
    monkeypatch.setattr(dd,'get_conn',conciliacion_db)
    monkeypatch.setattr(pc,'_operadores_cliente',lambda _:[])
    monkeypatch.setattr(po.oca,'adapter_cliente',lambda _: (type('C',(),{'insured_operation':True})(),None))
    monkeypatch.setattr(pc,'get_productos',lambda _:[])
    monkeypatch.setattr(pc,'tax_paga_cliente',lambda _:'DESTINATARIO')
    monkeypatch.setattr(pc,'courier_default_cliente',lambda _:'DHL')
    from core import database
    monkeypatch.setattr(database,'get_conn',conciliacion_db)
    with conciliacion_db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO clientes(cliente_id,nombre,email) VALUES ('AGENDA-A','Demo A','a@example.invalid'),('AGENDA-B','Demo B','b@example.invalid')")
    app=FastAPI();app.include_router(pc.router);app.include_router(po.router)
    session={'cliente':'AGENDA-A'}
    app.dependency_overrides[pc.cliente_actual]=lambda:session['cliente']
    @app.middleware('http')
    async def nonce(request,call_next):
        request.state.csp_nonce='test'
        return await call_next(request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
            fields=dict(nombre='Ana',apellido='Pérez',calle='Mitre',numero='123',piso='2',depto='A',pais='AR',estado='Buenos Aires',cp='1875',ciudad='Wilde',email='ana@example.invalid',tipo='DESTINATARIO',cliente_id='AGENDA-B')
            saved=await client.post('/portal/clientes',data=fields)
            assert saved.headers['location']=='/portal/clientes?ok=1'
            agenda=await client.get('/portal/agenda')
            assert 'no-store' in agenda.headers['cache-control']
            a=agenda.json();ident=a['contactos'][0]['id']
            assert a['contactos'][0]['nombre']=='Ana Pérez'
            assert 'cliente_id' not in a['contactos'][0]
            assert a['nacionales'][0]['fields']['numero']=='123'
            row=dd.obtener_direccion('AGENDA-A',ident)
            assert row['direccion']=='Mitre 123 · Piso 2 · Depto. A'
            assert dd.obtener_direccion('AGENDA-B',ident) is None
            form=await client.get('/portal/oca/nuevo',params={'destinatario_id':ident})
            assert form.status_code==200 and 'value="Pérez"' in form.text and 'value="123"' in form.text
            scope=await client.get('/portal/envios/nuevo',params={'destinatario_id':ident,'ambito':'nacional'})
            assert scope.headers['location']==f'/portal/oca/nuevo?destinatario_id={ident}'
            international=await client.get('/portal/envios/nuevo',params={'destinatario_id':ident,'ambito':'internacional'})
            assert international.status_code==200 and row['direccion'] in international.text
            # Both roles use the same account-scoped CRUD and return to their selectors.
            saved=await client.post('/portal/clientes',data={**fields,'direccion_id':ident,'tipo':'REMITENTE','numero':'456'})
            assert saved.headers['location']=='/portal/clientes?ok=1'
            sender=await client.get('/portal/envios/nuevo',params={'remitente_id':ident,'ambito':'internacional'})
            assert sender.status_code==200 and 'Mitre 456' in sender.text
            badrole=await client.post('/portal/clientes',data={**fields,'tipo':'ADMIN'})
            assert '?error=' in badrole.headers['location']
            session['cliente']='AGENDA-B'
            assert (await client.get('/portal/agenda')).json()=={'scope':'portal:AGENDA-B','contactos':[],'nacionales':[]}
            for action in ('/portal/clientes',f'/portal/clientes/{ident}/eliminar'):
                result=await client.post(action,data={**fields,'direccion_id':ident})
                assert '?error=' in result.headers['location']
            for path,param in [('/portal/oca/nuevo','remitente_id'),('/portal/envios/nuevo','remitente_id')]:
                result=await client.get(path,params={param:ident,'ambito':'internacional'})
                assert 'Mitre 456' not in result.text
                if path.endswith('/envios/nuevo'):
                    assert 'id="remitente_id"' in result.text and 'id="destinatario_id"' in result.text
            assert dd.obtener_direccion('AGENDA-A',ident)['datos_nacionales']['numero']=='456'
            # Legacy editor preserves structured info for contact-only changes,
            # and invalidates it if an unstructured address changes.
            own=dd.obtener_direccion('AGENDA-A',ident)
            update={k:own.get(k) or '' for k in ('cliente_id','tipo','nombre','direccion','ciudad','estado','cp','pais')}
            dd.actualizar_direccion(ident,**update,email='changed@example.invalid')
            assert dd.obtener_direccion('AGENDA-A',ident)['datos_nacionales']['numero']=='456'
            dd.actualizar_direccion(ident,**{**update,'direccion':'Otra calle 77'})
            assert dd.obtener_direccion('AGENDA-A',ident)['datos_nacionales']=={}
    asyncio.run(run())
