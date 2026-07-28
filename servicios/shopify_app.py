# ============================================================
# App pública de TAURO para Shopify
# ============================================================
# Cubre el ciclo completo que decidimos:
#   · INSTALACIÓN  → OAuth: el comerciante da un click y queda conectado
#                    (registramos sus webhooks solos, sin pasos manuales).
#   · CHECKOUT     → CarrierService: si el plan de la tienda lo permite,
#                    el comprador ve la tarifa TAURO en vivo. Si no, la
#                    tienda usa su envío fijo y la app funciona igual.
#   · VENTA        → webhook orders/create → pedido pendiente en el portal.
#   · GUÍA         → al emitirla en TAURO, marcamos el pedido como enviado
#                    en Shopify con su tracking: el comprador recibe el mail
#                    de "tu pedido va en camino" sin que nadie toque nada.
#
# Se enciende cuando estén SHOPIFY_API_KEY / SHOPIFY_API_SECRET en Railway
# (mismo patrón feature-flag que UPS/DHL). Sin credenciales, el módulo
# queda inerte y el resto de la plataforma no se entera.
# ============================================================
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
from typing import Optional

import requests

from core.database import get_conn

API_VERSION = "2025-01"

# Permisos mínimos: leer/escribir envíos y leer pedidos. Nada más.
SCOPES = "read_orders,write_orders,write_fulfillments,read_fulfillments,write_shipping"

_tabla_lista = False


def _ensure_tabla() -> None:
    """Guarda el token de acceso de cada tienda instalada."""
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shopify_instalaciones (
                    id             SERIAL PRIMARY KEY,
                    dominio        TEXT NOT NULL UNIQUE,
                    access_token   TEXT NOT NULL,
                    scopes         TEXT,
                    cliente_id     TEXT,
                    carrier_id     TEXT,
                    instalada_en   TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
        conn.commit()
    _tabla_lista = True


def app_configurada() -> bool:
    return bool(os.getenv("SHOPIFY_API_KEY") and os.getenv("SHOPIFY_API_SECRET"))


def _base_url() -> str:
    return (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")


# ── Instalación (OAuth) ─────────────────────────────────────

def url_instalacion(dominio: str, state: str) -> str:
    """A dónde mandamos al comerciante para que autorice la app."""
    params = {
        "client_id": os.getenv("SHOPIFY_API_KEY", ""),
        "scope": SCOPES,
        "redirect_uri": f"{_base_url()}/shopify/callback",
        "state": state,
    }
    return f"https://{dominio}/admin/oauth/authorize?" + urllib.parse.urlencode(params)


def validar_hmac_query(params: dict) -> bool:
    """
    Shopify firma los parámetros de la redirección OAuth. Sin esta
    verificación, cualquiera podría hacerse pasar por Shopify e
    instalarnos una tienda falsa.
    """
    secreto = os.getenv("SHOPIFY_API_SECRET", "")
    firma = params.get("hmac", "")
    if not secreto or not firma:
        return False
    resto = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
    mensaje = "&".join(f"{k}={resto[k]}" for k in sorted(resto))
    calculada = hmac.new(secreto.encode(), mensaje.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculada, firma)


def dominio_valido(dominio: str) -> bool:
    """Solo aceptamos dominios de Shopify: corta el paso a redirecciones a sitios ajenos."""
    d = (dominio or "").strip().lower()
    return bool(d) and d.endswith(".myshopify.com") and "/" not in d and " " not in d


def canjear_token(dominio: str, code: str) -> Optional[dict]:
    """El código de un solo uso se cambia por el token permanente de esa tienda."""
    try:
        r = requests.post(
            f"https://{dominio}/admin/oauth/access_token",
            json={
                "client_id": os.getenv("SHOPIFY_API_KEY", ""),
                "client_secret": os.getenv("SHOPIFY_API_SECRET", ""),
                "code": code,
            },
            timeout=20,
        )
        if r.status_code != 200:
            print(f"[shopify] canje de token falló {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[shopify] excepción canjeando token: {e}")
        return None


def guardar_instalacion(dominio: str, access_token: str, scopes: str = "") -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shopify_instalaciones (dominio, access_token, scopes)
                VALUES (%s, %s, %s)
                ON CONFLICT (dominio) DO UPDATE
                    SET access_token = EXCLUDED.access_token,
                        scopes = EXCLUDED.scopes
            """, (dominio, access_token, scopes))
        conn.commit()


def instalacion(dominio: str) -> Optional[dict]:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM shopify_instalaciones WHERE dominio = %s", (dominio,))
            row = cur.fetchone()
            return dict(row) if row else None


def vincular_cliente(dominio: str, cliente_id: str) -> None:
    """
    Ata la tienda instalada a la cuenta TAURO del comerciante.

    Además la registra en `tiendas_conectadas`, que es la tabla que usa
    todo el resto del portal (pedidos pendientes, lista de tiendas,
    política de flete). Así la app y el modo manual conviven sin que
    nada más tenga que saber por dónde entró cada tienda. El "secreto"
    guardado es el API secret de la app, porque con eso firma Shopify
    los webhooks de las apps.
    """
    _ensure_tabla()
    dominio = (dominio or "").strip().lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shopify_instalaciones SET cliente_id = %s WHERE dominio = %s",
                (cliente_id, dominio),
            )
        conn.commit()

    secreto = os.getenv("SHOPIFY_API_SECRET", "").strip()
    if not secreto:
        return
    try:
        from servicios.integraciones_tienda import conectar_tienda
        conectar_tienda(cliente_id, "shopify", dominio, secreto)
    except Exception as e:
        print(f"[shopify] no pude registrar {dominio} en tiendas_conectadas: {e}")


def instalaciones_sin_dueno() -> list[dict]:
    """Tiendas que instalaron la app pero todavía no se ataron a una cuenta."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT dominio, instalada_en FROM shopify_instalaciones
                WHERE cliente_id IS NULL OR cliente_id = ''
                ORDER BY instalada_en DESC
            """)
            return [dict(r) for r in cur.fetchall()]


def desinstalar(dominio: str) -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM shopify_instalaciones WHERE dominio = %s", (dominio,))
        conn.commit()


# ── Llamadas a la API de la tienda ──────────────────────────

def _api(dominio: str, token: str, metodo: str, path: str, payload: dict | None = None):
    url = f"https://{dominio}/admin/api/{API_VERSION}/{path.lstrip('/')}"
    try:
        r = requests.request(
            metodo, url,
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json=payload, timeout=25,
        )
        return r
    except Exception as e:
        print(f"[shopify] error llamando {metodo} {path}: {e}")
        return None


def registrar_webhooks(dominio: str, token: str) -> list[str]:
    """
    Al instalar, damos de alta los webhooks solos. El comerciante no
    configura nada a mano (esa es la diferencia con el modo manual).
    """
    base = _base_url()
    topics = [
        ("orders/create", f"{base}/integraciones/shopify/webhook"),
        ("orders/updated", f"{base}/integraciones/shopify/webhook"),
        ("app/uninstalled", f"{base}/shopify/webhook/desinstalada"),
    ]
    ok = []
    for topic, address in topics:
        r = _api(dominio, token, "POST", "webhooks.json", {
            "webhook": {"topic": topic, "address": address, "format": "json"}
        })
        if r is not None and r.status_code in (200, 201):
            ok.append(topic)
        elif r is not None and r.status_code == 422 and "already been taken" in r.text:
            ok.append(topic)  # ya existía: la reinstalación no debe fallar
        else:
            print(f"[shopify] webhook {topic} no quedó: {r.status_code if r else 'sin respuesta'}")
    return ok


def registrar_carrier_service(dominio: str, token: str) -> Optional[str]:
    """
    Registra a TAURO como transportista para que el comprador vea la
    tarifa en vivo en el checkout. Shopify sólo lo habilita en planes
    Advanced/Plus: si la tienda no califica devuelve error y seguimos
    andando igual (la tienda usa su envío fijo).
    """
    r = _api(dominio, token, "POST", "carrier_services.json", {
        "carrier_service": {
            "name": "TAURO Solutions",
            "callback_url": f"{_base_url()}/shopify/tarifas",
            "service_discovery": True,
            "format": "json",
        }
    })
    if r is None:
        return None
    if r.status_code in (200, 201):
        try:
            cid = str(r.json()["carrier_service"]["id"])
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shopify_instalaciones SET carrier_id = %s WHERE dominio = %s",
                        (cid, dominio),
                    )
                conn.commit()
            return cid
        except Exception:
            return None
    print(f"[shopify] carrier service no disponible para {dominio} "
          f"({r.status_code}) — la tienda usa su tarifa fija")
    return None


def marcar_enviado(dominio: str, pedido_externo_id: str, tracking: str,
                   courier: str = "FedEx") -> bool:
    """
    Cierra el círculo: cuando TAURO emite la guía, el pedido queda
    "Enviado" en Shopify con su número de seguimiento, y Shopify le
    manda solo el mail al comprador.
    """
    inst = instalacion(dominio)
    if not inst:
        return False
    token = inst["access_token"]

    # 1) Qué se puede despachar de ese pedido
    r = _api(dominio, token, "GET", f"orders/{pedido_externo_id}/fulfillment_orders.json")
    if r is None or r.status_code != 200:
        print(f"[shopify] no pude leer fulfillment_orders de {pedido_externo_id}")
        return False
    try:
        fos = [fo for fo in r.json().get("fulfillment_orders", [])
               if fo.get("status") in ("open", "in_progress")]
    except Exception:
        return False
    if not fos:
        return False

    url_track = f"https://www.fedex.com/fedextrack/?trknbr={tracking}" if courier.upper() == "FEDEX" else None

    r2 = _api(dominio, token, "POST", "fulfillments.json", {
        "fulfillment": {
            "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo["id"]} for fo in fos],
            "tracking_info": {
                "number": tracking,
                "company": courier,
                **({"url": url_track} if url_track else {}),
            },
            "notify_customer": True,
        }
    })
    if r2 is not None and r2.status_code in (200, 201):
        print(f"[shopify] pedido {pedido_externo_id} marcado enviado con tracking {tracking}")
        return True
    print(f"[shopify] no pude marcar enviado {pedido_externo_id}: "
          f"{r2.status_code if r2 else 'sin respuesta'}")
    return False


# ── Tarifas para el checkout ────────────────────────────────

def cotizar_para_checkout(payload: dict) -> dict:
    """
    Shopify nos manda el carrito y el destino; devolvemos las opciones
    de envío que verá el comprador. Formato fijo de Shopify: el precio
    va en centavos y como string.
    """
    from servicios.carriers import cotizar_carriers
    from servicios.politica_envio import (
        obtener_config, aplicar_politica, calcular_tax_estimado,
    )

    rate = payload.get("rate") or {}
    destino = rate.get("destination") or {}
    items = rate.get("items") or []
    dominio = (payload.get("_dominio") or "").strip().lower()

    pais = (destino.get("country") or "").upper()
    if not pais or pais == "AR":
        return {"rates": []}   # nacional no va por esta vía

    # Shopify manda gramos por unidad
    peso_kg = sum((it.get("grams") or 0) * (it.get("quantity") or 1) for it in items) / 1000.0
    peso_kg = max(round(peso_kg, 2), 0.5)
    valor_usd = max(round(sum((it.get("price") or 0) * (it.get("quantity") or 1)
                              for it in items) / 100.0, 2), 1.0)

    origen = {
        "street": "Av. Corrientes 1234", "city": "BUENOS AIRES",
        "state": "B", "postal_code": "1043", "country": "AR",
    }
    dest = {
        "city": (destino.get("city") or "")[:35],
        "state": (destino.get("province") or "")[:35],
        "postal_code": (destino.get("postal_code") or "")[:12],
        "country": pais,
    }
    paquete = {
        "peso_kg": peso_kg, "largo": 30, "ancho": 20, "alto": 15,
        "valor_declarado_usd": valor_usd, "descripcion_en": "Merchandise",
    }

    dolar = float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))
    markup = float(os.getenv("WEB_MARKUP_PCT", "20"))

    # CASCADA DE RESILIENCIA — Shopify corta a los ~10s y un checkout sin
    # opción de envío es una venta perdida. Por eso acá NUNCA se sale con
    # las manos vacías y NUNCA se espera a un courier si se puede evitar.
    from servicios.tarifas_cache import buscar_tarifas, tarifa_emergencia

    opciones = []
    try:
        opciones = buscar_tarifas(pais, peso_kg)          # 1. instantáneo
    except Exception as e:
        print(f"[shopify] cache de tarifas falló: {e}")

    if not opciones:
        try:
            opciones = cotizar_carriers(origen, dest, paquete, dolar, markup)  # 2. en vivo
        except Exception as e:
            print(f"[shopify] cotización en vivo falló: {e}")
            opciones = []

    if not [c for c in opciones if c.get("estado") == "cotizado"]:
        print(f"[shopify] sin tarifas para {pais}/{peso_kg}kg → tarifa de emergencia")
        opciones = tarifa_emergencia(pais, peso_kg, dolar)   # 3. nunca vacío

    # Qué ve el comprador lo decide el comerciante (política de flete):
    # tarifa real, +markup, precio fijo, o gratis. Y si quiere, con los
    # impuestos de destino estimados incluidos.
    config = obtener_config(dominio) if dominio else None
    if config is None:
        from servicios.politica_envio import DEFAULTS
        config = dict(DEFAULTS)

    inst = instalacion(dominio) if dominio else None
    cliente_id = (inst or {}).get("cliente_id") or ""
    tax_ars = round(calcular_tax_estimado(cliente_id, items, config, dolar))

    etiqueta = (config.get("etiqueta") or "").strip()

    rates = []
    for c in opciones:
        if c.get("estado") != "cotizado":
            continue
        precio = aplicar_politica(float(c["precio_ars"]), config)
        if precio is None:
            continue

        detalle = f"Entrega estimada {c.get('dias_estimados', '3-5')} días hábiles · vía TAURO Solutions"
        # El impuesto NO se suma cuando el comerciante decidió el precio a
        # mano: si eligió "gratis" el envío tiene que salir $0 (si no, deja
        # de ser gratis), y si eligió un precio fijo tiene que ser ese.
        if tax_ars and config.get("politica") not in ("gratis", "fijo"):
            precio += tax_ars
            detalle += " · impuestos de destino estimados incluidos"

        rates.append({
            "service_name": etiqueta or f"{c['nombre']} — {c['servicio']}",
            "service_code": f"TAURO_{c['id'].upper()}",
            "total_price": str(int(round(precio * 100))),
            "currency": "ARS",
            "description": detalle,
        })

        # Con precio fijo o gratis, todas las opciones costarían lo mismo:
        # mostramos una sola para no confundir al comprador.
        if config.get("politica") in ("fijo", "gratis"):
            break

    return {"rates": rates}


def nuevo_state() -> str:
    return secrets.token_urlsafe(24)
