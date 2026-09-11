"""CarrierService opt-in por tienda. El callback no confía en un header shop.

El secreto identifica una instalación y su generación. No usa Admin API en
el camino del checkout. Los workers están acotados incluso si un courier
sigue trabajando después del límite de respuesta de Shopify.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import re
import secrets
from decimal import Decimal
from threading import BoundedSemaphore
from urllib.parse import urlparse

from core.database import get_conn
from servicios import paquetes as pkg
from servicios import paquetes_cotizacion as quotes

_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="paquetes-checkout")
_SLOTS = BoundedSemaphore(4)
CALLBACK_SECONDS = 2.4
_NAME = "TAURO Solutions · Paquetes"


class CheckoutNoDisponible(RuntimeError):
    pass


def hash_token(value):
    return hashlib.sha256(value.encode()).hexdigest()


def config_callback(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", token or ""):
        return None
    digest = hash_token(token)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT c.* FROM paquetes_tiendas c
            JOIN tiendas_conectadas t ON t.cliente_id=c.cliente_id
                AND t.plataforma=c.plataforma AND t.dominio=c.dominio
            JOIN shopify_instalaciones i ON i.dominio=c.dominio
                AND i.cliente_id=c.cliente_id AND i.install_generation=c.install_generation
            WHERE c.callback_token_hash=%s AND c.plataforma='shopify'
                AND c.usar_paquetes AND c.checkout_activo AND t.activa
                AND t.secreto='oauth:shopify-app' AND i.webhooks_ready
                AND NOT i.token_reauth_required AND nullif(trim(i.access_token),'') IS NOT NULL
                AND 'write_shipping'=ANY(string_to_array(i.scopes,','))""", (digest,))
        row = cur.fetchone()
    if not row or not hmac.compare_digest(str(row["callback_token_hash"]), digest):
        return None
    return dict(row)


def _rates(payload, token):
    config = config_callback(token)
    if not config:
        raise CheckoutNoDisponible("El servicio de envío no está activo.")
    if not isinstance(payload, dict) or not isinstance(payload.get("rate"), dict):
        raise pkg.PaqueteError("El carrito es inválido.")
    rate = payload["rate"]
    if str(rate.get("currency") or "").upper() != "ARS":
        raise pkg.PaqueteError("Esta configuración de envío cotiza en ARS.")
    origen = quotes.direccion(rate.get("origin"), "origen")
    destino = quotes.direccion(rate.get("destination"), "destino")
    ambito = "nacional" if origen["pais"] == destino["pais"] == "AR" else "internacional"
    politica = config[ambito]
    if not politica.get("habilitado"):
        return {"rates": []}
    items = rate.get("items")
    plan, _ = quotes.plan_tienda(config["cliente_id"],"shopify",config["dominio"],items)
    subtotal = Decimal("0")
    for item in items:
        if item.get("requires_shipping") is False:
            continue
        centavos = pkg.numero(item.get("price"), "Precio del producto", decimales=0, maximo=10000000000)
        subtotal += centavos * pkg.entero(item.get("quantity")) / 100
    result = quotes.cotizar_plan(config["cliente_id"],plan,origen,destino,subtotal,politica)
    if not result["encontrado"]:
        # Shopify activa tarifas de respaldo ante errores; nunca devolvemos $0
        # para disimular falta de configuración o una caída del transportista.
        raise CheckoutNoDisponible("No hay tarifa disponible.")
    rates = []
    for op in result["opciones"]:
        price = Decimal(op["precio_comprador_ars"])
        code = re.sub(r"[^a-zA-Z0-9_-]", "_", str(op["codigo_servicio"]))[:80]
        rates.append({"service_name":f"TAURO · {op['servicio']}",
            "service_code":f"tauro_{ambito}_{op['courier']}_{code}",
            "total_price":str(int(price * 100)),"currency":"ARS",
            "description":"Envío gratis" if price == 0 else "Entrega con seguimiento"})
    return {"rates":rates}


async def cotizar_callback(payload, token):
    if not _SLOTS.acquire(blocking=False):
        raise CheckoutNoDisponible("Cotizador ocupado.")
    try:
        future = _WORKERS.submit(_rates,payload,token)
    except Exception:
        _SLOTS.release()
        raise
    future.add_done_callback(lambda _: _SLOTS.release())
    wrapped = asyncio.wrap_future(future)
    # Un courier puede fallar después del timeout; recuperar esa excepción
    # evita avisos de futures abandonados sin prolongar el checkout.
    wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    try:
        return await asyncio.wait_for(asyncio.shield(wrapped),CALLBACK_SECONDS)
    except asyncio.TimeoutError:
        raise CheckoutNoDisponible("Se agotó el tiempo de cotización.") from None


def _token_url(url, base):
    prefix = base.rstrip("/") + "/integraciones/paquetes/shopify/"
    if not str(url or "").startswith(prefix):
        return ""
    token = url[len(prefix):]
    return token if re.fullmatch(r"[A-Za-z0-9_-]{32,100}", token) else ""


def activar(cliente, tienda_id):
    from servicios import shopify_app as shopify
    tienda = quotes.tienda_propia(cliente,tienda_id)
    if not tienda or tienda["plataforma"] != "shopify":
        raise pkg.PaqueteError("Elegí una tienda Shopify vinculada a tu cuenta.")
    dominio = tienda["dominio"]
    config = quotes.config_tienda(cliente,"shopify",dominio)
    if not config or not config["usar_paquetes"] or not any(config[k].get("habilitado") for k in ("nacional","internacional")):
        raise pkg.PaqueteError("Guardá la configuración de paquetes y habilitá al menos un ámbito de envío.")
    inst = shopify.instalacion(dominio)
    if not inst or inst.get("cliente_id") != cliente or not inst.get("access_token") or not inst.get("webhooks_ready"):
        raise pkg.PaqueteError("La tienda necesita completar su autorización de Shopify.")
    if "write_shipping" not in {s.strip() for s in (inst.get("scopes") or "").split(",")}:
        raise pkg.PaqueteError("Primero autorizá el permiso de tarifas de Shopify con el botón del portal.")
    base = shopify._base_url()
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise CheckoutNoDisponible("El portal necesita una dirección HTTPS configurada.")
    # Serializa altas/reintentos. Reconciliar el servicio remoto evita duplicarlo
    # si Shopify lo creó pero la respuesta o el guardado local se perdieron.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"paquetes-shopify:{dominio}",))
        data = shopify._graphql(dominio,inst["access_token"],"""query PaquetesCarriers {
            carrierServices(first: 100) { edges { node { id name callbackUrl active } }
            pageInfo { hasNextPage } } }""")
        connection = (data or {}).get("carrierServices")
        if not isinstance(connection,dict) or connection.get("pageInfo",{}).get("hasNextPage"):
            raise CheckoutNoDisponible("No pudimos verificar los servicios de envío de Shopify. Revisá el permiso y la disponibilidad del plan.")
        own = [e["node"] for e in connection.get("edges",[]) if e.get("node",{}).get("name") == _NAME
            and _token_url(e["node"].get("callbackUrl"),base)]
        if len(own) > 1:
            raise CheckoutNoDisponible("Hay más de un servicio TAURO configurado. Contactá a soporte para unificarlos.")
        token = _token_url(own[0]["callbackUrl"],base) if own else secrets.token_urlsafe(32)
        carrier = own[0] if own else None
        if carrier and not carrier["active"]:
            data = shopify._graphql(dominio,inst["access_token"],"""mutation PaquetesActivar($input: DeliveryCarrierServiceUpdateInput!) {
                carrierServiceUpdate(input:$input) { carrierService { id active } userErrors { field message } } }""",
                {"input":{"id":carrier["id"],"active":True}})
            operation = (data or {}).get("carrierServiceUpdate",{})
            carrier = operation.get("carrierService") if not operation.get("userErrors") else None
        elif not carrier:
            data = shopify._graphql(dominio,inst["access_token"],"""mutation PaquetesCrear($input: DeliveryCarrierServiceCreateInput!) {
                carrierServiceCreate(input:$input) { carrierService { id active } userErrors { field message } } }""",
                {"input":{"name":_NAME,"callbackUrl":base+"/integraciones/paquetes/shopify/"+token,
                          "active":True,"supportsServiceDiscovery":False}})
            operation = (data or {}).get("carrierServiceCreate",{})
            carrier = operation.get("carrierService") if not operation.get("userErrors") else None
        if not carrier or not carrier.get("active") or not carrier.get("id"):
            raise CheckoutNoDisponible("Shopify no habilitó las tarifas. Revisá que la tienda tenga envío calculado por terceros y el permiso de tarifas.")
        cur.execute("""UPDATE paquetes_tiendas c SET callback_token_hash=%s,carrier_id=%s,
            install_generation=%s,checkout_activo=TRUE,actualizado_en=now()
            FROM shopify_instalaciones i, tiendas_conectadas t
            WHERE c.cliente_id=%s AND c.plataforma='shopify' AND c.dominio=%s
                AND c.usar_paquetes AND i.cliente_id=c.cliente_id AND i.dominio=c.dominio
                AND i.install_generation=%s AND i.webhooks_ready AND NOT i.token_reauth_required
                AND t.cliente_id=c.cliente_id AND t.dominio=c.dominio AND t.activa
                AND t.plataforma='shopify' AND t.secreto='oauth:shopify-app' RETURNING c.carrier_id""",
            (hash_token(token),carrier["id"],inst["install_generation"],cliente,dominio,inst["install_generation"]))
        if not cur.fetchone():
            raise CheckoutNoDisponible("La autorización cambió durante la activación. Volvé a intentarlo.")
    return {"ok":True,"mensaje":"Servicio conectado. Agregá TAURO a las zonas de envío de Shopify y hacé una compra de prueba."}


def pausar(cliente,tienda_id):
    tienda = quotes.tienda_propia(cliente,tienda_id)
    if not tienda:
        raise pkg.PaqueteError("La tienda no está vinculada.")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE paquetes_tiendas SET checkout_activo=FALSE
            WHERE cliente_id=%s AND plataforma=%s AND dominio=%s""",
            (cliente,tienda["plataforma"],tienda["dominio"]))
