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
        # 401 sin detalle: no le confirmamos a un tercero qué dominios existen.
        return JSONResponse({"ok": False}, status_code=401)

    if not verificar_hmac_shopify(tienda["secreto"], cuerpo, firma):
        print(f"[integraciones] firma shopify INVALIDA para {dominio} (topic {topic})")
        return JSONResponse({"ok": False}, status_code=401)

    # Solo nos interesan órdenes; otros topics se aceptan y se ignoran
    # (devolver 200 evita que Shopify reintente o dé de baja el webhook).
    if topic and not topic.startswith("orders/"):
        return {"ok": True, "ignorado": topic}

    try:
        order = json.loads(cuerpo.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)

    pedido = parsear_pedido_shopify(order)
    if not pedido:
        # Pedido sin dirección de envío (digital/retiro): no es un envío.
        return {"ok": True, "ignorado": "sin_direccion_envio"}

    creado = guardar_pedido(tienda["cliente_id"], tienda["id"], "shopify", pedido)
    print(f"[integraciones] shopify {dominio} pedido {pedido['numero']} → "
          f"{'guardado' if creado else 'ya existia'}")
    return {"ok": True, "nuevo": creado}


@router.post("/tiendanube/webhook")
async def tiendanube_webhook(request: Request):
    """
    Tiendanube manda webhooks solo a través de una app registrada
    (OAuth de Partner). El receptor queda listo; se activa cuando la
    cuenta de Partner de TAURO esté aprobada y las credenciales
    (TIENDANUBE_CLIENT_ID / TIENDANUBE_CLIENT_SECRET) estén en Railway.
    """
    cuerpo = await request.body()
    print(f"[integraciones] webhook tiendanube recibido ({len(cuerpo)} bytes) — app Partner pendiente")
    return {"ok": True, "estado": "tiendanube_proximamente"}
