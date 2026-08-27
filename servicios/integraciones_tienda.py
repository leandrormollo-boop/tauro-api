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
OAUTH_SECRET_MARKER = "oauth:shopify-app"


class TiendaNoOperativaError(RuntimeError):
    """El dominio no tiene un único owner activo y coherente."""


class PedidoShopifyCanceladoError(TiendaNoOperativaError):
    """La cancelación durable ganó a un create/update tardío."""


def _bloquear_dominio_shopify(cur, dominio: str) -> None:
    """Serializa creación, redacción y cambios de ownership de una tienda."""
    dominio = (dominio or "").strip().lower()
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"tauro:shopify:{dominio}",),
    )


def _registrar_cancelacion_shopify_con_cursor(
    cur,
    dominio: str,
    pedido_externo_id: str,
    install_generation: str,
    evento_at: str = "",
) -> None:
    cur.execute(
        """
        INSERT INTO shopify_huerfanos_cancelados
            (dominio, pedido_externo_id, install_generation, cancelado_at)
        VALUES (
            %s, %s, %s,
            COALESCE(NULLIF(%s, '')::timestamptz, NOW())
        )
        ON CONFLICT (dominio, pedido_externo_id, install_generation)
        DO UPDATE SET
            cancelado_at = GREATEST(
                shopify_huerfanos_cancelados.cancelado_at,
                EXCLUDED.cancelado_at
            )
        """,
        (
            dominio,
            pedido_externo_id,
            install_generation,
            str(evento_at or "").strip(),
        ),
    )


def validar_origen_shopify_con_cursor(
    cur,
    *,
    cliente_id: str,
    dominio: str,
    pedido_externo_id: str,
) -> bool:
    """Revalida tenant y privacidad dentro del INSERT derivado.

    La verificación temprana del portal mejora el mensaje, pero esta es la
    barrera contra una carrera con uninstall/customer-redact: comparte el lock
    por dominio y consulta nuevamente las identidades antes de persistir PII.
    """
    cliente_id = (cliente_id or "").strip().upper()
    dominio = (dominio or "").strip().lower()
    pedido_externo_id = str(pedido_externo_id or "").strip()
    if not (cliente_id and dominio and pedido_externo_id):
        return False
    _bloquear_dominio_shopify(cur, dominio)
    cur.execute(
        """
        SELECT p.id
          FROM pedidos_tienda p
          JOIN tiendas_conectadas t ON t.id = p.tienda_id
          JOIN shopify_instalaciones i ON i.dominio = t.dominio
         WHERE p.cliente_id = %s
           AND t.cliente_id = p.cliente_id
           AND t.plataforma = 'shopify'
           AND t.activa = TRUE
           AND t.secreto = %s
           AND LOWER(t.dominio) = %s
           AND p.pedido_externo_id = %s
           AND NOT EXISTS (
               SELECT 1
                 FROM shopify_pedidos_redactados r
                WHERE r.dominio = %s
                  AND r.pedido_externo_id = p.pedido_externo_id
           )
           AND NULLIF(BTRIM(i.access_token), '') IS NOT NULL
           AND UPPER(COALESCE(i.cliente_id, '')) = p.cliente_id
         LIMIT 1
        """,
        (
            cliente_id, OAUTH_SECRET_MARKER, dominio,
            pedido_externo_id, dominio,
        ),
    )
    return cur.fetchone() is not None


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
                CREATE TABLE IF NOT EXISTS pedidos_huerfanos (
                    id                SERIAL PRIMARY KEY,
                    dominio           TEXT NOT NULL,
                    pedido_externo_id TEXT NOT NULL,
                    payload           JSONB NOT NULL,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (dominio, pedido_externo_id)
                );
                ALTER TABLE pedidos_huerfanos
                    ADD COLUMN IF NOT EXISTS install_generation TEXT;
                CREATE TABLE IF NOT EXISTS shopify_pedidos_redactados (
                    dominio           TEXT NOT NULL,
                    pedido_externo_id TEXT NOT NULL,
                    redactado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (dominio, pedido_externo_id)
                );
                CREATE TABLE IF NOT EXISTS shopify_webhook_recibidos (
                    webhook_id        TEXT PRIMARY KEY,
                    dominio           TEXT NOT NULL,
                    topic             TEXT NOT NULL,
                    install_generation TEXT NOT NULL,
                    procesado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS ix_shopify_webhook_recibidos_fecha
                    ON shopify_webhook_recibidos(procesado_at);
                CREATE TABLE IF NOT EXISTS shopify_huerfanos_cancelados (
                    dominio           TEXT NOT NULL,
                    pedido_externo_id TEXT NOT NULL,
                    install_generation TEXT NOT NULL,
                    cancelado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (
                        dominio, pedido_externo_id, install_generation
                    )
                );
                CREATE INDEX IF NOT EXISTS ix_shopify_huerfanos_cancelados_fecha
                    ON shopify_huerfanos_cancelados(cancelado_at);
                -- También se materializa acá porque shop/redact debe poder
                -- purgarla aunque el comercio nunca haya abierto la pantalla
                -- que configura su política de envío.
                CREATE TABLE IF NOT EXISTS config_envio_tienda (
                    dominio          TEXT PRIMARY KEY,
                    cliente_id       TEXT,
                    politica         TEXT NOT NULL DEFAULT 'real',
                    markup_pct       NUMERIC(6,2) NOT NULL DEFAULT 0,
                    precio_fijo_ars  NUMERIC(14,2) NOT NULL DEFAULT 0,
                    mostrar_tax      BOOLEAN NOT NULL DEFAULT FALSE,
                    tax_pct_default  NUMERIC(6,2) NOT NULL DEFAULT 0,
                    etiqueta         TEXT DEFAULT '',
                    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS ix_pedidos_tienda_cliente
                    ON pedidos_tienda (cliente_id, estado);
                CREATE INDEX IF NOT EXISTS ix_pedidos_huerfanos_dominio_fecha
                    ON pedidos_huerfanos (dominio, created_at);
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

def conectar_tienda(
    cliente_id: str,
    plataforma: str,
    dominio: str,
    secreto: str,
    *,
    reasignar_confirmado: bool = False,
) -> dict:
    _ensure_tablas()
    plataforma = plataforma.strip().lower()
    if plataforma not in ("shopify", "tiendanube"):
        return {"ok": False, "error": "Plataforma inválida."}

    dominio = dominio.strip().lower().replace("https://", "").replace("http://", "").strip("/ ")
    if not dominio or "." not in dominio:
        return {"ok": False, "error": "El dominio no parece válido (ej: mitienda.myshopify.com)."}
    if not secreto or len(secreto.strip()) < 8:
        return {"ok": False, "error": "El secreto de firma es demasiado corto — copialo completo desde tu tienda."}

    cliente_id = cliente_id.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Una conexión manual jamás puede apropiarse de un dominio ajeno.
            # Una reautorización OAuth firmada sí puede migrar una asociación
            # histórica de TEST_CLIENT al dueño real. La decisión y la
            # actualización ocurren en una sola sentencia para evitar carreras.
            cur.execute("""
                INSERT INTO tiendas_conectadas
                    (cliente_id, plataforma, dominio, secreto, activa)
                VALUES
                    (%(cliente)s, %(plataforma)s, %(dominio)s, %(secreto)s, TRUE)
                ON CONFLICT (dominio) DO UPDATE SET
                    cliente_id = CASE
                        WHEN tiendas_conectadas.cliente_id = EXCLUDED.cliente_id
                             OR %(reasignar)s
                        THEN EXCLUDED.cliente_id
                        ELSE tiendas_conectadas.cliente_id
                    END,
                    plataforma = CASE
                        WHEN tiendas_conectadas.cliente_id = EXCLUDED.cliente_id
                             OR %(reasignar)s
                        THEN EXCLUDED.plataforma
                        ELSE tiendas_conectadas.plataforma
                    END,
                    secreto = CASE
                        WHEN tiendas_conectadas.cliente_id = EXCLUDED.cliente_id
                             OR %(reasignar)s
                        THEN EXCLUDED.secreto
                        ELSE tiendas_conectadas.secreto
                    END,
                    activa = CASE
                        WHEN tiendas_conectadas.cliente_id = EXCLUDED.cliente_id
                             OR %(reasignar)s
                        THEN TRUE
                        ELSE tiendas_conectadas.activa
                    END
                RETURNING id, cliente_id
            """, {
                "cliente": cliente_id,
                "plataforma": plataforma,
                "dominio": dominio,
                "secreto": secreto.strip(),
                "reasignar": bool(reasignar_confirmado),
            })
            fila = cur.fetchone()
        conn.commit()
    if str((fila or {}).get("cliente_id") or "").strip().upper() != cliente_id:
        return {"ok": False, "error": "Ese dominio ya está conectado a otra cuenta."}
    return {"ok": True, "tienda_id": fila["id"]}


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


def reiniciar_integracion_shopify_cliente(
    cliente_id: str,
    dominio: str,
) -> dict:
    """Retira una instalación Shopify y su espejo sin tocar historia TAURO.

    Es una operación deliberadamente más fuerte que ``desconectar_tienda``:
    elimina token, binding, catálogo/stock, cola y pedidos importados de ESA
    tienda para permitir una instalación limpia. Envíos, pagos, facturas y
    solicitudes de guía permanecen intactos y se verifican antes del commit.
    """
    _ensure_tablas()
    cliente_id = (cliente_id or "").strip().upper()
    dominio = (dominio or "").strip().lower()
    if not cliente_id or not dominio.endswith(".myshopify.com"):
        raise ValueError("Tienda Shopify inválida.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT t.id, t.cliente_id, i.cliente_id AS owner_instalacion
                  FROM tiendas_conectadas t
                  LEFT JOIN shopify_instalaciones i
                    ON LOWER(i.dominio) = LOWER(t.dominio)
                 WHERE LOWER(t.dominio) = %s
                   AND t.plataforma = 'shopify'
                 FOR UPDATE OF t
                """,
                (dominio,),
            )
            binding = cur.fetchone()
            if not binding:
                raise ValueError("La tienda ya no está vinculada.")
            if str(binding.get("cliente_id") or "").strip().upper() != cliente_id:
                raise ValueError("La tienda pertenece a otra cuenta TAURO.")
            owner = str(binding.get("owner_instalacion") or "").strip().upper()
            if owner and owner != cliente_id:
                raise ValueError("La instalación Shopify pertenece a otra cuenta TAURO.")

            tienda_id = int(binding["id"])
            cur.execute(
                """
                SELECT COUNT(*) AS n
                  FROM tiendas_conectadas
                 WHERE UPPER(cliente_id) = %s
                   AND plataforma = 'shopify'
                   AND id <> %s
                """,
                (cliente_id, tienda_id),
            )
            otras_tiendas = int((cur.fetchone() or {}).get("n") or 0)
            incluir_legado = otras_tiendas == 0
            filtro_productos = (
                "cliente_id = %s AND plataforma = 'shopify' AND ("
                "LOWER(COALESCE(tienda_dominio, '')) = %s"
                + (
                    " OR LOWER(COALESCE(tienda_dominio, '')) "
                    "IN ('', 'legacy.myshopify.com')"
                    if incluir_legado else ""
                )
                + ")"
            )
            params_productos = (cliente_id, dominio)
            snapshot = {}
            conteos = {
                "productos": (
                    f"SELECT COUNT(*) AS n FROM productos WHERE {filtro_productos}",
                    params_productos,
                ),
                "inventario_ubicaciones": (
                    "SELECT COUNT(*) AS n FROM producto_inventario_ubicaciones "
                    "WHERE producto_id IN (SELECT id FROM productos WHERE "
                    f"{filtro_productos})",
                    params_productos,
                ),
                "pedidos_importados": (
                    "SELECT COUNT(*) AS n FROM pedidos_tienda WHERE tienda_id = %s",
                    (tienda_id,),
                ),
                "envios_preservados": (
                    "SELECT COUNT(*) AS n FROM envios WHERE UPPER(cliente_id) = %s",
                    (cliente_id,),
                ),
                "pagos_preservados": (
                    "SELECT COUNT(*) AS n FROM pagos WHERE UPPER(cliente_id) = %s",
                    (cliente_id,),
                ),
                "solicitudes_preservadas": (
                    "SELECT COUNT(*) AS n FROM solicitudes_guia "
                    "WHERE UPPER(cliente_id) = %s",
                    (cliente_id,),
                ),
            }
            for clave, (sql, params) in conteos.items():
                cur.execute(sql, params)
                snapshot[clave] = int((cur.fetchone() or {}).get("n") or 0)

            # Invalidar primero la credencial local. Todo queda dentro de una
            # sola transacción y bajo el lock por dominio, de modo que ningún
            # webhook o worker atrasado puede recrear el espejo entre deletes.
            cur.execute(
                """
                DELETE FROM shopify_instalaciones
                 WHERE LOWER(dominio) = %s
                   AND (
                        NULLIF(BTRIM(cliente_id), '') IS NULL
                        OR UPPER(BTRIM(cliente_id)) = %s
                   )
                """,
                (dominio, cliente_id),
            )
            cur.execute(
                "DELETE FROM shopify_webhook_eventos WHERE LOWER(dominio) = %s",
                (dominio,),
            )
            cur.execute(
                """
                DELETE FROM shopify_sync_estado
                 WHERE LOWER(dominio) = %s
                    OR (%s AND UPPER(cliente_id) = %s)
                """,
                (dominio, incluir_legado, cliente_id),
            )
            cur.execute(
                "DELETE FROM config_envio_tienda WHERE LOWER(dominio) = %s",
                (dominio,),
            )
            cur.execute(
                "DELETE FROM pedidos_huerfanos WHERE LOWER(dominio) = %s",
                (dominio,),
            )
            cur.execute(
                "DELETE FROM shopify_huerfanos_cancelados WHERE LOWER(dominio) = %s",
                (dominio,),
            )
            cur.execute(
                "DELETE FROM producto_inventario_ubicaciones "
                "WHERE producto_id IN (SELECT id FROM productos WHERE "
                + filtro_productos + ")",
                params_productos,
            )
            cur.execute(
                f"DELETE FROM productos WHERE {filtro_productos}",
                params_productos,
            )
            # pedidos_tienda cae por ON DELETE CASCADE. No son guías ni
            # movimientos contables: son exclusivamente el inbox importado.
            cur.execute(
                """
                DELETE FROM tiendas_conectadas
                 WHERE id = %s AND UPPER(cliente_id) = %s
                """,
                (tienda_id, cliente_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("El binding Shopify cambió durante el reinicio.")

            for clave, tabla in (
                ("envios_preservados", "envios"),
                ("pagos_preservados", "pagos"),
                ("solicitudes_preservadas", "solicitudes_guia"),
            ):
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {tabla} WHERE UPPER(cliente_id) = %s",
                    (cliente_id,),
                )
                despues = int((cur.fetchone() or {}).get("n") or 0)
                if despues != snapshot[clave]:
                    raise RuntimeError(
                        f"Control de preservación falló para {tabla}."
                    )

            from servicios.auditoria import registrar_evento_con_cursor
            registrar_evento_con_cursor(
                cur,
                event="shopify.integration_reset",
                actor_type="cliente",
                actor_ref=cliente_id,
                ip=None,
                method="POST",
                path="/portal/tienda/reiniciar-shopify",
                status_code=303,
                success=True,
                request_id=None,
                metadata={"dominio": dominio, **snapshot},
            )
        conn.commit()
    return {"ok": True, "dominio": dominio, **snapshot}


def limpiar_espejo_shopify_huerfano_cliente(cliente_id: str) -> dict:
    """Limpia catálogo Shopify legado cuando ya no existe una instalación.

    Cubre importaciones antiguas guardadas como ``legacy.myshopify.com`` o sin
    dominio. Se niega a actuar si el cliente tiene cualquier binding o token
    Shopify vigente, evitando mezclar catálogos de dos tiendas.
    """
    _ensure_tablas()
    cliente_id = (cliente_id or "").strip().upper()
    if not cliente_id:
        raise ValueError("Cliente inválido.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"tauro:shopify:legacy:{cliente_id}",),
            )
            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM tiendas_conectadas
                      WHERE UPPER(cliente_id) = %s
                        AND plataforma = 'shopify') AS bindings,
                    (SELECT COUNT(*) FROM shopify_instalaciones
                      WHERE UPPER(COALESCE(cliente_id, '')) = %s) AS instalaciones
                """,
                (cliente_id, cliente_id),
            )
            estado = cur.fetchone() or {}
            if int(estado.get("bindings") or 0):
                raise ValueError(
                    "Todavía existe una conexión Shopify. Reiniciala desde Mi tienda."
                )

            snapshot = {
                "instalaciones_retiradas": int(
                    estado.get("instalaciones") or 0
                ),
            }
            for clave, tabla in (
                ("productos", "productos"),
                ("inventario_ubicaciones", "producto_inventario_ubicaciones"),
                ("envios_preservados", "envios"),
                ("pagos_preservados", "pagos"),
                ("solicitudes_preservadas", "solicitudes_guia"),
            ):
                extra = " AND plataforma = 'shopify'" if tabla in {
                    "productos", "producto_inventario_ubicaciones",
                } else ""
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {tabla} "
                    f"WHERE UPPER(cliente_id) = %s{extra}",
                    (cliente_id,),
                )
                snapshot[clave] = int((cur.fetchone() or {}).get("n") or 0)

            # Sin binding no hay una instalación operativa. Retirar estos
            # tokens owner-only cierra webhooks viejos y permite que el próximo
            # OAuth empiece con una generación realmente nueva.
            cur.execute(
                """
                DELETE FROM shopify_instalaciones
                 WHERE UPPER(COALESCE(cliente_id, '')) = %s
                """,
                (cliente_id,),
            )
            cur.execute(
                """
                DELETE FROM producto_inventario_ubicaciones
                 WHERE UPPER(cliente_id) = %s AND plataforma = 'shopify'
                """,
                (cliente_id,),
            )
            cur.execute(
                """
                DELETE FROM productos
                 WHERE UPPER(cliente_id) = %s AND plataforma = 'shopify'
                """,
                (cliente_id,),
            )
            cur.execute(
                "DELETE FROM shopify_sync_estado WHERE UPPER(cliente_id) = %s",
                (cliente_id,),
            )

            for clave, tabla in (
                ("envios_preservados", "envios"),
                ("pagos_preservados", "pagos"),
                ("solicitudes_preservadas", "solicitudes_guia"),
            ):
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {tabla} WHERE UPPER(cliente_id) = %s",
                    (cliente_id,),
                )
                if int((cur.fetchone() or {}).get("n") or 0) != snapshot[clave]:
                    raise RuntimeError(
                        f"Control de preservación falló para {tabla}."
                    )

            from servicios.auditoria import registrar_evento_con_cursor
            registrar_evento_con_cursor(
                cur,
                event="shopify.orphan_mirror_cleanup",
                actor_type="cliente",
                actor_ref=cliente_id,
                ip=None,
                method="POST",
                path="/portal/tienda/limpiar-shopify-legado",
                status_code=303,
                success=True,
                request_id=None,
                metadata=snapshot,
            )
        conn.commit()
    return {"ok": True, **snapshot}


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


def webhook_shopify_ya_procesado(webhook_id: str) -> bool:
    """Dedupe durable por el identificador único que entrega Shopify."""
    _ensure_tablas()
    webhook_id = str(webhook_id or "").strip()
    if not webhook_id:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM shopify_webhook_recibidos WHERE webhook_id = %s",
                (webhook_id,),
            )
            return cur.fetchone() is not None


def marcar_webhook_shopify_procesado(
    webhook_id: str,
    dominio: str,
    topic: str,
    install_generation: str,
) -> None:
    """Marca sólo después del efecto idempotente; un fallo provoca retry."""
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shopify_webhook_recibidos
                    (webhook_id, dominio, topic, install_generation)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (webhook_id) DO NOTHING
                """,
                (
                    str(webhook_id or "").strip(),
                    (dominio or "").strip().lower(),
                    (topic or "").strip().lower(),
                    str(install_generation or "").strip(),
                ),
            )
            cur.execute(
                "DELETE FROM shopify_webhook_recibidos "
                "WHERE procesado_at < NOW() - INTERVAL '180 days'"
            )
        conn.commit()


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

    items = []
    for it in (order.get("line_items") or []):
        product_id = str(it.get("product_id") or "").strip()
        variant_id = str(it.get("variant_id") or "").strip()
        items.append({
            "titulo": (it.get("title") or "")[:180],
            "variante": (it.get("variant_title") or "")[:180],
            "cantidad": int(it.get("quantity") or 1),
            "precio": it.get("price"),
            "sku": (it.get("sku") or "")[:160],
            "peso_gr": it.get("grams"),
            # IDs estables: el SKU puede cambiar y no es obligatorio. Son la
            # llave correcta para encontrar imagen, stock y datos aduaneros.
            "external_product_id": (
                f"gid://shopify/Product/{product_id}" if product_id.isdigit() else product_id
            ),
            "external_variant_id": (
                f"gid://shopify/ProductVariant/{variant_id}" if variant_id.isdigit() else variant_id
            ),
        })

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

def guardar_pedido(
    cliente_id: str,
    tienda_id: int,
    plataforma: str,
    pedido: dict,
    *,
    dominio_verificado: str = "",
    install_generation_verificada: str = "",
) -> bool:
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
    cliente_id = (cliente_id or "").strip().upper()
    plataforma = (plataforma or "").strip().lower()
    dominio_verificado = (dominio_verificado or "").strip().lower()
    install_generation_verificada = str(
        install_generation_verificada or ""
    ).strip()
    if plataforma == "shopify" and not dominio_verificado:
        raise TiendaNoOperativaError("Falta verificar la instalación Shopify.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if plataforma == "shopify":
                # Esta revalidación vive en la MISMA transacción que el INSERT.
                # El lock serializa el webhook con uninstall/shop-redact y evita
                # que una fila leída antes del borrado reaparezca después.
                _bloquear_dominio_shopify(cur, dominio_verificado)
                cur.execute(
                    """
                    SELECT t.id
                      FROM tiendas_conectadas t
                      JOIN shopify_instalaciones i ON i.dominio = t.dominio
                     WHERE t.id = %s
                       AND t.cliente_id = %s
                       AND t.plataforma = 'shopify'
                       AND t.activa = TRUE
                       AND LOWER(t.dominio) = %s
                       AND t.secreto = %s
                       AND UPPER(COALESCE(i.cliente_id, '')) = t.cliente_id
                       AND NULLIF(BTRIM(i.access_token), '') IS NOT NULL
                       AND (%s = '' OR i.install_generation = %s)
                       AND NOT EXISTS (
                           SELECT 1
                             FROM shopify_pedidos_redactados r
                            WHERE r.dominio = %s
                              AND r.pedido_externo_id = %s
                       )
                       AND NOT EXISTS (
                           SELECT 1
                             FROM shopify_huerfanos_cancelados c
                            WHERE c.dominio = %s
                              AND c.pedido_externo_id = %s
                              AND c.install_generation = %s
                       )
                     LIMIT 1
                    """,
                    (
                        tienda_id,
                        cliente_id,
                        dominio_verificado,
                        OAUTH_SECRET_MARKER,
                        install_generation_verificada,
                        install_generation_verificada,
                        dominio_verificado,
                        str(pedido.get("pedido_externo_id") or ""),
                        dominio_verificado,
                        str(pedido.get("pedido_externo_id") or ""),
                        install_generation_verificada,
                    ),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        """
                        SELECT 1
                          FROM shopify_huerfanos_cancelados
                         WHERE dominio = %s
                           AND pedido_externo_id = %s
                           AND install_generation = %s
                         LIMIT 1
                        """,
                        (
                            dominio_verificado,
                            str(pedido.get("pedido_externo_id") or ""),
                            install_generation_verificada,
                        ),
                    )
                    if cur.fetchone() is not None:
                        raise PedidoShopifyCanceladoError(
                            "El pedido Shopify ya fue cancelado."
                        )
                    raise TiendaNoOperativaError(
                        "La instalación Shopify ya no está operativa."
                    )
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
                  AND pedidos_tienda.cliente_id = EXCLUDED.cliente_id
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


def guardar_pedido_huerfano(
    dominio: str,
    cuerpo: bytes,
    *,
    app_client_id_verificado: str = "",
    install_generation_verificada: str = "",
) -> bool:
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
        return False
    pedido_id = str(orden.get("id") or "")
    if not pedido_id:
        return False

    dominio = (dominio or "").strip().lower()
    app_client_id_verificado = (app_client_id_verificado or "").strip()
    install_generation_verificada = str(
        install_generation_verificada or ""
    ).strip()
    if not dominio or not app_client_id_verificado or not install_generation_verificada:
        raise TiendaNoOperativaError("No se verificó la instalación Shopify.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            # El endpoint verificó el HMAC antes del lock; acá se revalida que
            # esa misma app siga instalada. Si uninstall ganó la carrera, no
            # se vuelve a persistir el payload con PII.
            cur.execute(
                """
                SELECT install_generation
                  FROM shopify_instalaciones
                 WHERE dominio = %s
                   AND app_client_id = %s
                   AND install_generation = %s
                   AND NULLIF(BTRIM(access_token), '') IS NOT NULL
                   AND NULLIF(BTRIM(COALESCE(cliente_id, '')), '') IS NULL
                   AND NOT EXISTS (
                       SELECT 1
                         FROM tiendas_conectadas t
                        WHERE LOWER(t.dominio) = %s
                          AND t.plataforma = 'shopify'
                          AND t.secreto = %s
                          AND t.activa = TRUE
                          AND NULLIF(BTRIM(t.cliente_id), '') IS NOT NULL
                   )
                 LIMIT 1
                """,
                (
                    dominio,
                    app_client_id_verificado,
                    install_generation_verificada,
                    dominio,
                    OAUTH_SECRET_MARKER,
                ),
            )
            instalacion_actual = cur.fetchone()
            if not instalacion_actual:
                raise TiendaNoOperativaError(
                    "La instalación Shopify ya no está operativa."
                )
            cur.execute(
                """
                SELECT 1
                  FROM shopify_pedidos_redactados
                 WHERE dominio = %s AND pedido_externo_id = %s
                 LIMIT 1
                """,
                (dominio, pedido_id),
            )
            if cur.fetchone() is not None:
                return False
            cur.execute(
                """
                SELECT 1
                  FROM shopify_huerfanos_cancelados
                 WHERE dominio = %s
                   AND pedido_externo_id = %s
                   AND install_generation = %s
                 LIMIT 1
                """,
                (dominio, pedido_id, install_generation_verificada),
            )
            if cur.fetchone() is not None:
                # La cancelación es monotónica: un create/update tardío de la
                # misma generación nunca puede volver a introducir el PII.
                return False
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pedidos_huerfanos (
                    id                SERIAL PRIMARY KEY,
                    dominio           TEXT NOT NULL,
                    pedido_externo_id TEXT NOT NULL,
                    payload           JSONB NOT NULL,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (dominio, pedido_externo_id)
                );
                ALTER TABLE pedidos_huerfanos
                    ADD COLUMN IF NOT EXISTS install_generation TEXT;
            """)
            cur.execute("""
                INSERT INTO pedidos_huerfanos
                    (dominio, pedido_externo_id, payload, install_generation)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (dominio, pedido_externo_id) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        install_generation = EXCLUDED.install_generation
            """, (dominio.strip().lower(), pedido_id,
                  _json.dumps(orden, ensure_ascii=False),
                  instalacion_actual["install_generation"]))
            # RETENCIÓN ACOTADA. El payload es la orden entera: nombre,
            # dirección y teléfono del comprador final. Una tienda que probó
            # la app, no se vinculó nunca y desinstaló nos dejaba esos datos
            # guardados para siempre. A los 90 días la venta ya no se puede
            # despachar, así que no hay motivo para conservarla.
            cur.execute("""
                DELETE FROM pedidos_huerfanos
                WHERE created_at < NOW() - INTERVAL '90 days'
            """)
        conn.commit()
    return True


def cancelar_pedido_huerfano(
    dominio: str,
    pedido_externo_id: str,
    *,
    app_client_id_verificado: str,
    install_generation_verificada: str,
    evento_at: str = "",
) -> bool:
    """Tombstone durable de una cancelación recibida antes del claim.

    Se valida bajo el lock que la generación todavía sea ownerless. El
    tombstone gana de forma monotónica a create/updated tardíos y se elimina
    cualquier payload huérfano ya guardado para no retener PII ni despacharlo
    cuando el comercio vincule su cuenta.
    """
    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    pedido_externo_id = str(pedido_externo_id or "").strip()
    app_client_id_verificado = (app_client_id_verificado or "").strip()
    install_generation_verificada = str(
        install_generation_verificada or ""
    ).strip()
    if not all((
        dominio, pedido_externo_id, app_client_id_verificado,
        install_generation_verificada,
    )):
        raise TiendaNoOperativaError("No se verificó la instalación Shopify.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT 1
                  FROM shopify_instalaciones i
                 WHERE i.dominio = %s
                   AND i.app_client_id = %s
                   AND i.install_generation = %s
                   AND NULLIF(BTRIM(i.access_token), '') IS NOT NULL
                   AND NULLIF(BTRIM(COALESCE(i.cliente_id, '')), '') IS NULL
                   AND NOT EXISTS (
                       SELECT 1
                         FROM tiendas_conectadas t
                        WHERE LOWER(t.dominio) = %s
                          AND t.plataforma = 'shopify'
                          AND t.secreto = %s
                          AND t.activa = TRUE
                          AND NULLIF(BTRIM(t.cliente_id), '') IS NOT NULL
                   )
                 LIMIT 1
                """,
                (
                    dominio,
                    app_client_id_verificado,
                    install_generation_verificada,
                    dominio,
                    OAUTH_SECRET_MARKER,
                ),
            )
            if cur.fetchone() is None:
                raise TiendaNoOperativaError(
                    "La instalación Shopify ya fue vinculada o reemplazada."
                )
            _registrar_cancelacion_shopify_con_cursor(
                cur,
                dominio,
                pedido_externo_id,
                install_generation_verificada,
                evento_at,
            )
            cur.execute(
                """
                DELETE FROM pedidos_huerfanos
                 WHERE dominio = %s
                   AND pedido_externo_id = %s
                   AND install_generation = %s
                """,
                (dominio, pedido_externo_id, install_generation_verificada),
            )
        conn.commit()
    return True


def limpiar_pedidos_huerfanos_vencidos() -> int:
    """Poda global diaria de órdenes sin dueño con más de 90 días."""
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM pedidos_huerfanos
                WHERE created_at < NOW() - INTERVAL '90 days'
            """)
            eliminados = cur.rowcount
            cur.execute("""
                DELETE FROM shopify_huerfanos_cancelados
                WHERE cancelado_at < NOW() - INTERVAL '90 days'
            """)
            eliminados += cur.rowcount
        conn.commit()
    return int(eliminados or 0)


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
                    SELECT h.pedido_externo_id, h.payload,
                           h.install_generation
                      FROM pedidos_huerfanos h
                      JOIN shopify_instalaciones i
                        ON i.dominio = h.dominio
                       AND i.install_generation = h.install_generation
                      LEFT JOIN shopify_huerfanos_cancelados c
                        ON c.dominio = h.dominio
                       AND c.pedido_externo_id = h.pedido_externo_id
                       AND c.install_generation = h.install_generation
                     WHERE h.dominio = %s
                       AND h.created_at > NOW() - INTERVAL '90 days'
                       AND c.pedido_externo_id IS NULL
                    ORDER BY h.created_at
                    LIMIT 500
                """, (dominio,))
                filas = cur.fetchall()
    except Exception as exc:
        print(f"[integraciones] no pude leer huérfanos: {type(exc).__name__}")
        return 0

    procesados: list[str] = []
    for f in filas:
        try:
            pedido = parsear_pedido_shopify(f["payload"])
            if not pedido or pedido.get("cancelado"):
                procesados.append(f["pedido_externo_id"])
                continue
            nuevo = guardar_pedido(
                cliente_id,
                tienda_id,
                "shopify",
                pedido,
                dominio_verificado=dominio,
                install_generation_verificada=f.get("install_generation") or "",
            )
            procesados.append(f["pedido_externo_id"])
            if nuevo:
                volcados += 1
        except Exception as exc:
            # En error transitorio o mismatch de generación el payload queda
            # disponible para retry y el log no copia detalle DB/PII.
            print(f"[integraciones] huérfano no procesado: {type(exc).__name__}")

    if procesados:
        # Se borra SÓLO lo que se acaba de procesar (más lo vencido). Si la
        # tienda tenía más de 500 huérfanos, la tanda que quedó afuera sigue
        # ahí para el próximo vínculo en vez de desaparecer sin que nadie
        # se entere.
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
                print(f"[integraciones] quedan {quedan} huérfano(s) para retry")
        except Exception as exc:
            print(f"[integraciones] no pude limpiar huérfanos: {type(exc).__name__}")

    if volcados:
        print(f"[integraciones] {volcados} venta(s) huérfana(s) recuperada(s)")
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


def cancelar_pedido_externo(
    tienda_id: int,
    pedido_externo_id: str,
    *,
    cliente_id: str = "",
    dominio_verificado: str = "",
    install_generation_verificada: str = "",
    evento_at: str = "",
) -> bool:
    """
    El comprador canceló en la tienda: sacamos el pedido de los pendientes
    para que nadie despache algo que ya no se vende. Sólo toca los que
    todavía no se convirtieron en envío.

    Para Shopify, el endpoint entrega tenant, dominio y generación ya
    verificados. Se vuelven a validar bajo el mismo advisory lock y la misma
    transacción del UPDATE para que uninstall/reinstall no pueda cancelar un
    pedido perteneciente a otro owner o a una generación posterior.
    """
    _ensure_tablas()
    pedido_externo_id = str(pedido_externo_id or "").strip()
    cliente_id = (cliente_id or "").strip().upper()
    dominio_verificado = (dominio_verificado or "").strip().lower()
    install_generation_verificada = str(
        install_generation_verificada or ""
    ).strip()
    es_shopify = bool(
        cliente_id or dominio_verificado or install_generation_verificada
    )
    if es_shopify and not all((
        cliente_id, dominio_verificado, install_generation_verificada,
    )):
        raise TiendaNoOperativaError("Falta verificar la instalación Shopify.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if es_shopify:
                _bloquear_dominio_shopify(cur, dominio_verificado)
                cur.execute(
                    """
                    SELECT t.id
                      FROM tiendas_conectadas t
                      JOIN shopify_instalaciones i
                        ON LOWER(i.dominio) = LOWER(t.dominio)
                     WHERE t.id = %s
                       AND UPPER(t.cliente_id) = %s
                       AND t.plataforma = 'shopify'
                       AND t.activa = TRUE
                       AND LOWER(t.dominio) = %s
                       AND t.secreto = %s
                       AND UPPER(COALESCE(i.cliente_id, '')) = %s
                       AND i.install_generation = %s
                       AND NULLIF(BTRIM(i.access_token), '') IS NOT NULL
                     LIMIT 1
                    """,
                    (
                        tienda_id,
                        cliente_id,
                        dominio_verificado,
                        OAUTH_SECRET_MARKER,
                        cliente_id,
                        install_generation_verificada,
                    ),
                )
                if cur.fetchone() is None:
                    raise TiendaNoOperativaError(
                        "La instalación Shopify ya no está operativa."
                    )
                _registrar_cancelacion_shopify_con_cursor(
                    cur,
                    dominio_verificado,
                    pedido_externo_id,
                    install_generation_verificada,
                    evento_at,
                )
            cur.execute("""
                UPDATE pedidos_tienda SET estado = 'CANCELADO'
                WHERE tienda_id = %s AND pedido_externo_id = %s
                  AND estado = 'PENDIENTE'
                  AND (%s = '' OR UPPER(cliente_id) = %s)
                RETURNING id
            """, (tienda_id, pedido_externo_id, cliente_id, cliente_id))
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


def _anonimizar_solicitudes_con_cursor(
    cur,
    solicitudes_ids: list[int],
    *,
    incluir_remitente: bool = False,
) -> int:
    """Elimina copias PII/labels sin borrar la evidencia financiera.

    Una solicitud convertida duplica parte de sus datos en ``envios`` y una
    recoleccion puede volver a copiar la direccion del remitente. Las tres
    tablas se sanitizan por el mismo ``solicitud_id`` dentro de la transaccion
    del caller; conservar solamente importes, fechas y estados evita acusar un
    redact exitoso mientras quedan identificadores o direcciones derivados.
    """
    if not solicitudes_ids:
        return 0
    remitente_sql = """
            remitente_alias = NULL,
            remitente_nombre = '[dato eliminado por desinstalación]',
            remitente_contacto = NULL,
            remitente_documento = NULL,
            remitente_email = NULL,
            remitente_telefono = NULL,
            remitente_direccion = '[dato eliminado por desinstalación]',
            remitente_ciudad = '',
            remitente_estado = NULL,
            remitente_zip = '',
    """ if incluir_remitente else ""
    cur.execute(f"""
        UPDATE solicitudes_guia
        SET estado = CASE
                WHEN NULLIF(BTRIM(tracking), '') IS NULL
                     AND guia_generada_at IS NULL
                THEN 'CANCELADO'
                ELSE estado
            END,
            dest_nombre = '[dato eliminado a pedido del comprador]',
            dest_contacto = NULL,
            dest_documento = NULL,
            dest_email = NULL,
            dest_telefono = NULL,
            dest_direccion = '[dato eliminado a pedido del comprador]',
            dest_ciudad = '',
            dest_estado = NULL,
            dest_zip = '',
            observaciones = NULL,
            courier_error = NULL,
            tracking = NULL,
            label_pdf = NULL,
            guia_url = NULL,
            {remitente_sql}
            updated_at = NOW()
        WHERE id = ANY(%s)
    """, (solicitudes_ids,))
    total = max(int(cur.rowcount or 0), 0)

    # El asiento financiero se conserva, pero no sus copias descriptivas del
    # comprador ni el identificador externo del paquete.
    cur.execute("""
        UPDATE envios
           SET tracking = NULL,
               descripcion = NULL
         WHERE solicitud_id = ANY(%s)
    """, (solicitudes_ids,))
    total += max(int(cur.rowcount or 0), 0)

    # Una recoleccion historica conserva fecha, courier, estado, peso y codigo
    # de confirmacion; direccion e instrucciones son PII operativa duplicada.
    cur.execute("""
        UPDATE recolecciones
           SET direccion = NULL,
               instrucciones = NULL,
               ubicacion = NULL,
               error_operativo = NULL,
               updated_at = NOW()
         WHERE solicitud_id = ANY(%s)
    """, (solicitudes_ids,))
    total += max(int(cur.rowcount or 0), 0)
    return total


def _registrar_pedidos_redactados_con_cursor(
    cur, dominio: str, pedidos_externos: list[str],
) -> None:
    """Tombstone durable: una carrera/retry no puede recrear PII borrada."""
    pedidos_externos = [str(x) for x in pedidos_externos if str(x).strip()]
    if not pedidos_externos:
        return
    cur.execute(
        """
        INSERT INTO shopify_pedidos_redactados (dominio, pedido_externo_id)
        SELECT %s, pedido_id
          FROM unnest(%s::text[]) AS pedido_id
        ON CONFLICT (dominio, pedido_externo_id) DO NOTHING
        """,
        (dominio, pedidos_externos),
    )


def _solicitudes_shopify_con_cursor(
    cur, dominio: str, pedidos_externos: list[str] | None = None,
) -> list[int]:
    """Resuelve toda copia downstream, incluso si falló el link legado.

    ``pedidos_tienda.solicitud_id`` se conserva como fallback para historia
    anterior a las columnas de linaje. Las filas nuevas se encuentran por su
    origen durable aunque el paso posterior ``marcar_convertido`` haya fallado.
    """
    dominio = (dominio or "").strip().lower()
    pedidos_externos = (
        [str(x) for x in pedidos_externos]
        if pedidos_externos is not None else None
    )
    if pedidos_externos is None:
        cur.execute("""
            SELECT p.solicitud_id
              FROM pedidos_tienda p
              JOIN tiendas_conectadas t ON t.id = p.tienda_id
              JOIN solicitudes_guia s
                ON s.id = p.solicitud_id
               AND UPPER(s.cliente_id) = UPPER(p.cliente_id)
             WHERE t.dominio = %s
               AND p.solicitud_id IS NOT NULL
             FOR UPDATE OF p, s
        """, (dominio,))
    else:
        cur.execute("""
            SELECT p.solicitud_id
              FROM pedidos_tienda p
              JOIN tiendas_conectadas t ON t.id = p.tienda_id
              JOIN solicitudes_guia s
                ON s.id = p.solicitud_id
               AND UPPER(s.cliente_id) = UPPER(p.cliente_id)
             WHERE t.dominio = %s
               AND p.solicitud_id IS NOT NULL
               AND p.pedido_externo_id = ANY(%s)
             FOR UPDATE OF p, s
        """, (dominio, pedidos_externos))
    ids = {
        int(fila["solicitud_id"])
        for fila in cur.fetchall()
        if fila.get("solicitud_id") is not None
    }

    if pedidos_externos is None:
        cur.execute("""
            SELECT id
              FROM solicitudes_guia
             WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
               AND LOWER(COALESCE(origen_dominio, '')) = %s
             FOR UPDATE
        """, (dominio,))
    else:
        cur.execute("""
            SELECT id
              FROM solicitudes_guia
             WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
               AND LOWER(COALESCE(origen_dominio, '')) = %s
               AND origen_pedido_externo_id = ANY(%s)
             FOR UPDATE
        """, (dominio, pedidos_externos))
    ids.update(int(fila["id"]) for fila in cur.fetchall())
    return sorted(ids)


def _borrar_direcciones_shopify_con_cursor(
    cur, dominio: str, pedidos_externos: list[str] | None = None,
) -> int:
    """Borra copias de libreta derivadas del comprador Shopify."""
    if pedidos_externos is None:
        cur.execute("""
            DELETE FROM direcciones
             WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
               AND LOWER(COALESCE(origen_dominio, '')) = %s
        """, (dominio,))
    else:
        cur.execute("""
            DELETE FROM direcciones
             WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
               AND LOWER(COALESCE(origen_dominio, '')) = %s
               AND origen_pedido_externo_id = ANY(%s)
        """, (dominio, pedidos_externos))
    return max(int(cur.rowcount or 0), 0)


def anonimizar_pedidos(dominio: str, pedidos_externos: list[str]) -> int:
    """
    GDPR — "borrame mis datos" de un comprador: sacamos sus datos
    personales de los pedidos, dejando el registro comercial (montos,
    fechas) que hace falta conservar por contabilidad.
    """
    dominio = (dominio or "").strip().lower()
    pedidos_externos = [str(x) for x in (pedidos_externos or []) if str(x).strip()]
    # Shopify puede mandar customers/redact sin orders_to_redact. Eso indica
    # que no hay pedidos de ese comprador para tocar; jamás significa "todos
    # los pedidos de la tienda".
    if not dominio or not pedidos_externos:
        return 0
    _ensure_tablas()
    anonimo = json.dumps({"nombre": "[dato eliminado a pedido del comprador]"},
                         ensure_ascii=False)
    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            _registrar_pedidos_redactados_con_cursor(
                cur, dominio, pedidos_externos,
            )
            # Capturamos las solicitudes de guia vinculadas en la misma
            # transaccion. Esas filas duplican direccion/contacto y el PDF de
            # la etiqueta contiene todos esos datos: anonimizar solamente el
            # pedido dejaba una copia completa accesible desde el portal.
            solicitudes_ids = _solicitudes_shopify_con_cursor(
                cur, dominio, pedidos_externos,
            )
            cur.execute("""
                UPDATE pedidos_tienda p SET destinatario = %s::jsonb
                FROM tiendas_conectadas t
                WHERE p.tienda_id = t.id AND t.dominio = %s
                  AND p.pedido_externo_id = ANY(%s)
            """, (anonimo, dominio, pedidos_externos))
            n = cur.rowcount
            n += _anonimizar_solicitudes_con_cursor(cur, solicitudes_ids)
            n += _borrar_direcciones_shopify_con_cursor(
                cur, dominio, pedidos_externos,
            )
            # Una orden puede haber llegado antes de que el comercio vincule
            # la tienda. Ese payload huérfano contiene la dirección completa;
            # ante customers/redact se elimina por id en vez de conservar PII.
            cur.execute("""
                DELETE FROM pedidos_huerfanos
                WHERE dominio = %s AND pedido_externo_id = ANY(%s)
            """, (dominio, pedidos_externos))
            n += cur.rowcount
            cur.execute("""
                DELETE FROM shopify_huerfanos_cancelados
                WHERE dominio = %s AND pedido_externo_id = ANY(%s)
            """, (dominio, pedidos_externos))
            n += cur.rowcount
        conn.commit()
    return n


def _borrar_datos_tienda_con_cursor(cur, dominio: str) -> int:
    """Purga operacional completa; el caller controla lock y transacción."""
    total = 0

    # Registrar los ids ANTES de borrar sus fuentes. Además de cubrir retries,
    # estos tombstones impiden que un worker atrasado recree una dirección o
    # solicitud de guía después del purge.
    cur.execute(
        """
        INSERT INTO shopify_pedidos_redactados (dominio, pedido_externo_id)
        SELECT DISTINCT %s, ids.pedido_externo_id
          FROM (
                SELECT p.pedido_externo_id
                  FROM pedidos_tienda p
                  JOIN tiendas_conectadas t ON t.id = p.tienda_id
                 WHERE LOWER(t.dominio) = %s
                UNION
                SELECT pedido_externo_id
                  FROM pedidos_huerfanos
                 WHERE LOWER(dominio) = %s
                UNION
                SELECT origen_pedido_externo_id
                  FROM solicitudes_guia
                 WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
                   AND LOWER(COALESCE(origen_dominio, '')) = %s
                UNION
                SELECT origen_pedido_externo_id
                  FROM direcciones
                 WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
                   AND LOWER(COALESCE(origen_dominio, '')) = %s
          ) ids
         WHERE NULLIF(BTRIM(ids.pedido_externo_id), '') IS NOT NULL
        ON CONFLICT (dominio, pedido_externo_id) DO NOTHING
        """,
        (dominio, dominio, dominio, dominio, dominio),
    )

    # Las solicitudes/cargos se conservan como evidencia financiera, pero
    # pierden PII de destinatario Y remitente, además de etiqueta/errores.
    solicitudes_ids = _solicitudes_shopify_con_cursor(cur, dominio)
    total += _anonimizar_solicitudes_con_cursor(
        cur, solicitudes_ids, incluir_remitente=True,
    )
    total += _borrar_direcciones_shopify_con_cursor(cur, dominio)
    cur.execute("DELETE FROM pedidos_huerfanos WHERE LOWER(dominio) = %s", (dominio,))
    total += cur.rowcount
    cur.execute(
        "DELETE FROM shopify_huerfanos_cancelados WHERE LOWER(dominio) = %s",
        (dominio,),
    )
    total += cur.rowcount
    # Las solicitudes GDPR pendientes son una obligación independiente y no
    # se borran aquí. Su poda retira sólo filas resueltas.
    cur.execute("DELETE FROM shopify_webhook_eventos WHERE LOWER(dominio) = %s", (dominio,))
    total += cur.rowcount
    cur.execute("DELETE FROM shopify_sync_estado WHERE LOWER(dominio) = %s", (dominio,))
    total += cur.rowcount
    cur.execute("DELETE FROM config_envio_tienda WHERE LOWER(dominio) = %s", (dominio,))
    total += cur.rowcount
    cur.execute("""
        DELETE FROM producto_inventario_ubicaciones
        WHERE plataforma = 'shopify' AND LOWER(tienda_dominio) = %s
    """, (dominio,))
    total += cur.rowcount
    cur.execute("""
        DELETE FROM productos
        WHERE plataforma = 'shopify' AND LOWER(tienda_dominio) = %s
    """, (dominio,))
    total += cur.rowcount
    # Se elimina el binding en vez de dejar una fila inactiva que bloquee o
    # pueda atribuir eventos de una reinstalación al owner anterior.
    cur.execute("DELETE FROM tiendas_conectadas WHERE LOWER(dominio) = %s", (dominio,))
    total += cur.rowcount
    return max(int(total or 0), 0)


def borrar_datos_tienda(dominio: str) -> int:
    """
    GDPR — el comercio desinstaló y pasaron 48 hs: se borra todo lo suyo.

    El dominio es la única clave común cuando la tienda todavía no llegó a
    vincularse con un cliente. Por eso la purga también incluye huérfanos,
    payloads pendientes de webhooks, estado de sincronización y el espejo de
    catálogo/stock. Los pedidos vinculados caen por la clave foránea de
    tiendas_conectadas; el inventario cae al borrar sus productos, aunque se
    elimina explícitamente primero para que la intención de privacidad quede
    verificable y no dependa sólo del cascade.
    """
    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            total = _borrar_datos_tienda_con_cursor(cur, dominio)
        conn.commit()
    return total


def descartar_pedido(cliente_id: str, pedido_id: int) -> None:
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pedidos_tienda SET estado = 'DESCARTADO'
                WHERE id = %s AND cliente_id = %s AND estado = 'PENDIENTE'
            """, (pedido_id, cliente_id))
        conn.commit()
