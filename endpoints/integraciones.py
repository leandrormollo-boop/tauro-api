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
    tienda_por_dominio,
    verificar_hmac_shopify,
    parsear_pedido_shopify,
    guardar_pedido,
)

router = APIRouter(prefix="/integraciones", tags=["integraciones"])


@router.post("/shopify/webhook")
async def shopify_webhook(request: Request):
    cuerpo = await request.body()
    dominio = request.headers.get("x-shopify-shop-domain", "")
    firma = request.headers.get("x-shopify-hmac-sha256", "")
    topic = request.headers.get("x-shopify-topic", "")

    tienda = tienda_por_dominio(dominio)
    if not tienda or tienda["plataforma"] != "shopify":
        # TIENDA INSTALADA PERO SIN VINCULAR a una cuenta TAURO. Antes esto
        # devolvía 401 y la venta se perdía; peor: a las 8 fallas Shopify da
        # de baja la suscripción y se pierde el CANAL entero, no una venta.
        # Ahora se valida la firma con el secreto de la APP (es el mismo con
        # el que Shopify firma los webhooks de todas sus tiendas) y, si es
        # legítima, se guarda en la bandeja de huérfanos y se contesta 200.
        # Tiendanube ya resolvía esto bien; faltaba portarlo.
        from servicios.shopify_app import firma_valida_webhook_app, instalacion
        try:
            instalacion_oauth = instalacion(dominio)
        except Exception:
            instalacion_oauth = None
        app_esperada = str((instalacion_oauth or {}).get("app_client_id") or "")
        if firma_valida_webhook_app(cuerpo, firma, app_esperada):
            if topic.startswith("orders/"):
                try:
                    from servicios.integraciones_tienda import guardar_pedido_huerfano
                    guardar_pedido_huerfano(dominio, cuerpo)
                except Exception as e:
                    print(f"[integraciones] no pude guardar el huérfano de {dominio}: {e}")
                print(f"[integraciones] shopify {dominio} SIN VINCULAR: venta guardada "
                      f"como huérfana. El comerciante tiene que vincular su tienda "
                      f"en /portal/tienda para verla.")
            # Catálogo/inventario se recuperan completos al vincular: no se
            # guardan payloads sin tenant.
            return {"ok": True, "estado": "sin_vincular"}
        # Firma inválida o app sin configurar: 401 sin detalle, no le
        # confirmamos a un tercero qué dominios existen.
        return JSONResponse({"ok": False}, status_code=401)

    secreto_webhook = tienda["secreto"]
    try:
        from servicios.shopify_app import instalacion
        instalacion_oauth = instalacion(dominio)
        if instalacion_oauth:
            # Tienda OAuth: Shopify firma con el secret único de la app. El
            # marcador guardado en tiendas_conectadas no es una credencial.
            from servicios.shopify_app import firma_valida_webhook_app
            if not firma_valida_webhook_app(
                cuerpo, firma, str(instalacion_oauth.get("app_client_id") or "")
            ):
                print(f"[integraciones] firma shopify INVALIDA para {dominio} "
                      f"(topic {topic})")
                return JSONResponse({"ok": False}, status_code=401)
            secreto_webhook = "oauth:verificado"
    except Exception:
        pass

    if secreto_webhook != "oauth:verificado" and (
        not secreto_webhook or not verificar_hmac_shopify(secreto_webhook, cuerpo, firma)
    ):
        print(f"[integraciones] firma shopify INVALIDA para {dominio} (topic {topic})")
        return JSONResponse({"ok": False}, status_code=401)

    try:
        order = json.loads(cuerpo.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)

    topics_sync = {
        "products/create", "products/update", "products/delete",
        "inventory_levels/update", "inventory_items/update",
    }
    if topic in topics_sync:
        from servicios.shopify_catalogo import encolar_evento, lanzar_procesamiento_eventos
        webhook_id = request.headers.get("x-shopify-webhook-id", "").strip()
        if not webhook_id:
            # Shopify siempre lo envía; el hash permite que Postman/tests
            # conserven la misma garantía idempotente.
            import hashlib
            webhook_id = hashlib.sha256(
                dominio.encode() + b"\0" + topic.encode() + b"\0" + cuerpo
            ).hexdigest()
        nuevo = encolar_evento(
            webhook_id, dominio, topic, order,
            request.headers.get("x-shopify-triggered-at"),
        )
        if nuevo:
            lanzar_procesamiento_eventos()
        return {"ok": True, "encolado": nuevo, "duplicado": not nuevo}

    # Otros topics se aceptan y se ignoran para no provocar reintentos.
    if topic and not topic.startswith("orders/"):
        return {"ok": True, "ignorado": topic}

    pedido = parsear_pedido_shopify(order)
    if not pedido:
        # Pedido sin dirección de envío (digital/retiro): no es un envío.
        # Se loguea igual para no quedarnos sin rastro si alguien reclama.
        print(f"[integraciones] shopify {dominio} pedido {order.get('name') or order.get('id')} "
              f"ignorado: sin dirección de envío")
        return {"ok": True, "ignorado": "sin_direccion_envio"}

    # Un pedido cancelado (o cuyo pago se anuló) no se despacha: se saca de
    # pendientes para que nadie mande mercadería de una venta caída.
    if pedido.get("cancelado") or pedido.get("estado_pago") in ("refunded", "voided"):
        from servicios.integraciones_tienda import cancelar_pedido_externo
        cambio = cancelar_pedido_externo(tienda["id"], pedido["pedido_externo_id"])
        print(f"[integraciones] shopify {dominio} pedido {pedido['numero']} "
              f"cancelado/anulado → {'sacado de pendientes' if cambio else 'no estaba pendiente'}")
        return {"ok": True, "cancelado": True}

    creado = guardar_pedido(tienda["cliente_id"], tienda["id"], "shopify", pedido)
    print(f"[integraciones] shopify {dominio} pedido {pedido['numero']} → "
          f"{'guardado' if creado else 'actualizado'}")

    # La solicitud de guía se arma SOLA: el comerciante no retipea nada, sólo
    # revisa y genera. Va en un hilo porque cotizar tarda y la tienda espera
    # un 200 rápido; demasiados timeouts hacen que Shopify dé de baja el
    # webhook. La guía NO se emite sola: eso cuesta plata y no se deshace.
    if creado:
        try:
            from servicios.integraciones_tienda import id_de_pedido
            from servicios.solicitud_automatica import intentar_en_segundo_plano
            pid = id_de_pedido(tienda["id"], pedido["pedido_externo_id"])
            if pid:
                intentar_en_segundo_plano(pid)
        except Exception as e:
            # Que falle el armado automático no puede tumbar el webhook: el
            # pedido ya está guardado y el comerciante lo arma a mano.
            print(f"[integraciones] no pude lanzar el armado automático: {e}")

    return {"ok": True, "nuevo": creado}


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
        print(f"[tiendanube] no pude vincular la tienda {store_id}: {e}")

    print(f"[tiendanube] instalada tienda {store_id} ({nombre or 's/nombre'}) · "
          f"webhooks {eventos} · cliente {dueno or 'sin vincular'}")

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
        print(f"[tiendanube] tienda {store_id} desinstaló la app")
        return {"ok": True}

    inst = instalacion(store_id)
    if not inst or not inst.get("cliente_id"):
        print(f"[tiendanube] tienda {store_id} sin vincular a una cuenta TAURO")
        return {"ok": True, "estado": "sin_vincular"}

    # El webhook sólo trae el id: el pedido completo se pide a la API.
    r = _api(store_id, inst["access_token"], "GET", f"orders/{pedido_id}")
    if r is None or r.status_code != 200:
        print(f"[tiendanube] no pude leer el pedido {pedido_id}: "
              f"{r.status_code if r is not None else 'sin respuesta'}")
        return {"ok": True, "estado": "pedido_no_leido"}

    pedido = parsear_pedido(r.json())
    if not pedido:
        print(f"[tiendanube] pedido {pedido_id} sin dirección de envío, ignorado")
        return {"ok": True, "ignorado": "sin_direccion_envio"}

    from servicios.integraciones_tienda import tienda_por_dominio
    tienda = tienda_por_dominio(f"{store_id}.tiendanube")
    if not tienda:
        return {"ok": True, "estado": "tienda_no_registrada"}

    if evento == "order/cancelled" or pedido.get("cancelado"):
        cancelar_pedido_externo(tienda["id"], pedido["pedido_externo_id"])
        print(f"[tiendanube] pedido {pedido['numero']} cancelado → fuera de pendientes")
        return {"ok": True, "cancelado": True}

    creado = guardar_pedido(inst["cliente_id"], tienda["id"], "tiendanube", pedido)
    print(f"[tiendanube] tienda {store_id} pedido {pedido['numero']} → "
          f"{'guardado' if creado else 'actualizado'}")

    if creado:
        try:
            from servicios.integraciones_tienda import id_de_pedido
            from servicios.solicitud_automatica import intentar_en_segundo_plano
            pid = id_de_pedido(tienda["id"], pedido["pedido_externo_id"])
            if pid:
                intentar_en_segundo_plano(pid)
        except Exception as e:
            print(f"[tiendanube] no pude lanzar el armado automático: {e}")

    return {"ok": True, "nuevo": creado}
