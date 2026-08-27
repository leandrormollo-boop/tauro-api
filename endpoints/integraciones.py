# ============================================================
# Webhooks de tiendas: acá llegan las ventas de Shopify/Tiendanube.
# ============================================================
# Seguridad: cada webhook viene firmado (HMAC-SHA256 sobre el cuerpo
# crudo) con el secreto que el cliente guardó al conectar su tienda.
# Firma inválida → 401 y no se guarda nada. La verificación es por
# tienda (dominio), así cada cliente usa SU secreto.
#
# Respuesta siempre rápida: la tienda reintenta si no le contestás
# 200 a tiempo, y demasiados timeouts hacen que dé de baja el webhook.
# ============================================================
from __future__ import annotations

import json
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from servicios.integraciones_tienda import (
    OAUTH_SECRET_MARKER,
    PedidoShopifyCanceladoError,
    TiendaNoOperativaError,
    tienda_por_dominio,
    verificar_hmac_shopify,  # API legacy; Shopify OAuth ya no usa secreto manual.
    parsear_pedido_shopify,
    guardar_pedido,
)

router = APIRouter(prefix="/integraciones", tags=["integraciones"])


_TOPICS_CATALOGO = {
    "products/create", "products/update", "products/delete",
    "inventory_levels/update", "inventory_items/update",
}
_TOPICS_ORDEN = {"orders/create", "orders/updated", "orders/cancelled"}


def _contrato_payload_shopify(topic: str, datos: dict) -> bool:
    """Contrato mínimo exclusivo para impedir replay entre clases de evento."""
    if not isinstance(datos, dict):
        return False
    if topic in _TOPICS_ORDEN:
        if not str(datos.get("id") or "").strip():
            return False
        cancelado = bool(datos.get("cancelled_at")) or str(
            datos.get("financial_status") or ""
        ).lower() in {"refunded", "voided"}
        if topic == "orders/create":
            return not cancelado
        if topic == "orders/cancelled":
            return cancelado
        return True
    if topic in {"products/create", "products/update"}:
        return bool(datos.get("id")) and isinstance(datos.get("variants"), list)
    if topic == "products/delete":
        return bool(datos.get("id")) and "variants" not in datos
    if topic == "inventory_levels/update":
        return bool(datos.get("inventory_item_id") and datos.get("location_id"))
    if topic == "inventory_items/update":
        return bool(datos.get("id")) and "inventory_item_id" not in datos
    return False


def _webhook_id_shopify(request: Request, dominio: str, topic: str, cuerpo: bytes) -> str:
    valor = request.headers.get("x-shopify-webhook-id", "").strip()
    if valor:
        return valor
    import hashlib
    return hashlib.sha256(
        dominio.encode() + b"\0" + topic.encode() + b"\0" + cuerpo
    ).hexdigest()


async def _procesar_shopify_webhook(request: Request, topic_esperado: str):
    topic = request.headers.get("x-shopify-topic", "").strip().lower()
    if topic != topic_esperado:
        return JSONResponse({"ok": False}, status_code=400)

    cuerpo = await request.body()
    dominio = request.headers.get("x-shopify-shop-domain", "").strip().lower()
    firma = request.headers.get("x-shopify-hmac-sha256", "")
    from servicios.shopify_app import (
        clasificar_evento_instalacion, dominio_valido,
        firma_valida_webhook_app, instalacion,
    )
    if not dominio_valido(dominio):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        tienda = tienda_por_dominio(dominio)
        instalacion_oauth = instalacion(dominio)
    except Exception as exc:
        print(f"[integraciones] no pude resolver instalación: {type(exc).__name__}")
        return JSONResponse({"ok": False}, status_code=503)

    app_esperada = str((instalacion_oauth or {}).get("app_client_id") or "")
    if not app_esperada or not firma_valida_webhook_app(cuerpo, firma, app_esperada):
        return JSONResponse({"ok": False}, status_code=401)
    estado_temporal = clasificar_evento_instalacion(
        instalacion_oauth, request.headers.get("x-shopify-triggered-at", ""),
    )
    if estado_temporal == "ANTERIOR":
        return {"ok": True, "ignorado": "generacion_anterior"}
    if estado_temporal != "ACTUAL":
        return JSONResponse({"ok": False}, status_code=400)

    try:
        datos = json.loads(cuerpo.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    if not _contrato_payload_shopify(topic, datos):
        return JSONResponse({"ok": False}, status_code=400)

    generation = str((instalacion_oauth or {}).get("install_generation") or "")
    webhook_id = _webhook_id_shopify(request, dominio, topic, cuerpo)
    if topic in _TOPICS_ORDEN:
        from servicios.integraciones_tienda import webhook_shopify_ya_procesado
        try:
            if webhook_shopify_ya_procesado(webhook_id):
                return {"ok": True, "duplicado": True}
        except Exception as exc:
            print(f"[integraciones] no pude consultar dedupe: {type(exc).__name__}")
            return JSONResponse({"ok": False}, status_code=503)

    owner_tienda = str((tienda or {}).get("cliente_id") or "").strip().upper()
    owner_inst = str((instalacion_oauth or {}).get("cliente_id") or "").strip().upper()
    coherente = bool(
        tienda
        and tienda.get("plataforma") == "shopify"
        and tienda.get("activa") is True
        and tienda.get("secreto") == OAUTH_SECRET_MARKER
        and owner_tienda
        and owner_tienda == owner_inst
        and (instalacion_oauth or {}).get("access_token")
    )

    def _marcar_procesado():
        from servicios.integraciones_tienda import marcar_webhook_shopify_procesado
        marcar_webhook_shopify_procesado(
            webhook_id, dominio, topic, generation,
        )

    if not coherente:
        try:
            if topic in {"orders/create", "orders/updated"}:
                from servicios.integraciones_tienda import guardar_pedido_huerfano
                guardar_pedido_huerfano(
                    dominio,
                    cuerpo,
                    app_client_id_verificado=app_esperada,
                    install_generation_verificada=generation,
                )
            elif topic == "orders/cancelled":
                from servicios.integraciones_tienda import cancelar_pedido_huerfano
                cancelar_pedido_huerfano(
                    dominio,
                    str(datos.get("id") or ""),
                    app_client_id_verificado=app_esperada,
                    install_generation_verificada=generation,
                    evento_at=request.headers.get("x-shopify-triggered-at", ""),
                )
            if topic in _TOPICS_ORDEN:
                _marcar_procesado()
        except Exception as exc:
            print(f"[integraciones] no pude persistir evento ownerless: {type(exc).__name__}")
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True, "estado": "sin_vincular"}

    if topic in _TOPICS_CATALOGO:
        from servicios.shopify_catalogo import (
            ShopifyCatalogError, encolar_evento, lanzar_procesamiento_eventos,
        )
        try:
            nuevo = encolar_evento(
                webhook_id, dominio, topic, datos,
                request.headers.get("x-shopify-triggered-at"), generation,
            )
        except ShopifyCatalogError as exc:
            if exc.codigo == "GENERACION_OBSOLETA":
                return {"ok": True, "ignorado": "generacion_anterior"}
            return JSONResponse({"ok": False}, status_code=503)
        except Exception as exc:
            print(f"[integraciones] no pude encolar catálogo: {type(exc).__name__}")
            return JSONResponse({"ok": False}, status_code=503)
        if nuevo:
            lanzar_procesamiento_eventos()
        return {"ok": True, "encolado": nuevo, "duplicado": not nuevo}

    pedido_externo_id = str(datos.get("id") or "")
    cancelado = topic == "orders/cancelled" or bool(datos.get("cancelled_at")) or str(
        datos.get("financial_status") or ""
    ).lower() in {"refunded", "voided"}
    if cancelado:
        from servicios.integraciones_tienda import cancelar_pedido_externo
        try:
            cancelar_pedido_externo(
                tienda["id"],
                pedido_externo_id,
                cliente_id=owner_tienda,
                dominio_verificado=dominio,
                install_generation_verificada=generation,
                evento_at=request.headers.get("x-shopify-triggered-at", ""),
            )
        except TiendaNoOperativaError:
            return {"ok": True, "ignorado": "generacion_anterior"}
        except Exception as exc:
            print(f"[integraciones] no pude cancelar orden: {type(exc).__name__}")
            return JSONResponse({"ok": False}, status_code=503)
        try:
            _marcar_procesado()
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True, "cancelado": True}

    pedido = parsear_pedido_shopify(datos)
    if not pedido:
        try:
            _marcar_procesado()
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True, "ignorado": "sin_direccion_envio"}
    try:
        creado = guardar_pedido(
            tienda["cliente_id"], tienda["id"], "shopify", pedido,
            dominio_verificado=dominio,
            install_generation_verificada=generation,
        )
        _marcar_procesado()
    except PedidoShopifyCanceladoError:
        try:
            _marcar_procesado()
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True, "ignorado": "pedido_cancelado"}
    except TiendaNoOperativaError:
        return JSONResponse({"ok": False}, status_code=503)
    except Exception as exc:
        print(f"[integraciones] no pude procesar orden: {type(exc).__name__}")
        return JSONResponse({"ok": False}, status_code=503)

    if creado:
        try:
            from servicios.integraciones_tienda import id_de_pedido
            from servicios.solicitud_automatica import intentar_en_segundo_plano
            pedido_id = id_de_pedido(tienda["id"], pedido["pedido_externo_id"])
            if pedido_id:
                intentar_en_segundo_plano(pedido_id)
        except Exception as exc:
            print(f"[integraciones] armado automático no iniciado: {type(exc).__name__}")
    return {"ok": True, "nuevo": creado}


@router.post("/shopify/webhook")
async def shopify_webhook(_request: Request):
    """Ruta heredada sin contrato de topic: ya no procesa payloads."""
    return JSONResponse({"ok": False, "error": "endpoint_obsoleto"}, status_code=410)


@router.post("/shopify/webhook/orders-create")
async def shopify_orders_create(request: Request):
    return await _procesar_shopify_webhook(request, "orders/create")


@router.post("/shopify/webhook/orders-updated")
async def shopify_orders_updated(request: Request):
    return await _procesar_shopify_webhook(request, "orders/updated")


@router.post("/shopify/webhook/orders-cancelled")
async def shopify_orders_cancelled(request: Request):
    return await _procesar_shopify_webhook(request, "orders/cancelled")


@router.post("/shopify/webhook/products-create")
async def shopify_products_create(request: Request):
    return await _procesar_shopify_webhook(request, "products/create")


@router.post("/shopify/webhook/products-update")
async def shopify_products_update(request: Request):
    return await _procesar_shopify_webhook(request, "products/update")


@router.post("/shopify/webhook/products-delete")
async def shopify_products_delete(request: Request):
    return await _procesar_shopify_webhook(request, "products/delete")


@router.post("/shopify/webhook/inventory-levels-update")
async def shopify_inventory_levels_update(request: Request):
    return await _procesar_shopify_webhook(request, "inventory_levels/update")


@router.post("/shopify/webhook/inventory-items-update")
async def shopify_inventory_items_update(request: Request):
    return await _procesar_shopify_webhook(request, "inventory_items/update")


@router.get("/tiendanube/callback")
def tiendanube_callback(request: Request, code: str = "", state: str = ""):
    """
    Tiendanube vuelve acá con el `code` después de que el comerciante
    aceptó los permisos. Lo canjeamos por el token permanente, damos de
    alta los webhooks y, si tiene sesión del portal abierta, atamos la
    tienda a su cuenta en el acto.
    """
    from fastapi.responses import HTMLResponse

    from servicios.tiendanube_app import (
        app_configurada, canjear_token, guardar_instalacion,
        registrar_webhooks, datos_tienda, vincular_cliente,
    )

    def _pag(titulo: str, texto: str, boton: str = "", status: int = 200):
        return HTMLResponse(
            f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo} · TAURO Solutions</title><style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0c0a14;color:#f4f5f7;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;
text-align:center;padding:24px;}} .b{{max-width:480px;}}
h1{{font-size:30px;margin:0 0 14px;}} p{{color:#b9bfc7;line-height:1.7;margin:0 0 26px;}}
a{{display:inline-block;background:#7c5cf6;color:#fff;padding:14px 30px;
border-radius:999px;text-decoration:none;font-weight:600;}}
</style></head><body><div class="b"><h1>{titulo}</h1><p>{texto}</p>{boton}</div></body></html>""",
            status_code=status,
        )

    if not app_configurada():
        return _pag("App en preparación",
                    "La integración con Tiendanube todavía no está habilitada.",
                    '<a href="https://taurosolutions.ar/portal/tienda">Ir al portal</a>',
                    status=503)
    if not code:
        return _pag("Instalación incompleta",
                    "Tiendanube no nos devolvió el código de autorización. "
                    "Probá instalar de nuevo desde tu panel.", status=400)

    data = canjear_token(code)
    if not data or not data.get("access_token") or not data.get("user_id"):
        return _pag("No pudimos conectar",
                    "Tiendanube no nos dio el permiso. Probá de nuevo.", status=502)

    store_id = str(data["user_id"])
    token = data["access_token"]

    info = datos_tienda(store_id, token)
    nombre = ""
    if isinstance(info, dict):
        n = info.get("name")
        nombre = (n.get("es") or n.get("pt") or "") if isinstance(n, dict) else str(n or "")

    guardar_instalacion(store_id, token, nombre)
    eventos = registrar_webhooks(store_id, token)

    # Vinculación anti-CSRF: sólo se ata la tienda a una cuenta si el navegador
    # trae la cookie `tn_oauth` que sembró el botón del portal (state:cliente).
    # Así un callback disparado por un tercero —sin haber pasado por el botón—
    # NO puede colgar una tienda ajena en la cuenta de la víctima. Si Tiendanube
    # devuelve el state en la query, se exige que coincida (defensa extra).
    dueno = None
    try:
        cookie = request.cookies.get("tn_oauth") or ""
        if cookie and ":" in cookie:
            state_cookie, cliente_cookie = cookie.split(":", 1)
            if state_cookie and (not state or state == state_cookie):
                dueno = cliente_cookie
                vincular_cliente(store_id, dueno)
    except Exception as e:
        print(f"[tiendanube] no pude vincular la tienda: {type(e).__name__}")

    print(f"[tiendanube] instalación procesada · {len(eventos)} webhook(s) · "
          f"{'con claim' if dueno else 'ownerless'}")

    if dueno:
        texto = ("Listo: desde ahora cada venta con envío aparece en tu portal "
                 "lista para generar la guía con un click.")
    else:
        texto = ("Tu tienda quedó conectada. Entrá al portal, sección "
                 "<b>Mi tienda</b>, e instalá desde el botón de Tiendanube para "
                 "atarla a tu cuenta y empezar a recibir tus ventas.")
    resp = _pag("¡Tienda conectada!", texto,
                '<a href="https://taurosolutions.ar/portal/tienda">Ver mis pedidos</a>')
    resp.delete_cookie("tn_oauth")   # de un solo uso
    return resp


@router.post("/tiendanube/webhook")
async def tiendanube_webhook(request: Request):
    """
    Ventas de Tiendanube. El webhook trae sólo el id del pedido, así que
    hay que ir a buscarlo a la API con el token de esa tienda.

    Firma: HMAC-SHA256 en HEXADECIMAL (Shopify usa base64) con el
    client_secret de la app.
    """
    import json as _json
    import os as _os

    from servicios.integraciones_tienda import (
        verificar_hmac_tiendanube, guardar_pedido, cancelar_pedido_externo,
    )
    from servicios.tiendanube_app import (
        app_configurada, instalacion, parsear_pedido, _api, desinstalar,
    )

    cuerpo = await request.body()
    if not app_configurada():
        print("[tiendanube] webhook recibido pero la app no está configurada")
        return {"ok": True, "estado": "app_no_configurada"}

    secreto = _os.getenv("TIENDANUBE_CLIENT_SECRET", "")
    firma = request.headers.get("x-linkedstore-hmac-sha256", "")
    if not verificar_hmac_tiendanube(secreto, cuerpo, firma):
        print("[tiendanube] firma INVÁLIDA")
        return JSONResponse({"ok": False}, status_code=401)

    try:
        datos = _json.loads(cuerpo.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)

    store_id = str(datos.get("store_id") or "")
    evento = datos.get("event") or ""
    pedido_id = str(datos.get("id") or "")

    if evento == "app/uninstalled":
        desinstalar(store_id)
        print("[tiendanube] app/uninstalled procesado")
        return {"ok": True}

    inst = instalacion(store_id)
    if not inst or not inst.get("cliente_id"):
        print("[tiendanube] evento ownerless")
        return {"ok": True, "estado": "sin_vincular"}

    # El webhook sólo trae el id: el pedido completo se pide a la API.
    r = _api(store_id, inst["access_token"], "GET", f"orders/{pedido_id}")
    if r is None or r.status_code != 200:
        print("[tiendanube] no pude leer el pedido")
        return {"ok": True, "estado": "pedido_no_leido"}

    pedido = parsear_pedido(r.json())
    if not pedido:
        print("[tiendanube] pedido sin dirección de envío, ignorado")
        return {"ok": True, "ignorado": "sin_direccion_envio"}

    from servicios.integraciones_tienda import tienda_por_dominio
    tienda = tienda_por_dominio(f"{store_id}.tiendanube")
    if not tienda:
        return {"ok": True, "estado": "tienda_no_registrada"}

    if evento == "order/cancelled" or pedido.get("cancelado"):
        cancelar_pedido_externo(tienda["id"], pedido["pedido_externo_id"])
        print("[tiendanube] pedido cancelado → fuera de pendientes")
        return {"ok": True, "cancelado": True}

    creado = guardar_pedido(inst["cliente_id"], tienda["id"], "tiendanube", pedido)
    print(f"[tiendanube] pedido {'guardado' if creado else 'actualizado'}")

    if creado:
        try:
            from servicios.integraciones_tienda import id_de_pedido
            from servicios.solicitud_automatica import intentar_en_segundo_plano
            pid = id_de_pedido(tienda["id"], pedido["pedido_externo_id"])
            if pid:
                intentar_en_segundo_plano(pid)
        except Exception as e:
            print(f"[tiendanube] armado automático no iniciado: {type(e).__name__}")

    return {"ok": True, "nuevo": creado}
