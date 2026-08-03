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
                -- Lo que el comprador REALMENTE pagó de envío en el checkout.
                -- Sin esto no hay forma de comparar lo cobrado contra lo que
                -- termina costando la guía: si el precio se calcula mal, se
                -- pierde plata en cada venta y no queda rastro para notarlo.
                ALTER TABLE pedidos_tienda
                    ADD COLUMN IF NOT EXISTS flete_cobrado NUMERIC(14,2);
                ALTER TABLE pedidos_tienda
                    ADD COLUMN IF NOT EXISTS flete_detalle JSONB;
                -- Por qué este pedido no se convirtió solo en solicitud de
                -- guía (SKU sin catálogo, sin remitente, país sin ruta). Se le
                -- muestra al comerciante: "no se armó" a secas no le dice
                -- qué tiene que corregir.
                ALTER TABLE pedidos_tienda
                    ADD COLUMN IF NOT EXISTS motivo_pendiente TEXT;
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

    # province_code ANTES que province: los couriers piden el código de dos
    # letras ("FL"), no el nombre ("Florida"). Mandar el nombre hace que
    # FedEx rechace la guía y el envío quede trabado.
    estado = (ship.get("province_code") or ship.get("province") or "")

    # address2 (piso/depto) va aparte: fusionarla en una sola línea larga
    # hace que el courier la trunque y el paquete llegue al edificio pero
    # no al departamento.
    direccion = (ship.get("address1") or "").strip()
    direccion2 = (ship.get("address2") or "").strip()

    # FLETE COBRADO: lo que el comprador pagó de envío. Es el único dato que
    # permite después comparar lo cobrado contra lo que costó la guía real.
    # Puede haber más de una línea (envío + seguro, por ejemplo), así que se
    # suman todas y se guarda el detalle para poder auditar de dónde salió.
    lineas_flete = order.get("shipping_lines") or []
    flete_total = 0.0
    detalle_flete = []
    for ln in lineas_flete:
        try:
            monto = float(ln.get("price") or 0)
        except (TypeError, ValueError):
            monto = 0.0
        flete_total += monto
        detalle_flete.append({
            "titulo": (ln.get("title") or "")[:120],
            # `code` es el service_code que devolvió TAURO (TAURO_FEDEX, etc.):
            # sirve para saber si el envío se cotizó con nuestra app o si el
            # comerciante usó otra tarifa suya.
            "codigo": (ln.get("code") or "")[:80],
            "precio": monto,
        })

    return {
        "pedido_externo_id": str(order.get("id") or ""),
        "numero": str(order.get("name") or order.get("order_number") or ""),
        "destinatario": {
            "nombre": nombre[:160],
            # company: en ventas B2B la razón social es lo que figura en
            # recepción; sin esto el paquete puede rebotar.
            "empresa": (ship.get("company") or "")[:160],
            "email": (order.get("email") or order.get("contact_email") or "")[:160],
            "telefono": (ship.get("phone") or order.get("phone") or "")[:40],
            "direccion": direccion[:300],
            "direccion2": direccion2[:150],
            "ciudad": (ship.get("city") or "")[:120],
            "estado": estado[:120],
            "cp": (ship.get("zip") or "")[:24],
            "pais": (ship.get("country_code") or "")[:3],
        },
        "items": items,
        "valor_total": order.get("total_price"),
        "moneda": (order.get("currency") or "")[:6],
        "flete_cobrado": round(flete_total, 2),
        "flete_detalle": detalle_flete,
        # Estado comercial: un pedido cancelado o sin pagar no se despacha.
        "cancelado": bool(order.get("cancelled_at")),
        "estado_pago": (order.get("financial_status") or "")[:30],
    }


# ── Pedidos pendientes ──────────────────────────────────────

def guardar_pedido(cliente_id: str, tienda_id: int, plataforma: str, pedido: dict) -> bool:
    """
    Guarda o ACTUALIZA un pedido de la tienda.

    Idempotente por (tienda, pedido): un webhook repetido no duplica. Y si
    llega un `orders/updated` —el comprador corrigió su dirección, cosa que
    pasa seguido— los datos nuevos pisan a los viejos MIENTRAS el pedido
    siga pendiente. Si ya se convirtió en envío no se toca: la guía se
    emitió con los datos de ese momento y cambiarlos a mano confundiría.
    """
    _ensure_tablas()
    if not pedido.get("pedido_externo_id"):
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pedidos_tienda
                    (cliente_id, tienda_id, plataforma, pedido_externo_id, numero,
                     destinatario, items, valor_total, moneda,
                     flete_cobrado, flete_detalle)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tienda_id, pedido_externo_id) DO UPDATE SET
                    destinatario  = EXCLUDED.destinatario,
                    items         = EXCLUDED.items,
                    valor_total   = EXCLUDED.valor_total,
                    moneda        = EXCLUDED.moneda,
                    flete_cobrado = EXCLUDED.flete_cobrado,
                    flete_detalle = EXCLUDED.flete_detalle,
                    numero        = EXCLUDED.numero
                WHERE pedidos_tienda.estado = 'PENDIENTE'
                RETURNING (xmax = 0) AS es_nuevo
            """, (
                cliente_id, tienda_id, plataforma,
                pedido["pedido_externo_id"], pedido.get("numero"),
                json.dumps(pedido.get("destinatario") or {}, ensure_ascii=False),
                json.dumps(pedido.get("items") or [], ensure_ascii=False),
                pedido.get("valor_total"), pedido.get("moneda"),
                pedido.get("flete_cobrado"),
                json.dumps(pedido.get("flete_detalle") or [], ensure_ascii=False),
            ))
            fila = cur.fetchone()
            creado = bool(fila and fila.get("es_nuevo"))
        conn.commit()
    return creado


def guardar_pedido_huerfano(dominio: str, cuerpo: bytes) -> None:
    """
    Venta de una tienda instalada pero SIN vincular a una cuenta TAURO.

    Se guarda el pedido crudo para no perder la venta: cuando el comerciante
    vincule su tienda desde el portal, `volcar_huerfanos` los pasa a
    pedidos_tienda como si hubieran entrado normalmente. Idempotente por
    (dominio, pedido): Shopify reintenta.
    """
    import json as _json

    _ensure_tablas()
    try:
        orden = _json.loads(cuerpo.decode("utf-8"))
    except Exception:
        return
    pedido_id = str(orden.get("id") or "")
    if not pedido_id:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pedidos_huerfanos (
                    id                SERIAL PRIMARY KEY,
                    dominio           TEXT NOT NULL,
                    pedido_externo_id TEXT NOT NULL,
                    payload           JSONB NOT NULL,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (dominio, pedido_externo_id)
                );
            """)
            cur.execute("""
                INSERT INTO pedidos_huerfanos (dominio, pedido_externo_id, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (dominio, pedido_externo_id) DO UPDATE
                    SET payload = EXCLUDED.payload
            """, (dominio.strip().lower(), pedido_id,
                  _json.dumps(orden, ensure_ascii=False)))
            # RETENCIÓN ACOTADA. El payload es la orden entera: nombre,
            # dirección y teléfono del comprador final. Una tienda que probó
            # la app, no se vinculó nunca y desinstaló nos dejaba esos datos
            # guardados para siempre. A los 90 días la venta ya no se puede
            # despachar, así que no hay motivo para conservarla.
            cur.execute("""
                DELETE FROM pedidos_huerfanos
                WHERE created_at < NOW() - INTERVAL '90 days'
            """)


def volcar_huerfanos(cliente_id: str, tienda_id: int, dominio: str) -> int:
    """
    Al vincular una tienda, recupera las ventas que entraron mientras estaba
    huérfana. Devuelve cuántas se volcaron. Un fallo acá no puede romper la
    vinculación: se loguea y sigue.
    """
    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    volcados = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT pedido_externo_id, payload FROM pedidos_huerfanos
                    WHERE dominio = %s
                      AND created_at > NOW() - INTERVAL '90 days'
                    ORDER BY created_at
                    LIMIT 500
                """, (dominio,))
                filas = cur.fetchall()
    except Exception as e:
        print(f"[integraciones] no pude leer huérfanos de {dominio}: {e}")
        return 0

    for f in filas:
        try:
            pedido = parsear_pedido_shopify(f["payload"])
            if not pedido or pedido.get("cancelado"):
                continue
            if guardar_pedido(cliente_id, tienda_id, "shopify", pedido):
                volcados += 1
        except Exception as e:
            print(f"[integraciones] huérfano {f['pedido_externo_id']} no se pudo volcar: {e}")

    if filas:
        # Se borra SÓLO lo que se acaba de procesar (más lo vencido). Si la
        # tienda tenía más de 500 huérfanos, la tanda que quedó afuera sigue
        # ahí para el próximo vínculo en vez de desaparecer sin que nadie
        # se entere.
        procesados = [f["pedido_externo_id"] for f in filas]
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        DELETE FROM pedidos_huerfanos
                        WHERE dominio = %s
                          AND (pedido_externo_id = ANY(%s)
                               OR created_at < NOW() - INTERVAL '90 days')
                    """, (dominio, procesados))
                    cur.execute("SELECT COUNT(*) AS n FROM pedidos_huerfanos WHERE dominio = %s",
                                (dominio,))
                    quedan = cur.fetchone()["n"]
            if quedan:
                print(f"[integraciones] {dominio}: quedan {quedan} huérfano(s) sin volcar "
                      f"(tope de 500 por tanda) — se vuelcan al próximo intento")
        except Exception as e:
            print(f"[integraciones] no pude limpiar huérfanos de {dominio}: {e}")

    if volcados:
        print(f"[integraciones] {dominio}: {volcados} venta(s) recuperada(s) al vincular")
    return volcados


def id_de_pedido(tienda_id: int, pedido_externo_id: str) -> Optional[int]:
    """Id interno de un pedido, para poder armarle la solicitud automática."""
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM pedidos_tienda
                WHERE tienda_id = %s AND pedido_externo_id = %s
            """, (tienda_id, str(pedido_externo_id)))
            row = cur.fetchone()
            return int(row["id"]) if row else None


def cancelar_pedido_externo(tienda_id: int, pedido_externo_id: str) -> bool:
    """
    El comprador canceló en la tienda: sacamos el pedido de los pendientes
    para que nadie despache algo que ya no se vende. Sólo toca los que
    todavía no se convirtieron en envío.
    """
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pedidos_tienda SET estado = 'CANCELADO'
                WHERE tienda_id = %s AND pedido_externo_id = %s
                  AND estado = 'PENDIENTE'
                RETURNING id
            """, (tienda_id, str(pedido_externo_id)))
            cambio = cur.fetchone() is not None
        conn.commit()
    return cambio


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


def marcar_convertido(cliente_id: str, pedido_id: int, solicitud_id: Optional[int] = None) -> None:
    """
    El pedido pasó a ser un envío. Guardamos con qué solicitud quedó atado:
    ese vínculo es el que después permite avisarle a la tienda el tracking
    cuando se emite la guía.
    """
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pedidos_tienda
                SET estado = 'CONVERTIDO', solicitud_id = COALESCE(%s, solicitud_id)
                WHERE id = %s AND cliente_id = %s AND estado = 'PENDIENTE'
            """, (solicitud_id, pedido_id, cliente_id))
        conn.commit()


def anonimizar_pedidos(dominio: str, pedidos_externos: list[str]) -> int:
    """
    GDPR — "borrame mis datos" de un comprador: sacamos sus datos
    personales de los pedidos, dejando el registro comercial (montos,
    fechas) que hace falta conservar por contabilidad.
    """
    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return 0
    anonimo = json.dumps({"nombre": "[dato eliminado a pedido del comprador]"},
                         ensure_ascii=False)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if pedidos_externos:
                cur.execute("""
                    UPDATE pedidos_tienda p SET destinatario = %s::jsonb
                    FROM tiendas_conectadas t
                    WHERE p.tienda_id = t.id AND t.dominio = %s
                      AND p.pedido_externo_id = ANY(%s)
                """, (anonimo, dominio, [str(x) for x in pedidos_externos]))
            else:
                cur.execute("""
                    UPDATE pedidos_tienda p SET destinatario = %s::jsonb
                    FROM tiendas_conectadas t
                    WHERE p.tienda_id = t.id AND t.dominio = %s
                """, (anonimo, dominio))
            n = cur.rowcount
        conn.commit()
    return n


def borrar_datos_tienda(dominio: str) -> int:
    """
    GDPR — el comercio desinstaló y pasaron 48 hs: se borra todo lo suyo.
    Los pedidos caen solos por la clave foránea de tiendas_conectadas.
    """
    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tiendas_conectadas WHERE dominio = %s", (dominio,))
            n = cur.rowcount
            cur.execute("DELETE FROM config_envio_tienda WHERE dominio = %s", (dominio,))
        conn.commit()
    return n


def descartar_pedido(cliente_id: str, pedido_id: int) -> None:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pedidos_tienda SET estado = 'DESCARTADO'
                WHERE id = %s AND cliente_id = %s AND estado = 'PENDIENTE'
            """, (pedido_id, cliente_id))
        conn.commit()
