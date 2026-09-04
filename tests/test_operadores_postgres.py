from concurrent.futures import ThreadPoolExecutor
from datetime import date,timedelta
from uuid import uuid4
import psycopg2
import pytest
from servicios import operadores_logisticos as op
from servicios import conciliacion_couriers as cc
from test_conciliacion_couriers_postgres import conciliacion_db, DATABASE_URL, _crear_solicitud, _confirmar_todos

pytestmark=pytest.mark.skipif(not DATABASE_URL,reason='requiere PostgreSQL aislado')


@pytest.fixture
def db(conciliacion_db,monkeypatch):
    monkeypatch.setattr(op,'get_conn',conciliacion_db)
    return conciliacion_db


def fc(numero=None,**kwargs):
    datos=dict(courier='DHL',tipo_documento='FC',numero=numero or str(uuid4()),moneda='USD',
        total='100',fecha_emision='2026-08-01',fecha_vencimiento='2026-08-31',actor='test',items=[])
    datos.update(kwargs)
    return cc.registrar_factura_courier(**datos)['id']


def pago(**kwargs):
    datos=dict(courier='DHL',fecha='2026-09-01',moneda='USD',importe='100',referencia=str(uuid4()),
        pdf=b'%PDF-'+uuid4().bytes,clave=str(uuid4()))
    datos.update(kwargs)
    return op.registrar_pago(**datos)


def aplicar(fid,**kwargs):
    datos=dict(importe='100',tipo_cambio='1',motivo='Comprobante verificado',clave=str(uuid4()))
    datos.update(kwargs)
    return op.aplicar('DHL',fid,**datos)


def detalle(fid): return op.detalle_documento('DHL',fid)


def test_historica_sin_verificar_parcial_y_cancelada(db):
    fid=fc()
    assert detalle(fid)['estado_pago']=='SIN_VERIFICAR'
    op.verificar_historial('DHL',fid,'Estado de cuenta al corte verificado')
    assert detalle(fid)['estado_pago']=='IMPAGA'
    pid=pago(importe='120')
    aplicar(fid,pago_id=pid,importe='40')
    assert detalle(fid)['estado_pago']=='PARCIAL' and detalle(fid)['saldo']==60
    aplicar(fid,pago_id=pid,importe='60')
    d=detalle(fid)
    assert d['estado_pago']=='CANCELADA' and d['saldo']==0 and d['fecha_cancelacion']==date(2026,9,1)
    assert op.listar_pagos('DHL')[0]['disponible']==20
    with db() as conn,conn.cursor() as cur:
        for tabla in ['pagos','envios','ajustes_cliente']:
            cur.execute('SELECT count(*) FROM '+tabla)
            assert cur.fetchone()['count']==0


def test_pago_usd_ars_tc_explicito_y_varias_facturas(db):
    a,b=fc(),fc()
    pid=pago(moneda='ARS',importe='150000')
    aplicar(a,pago_id=pid,importe='100',tipo_cambio='1000',conversion_confirmada=True)
    aplicar(b,pago_id=pid,importe='50',tipo_cambio='1000',conversion_confirmada=True)
    assert detalle(a)['saldo']==0 and detalle(b)['saldo']==50
    assert op.listar_pagos('DHL')[0]['disponible']==0
    with pytest.raises(op.OperadorError,match='origen insuficiente'):
        aplicar(b,pago_id=pid,importe='1',tipo_cambio='1000',conversion_confirmada=True)


def test_idempotencia_pago_aplicacion_y_datos_distintos(db):
    fid=fc()
    key=str(uuid4())
    datos=dict(clave=key,referencia='TRANSFER-UNICA',pdf=b'%PDF-unico')
    pid=pago(**datos)
    assert pago(**datos)==pid
    with pytest.raises(op.OperadorError,match='datos diferentes'): pago(**datos,importe='1')
    with pytest.raises(op.OperadorError,match='duplicada'): pago(referencia=' transfer-unica ')
    with pytest.raises(op.OperadorError,match='duplicada'): pago(pdf=b'%PDF-unico')
    key=str(uuid4())
    ident=aplicar(fid,pago_id=pid,clave=key,importe='50')
    assert aplicar(fid,pago_id=pid,clave=key,importe='50')==ident
    with pytest.raises(op.OperadorError,match='datos diferentes'):
        aplicar(fid,pago_id=pid,clave=key,importe='40')
    assert detalle(fid)['aplicado']==50


def test_no_sobreaplicar_ni_cruzar_operadores_ni_moneda_tc(db):
    fid=fc()
    pid=pago()
    with pytest.raises(op.OperadorError,match='saldo de la factura'): aplicar(fid,pago_id=pid,importe='101')
    with pytest.raises(op.OperadorError,match='Misma moneda'): aplicar(fid,pago_id=pid,importe='10',tipo_cambio='2')
    otro=pago(courier='FEDEX')
    with pytest.raises(op.OperadorError,match='Operador de pago'): aplicar(fid,pago_id=otro)
    assert detalle(fid)['saldo']==100 and not detalle(fid)['aplicaciones']
    with pytest.raises(op.OperadorError,match='pago o una NC'): aplicar(fid,pago_id=pid,nc_id=fid)


def test_nc_evidencia_referencia_saldo_y_no_pagable(db):
    a,b=fc(),fc()
    n=fc(tipo_documento='NC',total='40',archivo_contenido=b'%PDF-nc',factura_referenciada_id=a)
    assert detalle(n)['estado_pago']=='CREDITO_SIN_VERIFICAR'
    with pytest.raises(op.OperadorError,match='historial'): aplicar(a,nc_id=n,importe='20')
    op.verificar_historial('DHL',n,'Verificado crédito sin aplicaciones anteriores')
    assert detalle(n)['estado_pago']=='CREDITO_DISPONIBLE'
    with pytest.raises(op.OperadorError,match='NC incompatible'): aplicar(b,nc_id=n,importe='20')
    with pytest.raises(op.OperadorError,match='no pagable'): aplicar(n,pago_id=pago(),importe='10')
    aplicar(a,nc_id=n,importe='30')
    assert detalle(a)['saldo']==70 and detalle(n)['saldo']==10
    with pytest.raises(op.OperadorError,match='origen insuficiente'): aplicar(a,nc_id=n,importe='20')
    sin_pdf=fc(tipo_documento='NC')
    op.verificar_historial('DHL',sin_pdf,'Revisión de aplicaciones previas')
    with pytest.raises(op.OperadorError,match='sin PDF'): aplicar(a,nc_id=sin_pdf,importe='1')


def test_vencimiento_snapshot_nuevo_historico_y_precedencia(db):
    antigua=fc(fecha_vencimiento=None)
    assert detalle(antigua)['vencimiento'] is None
    c=op.configurar_plazo('DHL','15','Convenio de prueba documentado')
    nueva=fc(fecha_vencimiento=None,fecha_emision=op.hoy_ar().isoformat())
    assert detalle(nueva)['vencimiento']==op.hoy_ar()+timedelta(days=15)
    assert detalle(antigua)['vencimiento'] is None
    op.fijar_plazo_historico('DHL',antigua,c)
    op.configurar_plazo('DHL','30','Nuevo convenio documentado')
    assert detalle(nueva)['vencimiento']==op.hoy_ar()+timedelta(days=15)
    assert detalle(antigua)['vencimiento']==date(2026,8,16)
    explicita=fc(fecha_vencimiento='2026-08-20')
    assert detalle(explicita)['vencimiento']==date(2026,8,20)
    op.configurar_plazo('DHL','','No hay plazo conocido')
    assert detalle(fc(fecha_vencimiento=None))['vencimiento'] is None
    assert detalle(fc(fecha_emision=None,fecha_vencimiento=None))['dias_emision'] is None


def test_historia_inmutable_y_documentos_protegidos(db):
    fid=fc()
    pid=pago()
    aplicar(fid,pago_id=pid)
    for sql in ['UPDATE pagos_operador SET importe=1','DELETE FROM aplicaciones_operador',
                f"UPDATE facturas_courier SET total=999 WHERE id={fid}"]:
        with pytest.raises(psycopg2.errors.RaiseException):
            with db() as conn,conn.cursor() as cur: cur.execute(sql)
    assert detalle(fid)['saldo']==0


def test_concurrencia_solo_una_aplicacion_cabe(db):
    fid,pid=fc(),pago()
    def intentar(_):
        try: return aplicar(fid,pago_id=pid,importe='80')
        except op.OperadorError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados=list(pool.map(intentar,range(2)))
    assert sum(r is not None for r in resultados)==1
    assert detalle(fid)['saldo']==20 and op.listar_pagos('DHL')[0]['disponible']==20


def test_factura_dos_clientes_no_duplica_deuda_y_aisla_lineas(db):
    a=_crear_solicitud(db,sufijo='ALFA',tracking='DHLALFA')
    b=_crear_solicitud(db,sufijo='BETA',tracking='DHLBETA')
    fid=fc(moneda='ARS',total='100',items=[
        dict(linea_numero=1,tracking='DHLALFA',importe='60',peso_facturado_kg='3'),
        dict(linea_numero=2,tracking='DHLBETA',importe='30',peso_facturado_kg='4'),
        dict(linea_numero=3,importe='10',concepto_tipo='IMPUESTO')])
    cc.matchear_items_exactos(fid)
    _confirmar_todos(db,a)
    _confirmar_todos(db,b)
    op.verificar_historial('DHL',fid,'Sin pagos previos verificado')
    d=detalle(fid)
    assert d['clientes']==2 and d['total']==100 and len(d['items'])==3
    assert sum(c['asignado'] for c in d['por_cliente'])==90
    assert op.resumen_documentos(op.listar_documentos('DHL'))[0]['pendiente']==100
    solo_a=op.documentos_cliente('CLIENTE_ALFA')
    assert solo_a['total']==1 and solo_a['items'][0]['importe_asignado']==60
    assert all(i['cliente_id']=='CLIENTE_ALFA' for i in solo_a['items'])
    assert op.documentos_cliente('NO_EXISTE') is None
    assert op.detalle_documento('FEDEX',fid) is None


def test_validaciones_pago_y_rollback_auditoria(db,monkeypatch):
    with pytest.raises(op.OperadorError,match='futuro'): pago(fecha='2999-01-01')
    with pytest.raises(op.OperadorError,match='PDF'): pago(pdf=b'no es pdf')
    def falla(*args,**kwargs): raise RuntimeError('Falla auditoría')
    monkeypatch.setattr(op,'_auditar',falla)
    with pytest.raises(RuntimeError): pago()
    assert op.listar_pagos('DHL')==[]


@pytest.mark.parametrize('source',['pago_id','nc_id'])
def test_origen_inexistente_es_error_controlado(db,source):
    with pytest.raises(op.OperadorError,match='no existe'):
        aplicar(fc(),**{source:999999})


def test_conversion_incluso_paridad_exige_confirmacion_separada(db):
    fid,pid=fc(),pago(moneda='ARS',importe='1000')
    with pytest.raises(op.OperadorError,match='conversión'):
        aplicar(fid,pago_id=pid,tipo_cambio='1')
    assert detalle(fid)['saldo']==100
    aplicar(fid,pago_id=pid,tipo_cambio='1',conversion_confirmada=True)
    assert detalle(fid)['saldo']==0


def test_reversa_conserva_historia_y_permite_corregir_mismo_comprobante(db):
    fid=fc()
    pid=pago(referencia='REFERENCIA-REAL',pdf=b'%PDF-real')
    aid=aplicar(fid,pago_id=pid)
    with pytest.raises(op.OperadorError,match='primero las aplicaciones'):
        op.revertir('DHL','pago',pid,'Importe mal tipeado al transcribir')
    r=op.revertir('DHL','aplicacion',aid,'Factura elegida erróneamente')
    assert op.revertir('DHL','aplicacion',aid,'Reintento de la misma reversión')==r
    d=detalle(fid)
    assert d['saldo']==100 and d['fecha_cancelacion'] is None and len(d['aplicaciones'])==1
    assert d['aplicaciones'][0]['fecha_reversion'] is not None
    assert op.listar_pagos('DHL')[0]['disponible']==100
    op.revertir('DHL','pago',pid,'Importe mal tipeado al transcribir')
    assert op.listar_pagos('DHL')[0]['revertido'] and op.listar_pagos('DHL')[0]['disponible']==0
    with pytest.raises(op.OperadorError,match='revertido'): aplicar(fid,pago_id=pid)
    corregido=pago(referencia='REFERENCIA-REAL',pdf=b'%PDF-real',importe='200')
    assert corregido!=pid
    aplicar(fid,pago_id=corregido)
    assert detalle(fid)['saldo']==0 and len(detalle(fid)['aplicaciones'])==2
    with db() as conn,conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM pagos_operador')
        assert cur.fetchone()['count']==2
    with pytest.raises(psycopg2.errors.RaiseException):
        with db() as conn,conn.cursor() as cur: cur.execute('DELETE FROM reversiones_pago_operador')


def test_reversa_nc_restaura_credito_y_no_cruza_operador(db):
    f=fc()
    n=fc(tipo_documento='NC',archivo_contenido=b'%PDF-credito')
    op.verificar_historial('DHL',n,'Crédito original no utilizado')
    aid=aplicar(f,nc_id=n)
    assert detalle(n)['estado_pago']=='CREDITO_AGOTADO'
    with pytest.raises(op.OperadorError,match='otro operador'):
        op.revertir('FEDEX','aplicacion',aid,'No corresponde a este operador')
    op.revertir('DHL','aplicacion',aid,'Imputación a factura equivocada')
    assert detalle(n)['saldo']==100 and detalle(n)['estado_pago']=='CREDITO_DISPONIBLE'
    assert detalle(f)['saldo']==100


def test_saldo_pago_compartido_concurrencia_entre_facturas(db):
    ids=[fc(),fc()]
    pid=pago()
    def intentar(fid):
        try: return aplicar(fid,pago_id=pid,importe='80')
        except op.OperadorError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados=list(pool.map(intentar,ids))
    assert sum(r is not None for r in resultados)==1
    assert op.listar_pagos('DHL')[0]['disponible']==20
    assert sum(detalle(fid)['saldo'] for fid in ids)==120


def test_match_parcial_muestra_remanente_y_nc_signo_credito(db):
    s=_crear_solicitud(db,sufijo='PARCIAL',tracking='DHL-PARCIAL')
    for tipo in ['FC','NC']:
        fid=fc(tipo_documento=tipo,moneda='ARS',items=[dict(linea_numero=1,tracking='DHL-PARCIAL',importe='100')])
        with db() as conn,conn.cursor() as cur:
            cur.execute('''INSERT INTO factura_courier_item_matches(item_id,solicitud_id,monto_asignado,
                monto_asignado_ars,metodo,estado,creado_por,confirmado_por,confirmado_at,evidencia_uri)
                SELECT id,%s,40,40,'MANUAL','CONFIRMADO','qa','qa',now(),'qa:asignacion-verificada'
                FROM facturas_courier_items WHERE factura_id=%s''',(s,fid))
        d=detalle(fid)
        assert d['pendientes_asignacion'][0]['pendiente']==60
        assert d['sin_desglosar']==0
        assert d['por_cliente'][0]['asignado']==(40 if tipo=='FC' else -40)
        assert d['pendientes_asignacion'][0]['signo']==(1 if tipo=='FC' else -1)


def test_fijar_plazo_historico_exige_condicion_vista(db):
    f=fc(fecha_vencimiento=None)
    c=op.configurar_plazo('DHL','10','Convenio anterior verificado')
    op.configurar_plazo('DHL','15','Nuevo convenio verificado')
    with pytest.raises(op.OperadorError,match='cambió'): op.fijar_plazo_historico('DHL',f,c)
    assert detalle(f)['vencimiento'] is None


def test_replay_revertido_no_devuelve_exito(db):
    k1,k2=str(uuid4()),str(uuid4())
    datos=dict(clave=k1,referencia='REPLAY-REVERSA',pdf=b'%PDF-replay')
    pid=pago(**datos)
    fid=fc()
    aid=aplicar(fid,pago_id=pid,clave=k2)
    op.revertir('DHL','aplicacion',aid,'Aplicación equivocada revisada')
    with pytest.raises(op.OperadorError,match='revertida'): aplicar(fid,pago_id=pid,clave=k2)
    op.revertir('DHL','pago',pid,'Pago equivocado revisado')
    with pytest.raises(op.OperadorError,match='revertido'): pago(**datos)


def test_plazos_verificaciones_idempotentes(db):
    key=str(uuid4())
    c=op.configurar_plazo('DHL','15','Acuerdo verificado',clave=key)
    assert op.configurar_plazo('DHL','15','Acuerdo verificado',clave=key)==c
    fid=fc(fecha_vencimiento=None)
    assert detalle(fid)['vencimiento'] is None  # FC emitida antes del acuerdo, aunque se ingrese hoy.
    op.fijar_plazo_historico('DHL',fid,c)
    vkey=str(uuid4())
    v=op.verificar_historial('DHL',fid,'Historial completo',clave=vkey)
    assert op.verificar_historial('DHL',fid,'Historial completo',clave=vkey)==v
    assert len(detalle(fid)['verificaciones'])==1


def test_rectificar_vencimiento_conserva_historia_y_no_cambia_documento(db):
    c=op.configurar_plazo('DHL','15','Acuerdo ingresado por error')
    fid=fc(fecha_vencimiento=None)
    op.fijar_plazo_historico('DHL',fid,c)
    key=str(uuid4())
    r=op.rectificar_vencimiento('DHL',fid,'2026-08-31','Se verificó que el plazo era 30 días',key)
    assert op.rectificar_vencimiento('DHL',fid,'2026-08-31','Se verificó que el plazo era 30 días',key)==r
    assert detalle(fid)['vencimiento']==date(2026,8,31) and detalle(fid)['fecha_vencimiento'] is None
    op.rectificar_vencimiento('DHL',fid,None,'No existe un acuerdo confirmado',str(uuid4()))
    d=detalle(fid)
    assert d['vencimiento'] is None and len(d['rectificaciones'])==2
    with pytest.raises(op.OperadorError,match='rectificaciones'):
        op.fijar_plazo_historico('DHL',fid,c)
    with pytest.raises(op.OperadorError,match='reemplazada'):
        op.rectificar_vencimiento('DHL',fid,'2026-08-31','Se verificó que el plazo era 30 días',key)
    with db() as conn,conn.cursor() as cur:
        cur.execute('SELECT fecha FROM vencimientos_operador WHERE factura_id=%s',(fid,))
        assert cur.fetchone()['fecha']==date(2026,8,16)
    with pytest.raises(op.OperadorError,match='original'):
        op.rectificar_vencimiento('DHL',fc(),'2026-10-01','No alterar documento original',str(uuid4()))


def test_plazo_no_bloquea_anular_documento_sin_aplicaciones_vigentes(db):
    op.configurar_plazo('DHL','15','Acuerdo verificado')
    fid=fc(fecha_vencimiento=None,fecha_emision=op.hoy_ar().isoformat())
    assert detalle(fid)['vencimiento'] is not None
    with db() as conn,conn.cursor() as cur:
        cur.execute("UPDATE facturas_courier SET estado='ANULADA' WHERE id=%s",(fid,))
    assert detalle(fid)['estado_pago']=='ANULADA'
    otro=fc()
    aid=aplicar(otro,pago_id=pago())
    with pytest.raises(psycopg2.errors.RaiseException):
        with db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE facturas_courier SET estado='ANULADA' WHERE id=%s",(otro,))
    op.revertir('DHL','aplicacion',aid,'Revertir imputación antes de anular')
    with db() as conn,conn.cursor() as cur:
        cur.execute("UPDATE facturas_courier SET estado='ANULADA' WHERE id=%s",(otro,))
    assert detalle(otro)['estado_pago']=='ANULADA'


def test_historial_permite_completar_pdf_faltante_pero_no_reemplazarlo(db):
    datos=dict(courier='DHL',tipo_documento='NC',numero='NC-PDF-PENDIENTE',moneda='USD',
        total='100',items=[],fecha_emision='2026-08-01',actor='qa')
    fid=cc.registrar_factura_courier(**datos)['id']
    op.verificar_historial('DHL',fid,'Saldo confirmado, PDF pendiente de adjuntar')
    r=cc.registrar_factura_courier(**datos,archivo_contenido=b'%PDF-evidencia-original')
    assert r['id']==fid and r['evidencia_actualizada']
    with pytest.raises(psycopg2.errors.RaiseException):
        with db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE facturas_courier SET archivo_pdf=%s WHERE id=%s",(b'%PDF-otro',fid))


def test_snapshot_protege_emision_y_rectificacion_valida_fecha(db):
    op.configurar_plazo('DHL','15','Acuerdo verificado')
    fid=fc(fecha_vencimiento=None,fecha_emision=op.hoy_ar().isoformat())
    with pytest.raises(psycopg2.errors.RaiseException,match='emisión'):
        with db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE facturas_courier SET fecha_emision=fecha_emision-1 WHERE id=%s",(fid,))
    with pytest.raises(op.OperadorError,match='incompatible'):
        op.rectificar_vencimiento('DHL',fid,(op.hoy_ar()-timedelta(days=1)).isoformat(),'Fecha anterior no válida',str(uuid4()))


def test_sql_rechaza_aislamiento_que_puede_ver_saldos_obsoletos(db):
    fid=fc()
    with pytest.raises(psycopg2.errors.RaiseException,match='READ COMMITTED'):
        with db() as conn,conn.cursor() as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cur.execute("UPDATE facturas_courier SET estado='ANULADA' WHERE id=%s",(fid,))
