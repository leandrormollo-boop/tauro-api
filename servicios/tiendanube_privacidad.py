"""Operación autenticada de solicitudes ``customers/data_request``.

La cola de Tiendanube conserva solamente referencias. Este módulo reconstruye
los datos vinculados en el momento de la descarga, sin crear otra copia de PII,
sin incluir binarios y sin exponer importes o documentos contables internos.
"""

from __future__ import annotations

from typing import Any

from core.database import get_conn


MAX_RECURSOS = 500


class SolicitudPrivacidadInvalida(ValueError):
    """El identificador administrativo no es válido."""


def _solicitud_id(valor: Any) -> int:
    if isinstance(valor, bool):
        raise SolicitudPrivacidadInvalida("solicitud_id invalido")
    try:
        resultado = int(valor)
    except (TypeError, ValueError) as exc:
        raise SolicitudPrivacidadInvalida("solicitud_id invalido") from exc
    if resultado <= 0:
        raise SolicitudPrivacidadInvalida("solicitud_id invalido")
    return resultado


def _recursos(valor: Any) -> list[str]:
    """Normaliza referencias persistidas sin convertirlas en datos confiables."""
    if not isinstance(valor, list):
        return []
    resultado: list[str] = []
    vistos: set[str] = set()
    for crudo in valor[:MAX_RECURSOS]:
        recurso = str(crudo or "").strip()[:80]
        if recurso and recurso not in vistos:
            vistos.add(recurso)
            resultado.append(recurso)
    return resultado


def listar_pendientes() -> list[dict[str, Any]]:
    """Lista accesos de datos y eventos destructivos en cuarentena."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, request_id, store_id, tipo, customer_id,
                       jsonb_array_length(recursos) AS cantidad_recursos,
                       estado, creado_at
                  FROM tiendanube_privacidad_solicitudes
                 WHERE estado <> 'RESUELTO'
                 ORDER BY creado_at, id
                """
            )
            return [dict(fila) for fila in cur.fetchall()]


def _obtener_solicitud(solicitud_id: int, cur) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT request_id, store_id, tipo, customer_id, recursos, estado,
               creado_at, resuelto_at
          FROM tiendanube_privacidad_solicitudes
         WHERE id = %s
           AND tipo = 'customers/data_request'
        """,
        (solicitud_id,),
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def generar_exportacion(solicitud_id: int) -> dict[str, Any] | None:
    """Reconstruye el JSON al descargarlo; no persiste snapshots ni binarios."""
    solicitud_id = _solicitud_id(solicitud_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            solicitud = _obtener_solicitud(solicitud_id, cur)
            if not solicitud:
                return None

            store_id = str(solicitud.get("store_id") or "")
            dominio = f"{store_id}.tiendanube"
            recursos = _recursos(solicitud.get("recursos"))
            pedidos: list[dict[str, Any]] = []
            guias: list[dict[str, Any]] = []
            direcciones: list[dict[str, Any]] = []
            envios: list[dict[str, Any]] = []

            if recursos:
                cur.execute(
                    """
                    SELECT p.pedido_externo_id, p.numero, p.estado,
                           p.destinatario, p.items, p.valor_total, p.moneda,
                           p.flete_cobrado, p.flete_detalle, p.created_at
                      FROM pedidos_tienda p
                      JOIN tiendas_conectadas t ON t.id = p.tienda_id
                     WHERE t.dominio = %s
                       AND t.plataforma = 'tiendanube'
                       AND p.plataforma = 'tiendanube'
                       AND p.pedido_externo_id = ANY(%s)
                     ORDER BY p.created_at, p.id
                    """,
                    (dominio, recursos),
                )
                pedidos = [dict(fila) for fila in cur.fetchall()]

                cur.execute(
                    """
                    SELECT id, estado, producto_alias, cantidad, destino_pais,
                           dest_nombre, dest_contacto, dest_documento,
                           dest_email, dest_telefono, dest_direccion,
                           dest_ciudad, dest_estado, dest_zip, observaciones,
                           peso_kg, largo_cm, ancho_cm, alto_cm,
                           valor_declarado_usd, tracking, courier,
                           servicio_courier, created_at, updated_at,
                           origen_plataforma, origen_dominio,
                           origen_pedido_externo_id,
                           (label_pdf IS NOT NULL OR NULLIF(guia_url, '') IS NOT NULL)
                               AS tiene_etiqueta
                      FROM solicitudes_guia
                     WHERE LOWER(COALESCE(origen_plataforma, '')) = 'tiendanube'
                       AND LOWER(COALESCE(origen_dominio, '')) = %s
                       AND origen_pedido_externo_id = ANY(%s)
                     ORDER BY created_at, id
                    """,
                    (dominio, recursos),
                )
                guias = [dict(fila) for fila in cur.fetchall()]

                cur.execute(
                    """
                    SELECT id, tipo, alias, nombre, documento, email, telefono,
                           direccion, ciudad, estado, cp, pais, notas,
                           created_at, updated_at, origen_plataforma,
                           origen_dominio, origen_pedido_externo_id
                      FROM direcciones
                     WHERE LOWER(COALESCE(origen_plataforma, '')) = 'tiendanube'
                       AND LOWER(COALESCE(origen_dominio, '')) = %s
                       AND origen_pedido_externo_id = ANY(%s)
                     ORDER BY created_at, id
                    """,
                    (dominio, recursos),
                )
                direcciones = [dict(fila) for fila in cur.fetchall()]

                guias_ids = [int(guia["id"]) for guia in guias]
                if guias_ids:
                    cur.execute(
                        """
                        SELECT id, fecha, estado, descripcion, tracking,
                               solicitud_id, ambito, created_at
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
            "store_id": store_id,
            "customer_id": solicitud.get("customer_id") or "",
            "resources_requested": recursos,
            "received_at": solicitud.get("creado_at"),
        },
        "datos_en_tauro": {
            "pedidos_vinculados": pedidos,
            "solicitudes_guia_vinculadas": guias,
            "direcciones_tiendanube_derivadas": direcciones,
            "envios_operativos_vinculados": envios,
            "adjuntos_binarios_no_incluidos": [
                "solicitudes_guia.label_pdf",
                "envios.factura_pdf",
            ],
        },
    }


def marcar_resuelta(solicitud_id: int) -> dict[str, Any] | None:
    """Cierra la solicitud en forma idempotente después de entregar los datos."""
    solicitud_id = _solicitud_id(solicitud_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tiendanube_privacidad_solicitudes
                   SET estado = 'RESUELTO',
                       resolucion = 'DATOS_ENTREGADOS',
                       resuelto_at = COALESCE(resuelto_at, NOW())
                 WHERE id = %s
                   AND tipo = 'customers/data_request'
                RETURNING id, request_id, store_id, customer_id,
                          jsonb_array_length(recursos) AS cantidad_recursos,
                          resuelto_at
                """,
                (solicitud_id,),
            )
            fila = cur.fetchone()
            conn.commit()
    return dict(fila) if fila else None


def resolver_cuarentena(
    solicitud_id: int,
    accion: str = "MANTENER_INSTALACION_ACTUAL",
) -> dict[str, Any] | None:
    """Cierra una contradicción sólo con una acción segura y auditable.

    La acción disponible nunca aplica el evento destructivo: confirma que el
    operador revisó el caso y preserva la instalación OAuth vigente.
    """
    solicitud_id = _solicitud_id(solicitud_id)
    if accion != "MANTENER_INSTALACION_ACTUAL":
        raise SolicitudPrivacidadInvalida("accion de cuarentena invalida")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tiendanube_privacidad_solicitudes
                   SET estado = 'RESUELTO', resolucion = %s,
                       resuelto_at = COALESCE(resuelto_at, NOW())
                 WHERE id = %s
                   AND estado = 'CUARENTENA'
                   AND tipo IN ('store/redact', 'app/uninstalled', 'app/suspended')
                RETURNING id, request_id, store_id, tipo, resolucion, resuelto_at
                """,
                (accion, solicitud_id),
            )
            fila = cur.fetchone()
            conn.commit()
    return dict(fila) if fila else None
