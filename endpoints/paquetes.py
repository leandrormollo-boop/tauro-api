"""Configuración autenticada de embalajes y callbacks de cotización."""
from __future__ import annotations

import json
from functools import partial
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from psycopg2.errors import UniqueViolation

from endpoints.portal_cliente import cliente_actual, templates
from servicios import paquetes as pkg
from servicios import paquetes_cotizacion as quotes

router = APIRouter(tags=["paquetes"])


async def _body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 131072:
            raise HTTPException(413,"La solicitud es demasiado grande.")
    try:
        data = json.loads(body)
    except (ValueError,UnicodeDecodeError):
        raise HTTPException(422,"El formulario es inválido.") from None
    if not isinstance(data,dict):
        raise HTTPException(422,"El formulario es inválido.")
    return data


def _csrf(request):
    # La verificación vive también en el handler, incluso en apps de preview
    # o instalaciones que no monten el middleware general del portal.
    from urllib.parse import urlparse
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403,"Recargá el portal para guardar los cambios.")
    if origin and urlparse(origin).netloc != request.url.netloc:
        raise HTTPException(403,"El formulario debe enviarse desde el portal.")


async def _guardar(request, fn):
    _csrf(request)
    data = await _body(request)
    try:
        result = await run_in_threadpool(fn,data)
    except pkg.PaqueteError as exc:
        return JSONResponse({"ok":False,"error":str(exc)},status_code=422)
    except UniqueViolation:
        return JSONResponse({"ok":False,"error":"Ya tenés un embalaje activo con ese nombre."},status_code=409)
    except RuntimeError as exc:
        from servicios.paquetes_shopify import CheckoutNoDisponible
        if isinstance(exc,CheckoutNoDisponible):
            return JSONResponse({"ok":False,"error":str(exc)},status_code=409)
        return JSONResponse({"ok":False,"error":"El servicio no está disponible en este momento. Volvé a intentar o contactá a TAURO."},status_code=503)
    return JSONResponse(jsonable_encoder({"ok":True,**(result or {})}))


@router.get("/portal/paquetes")
def vista(request:Request,cliente:str=Depends(cliente_actual)):
    from servicios.direcciones import obtener_remitente_para_envio
    state = pkg.cargar_configuracion(cliente)
    state["productos"] = pkg.cargar_catalogo(cliente)
    state["tiendas"] = quotes.tiendas(cliente)
    origen = obtener_remitente_para_envio(cliente,None) or {}
    # El simulador no necesita exponer documentos ni contactos del remitente.
    state["origen"] = {k:origen.get(k,"") for k in ("pais","cp","ciudad","estado")}
    return templates.TemplateResponse(request=request,name="portal/paquetes.html",
        context={"cliente":cliente,"paquetes_data":jsonable_encoder(state)})


@router.post("/portal/paquetes/embalajes")
async def crear(request:Request,cliente:str=Depends(cliente_actual)):
    return await _guardar(request,partial(pkg.guardar_paquete,cliente))


@router.post("/portal/paquetes/embalajes/{paquete_id}")
async def editar(paquete_id:int,request:Request,cliente:str=Depends(cliente_actual)):
    return await _guardar(request,lambda d:pkg.guardar_paquete(cliente,d,paquete_id))


@router.post("/portal/paquetes/asociaciones")
async def asociar(request:Request,cliente:str=Depends(cliente_actual)):
    return await _guardar(request,partial(pkg.guardar_asociacion,cliente))


@router.post("/portal/paquetes/combinaciones")
async def combinar(request:Request,cliente:str=Depends(cliente_actual)):
    return await _guardar(request,partial(pkg.guardar_combinacion,cliente))


@router.post("/portal/paquetes/archivar")
async def archivar(request:Request,cliente:str=Depends(cliente_actual)):
    return await _guardar(request,lambda d:pkg.archivar(cliente,d.get("tipo"),d.get("id")))


@router.post("/portal/paquetes/tiendas/{tienda_id}")
async def tienda(tienda_id:int,request:Request,cliente:str=Depends(cliente_actual)):
    return await _guardar(request,partial(quotes.guardar_tienda,cliente,tienda_id))


@router.post("/portal/paquetes/cotizar")
async def cotizar(request:Request,cliente:str=Depends(cliente_actual)):
    return await _guardar(request,partial(quotes.cotizar_carrito,cliente))


@router.post("/portal/paquetes/shopify/{tienda_id}/autorizar")
def autorizar(tienda_id:int,request:Request,cliente:str=Depends(cliente_actual)):
    _csrf(request)
    t = quotes.tienda_propia(cliente,tienda_id)
    if not t or t["plataforma"] != "shopify":
        raise HTTPException(404,"La tienda no está vinculada.")
    from servicios.shopify_app import instalacion
    i = instalacion(t["dominio"])
    if not i or i.get("cliente_id") != cliente:
        raise HTTPException(403,"La tienda necesita completar su vinculación.")
    from endpoints.shopify import _redirect_oauth
    return _redirect_oauth(t["dominio"],cotizar_checkout=True)


@router.post("/portal/paquetes/shopify/{tienda_id}/activar")
async def activar(tienda_id:int,request:Request,cliente:str=Depends(cliente_actual)):
    from servicios.paquetes_shopify import activar as connect, CheckoutNoDisponible
    try:
        return await _guardar(request,lambda _:connect(cliente,tienda_id))
    except CheckoutNoDisponible as exc:
        return JSONResponse({"ok":False,"error":str(exc)},status_code=409)


@router.post("/portal/paquetes/shopify/{tienda_id}/pausar")
async def pausar(tienda_id:int,request:Request,cliente:str=Depends(cliente_actual)):
    from servicios.paquetes_shopify import pausar as pause
    return await _guardar(request,lambda _:pause(cliente,tienda_id))


@router.post("/integraciones/paquetes/shopify/{token}")
async def shopify_rates(token:str,request:Request):
    from servicios.paquetes_shopify import cotizar_callback, CheckoutNoDisponible
    data = await _body(request)
    try:
        result = await cotizar_callback(data,token)
        return JSONResponse(result,headers={"Cache-Control":"no-store"})
    except (pkg.PaqueteError,CheckoutNoDisponible):
        return JSONResponse({"rates":[]},status_code=503,headers={"Cache-Control":"no-store"})
    except Exception as exc:
        print(f"[paquetes] callback Shopify no disponible: {type(exc).__name__}")
        return JSONResponse({"rates":[]},status_code=503,headers={"Cache-Control":"no-store"})


def cliente_api(x_api_key: str = Header(default=None)):
    from servicios.api_b2b import obtener_cliente_por_api_key
    if not x_api_key:
        raise HTTPException(403,"X-API-Key requerido.")
    perfil=obtener_cliente_por_api_key(x_api_key)
    if not perfil.get("encontrado"):
        raise HTTPException(403,"API key inválida.")
    return perfil["cliente_id"]


@router.get("/api/paquetes")
def catalogo_api(cliente: str = Depends(cliente_api)):
    data = {"embalajes":[b for b in pkg.cargar_configuracion(cliente)["paquetes"] if b["activo"]],
            "productos":[{"producto_id":p["id"],"sku":p["alias_interno"]}
                         for p in pkg.cargar_catalogo(cliente) if p["sync_activo"] and not p["source_deleted_at"]]}
    return JSONResponse(jsonable_encoder(data),headers={"Cache-Control":"private, no-store"})


@router.post("/api/paquetes/cotizar")
async def cotizar_api(request: Request,cliente: str = Depends(cliente_api)):
    data = await _body(request)
    try:
        result = await run_in_threadpool(quotes.cotizar_carrito,cliente,data)
        return JSONResponse(jsonable_encoder(result),headers={"Cache-Control":"private, no-store"})
    except pkg.PaqueteError as exc:
        raise HTTPException(422,str(exc)) from None
    except Exception as exc:
        print(f"[paquetes] cotizador API no disponible: {type(exc).__name__}")
        raise HTTPException(503,"No se pudo cotizar en este momento.") from None
