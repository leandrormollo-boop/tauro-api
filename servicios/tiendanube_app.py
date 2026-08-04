# ============================================================
# App de TAURO para Tiendanube
# ============================================================
# Diferencias con Shopify que importan:
#   · El OAuth devuelve un token que NO vence y un `user_id` que es el ID
#     numérico de la tienda (Shopify usa el dominio).
#   · Los webhooks se firman con HMAC-SHA256 en HEXADECIMAL (Shopify usa
#     base64) y con el client_secret de la app.
#   · La API exige cabecera `Authentication: bearer <token>` (así, con esa
#     grafía) y un User-Agent identificable, o rechaza la llamada.
#
# Se enciende sola cuando estén TIENDANUBE_CLIENT_ID y
# TIENDANUBE_CLIENT_SECRET en Railway → Variables.
# ============================================================
from __future__ import annotations

import os
from typing import Optional

import requests

from core.database import get_conn

API_BASE = "https://api.tiendanube.com/v1"
OAUTH_URL = "https://www.tiendanube.com/apps/authorize/token"
USER_AGENT = "TAURO Solutions (cotizaciones@taurosolutions.ar)"

_tabla_lista = False


def _ensure_tabla() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tiendanube_instalaciones (
                    id           SERIAL PRIMARY KEY,
                    store_id     TEXT NOT NULL UNIQUE,
                    access_token TEXT NOT NULL,
                    cliente_id   TEXT,
                    nombre       TEXT,
                    instalada_en TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
        conn.commit()
    _tabla_lista = True


def app_configurada() -> bool:
    return bool(os.getenv("TIENDANUBE_CLIENT_ID") and os.getenv("TIENDANUBE_CLIENT_SECRET"))


def url_instalacion(state: str = "") -> str:
    """
    Link para que un comerciante instale la app desde su Tiendanube. El `state`
    (si se pasa) viaja a la vuelta para atar la instalación al navegador que
    inició el flujo — anti-CSRF de la vinculación.
    """
    cid = os.getenv("TIENDANUBE_CLIENT_ID", "")
    url = f"https://www.tiendanube.com/apps/{cid}/authorize"
    if state:
        from urllib.parse import quote
        url += f"?state={quote(state)}"
    return url


def canjear_token(code: str) -> Optional[dict]:
    """El `code` del redirect se cambia por un token permanente."""
    try:
        r = requests.post(OAUTH_URL, json={
            "client_id": os.getenv("TIENDANUBE_CLIENT_ID", ""),
            "client_secret": os.getenv("TIENDANUBE_CLIENT_SECRET", ""),
            "grant_type": "authorization_code",
            "code": code,
        }, headers={"User-Agent": USER_AGENT}, timeout=25)
        if r.status_code != 200:
            print(f"[tiendanube] canje de token falló {r.status_code}: {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[tiendanube] error canjeando token: {e}")
        return None


def guardar_instalacion(store_id: str, access_token: str, nombre: str = "") -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tiendanube_instalaciones (store_id, access_token, nombre)
                VALUES (%s, %s, %s)
                ON CONFLICT (store_id) DO UPDATE
                    SET access_token = EXCLUDED.access_token,
                        nombre = COALESCE(NULLIF(EXCLUDED.nombre, ''),
                                          tiendanube_instalaciones.nombre)
            """, (str(store_id), access_token, nombre))
        conn.commit()


def instalacion(store_id: str) -> Optional[dict]:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tiendanube_instalaciones WHERE store_id = %s",
                        (str(store_id),))
            row = cur.fetchone()
            return dict(row) if row else None


def vincular_cliente(store_id: str, cliente_id: str) -> None:
    """
    Ata la tienda a la cuenta TAURO y la registra en `tiendas_conectadas`,
    que es la tabla que lee todo el portal — así Tiendanube y Shopify
    conviven sin que el resto del sistema tenga que distinguirlos.
    """
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tiendanube_instalaciones SET cliente_id = %s WHERE store_id = %s",
                (cliente_id, str(store_id)),
            )
        conn.commit()

    secreto = os.getenv("TIENDANUBE_CLIENT_SECRET", "").strip()
    if not secreto:
        return
    try:
        from servicios.integraciones_tienda import conectar_tienda
        # El "dominio" con el que se identifica en tiendas_conectadas: el
        # store_id de Tiendanube, que es lo que viaja en sus webhooks.
        conectar_tienda(cliente_id, "tiendanube", f"{store_id}.tiendanube", secreto)
    except Exception as e:
        print(f"[tiendanube] no pude registrar la tienda {store_id}: {e}")


def _api(store_id: str, token: str, metodo: str, path: str,
         payload: dict | None = None, timeout: int = 20):
    url = f"{API_BASE}/{store_id}/{path.lstrip('/')}"
    headers = {
        "Authentication": f"bearer {token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    try:
        return requests.request(metodo, url, headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        print(f"[tiendanube] {metodo} {path} falló: {e}")
        return None


def registrar_webhooks(store_id: str, token: str) -> list[str]:
    """Alta automática de los webhooks al instalar."""
    base = (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
    eventos = [
        ("order/created", f"{base}/integraciones/tiendanube/webhook"),
        ("order/updated", f"{base}/integraciones/tiendanube/webhook"),
        ("order/cancelled", f"{base}/integraciones/tiendanube/webhook"),
        ("app/uninstalled", f"{base}/integraciones/tiendanube/webhook"),
    ]
    ok = []
    for evento, url in eventos:
        r = _api(store_id, token, "POST", "webhooks",
                 {"event": evento, "url": url})
        if r is not None and r.status_code in (200, 201):
            ok.append(evento)
        elif r is not None and r.status_code == 422:
            ok.append(evento)   # ya existía: reinstalar no debe fallar
        else:
            print(f"[tiendanube] webhook {evento} no quedó: "
                  f"{r.status_code if r is not None else 'sin respuesta'}")
    return ok


def datos_tienda(store_id: str, token: str) -> dict:
    r = _api(store_id, token, "GET", "store")
    if r is not None and r.status_code == 200:
        try:
            return r.json()
        except Exception:
            pass
    return {}


def parsear_pedido(order: dict) -> Optional[dict]:
    """
    Reduce un pedido de Tiendanube a lo que TAURO necesita. Devuelve None
    si no hay dirección de envío (retiro en local / producto digital).
    """
    envio = order.get("shipping_address") or {}
    if not envio.get("address"):
        return None

    items = [
        {
            "titulo": (it.get("name") or "")[:180],
            "cantidad": int(it.get("quantity") or 1),
            "precio": it.get("price"),
            "sku": (it.get("sku") or "")[:80],
            "peso_gr": int(float(it.get("weight") or 0) * 1000) or None,
        }
        for it in (order.get("products") or [])
    ]

    nombre = (envio.get("name")
              or f"{envio.get('first_name', '')} {envio.get('last_name', '')}".strip()
              or (order.get("customer") or {}).get("name", ""))

    # Número + piso van separados: fusionarlos hace que el courier los
    # trunque y el paquete llegue al edificio pero no al departamento.
    calle = " ".join(x for x in [envio.get("address"), str(envio.get("number") or "")] if x).strip()

    # Flete que le cobró la tienda al comprador: la misma regla de negocio que
    # en Shopify (comparar lo cobrado vs lo que sale la guía). Tiendanube lo
    # trae en shipping_cost_customer, con el método en shipping_option.
    try:
        flete_cobrado = round(float(order.get("shipping_cost_customer") or 0), 2)
    except (TypeError, ValueError):
        flete_cobrado = 0.0
    flete_detalle = []
    if flete_cobrado or order.get("shipping_option"):
        flete_detalle.append({
            "titulo": (order.get("shipping_option") or "Envío")[:120],
            "codigo": (order.get("shipping") or "")[:80],
            "precio": flete_cobrado,
        })

    # Provincia: Tiendanube manda el nombre (no un código de 2 letras como
    # Shopify). Para envíos nacionales AR el nombre es lo correcto; si algún día
    # se despacha internacional desde una venta Tiendanube, revisar el mapeo a
    # código que piden los couriers (FL, CA…).
    return {
        "pedido_externo_id": str(order.get("id") or ""),
        "numero": str(order.get("number") or order.get("id") or ""),
        "destinatario": {
            "nombre": nombre[:160],
            "empresa": "",
            "email": ((order.get("customer") or {}).get("email") or "")[:160],
            "telefono": (envio.get("phone")
                         or (order.get("customer") or {}).get("phone") or "")[:40],
            "direccion": calle[:300],
            "direccion2": (envio.get("floor") or "")[:150],
            "ciudad": (envio.get("city") or "")[:120],
            "estado": (envio.get("province") or "")[:120],
            "cp": (envio.get("zipcode") or "")[:24],
            "pais": (envio.get("country") or "")[:3],
        },
        "items": items,
        "valor_total": order.get("total"),
        "moneda": (order.get("currency") or "")[:6],
        "flete_cobrado": flete_cobrado,
        "flete_detalle": flete_detalle,
        "cancelado": bool(order.get("cancelled_at")),
        "estado_pago": (order.get("payment_status") or "")[:30],
    }


def marcar_enviado(store_id: str, pedido_externo_id: str, tracking: str,
                   url_tracking: str = "") -> bool:
    """
    Cierra el ciclo: el pedido pasa a enviado en Tiendanube y el comprador
    recibe su seguimiento sin que el comerciante toque nada.
    """
    inst = instalacion(store_id)
    if not inst or not inst.get("access_token"):
        return False
    payload = {"status": "shipped", "shipping_tracking_number": tracking}
    if url_tracking:
        payload["shipping_tracking_url"] = url_tracking
    r = _api(store_id, inst["access_token"], "PUT",
             f"orders/{pedido_externo_id}", payload)
    ok = r is not None and r.status_code in (200, 201)
    if not ok:
        print(f"[tiendanube] no pude marcar enviado {pedido_externo_id}: "
              f"{r.status_code if r is not None else 'sin respuesta'}")
    return ok


def desinstalar(store_id: str) -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tiendanube_instalaciones WHERE store_id = %s",
                        (str(store_id),))
        conn.commit()
