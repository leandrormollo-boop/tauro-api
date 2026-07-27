# ============================================================
# Integraciones con tiendas (Shopify / Tiendanube)
# ============================================================
# El flujo completo:
#   1. El cliente conecta su tienda desde /portal/integraciones
#      (guarda dominio + secreto de firma de webhooks).
#   2. La tienda dispara un webhook por cada venta →
#      /integraciones/shopify/webhook verifica la firma HMAC y
#      guarda el pedido como PENDIENTE.
#   3. El cliente ve sus pedidos pendientes en el portal y con un
#      click los convierte en solicitud de guía (form prellenado).
#
# Las tablas se crean acá mismo (CREATE TABLE IF NOT EXISTS) para
# no depender de migraciones manuales: el primer webhook o la
# primera visita a la pantalla las materializa.
# ============================================================
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Optional

from core.database import get_conn

_tablas_listas = False


def _ensure_tablas() -> None:
    global _tablas_listas
    if _tablas_listas:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tiendas_conectadas (
                    id          SERIAL PRIMARY KEY,
                    cliente_id  TEXT NOT NULL,
                    plataforma  TEXT NOT NULL,
                    dominio     TEXT NOT NULL UNIQUE,
                    secreto     TEXT NOT NULL,
                    activa      BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS pedidos_tienda (
                    id                 SERIAL PRIMARY KEY,
                    cliente_id         TEXT NOT NULL,
                    tienda_id          INTEGER REFERENCES tiendas_conectadas(id) ON DELETE CASCADE,
                    plataforma         TEXT NOT NULL,
                    pedido_externo_id  TEXT NOT NULL,
                    numero             TEXT,
                    estado             TEXT NOT NULL DEFAULT 'PENDIENTE',
                    destinatario       JSONB,
                    items              JSONB,
                    valor_total        NUMERIC(14,2),
                    moneda             TEXT,
                    solicitud_id       INTEGER,
                    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (tienda_id, pedido_externo_id)
                );
                CREATE INDEX IF NOT EXISTS ix_pedidos_tienda_cliente
                    ON pedidos_tienda (cliente_id, estado);
            """)
        conn.commit()
    _tablas_listas = True


# ── Tiendas conectadas ──────────────────────────────────────

def conectar_tienda(cliente_id: str, plataforma: str, dominio: str, secreto: str) -> dict:
    _ensure_tablas()
    plataforma = plataforma.strip().lower()
    if plataforma not in ("shopify", "tiendanube"):
        return {"ok": False, "error": "Plataforma inválida."}

    dominio = dominio.strip().lower().replace("https://", "").replace("http://", "").strip("/ ")
    if not dominio or "." not in dominio:
        return {"ok": False, "error": "El dominio no parece válido (ej: mitienda.myshopify.com)."}
    if not secreto or len(secreto.strip()) < 8:
        return {"ok": False, "error": "El secreto de firma es demasiado corto — copialo completo desde tu tienda."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Un dominio pertenece a UN cliente: si ya existe y es de otro, error.
            cur.execute("SELECT cliente_id FROM tiendas_conectadas WHERE dominio = %s", (dominio,))
            row = cur.fetchone()
            if row and row["cliente_id"] != cliente_id:
                return {"ok": False, "error": "Ese dominio ya está conectado a otra cuenta."}
            cur.execute("""
                INSERT INTO tiendas_conectadas (cliente_id, plataforma, dominio, secreto, activa)
                VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (dominio) DO UPDATE
                    SET secreto = EXCLUDED.secreto,
                        plataforma = EXCLUDED.plataforma,
                        activa = TRUE
                RETURNING id
            """, (cliente_id, plataforma, dominio, secreto.strip()))
            tienda_id = cur.fetchone()["id"]
        conn.commit()
    return {"ok": True, "tienda_id": tienda_id}


def listar_tiendas(cliente_id: str) -> list[dict]:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, plataforma, dominio, activa, created_at
                FROM tiendas_conectadas
                WHERE cliente_id = %s
                ORDER BY id
            """, (cliente_id,))
            return [dict(r) for r in cur.fetchall()]


def desconectar_tienda(cliente_id: str, tienda_id: int) -> None:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tiendas_conectadas SET activa = FALSE WHERE id = %s AND cliente_id = %s",
                (tienda_id, cliente_id),
            )
        conn.commit()


def tienda_por_dominio(dominio: str) -> Optional[dict]:
    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tiendas_conectadas WHERE dominio = %s AND activa = TRUE",
                (dominio,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


# ── Firma de webhooks ───────────────────────────────────────

def verificar_hmac_shopify(secreto: str, cuerpo: bytes, firma_header: str) -> bool:
    """Shopify firma el cuerpo crudo con HMAC-SHA256 y lo manda en base64."""
    if not firma_header:
        return False
    calculada = base64.b64encode(
        hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(calculada, firma_header.strip())


def verificar_hmac_tiendanube(secreto: str, cuerpo: bytes, firma_header: str) -> bool:
    """Tiendanube firma el cuerpo crudo con HMAC-SHA256 en hexadecimal."""
    if not firma_header:
        return False
    calculada = hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculada, firma_header.strip().lower())


# ── Parseo de pedidos ───────────────────────────────────────

def parsear_pedido_shopify(order: dict) -> Optional[dict]:
    """
    Reduce el JSON gigante de una orden de Shopify a lo que TAURO necesita
    para armar el envío. Devuelve None si no hay dirección de envío
    (pedidos digitales / retiro en local no nos competen).
    """
    ship = order.get("shipping_address") or {}
    if not ship.get("address1"):
        return None

    items = [
        {
            "titulo": (it.get("title") or "")[:180],
            "cantidad": int(it.get("quantity") or 1),
            "precio": it.get("price"),
            "sku": (it.get("sku") or "")[:80],
            "peso_gr": it.get("grams"),
        }
        for it in (order.get("line_items") or [])
    ]

    nombre = (ship.get("name")
              or f"{ship.get('first_name', '')} {ship.get('last_name', '')}".strip())

    return {
        "pedido_externo_id": str(order.get("id") or ""),
        "numero": str(order.get("name") or order.get("order_number") or ""),
        "destinatario": {
            "nombre": nombre[:160],
            "email": (order.get("email") or order.get("contact_email") or "")[:160],
            "telefono": (ship.get("phone") or order.get("phone") or "")[:40],
            "direccion": " ".join(x for x in [ship.get("address1"), ship.get("address2")] if x)[:300],
            "ciudad": (ship.get("city") or "")[:120],
            "estado": (ship.get("province") or "")[:120],
            "cp": (ship.get("zip") or "")[:24],
            "pais": (ship.get("country_code") or "")[:3],
        },
        "items": items,
        "valor_total": order.get("total_price"),
        "moneda": (order.get("currency") or "")[:6],
    }


# ── Pedidos pendientes ──────────────────────────────────────

def guardar_pedido(cliente_id: str, tienda_id: int, plataforma: str, pedido: dict) -> bool:
    """Idempotente: si el webhook llega dos veces, la segunda no duplica."""
    _ensure_tablas()
    if not pedido.get("pedido_externo_id"):
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pedidos_tienda
                    (cliente_id, tienda_id, plataforma, pedido_externo_id, numero,
                     destinatario, items, valor_total, moneda)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tienda_id, pedido_externo_id) DO NOTHING
                RETURNING id
            """, (
                cliente_id, tienda_id, plataforma,
                pedido["pedido_externo_id"], pedido.get("numero"),
                json.dumps(pedido.get("destinatario") or {}, ensure_ascii=False),
                json.dumps(pedido.get("items") or [], ensure_ascii=False),
                pedido.get("valor_total"), pedido.get("moneda"),
            ))
            creado = cur.fetchone() is not None
        conn.commit()
    return creado


def listar_pedidos(cliente_id: str, estado: str = "PENDIENTE", limite: int = 100) -> list[dict]:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM pedidos_tienda
                WHERE cliente_id = %s AND estado = %s
                ORDER BY id DESC
                LIMIT %s
            """, (cliente_id, estado, limite))
            return [dict(r) for r in cur.fetchall()]


def contar_pendientes(cliente_id: str) -> int:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM pedidos_tienda WHERE cliente_id = %s AND estado = 'PENDIENTE'",
                (cliente_id,),
            )
            return int(cur.fetchone()["n"])


def obtener_pedido(cliente_id: str, pedido_id: int) -> Optional[dict]:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pedidos_tienda WHERE id = %s AND cliente_id = %s",
                (pedido_id, cliente_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def marcar_convertido(cliente_id: str, pedido_id: int) -> None:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pedidos_tienda SET estado = 'CONVERTIDO'
                WHERE id = %s AND cliente_id = %s AND estado = 'PENDIENTE'
            """, (pedido_id, cliente_id))
        conn.commit()


def descartar_pedido(cliente_id: str, pedido_id: int) -> None:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pedidos_tienda SET estado = 'DESCARTADO'
                WHERE id = %s AND cliente_id = %s AND estado = 'PENDIENTE'
            """, (pedido_id, cliente_id))
        conn.commit()
