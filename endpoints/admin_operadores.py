"""Vistas internas de operadores. Autenticación antes de cualquier lectura."""
from uuid import uuid4
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from servicios import operadores_logisticos as servicio
from servicios.numeros_humanos import parse_numero_humano

router = APIRouter()


def _admin():
    from endpoints import admin
    return admin


def _render(request, template, **context):
    admin = _admin()
    context.update(seccion='operadores', nuevo_id=lambda: str(uuid4()),
        csrf=lambda accion, courier, fid=0, rid=0: admin._csrf_dhl(
            f'operador:{courier}:{accion}:{fid}'+(f':{rid}' if accion.startswith('revertir-') else '')))
    return admin.templates.TemplateResponse(request=request, name='admin/'+template,
        context=context, headers={"Cache-Control": "private, no-store"})


@router.get('/operadores')
def inicio(request: Request, admin_token: str | None = Cookie(None)):
    if not _admin()._is_auth(admin_token):
        return _admin()._redirect_login()
    documentos = servicio.listar_documentos()
    mundos = [dict(courier=c, cantidad=sum(f['courier']==c for f in documentos),
              resumen=servicio.resumen_documentos([f for f in documentos if f['courier']==c]))
              for c in ('DHL','FEDEX','ANDREANI','OCA')]
    return _render(request, 'operadores.html', mundos=mundos)


@router.get('/operadores/{courier}')
def mundo(request: Request, courier: str, pagina: int=1, admin_token: str | None=Cookie(None)):
    if not _admin()._is_auth(admin_token):
        return _admin()._redirect_login()
    try:
        courier=servicio.operador(courier)
    except ValueError:
        return Response('Operador inexistente',status_code=404)
    documentos=servicio.listar_documentos(courier)
    estado=request.query_params.get('estado','')
    filtrados=[f for f in documentos if not estado or f['estado_pago']==estado]
    pagina=max(1,pagina)
    return _render(request,'operador.html',courier=courier,
        documentos=filtrados[(pagina-1)*50:pagina*50],total=len(filtrados),pagina=pagina,estado_filtro=estado,
        resumen=servicio.resumen_documentos(documentos),condicion=servicio.condicion_actual(courier),
        pagos=servicio.listar_pagos(courier))


@router.get('/operadores/{courier}/facturas/{factura_id}')
def factura(request: Request,courier: str,factura_id: int,admin_token: str | None=Cookie(None)):
    if not _admin()._is_auth(admin_token):
        return _admin()._redirect_login()
    try:
        courier=servicio.operador(courier)
        f=servicio.detalle_documento(courier,factura_id)
    except ValueError:
        f=None
    if not f:
        return Response('Documento inexistente',status_code=404)
    return _render(request,'operador_factura.html',factura=f,courier=courier,
        condicion=servicio.condicion_actual(courier),pagos=servicio.listar_pagos(courier),
        creditos=[n for n in servicio.listar_documentos(courier) if n['estado_pago']=='CREDITO_DISPONIBLE'
                  and n['estado']!='ANULADA' and n['saldo']>0 and n['moneda']==f['moneda']
                  and n['factura_referenciada_id'] in (None,factura_id)])


@router.get('/clientes/{cliente_id}/operadores')
def cliente(request: Request,cliente_id: str,pagina: int=1,admin_token: str | None=Cookie(None)):
    if not _admin()._is_auth(admin_token):
        return _admin()._redirect_login()
    datos=servicio.documentos_cliente(cliente_id,pagina)
    if not datos:
        return Response('Cliente inexistente',status_code=404)
    return _render(request,'operador_cliente.html',**datos)


@router.get('/operadores/{courier}/pagos/{pago_id}/pdf')
def pdf(courier: str,pago_id: int,admin_token: str | None=Cookie(None)):
    if not _admin()._is_auth(admin_token):
        return _admin()._redirect_login()
    try:
        contenido=servicio.comprobante_pago(courier,pago_id)
    except ValueError:
        contenido=None
    if not contenido:
        return Response('Comprobante inexistente',status_code=404)
    return Response(contenido,media_type='application/pdf',headers={
        "Content-Disposition": f'attachment; filename="pago-operador-{pago_id}.pdf"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})


@router.post('/operadores/{courier}/acciones/{accion}')
async def accion(request: Request,courier: str,accion: str,admin_token: str | None=Cookie(None)):
    admin=_admin()
    if not admin._is_auth(admin_token):
        return admin._redirect_login()
    try:
        courier=servicio.operador(courier)
    except ValueError:
        return Response('Operador inexistente',status_code=404)
    if accion not in {'plazo','verificar','vencimiento','pago','aplicar','revertir-pago','revertir-aplicacion','rectificar-vencimiento'}:
        return Response('Acción inexistente',status_code=404)
    form=await request.form()
    try:
        fid=int(form.get('factura_id') or 0)
        rid=int(form.get('registro_id') or 0)
    except ValueError:
        return Response('Documento inválido',status_code=400)
    scope=f'operador:{courier}:{accion}:{fid}'+(f':{rid}' if accion.startswith('revertir-') else '')
    if not admin._csrf_dhl_valido(form.get('csrf'),scope):
        return Response('Formulario vencido o inválido. Recargá la página.',status_code=403)
    destino=f'/admin/operadores/{courier}' + (f'/facturas/{fid}' if fid else '')
    try:
        if form.get('confirmo')!='si':
            raise ValueError('Confirmá expresamente la operación y su respaldo.')
        if accion=='plazo':
            await run_in_threadpool(servicio.configurar_plazo,courier,form.get('plazo_dias'),form.get('motivo'),clave=form.get('clave',''))
        elif accion=='verificar':
            await run_in_threadpool(servicio.verificar_historial,courier,fid,form.get('motivo'),clave=form.get('clave',''))
        elif accion=='vencimiento':
            await run_in_threadpool(servicio.fijar_plazo_historico,courier,fid,int(form.get('condicion_id') or 0))
        elif accion=='rectificar-vencimiento':
            await run_in_threadpool(servicio.rectificar_vencimiento,courier,fid,form.get('fecha'),
                form.get('motivo'),form.get('clave'))
        elif accion=='pago':
            from servicios.cuenta_corriente import leer_comprobante_con_tope
            contenido=await leer_comprobante_con_tope(form.get('comprobante_pdf'))
            await run_in_threadpool(servicio.registrar_pago,courier,fecha=form.get('fecha'),moneda=form.get('moneda'),
                importe=admin.parse_importe_humano(form.get('importe')),referencia=form.get('referencia'),
                pdf=contenido,clave=form.get('clave'))
        elif accion.startswith('revertir-'):
            await run_in_threadpool(servicio.revertir,courier,accion.removeprefix('revertir-'),
                rid,form.get('motivo'))
        else:
            await run_in_threadpool(servicio.aplicar,courier,fid,
                importe=admin.parse_importe_humano(form.get('importe')),
                tipo_cambio=parse_numero_humano(form.get('tipo_cambio')),
                motivo=form.get('motivo'),clave=form.get('clave'),
                pago_id=int(form['pago_id']) if form.get('pago_id') else None,
                nc_id=int(form['nc_id']) if form.get('nc_id') else None,
                conversion_confirmada=form.get('conversion_confirmada')=='si')
    except ValueError as exc:
        return RedirectResponse(destino+'?error='+quote(str(exc)),status_code=303)
    return RedirectResponse(destino+'?ok=registrado',status_code=303)
