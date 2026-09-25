"""Outboxes durables para pedidos de tienda y notificaciones de tracking.

Los hilos de este módulo sólo despiertan workers. La fuente de verdad es
PostgreSQL: cada efecto se reclama con ``FOR UPDATE SKIP LOCKED`` y ``claim_id``;
un proceso nuevo puede recuperar claims stale sin depender de memoria local.
"""
from __future__ import annotations

import os
import threading
import uuid
from typing import Optional

from core.database import get_conn


_order_worker_lock = threading.Lock()
_fulfillment_worker_lock = threading.Lock()
_MAX_ATTEMPTS = 5


def _flag(nombre: str, default: bool = False) -> bool:
    raw = os.getenv(nombre)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_PEDIDO_FINGERPRINT_SQL = """
md5(jsonb_build_object(
    'cliente_id', p.cliente_id,
    'tienda_id', p.tienda_id,
    'plataforma', p.plataforma,
    'pedido_externo_id', p.pedido_externo_id,
    'numero', p.numero,
    'destinatario', COALESCE(p.destinatario, '{}'::jsonb),
    'items', COALESCE(p.items, '[]'::jsonb),
    'valor_total', p.valor_total,
    'moneda', p.moneda,
    'flete_cobrado', p.flete_cobrado,
    'flete_detalle', COALESCE(p.flete_detalle, '[]'::jsonb)
)::text)
"""


def encolar_pedido_con_cursor(cur, pedido_id: int) -> bool:
    """Encola la versión actual del pedido dentro de la transacción caller."""
    cur.execute(
        f"""
        INSERT INTO solicitud_automatica_outbox
            (pedido_id, payload_fingerprint)
        SELECT p.id, {_PEDIDO_FINGERPRINT_SQL}
          FROM pedidos_tienda p
         WHERE p.id=%s AND p.estado='PENDIENTE'
           AND p.automatismos_bloqueados=FALSE
        ON CONFLICT (pedido_id, payload_fingerprint) DO UPDATE SET
            estado='PENDIENTE', proximo_intento_at=NOW(), claim_id=NULL,
            claimed_at=NULL, ultimo_error_codigo=NULL, ultimo_error=NULL,
            completed_at=NULL, updated_at=NOW()
        WHERE solicitud_automatica_outbox.estado='COMPLETADO'
        RETURNING id
        """,
        (int(pedido_id),),
    )
    return cur.fetchone() is not None


def encolar_pedido(pedido_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            return encolar_pedido_con_cursor(cur, pedido_id)


def reconciliar_pedidos_faltantes(limite: int = 200) -> int:
    """Recrea jobs ausentes para pedidos pendientes, sin duplicar versiones."""
    limite = max(1, min(int(limite), 1000))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH candidatos AS (
                    SELECT p.id, {_PEDIDO_FINGERPRINT_SQL} AS fingerprint
                      FROM pedidos_tienda p
                     WHERE p.estado='PENDIENTE'
                       AND p.automatismos_bloqueados=FALSE
                     ORDER BY p.updated_at, p.id
                     LIMIT %s
                )
                INSERT INTO solicitud_automatica_outbox
                    (pedido_id, payload_fingerprint)
                SELECT id, fingerprint FROM candidatos
                ON CONFLICT (pedido_id, payload_fingerprint) DO UPDATE SET
                    estado='PENDIENTE', proximo_intento_at=NOW(), claim_id=NULL,
                    claimed_at=NULL, ultimo_error_codigo=NULL, ultimo_error=NULL,
                    completed_at=NULL, updated_at=NOW()
                WHERE solicitud_automatica_outbox.estado='COMPLETADO'
                RETURNING id
                """,
                (limite,),
            )
            return len(cur.fetchall())


def _claim_pedido() -> Optional[dict]:
    claim_id = uuid.uuid4().hex
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH elegido AS (
                    SELECT id
                      FROM solicitud_automatica_outbox
                     WHERE (
                         estado IN ('PENDIENTE', 'REINTENTAR')
                         AND proximo_intento_at <= NOW()
                     ) OR (
                         estado='PROCESANDO'
                         AND claimed_at < NOW()-INTERVAL '10 minutes'
                     )
                     ORDER BY proximo_intento_at, created_at, id
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE solicitud_automatica_outbox o
                   SET estado='PROCESANDO', claim_id=%s, claimed_at=NOW(),
                       intentos=o.intentos+1, updated_at=NOW()
                  FROM elegido
                 WHERE o.id=elegido.id
                RETURNING o.*
                """,
                (claim_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def _finish_pedido(job: dict, estado: str, codigo: str = "", detalle: str = "") -> None:
    reintenta = estado == "REINTENTAR"
    demora = min(3600, 5 * (2 ** max(0, int(job.get("intentos") or 1) - 1)))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitud_automatica_outbox
                   SET estado=%s, claim_id=NULL, claimed_at=NULL,
                       proximo_intento_at=CASE WHEN %s
                           THEN NOW()+(%s * INTERVAL '1 second')
                           ELSE proximo_intento_at END,
                       ultimo_error_codigo=NULLIF(%s, ''),
                       ultimo_error=NULLIF(%s, ''), updated_at=NOW(),
                       completed_at=CASE WHEN %s IN ('COMPLETADO','CANCELADO')
                                         THEN NOW() ELSE NULL END
                 WHERE id=%s AND claim_id=%s AND estado='PROCESANDO'
                """,
                (
                    estado, reintenta, demora, codigo[:80], detalle[:500],
                    estado, job["id"], job["claim_id"],
                ),
            )


def procesar_solicitudes_automaticas(limite: int = 20) -> dict:
    procesados = errores = manuales = 0
    for _ in range(max(1, min(int(limite), 100))):
        job = _claim_pedido()
        if not job:
            break
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT estado, automatismos_bloqueados, solicitud_id
                          FROM pedidos_tienda WHERE id=%s
                        """,
                        (job["pedido_id"],),
                    )
                    pedido = cur.fetchone()
            if not pedido or pedido.get("automatismos_bloqueados") \
                    or pedido.get("estado") != "PENDIENTE":
                _finish_pedido(job, "CANCELADO", "PEDIDO_NO_ELEGIBLE")
                procesados += 1
                continue

            from servicios.solicitud_automatica import crear_desde_pedido
            resultado = crear_desde_pedido(int(job["pedido_id"]))
            if resultado.get("ok"):
                # El INSERT idempotente de la solicitud y el vínculo legado
                # ocurren en transacciones distintas. No se cierra el outbox
                # hasta comprobar que el segundo paso quedó reconciliado.
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT estado, solicitud_id
                              FROM pedidos_tienda WHERE id=%s
                            """,
                            (job["pedido_id"],),
                        )
                        vinculo = cur.fetchone()
                if not vinculo or vinculo.get("estado") != "CONVERTIDO" \
                        or not vinculo.get("solicitud_id"):
                    resultado = {
                        **resultado,
                        "reintentar": True,
                        "motivo": "Falta reconciliar el vínculo de la solicitud.",
                    }
            if resultado.get("reintentar") and int(job.get("intentos") or 1) < _MAX_ATTEMPTS:
                _finish_pedido(
                    job, "REINTENTAR", "EFECTO_TRANSITORIO",
                    str(resultado.get("motivo") or "Reintento requerido."),
                )
                errores += 1
            elif resultado.get("ok") and not resultado.get("reintentar"):
                _finish_pedido(job, "COMPLETADO")
                procesados += 1
            else:
                _finish_pedido(
                    job, "MANUAL_REVIEW", "DATOS_INCOMPLETOS",
                    str(resultado.get("motivo") or "Revisión manual requerida."),
                )
                manuales += 1
        except Exception as exc:
            codigo = type(exc).__name__
            if int(job.get("intentos") or 1) < _MAX_ATTEMPTS:
                _finish_pedido(job, "REINTENTAR", codigo, str(exc))
            else:
                _finish_pedido(job, "MANUAL_REVIEW", codigo, str(exc))
                manuales += 1
            errores += 1
    return {"procesados": procesados, "errores": errores, "manuales": manuales}


def lanzar_solicitudes_automaticas() -> None:
    """Wake-up oportunista; el scheduler y PostgreSQL garantizan durabilidad."""
    def _run():
        if not _order_worker_lock.acquire(blocking=False):
            return
        try:
            procesar_solicitudes_automaticas()
        except Exception as exc:
            print(f"[solicitud_auto] worker falló: {type(exc).__name__}")
        finally:
            _order_worker_lock.release()

    threading.Thread(
        target=_run, daemon=True, name="solicitud-automatica-outbox",
    ).start()


def encolar_fulfillment_con_cursor(
    cur, solicitud_id: int, tracking: str, courier: str,
) -> bool:
    """Inserta la notificación en la misma transacción que guarda tracking."""
    cur.execute(
        """
        WITH solicitud AS (
            SELECT id, LOWER(origen_plataforma) AS plataforma,
                   LOWER(origen_dominio) AS dominio,
                   origen_pedido_externo_id AS pedido_externo_id
              FROM solicitudes_guia
             WHERE id=%s
               AND LOWER(origen_plataforma) IN ('shopify','tiendanube')
               AND NULLIF(BTRIM(origen_dominio), '') IS NOT NULL
               AND NULLIF(BTRIM(origen_pedido_externo_id), '') IS NOT NULL
        ), origen AS (
            SELECT p.id AS pedido_id
              FROM solicitud s
              JOIN pedidos_tienda p ON (
                    p.solicitud_id=s.id OR (
                        LOWER(p.plataforma)=s.plataforma
                        AND p.pedido_externo_id=s.pedido_externo_id
                    )
              )
              JOIN tiendas_conectadas t ON t.id=p.tienda_id
             WHERE LOWER(t.dominio)=s.dominio
               AND p.estado NOT IN ('CANCELADO','CANCELACION_MANUAL')
               AND p.automatismos_bloqueados=FALSE
             ORDER BY (p.solicitud_id=s.id) DESC, p.id DESC
             LIMIT 1
        ), vinculo AS (
            UPDATE pedidos_tienda p
               SET estado='CONVERTIDO', solicitud_id=COALESCE(p.solicitud_id, %s),
                   updated_at=NOW()
              FROM origen o
             WHERE p.id=o.pedido_id
            RETURNING p.id
        )
        INSERT INTO tienda_fulfillment_outbox
            (solicitud_id, pedido_id, plataforma, dominio,
             pedido_externo_id, tracking, courier, estado,
             ultimo_error_codigo, ultimo_error)
        SELECT %s, COALESCE(v.id, o.pedido_id), s.plataforma, s.dominio,
               s.pedido_externo_id, %s, %s,
               CASE WHEN o.pedido_id IS NULL
                    THEN 'MANUAL_REVIEW' ELSE 'PENDIENTE' END,
               CASE WHEN o.pedido_id IS NULL
                    THEN 'PEDIDO_NO_VINCULADO' ELSE NULL END,
               CASE WHEN o.pedido_id IS NULL
                    THEN 'El tracking quedó guardado sin un pedido de tienda elegible.'
                    ELSE NULL END
          FROM solicitud s
          LEFT JOIN origen o ON TRUE
          LEFT JOIN vinculo v ON v.id=o.pedido_id
        ON CONFLICT (solicitud_id, plataforma, tracking) DO NOTHING
        RETURNING id
        """,
        (
            int(solicitud_id), int(solicitud_id), int(solicitud_id),
            str(tracking).strip(), str(courier or "").strip().upper(),
        ),
    )
    return cur.fetchone() is not None


def reconciliar_fulfillments_faltantes(limite: int = 200) -> int:
    """Recrea notificaciones ausentes para guías con origen verificable."""
    limite = max(1, min(int(limite), 1000))
    insertados = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.tracking, s.courier
                  FROM solicitudes_guia s
                 WHERE NULLIF(BTRIM(s.tracking), '') IS NOT NULL
                   AND NULLIF(BTRIM(s.origen_plataforma), '') IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM tienda_fulfillment_outbox o
                        WHERE o.solicitud_id=s.id AND o.tracking=s.tracking
                   )
                 ORDER BY s.guia_generada_at NULLS LAST, s.id
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
                """,
                (limite,),
            )
            filas = [dict(row) for row in cur.fetchall()]
            for fila in filas:
                if encolar_fulfillment_con_cursor(
                    cur, fila["id"], fila["tracking"], fila["courier"],
                ):
                    insertados += 1
    return insertados


def _claim_fulfillment() -> Optional[dict]:
    claim_id = uuid.uuid4().hex
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH elegido AS (
                    SELECT id, estado AS estado_anterior
                      FROM tienda_fulfillment_outbox
                     WHERE (
                         estado IN ('PENDIENTE','REINTENTAR','RECONCILIAR')
                         AND proximo_intento_at <= NOW()
                     ) OR (
                         estado='PROCESANDO'
                         AND claimed_at < NOW()-INTERVAL '10 minutes'
                     )
                     ORDER BY proximo_intento_at, created_at, id
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE tienda_fulfillment_outbox o
                   SET estado='PROCESANDO', claim_id=%s, claimed_at=NOW(),
                       intentos=o.intentos+1, updated_at=NOW()
                  FROM elegido
                 WHERE o.id=elegido.id
                RETURNING o.*, elegido.estado_anterior
                """,
                (claim_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def _finish_fulfillment(
    job: dict, estado: str, codigo: str = "", detalle: str = "",
    remote_reference: str = "",
) -> None:
    demora = min(3600, 10 * (2 ** max(0, int(job.get("intentos") or 1) - 1)))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tienda_fulfillment_outbox
                   SET estado=%s, claim_id=NULL, claimed_at=NULL,
                       proximo_intento_at=CASE
                           WHEN %s IN ('REINTENTAR','RECONCILIAR')
                           THEN NOW()+(%s * INTERVAL '1 second')
                           ELSE proximo_intento_at END,
                       ultimo_error_codigo=NULLIF(%s, ''),
                       ultimo_error=NULLIF(%s, ''),
                       remote_reference=COALESCE(NULLIF(%s, ''), remote_reference),
                       updated_at=NOW(),
                       completed_at=CASE WHEN %s='COMPLETADO' THEN NOW() ELSE NULL END
                 WHERE id=%s AND claim_id=%s AND estado='PROCESANDO'
                """,
                (
                    estado, estado, demora, codigo[:80], detalle[:500],
                    remote_reference[:200], estado, job["id"], job["claim_id"],
                ),
            )


def _ejecutar_fulfillment_bajo_lock(job: dict) -> str:
    """Serializa publicación con cancelación/redacción de la misma tienda."""
    plataforma = str(job["plataforma"]).lower()
    previo = str(job.get("estado_anterior") or "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            from servicios.integraciones_tienda import (
                _bloquear_dominio_shopify, _bloquear_dominio_tiendanube,
            )
            if plataforma == "shopify":
                _bloquear_dominio_shopify(cur, job["dominio"])
            elif plataforma == "tiendanube":
                _bloquear_dominio_tiendanube(cur, job["dominio"])
            else:
                return "MANUAL_REVIEW"
            cur.execute(
                """
                SELECT estado, automatismos_bloqueados
                  FROM pedidos_tienda WHERE id=%s
                 FOR UPDATE
                """,
                (job["pedido_id"],),
            )
            pedido = cur.fetchone()
            if not pedido or pedido.get("automatismos_bloqueados") \
                    or pedido.get("estado") != "CONVERTIDO":
                return "PEDIDO_BLOQUEADO"

            # El lock transaccional se conserva durante la llamada. Una
            # cancelación concurrente espera y luego observa el resultado
            # local; si la cancelación ganó primero, este branch no muta.
            if plataforma == "shopify":
                from servicios.shopify_app import marcar_enviado_resultado
                return marcar_enviado_resultado(
                    job["dominio"], job["pedido_externo_id"],
                    job["tracking"], job["courier"],
                    # Un claim PROCESANDO recuperado puede haber caído justo
                    # después de la mutación remota. Se trata igual que un
                    # resultado ambiguo y nunca se reescribe a ciegas.
                    solo_reconciliar=(previo in {"RECONCILIAR", "PROCESANDO"}),
                )
            from servicios.tiendanube_app import marcar_enviado
            store_id = str(job["dominio"]).replace(".tiendanube", "")
            if marcar_enviado(
                store_id, job["pedido_externo_id"], job["tracking"],
                solo_reconciliar=(previo in {"RECONCILIAR", "PROCESANDO"}),
            ):
                return "COMPLETADO"
            return (
                "REINTENTAR"
                if previo in {"RECONCILIAR", "PROCESANDO"}
                else "RECONCILIAR"
            )


def procesar_fulfillments(limite: int = 20) -> dict:
    """Publica tracking sólo si el gate explícito del piloto está activo."""
    if not _flag("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", False):
        return {"procesados": 0, "errores": 0, "manuales": 0, "disabled": True}
    procesados = errores = manuales = 0
    for _ in range(max(1, min(int(limite), 100))):
        job = _claim_fulfillment()
        if not job:
            break
        try:
            resultado = _ejecutar_fulfillment_bajo_lock(job)
            if resultado == "PEDIDO_BLOQUEADO":
                _finish_fulfillment(
                    job, "MANUAL_REVIEW", "PEDIDO_BLOQUEADO",
                    "La orden no permite notificación automática.",
                )
                manuales += 1
                continue

            if resultado == "COMPLETADO":
                _finish_fulfillment(job, "COMPLETADO")
                procesados += 1
            elif resultado == "RECONCILIAR" and int(job.get("intentos") or 1) < _MAX_ATTEMPTS:
                _finish_fulfillment(job, "RECONCILIAR", "RESULTADO_AMBIGUO")
            elif resultado == "REINTENTAR" and int(job.get("intentos") or 1) < _MAX_ATTEMPTS:
                _finish_fulfillment(job, "REINTENTAR", "NO_CONFIRMADO")
                errores += 1
            else:
                _finish_fulfillment(
                    job, "MANUAL_REVIEW", "FULFILLMENT_NO_SEGURO",
                    "No se confirmó una única operación remota segura.",
                )
                manuales += 1
        except Exception as exc:
            # Una excepción puede ocurrir después de una escritura remota.
            # Nunca se reintenta la mutación sin pasar primero por conciliación.
            estado = (
                "RECONCILIAR"
                if int(job.get("intentos") or 1) < _MAX_ATTEMPTS
                else "MANUAL_REVIEW"
            )
            _finish_fulfillment(job, estado, type(exc).__name__, str(exc))
            if estado == "MANUAL_REVIEW":
                manuales += 1
            errores += 1
    return {"procesados": procesados, "errores": errores, "manuales": manuales}


def lanzar_fulfillments() -> None:
    """Wake-up no durable; la fila PostgreSQL ya fue confirmada antes."""
    if not _flag("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", False):
        return

    def _run():
        if not _fulfillment_worker_lock.acquire(blocking=False):
            return
        try:
            procesar_fulfillments()
        except Exception as exc:
            print(f"[integraciones] worker fulfillment falló: {type(exc).__name__}")
        finally:
            _fulfillment_worker_lock.release()

    threading.Thread(
        target=_run, daemon=True, name="tienda-fulfillment-outbox",
    ).start()


def estado_operativo() -> dict:
    """Conteos sin PII para healthchecks y runbooks de Operaciones."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM solicitud_automatica_outbox
                    WHERE estado IN ('PENDIENTE','REINTENTAR','PROCESANDO')) AS pedidos_pendientes,
                  (SELECT COUNT(*) FROM solicitud_automatica_outbox
                    WHERE estado='MANUAL_REVIEW') AS pedidos_manual,
                  (SELECT COUNT(*) FROM tienda_fulfillment_outbox
                    WHERE estado IN ('PENDIENTE','REINTENTAR','RECONCILIAR','PROCESANDO')) AS fulfillment_pendiente,
                  (SELECT COUNT(*) FROM tienda_fulfillment_outbox
                    WHERE estado='MANUAL_REVIEW') AS fulfillment_manual,
                  (SELECT COUNT(*) FROM tienda_cancelacion_obligaciones
                    WHERE estado='MANUAL_REVIEW') AS cancelaciones_manual
                """
            )
            return dict(cur.fetchone())
