import asyncio
from types import SimpleNamespace
import pytest
from endpoints import admin,admin_operadores as ui
from servicios import operadores_logisticos as op
from test_admin_entrada_dhl import auth,request
from test_operadores_logisticos import documento


@pytest.mark.parametrize('fn,args',[(ui.inicio,dict(request=None)),(ui.mundo,dict(request=None,courier='DHL')),
    (ui.factura,dict(request=None,courier='DHL',factura_id=1)),(ui.cliente,dict(request=None,cliente_id='A')),
    (ui.pdf,dict(courier='DHL',pago_id=1))])
def test_auth_antes_de_io(fn,args):
    assert fn(**args,admin_token='invalido').status_code==303


def test_post_auth_csrf_y_confirmacion(monkeypatch):
    assert asyncio.run(ui.accion(None,'DHL','pago',admin_token='invalido')).status_code==303
    async def form(): return dict(factura_id='1',csrf=admin._csrf_dhl('operador:FEDEX:verificar:1'))
    assert asyncio.run(ui.accion(SimpleNamespace(form=form),'DHL','verificar',admin_token='valido')).status_code==403
    async def form2(): return dict(factura_id='1',csrf=admin._csrf_dhl('operador:DHL:verificar:1'))
    r=asyncio.run(ui.accion(SimpleNamespace(form=form2),'DHL','verificar',admin_token='valido'))
    assert '?error=' in r.headers['location']


def test_pdf_privado_y_aislado(monkeypatch):
    monkeypatch.setattr(op,'comprobante_pago',lambda c,i: b'%PDF-test' if c=='DHL' and i==1 else None)
    r=ui.pdf('DHL',1,admin_token='valido')
    assert r.headers['cache-control']=='private, no-store' and 'attachment' in r.headers['content-disposition']
    assert r.headers['content-security-policy']=='sandbox'
    assert ui.pdf('FEDEX',1,admin_token='valido').status_code==404


def test_vistas_renderizan_sin_cache_y_escapan_texto(monkeypatch):
    f=op.estado_documento(documento(id=1,courier='DHL',numero='<script>inseguro</script>',lineas=2,clientes=2))
    f.update(items=[],aplicaciones=[],por_cliente=[],verificaciones=[])
    monkeypatch.setattr(op,'listar_documentos',lambda *args:[f])
    monkeypatch.setattr(op,'listar_pagos',lambda *args:[])
    monkeypatch.setattr(op,'condicion_actual',lambda *args:None)
    monkeypatch.setattr(op,'detalle_documento',lambda *args:f)
    monkeypatch.setattr(op,'documentos_cliente',lambda *args:dict(cliente=dict(cliente_id='A',nombre='A'),items=[],pagina=1,total=0))
    for r in [ui.inicio(request(),admin_token='valido'),ui.mundo(request(),'DHL',admin_token='valido'),
              ui.factura(request(),'DHL',1,admin_token='valido'),ui.cliente(request(),'A',admin_token='valido')]:
        html=r.body.decode()
        assert r.status_code==200 and r.headers['cache-control']=='private, no-store'
        assert '<script>inseguro</script>' not in html
        assert '/admin/operadores' in html


def test_reversa_csrf_ata_registro_y_confirma_antes_de_io():
    async def form():
        return dict(factura_id='0',registro_id='2',csrf=admin._csrf_dhl('operador:DHL:revertir-pago:0:1'),confirmo='si')
    assert asyncio.run(ui.accion(SimpleNamespace(form=form),'DHL','revertir-pago',admin_token='valido')).status_code==403


def test_importe_pequeno_no_se_redondea_visualmente_a_cero():
    from decimal import Decimal
    macro=admin.templates.get_template('admin/operador_macros.html').module.dinero
    assert str(macro(Decimal('.0001')))=='0,0001'
    assert str(macro(Decimal('1000')))=='1.000,00'


def test_post_conversion_no_interpreta_tc_decimal_como_miles(monkeypatch):
    from decimal import Decimal
    capturado={}
    def aplicar(*args,**kwargs): capturado.update(kwargs)
    monkeypatch.setattr(op,'aplicar',aplicar)
    async def form():
        return dict(factura_id='1',csrf=admin._csrf_dhl('operador:DHL:aplicar:1'),confirmo='si',
            importe='100',tipo_cambio='1,23400000',motivo='Verificado',pago_id='1',clave='test',conversion_confirmada='si')
    r=asyncio.run(ui.accion(SimpleNamespace(form=form),'DHL','aplicar',admin_token='valido'))
    assert '?ok=' in r.headers['location'] and capturado['tipo_cambio']==Decimal('1.234')
    assert capturado['conversion_confirmada'] is True


def test_mensaje_error_html_escapado():
    r=request()
    r.scope['query_string']=b'error=%3Cscript%3Ealert%281%29%3C%2Fscript%3E'
    html=admin.templates.get_template('admin/operador_mensajes.html').render(request=r)
    assert '<script>' not in html and '&lt;script&gt;' in html
