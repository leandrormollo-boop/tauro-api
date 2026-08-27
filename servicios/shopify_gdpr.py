"""Flujo durable para solicitudes Shopify ``customers/data_request``.

La cola persiste solamente identificadores de la tienda, de la solicitud y
de las ordenes. El webhook original contiene PII del comprador (email,
telefono y, a veces, nombre), por lo que nunca se almacena ni se incluye en
logs o en el correo operativo. Los datos se reconstruyen desde las fuentes
operativas unicamente cuando un administrador autenticado descarga el JSON.
"""

from __future__ import annotations

from html import escape
import re
import secrets
from typing import Any

from psycopg2.extras import Json

from core.database import get_conn
from core.email_transport import OPERATIONS_EMAIL, send_transactional_email


MAX_INTENTOS = 5
CLAIM_STALE_MINUTOS = 15
RETENCION_RESUELTAS_DIAS = 90
MAX_ORDER_IDS = 500
_ID_NUMERICO = re.compile(r"^[0-9]{1,32}$")


class SolicitudGDPRInvalida(ValueError):
    """El payload firmado no tiene identificadores operables y no sensibles."""


class SolicitudGDPRConflictiva(RuntimeError):
    """El request_id ya existe asociado a otro contenido firmado."""


def _id_numerico(valor: Any, nombre: str) -> str:
    if isinstance(valor, bool):
        raise SolicitudGDPRInvalida(f"{nombre} invalido")
    texto = str(valor or "").strip()
    if not _ID_NUMERICO.fullmatch(texto):
        raise SolicitudGDPRInvalida(f"{nombre} invalido")
    return texto


def _email_busqueda(valor: Any) -> str:
    """Normalizacion minima para comparar un identificador firmado en SQL.

    No se valida como destinatario SMTP: emails Unicode o formatos atipicos
    siguen siendo claves legitimas de un comprador Shopify. El tope evita
    retener accidentalmente un cuerpo arbitrario en memoria.
    """
    texto = str(valor or "").strip().lower()
    return texto if 0 < len(texto) <= 320 else ""


def normalizar_order_ids(valor: Any, *, campo: str) -> list[str]:
    if valor is None:
        return []
    if not isinstance(valor, list) or len(valor) > MAX_ORDER_IDS:
        raise SolicitudGDPRInvalida(f"{campo} invalido")
    ordenes: list[str] = []
    vistas: set[str] = set()
    for crudo in valor:
        order_id = _id_numerico(crudo, "order_id")
        if order_id not in vistas:
            vistas.add(order_id)
            ordenes.append(order_id)
    return ordenes


def normalizar_payload_data_request(datos: Any) -> dict[str, Any]:
    """Extrae el contrato minimo sin copiar identidad del comprador."""
    if not isinstance(datos, dict):
        raise SolicitudGDPRInvalida("payload invalido")
    data_request = datos.get("data_request")
    if not isinstance(data_request, dict):
        raise SolicitudGDPRInvalida("falta data_request")

    request_id = _id_numerico(data_request.get("id"), "request_id")
    shop_id = _id_numerico(datos.get("shop_id"), "shop_id")
    dominio = str(datos.get("shop_domain") or "").strip().lower()

    ordenes = normalizar_order_ids(
        datos.get("orders_requested"), campo="orders_requested",
    )

    customer = datos.get("customer") if isinstance(datos.get("customer"), dict) else {}
    # Se devuelve solamente para una busqueda inmediata cuando Shopify no
    # enumera ordenes. El endpoint la quita antes de persistir la referencia.
    customer_email = _email_busqueda(customer.get("email"))
    return {
        "request_id": request_id,
        "dominio": dominio,
        "shop_id": shop_id,
        "orders_requested": ordenes,
        "customer_email_memoria": customer_email,
    }


def resolver_order_ids_por_email(dominio: str, customer_email: str) -> list[str]:
    """Busca IDs por email sin persistirlo ni retornarlo al caller.

    Shopify puede enviar ``orders_requested: []`` aunque TAURO tenga ordenes
    de ese comprador. El email firmado se usa como parametro transitorio para
    resolver tanto pedidos vinculados como payloads huerfanos.
    """
    dominio = (dominio or "").strip().lower()
    customer_email = _email_busqueda(customer_email)
    if not dominio or not customer_email:
        return []
    from servicios.integraciones_tienda import _ensure_tablas
    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pedido_externo_id
                  FROM pedidos_tienda p
                  JOIN tiendas_conectadas t ON t.id = p.tienda_id
                 WHERE t.dominio = %s
                   AND LOWER(COALESCE(p.destinatario->>'email', '')) = %s
                UNION
                SELECT pedido_externo_id
                  FROM pedidos_huerfanos
                 WHERE dominio = %s
                   AND LOWER(COALESCE(
                       payload->'destinatario'->>'email',
                       payload->>'email',
                       payload->>'contact_email',
                       payload->'customer'->>'email',
                       ''
                   )) = %s
                UNION
                SELECT origen_pedido_externo_id AS pedido_externo_id
                  FROM solicitudes_guia
                 WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
                   AND LOWER(COALESCE(origen_dominio, '')) = %s
                   AND NULLIF(origen_pedido_externo_id, '') IS NOT NULL
                   AND LOWER(COALESCE(dest_email, '')) = %s
                UNION
                SELECT origen_pedido_externo_id AS pedido_externo_id
                  FROM direcciones
                 WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
                   AND LOWER(COALESCE(origen_dominio, '')) = %s
                   AND NULLIF(origen_pedido_externo_id, '') IS NOT NULL
                   AND LOWER(COALESCE(email, '')) = %s
                 ORDER BY pedido_externo_id
                """,
                (
                    dominio, customer_email,
                    dominio, customer_email,
                    dominio, customer_email,
                    dominio, customer_email,
                ),
            )
            return [str(fila["pedido_externo_id"]) for fila in cur.fetchall()]


def encolar_data_request(
    *, request_id: str, dominio: str, shop_id: str,
    orders_requested: list[str],
) -> dict[str, Any]:
    """Confirma durablemente la obligacion antes de responder el webhook.

    Un retry identico es exitoso e idempotente. El mismo ``request_id`` con
    otra tienda u ordenes se rechaza en vez de pisar evidencia ya recibida.
    """
    request_id = _id_numerico(request_id, "request_id")
    shop_id = _id_numerico(shop_id, "shop_id")
    dominio = (dominio or "").strip().lower()
    # La lista es una referencia, no una secuencia: canonicalizarla evita que
    # un retry con los mismos IDs en otro orden parezca otro pedido GDPR.
    ordenes = sorted(
        normalizar_order_ids(orders_requested, campo="orders_requested"),
        key=int,
    )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shopify_gdpr_solicitudes (
                    request_id, dominio, shop_id, orders_requested,
                    estado, intentos, proximo_intento_at,
                    creado_at, actualizado_at
                ) VALUES (%s, %s, %s, %s, 'PENDIENTE', 0, NOW(), NOW(), NOW())
                ON CONFLICT (shop_id, request_id) DO NOTHING
                RETURNING id, request_id, estado
                """,
                (request_id, dominio, shop_id, Json(ordenes)),
            )
            fila = cur.fetchone()
            creada = bool(fila)
            if not fila:
                cur.execute(
                    """
                    SELECT id, request_id, dominio, shop_id, orders_requested,
                           estado
                      FROM shopify_gdpr_solicitudes
                     WHERE shop_id = %s AND request_id = %s
                    """,
                    (shop_id, request_id),
                )
                fila = cur.fetchone()
                if not fila:
                    raise SolicitudGDPRConflictiva("request_id no recuperable")
                existente = dict(fila)
                ordenes_existentes = sorted(
                    normalizar_order_ids(
                        existente.get("orders_requested") or [],
                        campo="orders_requested",
                    ),
                    key=int,
                )
                if (
                    str(existente.get("dominio") or "").lower() != dominio
                    or str(existente.get("shop_id") or "") != shop_id
                    or ordenes_existentes != ordenes
                ):
                    raise SolicitudGDPRConflictiva("request_id reutilizado")
            # El endpoint devuelve 200 recien despues de que este COMMIT
            # confirme. Si falla, la excepcion se convierte en 503.
            conn.commit()
    return {
        "id": int((fila or {}).get("id") or 0),
        "request_id": request_id,
        "estado": str((fila or {}).get("estado") or "PENDIENTE"),
        "creada": creada,
    }


def recuperar_claims_vencidos() -> int:
    """Aisla resultados SMTP ambiguos para no duplicar notificaciones."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopify_gdpr_solicitudes
                   SET estado = 'VERIFICAR_EMAIL',
                       claim_id = NULL,
                       claimed_at = NULL,
                       ultimo_error_code = 'CLAIM_EXPIRED',
                       actualizado_at = NOW()
                 WHERE estado = 'PROCESANDO'
                   AND claimed_at < NOW() - (%s * INTERVAL '1 minute')
                """,
                (CLAIM_STALE_MINUTOS,),
            )
            cantidad = max(int(cur.rowcount or 0), 0)
            conn.commit()
    return cantidad


def _reclamar_siguiente() -> dict[str, Any] | None:
    claim_id = secrets.token_urlsafe(18)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidata AS (
                    SELECT id
                      FROM shopify_gdpr_solicitudes
                     WHERE estado = 'PENDIENTE'
                       AND intentos < %s
                       AND proximo_intento_at <= NOW()
                     ORDER BY creado_at, id
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE shopify_gdpr_solicitudes s
                   SET estado = 'PROCESANDO',
                       intentos = s.intentos + 1,
                       claim_id = %s,
                       claimed_at = NOW(),
                       actualizado_at = NOW()
                  FROM candidata
                 WHERE s.id = candidata.id
                RETURNING s.id, s.request_id, s.dominio, s.shop_id,
                          s.orders_requested, s.intentos, s.claim_id
                """,
                (MAX_INTENTOS, claim_id),
            )
            fila = cur.fetchone()
            conn.commit()
    return dict(fila) if fila else None


def _admin_url() -> str:
    # El correo siempre dirige al origen productivo; una BASE_URL de preview o
    # manipulada no debe sacar al admin del dominio oficial.
    return "https://taurosolutions.ar/admin/shopify/privacidad"


def _marcar_notificada(fila: dict[str, Any], message_id: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopify_gdpr_solicitudes
                   SET estado = 'NOTIFICADO',
                       message_id = %s,
                       notificado_at = NOW(),
                       ultimo_error_code = NULL,
                       claim_id = NULL,
                       claimed_at = NULL,
                       actualizado_at = NOW()
                 WHERE id = %s AND estado = 'PROCESANDO' AND claim_id = %s
                """,
                (message_id or None, fila["id"], fila["claim_id"]),
            )
            actualizada = cur.rowcount == 1
            conn.commit()
    return actualizada


def _marcar_fallo(fila: dict[str, Any], codigo: str) -> str:
    reintenta = int(fila.get("intentos") or 0) < MAX_INTENTOS
    demora = min(180, 5 * (2 ** max(int(fila.get("intentos") or 1) - 1, 0)))
    estado = "PENDIENTE" if reintenta else "FALLIDO"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopify_gdpr_solicitudes
                   SET estado = %s,
                       proximo_intento_at = CASE
                           WHEN %s THEN NOW() + (%s * INTERVAL '1 minute')
                           ELSE proximo_intento_at
                       END,
                       ultimo_error_code = %s,
                       claim_id = NULL,
                       claimed_at = NULL,
                       actualizado_at = NOW()
                 WHERE id = %s AND estado = 'PROCESANDO' AND claim_id = %s
                """,
                (
                    estado, reintenta, demora, (codigo or "SMTP_ERROR")[:80],
                    fila["id"], fila["claim_id"],
                ),
            )
            conn.commit()
    return estado


def _marcar_incierto(fila: dict[str, Any], codigo: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopify_gdpr_solicitudes
                   SET estado = 'VERIFICAR_EMAIL',
                       ultimo_error_code = %s,
                       claim_id = NULL,
                       claimed_at = NULL,
                       actualizado_at = NOW()
                 WHERE id = %s AND estado = 'PROCESANDO' AND claim_id = %s
                """,
                ((codigo or "SMTP_OUTCOME_UNKNOWN")[:80], fila["id"], fila["claim_id"]),
            )
            conn.commit()
    return "VERIFICAR_EMAIL"


def _notificar_reclamada(fila: dict[str, Any]) -> str:
    dominio = str(fila.get("dominio") or "")
    request_id = str(fila.get("request_id") or "")
    cantidad = len(fila.get("orders_requested") or [])
    link = _admin_url()
    dominio_html = escape(dominio)
    request_html = escape(request_id)

    resultado = send_transactional_email(
        recipient=OPERATIONS_EMAIL,
        subject=f"Shopify: solicitud de acceso a datos {request_id}",
        text_body=(
            "Shopify registro una solicitud de acceso a datos.\n\n"
            f"Tienda: {dominio}\n"
            f"Solicitud: {request_id}\n"
            f"Ordenes solicitadas: {cantidad}\n"
            f"Gestionar: {link}\n"
        ),
        html_body=(
            "<p>Shopify registró una solicitud de acceso a datos.</p>"
            f"<p><b>Tienda:</b> {dominio_html}<br>"
            f"<b>Solicitud:</b> {request_html}<br>"
            f"<b>Órdenes solicitadas:</b> {cantidad}</p>"
            f'<p><a href="{link}">Abrir privacidad en TAURO Admin</a></p>'
        ),
        reply_to=OPERATIONS_EMAIL,
        dedupe_key=f"shopify-gdpr:{fila.get('shop_id')}:{request_id}",
    )
    if resultado.accepted:
        try:
            return (
                "NOTIFICADO"
                if _marcar_notificada(fila, resultado.message_id)
                else "CLAIM_LOST"
            )
        except Exception:
            # SMTP ya acepto el mensaje: reintentarlo podria duplicarlo. Se
            # aisla para conciliacion manual sin perder la solicitud.
            return _marcar_incierto(fila, "SMTP_ACCEPTED_DB_UNKNOWN")
    return _marcar_fallo(fila, str(resultado.code or "SMTP_ERROR"))


def procesar_solicitudes(limite: int = 10) -> dict[str, int]:
    """Worker concurrente, durable y acotado para APScheduler."""
    limite = min(max(int(limite or 0), 1), 50)
    resumen = {
        "claims_vencidos": recuperar_claims_vencidos(),
        "procesadas": 0,
        "notificadas": 0,
        "reprogramadas": 0,
        "fallidas": 0,
    }
    for _ in range(limite):
        fila = _reclamar_siguiente()
        if not fila:
            break
        resumen["procesadas"] += 1
        try:
            estado = _notificar_reclamada(fila)
        except Exception:
            # No guardar el texto de la excepcion: los errores de DB/SMTP
            # pueden contener datos de infraestructura o del mensaje.
            estado = _marcar_fallo(fila, "INTERNAL_ERROR")
        if estado == "NOTIFICADO":
            resumen["notificadas"] += 1
        elif estado == "PENDIENTE":
            resumen["reprogramadas"] += 1
        else:
            resumen["fallidas"] += 1
    return resumen


def listar_pendientes() -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, request_id, dominio, shop_id,
                       jsonb_array_length(orders_requested) AS cantidad_ordenes,
                       estado, intentos, ultimo_error_code, message_id,
                       notificado_at, creado_at, actualizado_at
                  FROM shopify_gdpr_solicitudes
                 WHERE estado <> 'RESUELTO'
                 ORDER BY creado_at, id
                """
            )
            return [dict(fila) for fila in cur.fetchall()]


def _obtener_solicitud(solicitud_id: int, cur) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT request_id, dominio, shop_id, orders_requested, estado,
               creado_at, notificado_at, resuelto_at
          FROM shopify_gdpr_solicitudes
         WHERE id = %s
        """,
        (solicitud_id,),
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def generar_exportacion(solicitud_id: int) -> dict[str, Any] | None:
    """Reconstruye los datos al momento de la descarga; no crea snapshots."""
    solicitud_id = int(solicitud_id)
    if solicitud_id <= 0:
        raise SolicitudGDPRInvalida("solicitud_id invalido")
    with get_conn() as conn:
        with conn.cursor() as cur:
            solicitud = _obtener_solicitud(solicitud_id, cur)
            if not solicitud:
                return None
            dominio = str(solicitud["dominio"])
            ordenes = [str(x) for x in (solicitud.get("orders_requested") or [])]
            pedidos: list[dict[str, Any]] = []
            huerfanos: list[dict[str, Any]] = []
            if ordenes:
                cur.execute(
                    """
                    SELECT p.pedido_externo_id, p.numero, p.estado,
                           p.destinatario, p.items, p.valor_total, p.moneda,
                           p.flete_cobrado, p.solicitud_id, p.created_at
                      FROM pedidos_tienda p
                      JOIN tiendas_conectadas t ON t.id = p.tienda_id
                     WHERE t.dominio = %s
                       AND p.pedido_externo_id = ANY(%s)
                     ORDER BY p.created_at, p.id
                    """,
                    (dominio, ordenes),
                )
                pedidos = [dict(fila) for fila in cur.fetchall()]
                cur.execute(
                    """
                    SELECT pedido_externo_id, payload, created_at
                      FROM pedidos_huerfanos
                     WHERE dominio = %s AND pedido_externo_id = ANY(%s)
                     ORDER BY created_at, id
                    """,
                    (dominio, ordenes),
                )
                huerfanos = [dict(fila) for fila in cur.fetchall()]

            solicitudes_ids = sorted({
                int(pedido["solicitud_id"])
                for pedido in pedidos
                if pedido.get("solicitud_id") is not None
            })
            guias: list[dict[str, Any]] = []
            direcciones: list[dict[str, Any]] = []
            envios: list[dict[str, Any]] = []
            if ordenes:
                cur.execute(
                    """
                    SELECT id, estado, producto_alias, cantidad, destino_pais,
                           dest_nombre, dest_contacto, dest_documento,
                           dest_email, dest_telefono, dest_direccion,
                           dest_ciudad, dest_estado, dest_zip,
                           observaciones, peso_kg, largo_cm, ancho_cm, alto_cm,
                           valor_declarado_usd, tracking, courier,
                           servicio_courier, created_at, updated_at,
                           origen_plataforma, origen_dominio,
                           origen_pedido_externo_id,
                           (label_pdf IS NOT NULL OR NULLIF(guia_url, '') IS NOT NULL)
                               AS tiene_label
                      FROM solicitudes_guia
                     WHERE id = ANY(%s)
                        OR (
                            LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
                            AND LOWER(COALESCE(origen_dominio, '')) = %s
                            AND origen_pedido_externo_id = ANY(%s)
                        )
                     ORDER BY created_at, id
                    """,
                    (solicitudes_ids or [-1], dominio, ordenes),
                )
                guias = [dict(fila) for fila in cur.fetchall()]
                cur.execute(
                    """
                    SELECT id, tipo, alias, nombre, documento, email, telefono,
                           direccion, ciudad, estado, cp, pais, notas,
                           created_at, updated_at, origen_plataforma,
                           origen_dominio, origen_pedido_externo_id
                      FROM direcciones
                     WHERE LOWER(COALESCE(origen_plataforma, '')) = 'shopify'
                       AND LOWER(COALESCE(origen_dominio, '')) = %s
                       AND origen_pedido_externo_id = ANY(%s)
                     ORDER BY created_at, id
                    """,
                    (dominio, ordenes),
                )
                direcciones = [dict(fila) for fila in cur.fetchall()]

                guias_ids = [int(guia["id"]) for guia in guias]
                if guias_ids:
                    cur.execute(
                        """
                        SELECT id, cliente_id, fecha, nro_fc, monto_ars,
                               estado, descripcion, tracking, solicitud_id,
                               ambito, factura_nombre, created_at,
                               (factura_pdf IS NOT NULL) AS tiene_factura_pdf
                          FROM envios
                         WHERE solicitud_id = ANY(%s)
                         ORDER BY fecha, id
                        """,
                        (guias_ids,),
                    )
                    envios = [dict(fila) for fila in cur.fetchall()]

    return {
        "solicitud": {
            "request_id": solicitud["request_id"],
            "shop_id": solicitud["shop_id"],
            "shop_domain": solicitud["dominio"],
            "orders_requested": ordenes,
            "received_at": solicitud.get("creado_at"),
        },
        "datos_en_tauro": {
            "pedidos_vinculados": pedidos,
            # El payload se lee de la fuente original solo durante esta
            # descarga autenticada. No se duplica en la cola GDPR.
            "pedidos_sin_vincular": huerfanos,
            "solicitudes_guia_vinculadas": guias,
            "direcciones_shopify_derivadas": direcciones,
            "envios_financieros_vinculados": envios,
            "retencion_y_adjuntos": {
                "envios_financieros_conservados": bool(envios),
                "motivo": "registro contable y financiero",
                "adjuntos_binarios_no_incluidos": [
                    "solicitudes_guia.label_pdf",
                    "envios.factura_pdf",
                ],
                "presencia_adjuntos_indicada_por": [
                    "tiene_label",
                    "tiene_factura_pdf",
                ],
            },
        },
    }


def marcar_resuelta(solicitud_id: int) -> dict[str, Any] | None:
    solicitud_id = int(solicitud_id)
    if solicitud_id <= 0:
        raise SolicitudGDPRInvalida("solicitud_id invalido")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopify_gdpr_solicitudes
                   SET estado = 'RESUELTO',
                       resuelto_at = COALESCE(resuelto_at, NOW()),
                       claim_id = NULL,
                       claimed_at = NULL,
                       actualizado_at = NOW()
                 WHERE id = %s
                RETURNING id, request_id, shop_id, dominio,
                          jsonb_array_length(orders_requested) AS cantidad_ordenes,
                          resuelto_at
                """,
                (solicitud_id,),
            )
            fila = cur.fetchone()
            conn.commit()
    return dict(fila) if fila else None


def limpiar_resueltas() -> int:
    """Elimina metadatos ya resueltos luego de los 90 dias acordados."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM shopify_gdpr_solicitudes
                 WHERE estado = 'RESUELTO'
                   AND resuelto_at < NOW() - (%s * INTERVAL '1 day')
                """,
                (RETENCION_RESUELTAS_DIAS,),
            )
            cantidad = max(int(cur.rowcount or 0), 0)
            conn.commit()
    return cantidad
