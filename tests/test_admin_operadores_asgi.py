from uuid import uuid4
import asyncio
from types import SimpleNamespace
from urllib.parse import urlencode
import pytest
from fastapi import FastAPI
from endpoints import admin
from servicios import operadores_logisticos as op
from test_admin_entrada_dhl import auth
from test_operadores_postgres import db,conciliacion_db,fc,DATABASE_URL

pytestmark=pytest.mark.skipif(not DATABASE_URL,reason='requiere PostgreSQL aislado')


class Cookies(dict):
    def set(self,k,v): self[k]=v


class ClienteASGI:
    """Mismo patrón ASGI del repo; prueba routing y multipart sin dependencias nuevas."""
    def __init__(self):
        self.app=FastAPI()
        self.app.include_router(admin.router)
        self.cookies=Cookies(admin_token='valido')

    def get(self,path,**kwargs): return self.request('GET',path)
    def post(self,path,**kwargs): return self.request('POST',path,**kwargs)

    def request(self,method,path,data=None,files=None,follow_redirects=False):
        headers=[(b'host',b'testserver')]
        if self.cookies:
            headers.append((b'cookie','; '.join(f'{k}={v}' for k,v in self.cookies.items()).encode()))
        if files:
            boundary='qa-'+uuid4().hex
            partes=[]
            for k,v in (data or {}).items():
                partes.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            for k,(name,content,mime) in files.items():
                partes.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()+content+b'\r\n')
            body=b''.join(partes)+f'--{boundary}--\r\n'.encode()
            headers.append((b'content-type',f'multipart/form-data; boundary={boundary}'.encode()))
        else:
            body=urlencode(data or {}).encode()
            headers.append((b'content-type',b'application/x-www-form-urlencoded'))
        headers.append((b'content-length',str(len(body)).encode()))
        messages=[]
        async def receive(): return dict(type='http.request',body=body,more_body=False)
        async def send(message): messages.append(message)
        scope=dict(type='http',asgi={'version':'3.0'},http_version='1.1',method=method,scheme='http',
            path=path,raw_path=path.encode(),query_string=b'',root_path='',headers=headers,
            client=('127.0.0.1',50000),server=('testserver',80),state={'csp_nonce':'qa'})
        asyncio.run(self.app(scope,receive,send))
        start=next(m for m in messages if m['type']=='http.response.start')
        return SimpleNamespace(status_code=start['status'],headers={k.decode().lower():v.decode() for k,v in start['headers']})


def cliente_http(): return ClienteASGI()


def test_asgi_routing_multipart_pago_aplicacion_y_privacidad(db):
    client=cliente_http()
    f=fc()
    datos=dict(csrf=admin._csrf_dhl('operador:DHL:pago:0'),factura_id='0',clave=str(uuid4()),
        confirmo='si',fecha='2026-09-01',moneda='USD',importe='100',referencia='ASGI-TRANSFER')
    r=client.post('/admin/operadores/DHL/acciones/pago',data=datos,
        files={'comprobante_pdf':('prueba.pdf',b'%PDF-asgi','application/pdf')},follow_redirects=False)
    assert r.status_code==303 and '?ok=' in r.headers['location']
    p=op.listar_pagos('DHL')[0]
    datos=dict(csrf=admin._csrf_dhl(f'operador:DHL:aplicar:{f}'),factura_id=str(f),clave=str(uuid4()),
        confirmo='si',importe='100',tipo_cambio='1',pago_id=str(p['id']),motivo='Comprobante confirmado en prueba')
    r=client.post('/admin/operadores/DHL/acciones/aplicar',data=datos,follow_redirects=False)
    assert r.status_code==303 and '?ok=' in r.headers['location']
    assert op.detalle_documento('DHL',f)['estado_pago']=='CANCELADA'
    for url in ['/admin/operadores','/admin/operadores/DHL',f'/admin/operadores/DHL/facturas/{f}',
                f"/admin/operadores/DHL/pagos/{p['id']}/pdf"]:
        r=client.get(url)
        assert r.status_code==200 and r.headers['cache-control']=='private, no-store'
    client.cookies.clear()
    assert client.get(f"/admin/operadores/DHL/pagos/{p['id']}/pdf",follow_redirects=False).status_code==303
    assert client.get('/portal/operadores').status_code==404


@pytest.mark.parametrize('importe',['NaN','1e10000','-1','0',''])
def test_asgi_pago_invalido_no_produce_500(db,importe):
    c=cliente_http()
    r=c.post('/admin/operadores/DHL/acciones/pago',data=dict(csrf=admin._csrf_dhl('operador:DHL:pago:0'),
        clave=str(uuid4()),confirmo='si',fecha='2026-09-01',moneda='USD',importe=importe,referencia='ASGI-INVALID'),
        files={'comprobante_pdf':('prueba.pdf',b'%PDF-invalido','application/pdf')},follow_redirects=False)
    assert r.status_code==303 and '?error=' in r.headers['location'] and op.listar_pagos('DHL')==[]


def test_asgi_tope_pdf_y_tc_ausente_no_producen_500(db):
    c=cliente_http()
    datos=dict(csrf=admin._csrf_dhl('operador:DHL:pago:0'),clave=str(uuid4()),confirmo='si',
        fecha='2026-09-01',moneda='USD',importe='100',referencia='ASGI-TOPE')
    r=c.post('/admin/operadores/DHL/acciones/pago',data=datos,
        files={'comprobante_pdf':('grande.pdf',b'%PDF-'+b'x'*(8*1024*1024),'application/pdf')},follow_redirects=False)
    assert r.status_code==303 and '?error=' in r.headers['location']
    f=fc()
    r=c.post('/admin/operadores/DHL/acciones/aplicar',data=dict(csrf=admin._csrf_dhl(f'operador:DHL:aplicar:{f}'),
        factura_id=str(f),clave=str(uuid4()),confirmo='si',importe='100',pago_id='999',motivo='TC ausente'),follow_redirects=False)
    assert r.status_code==303 and '?error=' in r.headers['location']
