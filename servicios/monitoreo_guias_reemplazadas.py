"""Control único a 7 días de guías DHL reemplazadas o canceladas.

Reemplazar una guía dentro de TAURO no invalida físicamente su etiqueta en
DHL. Este módulo consulta el tracking anterior una vez al cumplirse 7 días:
sin eventos confirma la cancelación y con movimiento enciende una alerta. El
cargo cancelado jamás se reactiva acá: una eventual factura del courier exige
conciliación documentada y una decisión humana separada.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from core.database import get_conn
from core.dhl_client import DHLClient
from servicios.auditoria import registrar_evento_con_cursor


VIGILAR = "VIGILAR"
ALERTA_MOVIMIENTO = "ALERTA_MOVIMIENTO"
CERRADA = "CERRADA"
RIESGOS_VALIDOS = {VIGILAR, ALERTA_MOVIMIENTO, CERRADA}

_LOCK_NAME = "tauro:tracking:dhl:reemplazadas:diario:v1"


def _texto(valor: Any, maximo: int = 300) -> str:
    return " ".join(str(valor or "").strip().split())[:maximo]


def _id_opcional(valor: Any) -> Optional[int]:
    return int(valor) if valor is not None else None


def _clave_evento(par: tuple[int, dict]) -> tuple[str, int]:
    indice, evento = par
    timestamp = _texto(
        evento.get("timestamp")
        or evento.get("dateTime")
        or evento.get("gmtDateTime"),
        100,
    )
    if not timestamp:
        timestamp = (
            f"{_texto(evento.get('date'), 20)}"
            f"T{_texto(evento.get('time'), 30)}"
            f"{_texto(evento.get('GMTOffset'), 12)}"
        )
    return timestamp, indice


def _ultimo_evento(respuesta: dict) -> dict:
    directo = respuesta.get("ultimo_evento")
    if isinstance(directo, dict):
        return directo
    eventos = [e for e in (respuesta.get("eventos") or []) if isinstance(e, dict)]
    if not eventos:
        return {}
    return max(enumerate(eventos), key=_clave_evento)[1]


def normalizar_consulta_reemplazada(respuesta: dict) -> dict:
    """Distingue una ausencia real de eventos de un fallo de la API.

    Un 404 de tracking es la respuesta esperable mientras DHL no recibió la
    etiqueta. Cualquier evento, incluso el primero, se trata como movimiento:
    es la señal conservadora que protege a TAURO ante una posible factura.
    """
    if not isinstance(respuesta, dict):
        return {"ok": False, "error": "Respuesta inválida de DHL."}

    if not respuesta.get("encontrado"):
        if respuesta.get("http_status") == 404:
            return {"ok": True, "movimiento": False}
        return {
            "ok": False,
            "error": _texto(respuesta.get("error"), 240)
            or "DHL no devolvió información de rastreo.",
        }

    evento = _ultimo_evento(respuesta)
    if not evento:
        return {"ok": True, "movimiento": False}

    codigo = _texto(
        evento.get("typeCode")
        or evento.get("statusCode")
        or evento.get("eventType")
        or respuesta.get("estado"),
        40,
    ).upper()
    descripcion = _texto(
        evento.get("description") or respuesta.get("descripcion"), 300
    )
    fecha = _texto(
        evento.get("timestamp")
        or evento.get("dateTime")
        or evento.get("gmtDateTime"),
        100,
    )
    if not fecha:
        fecha = " ".join(
            p for p in (
                _texto(evento.get("date"), 20),
                _texto(evento.get("time"), 30),
                _texto(evento.get("GMTOffset"), 12),
            ) if p
        )
    return {
        "ok": True,
        "movimiento": True,
        "estado_courier": codigo,
        "descripcion": descripcion,
        "evento_fecha": fecha,
    }


def _candidato(reemision_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.tracking_anterior, r.riesgo_estado,
                       r.solicitud_anterior_id, r.solicitud_nueva_id,
                       r.cliente_id
                FROM solicitudes_guia_reemisiones r
                JOIN solicitudes_guia anterior
                  ON anterior.id=r.solicitud_anterior_id
                WHERE r.id=%s
                  AND r.estado='EMITIDA'
                  AND r.riesgo_estado <> 'CERRADA'
                  AND UPPER(anterior.courier)='DHL'
                  AND NULLIF(BTRIM(r.tracking_anterior), '') IS NOT NULL
                """,
                (int(reemision_id),),
            )
            fila = cur.fetchone()
    return dict(fila) if fila else None


def actualizar_tracking_reemplazado_dhl(
    reemision_id: int,
    *,
    cliente_dhl: Optional[DHLClient] = None,
    confirmar_sin_movimiento: bool = False,
) -> dict:
    """Consulta una guía vieja; sólo el job a 7 días confirma su cancelación."""
    candidato = _candidato(reemision_id)
    if not candidato:
        return {"ok": True, "omitido": True, "reemision_id": int(reemision_id)}
    if (
        not confirmar_sin_movimiento
        and candidato.get("riesgo_estado") != ALERTA_MOVIMIENTO
    ):
        return {
            "ok": True,
            "omitido": True,
            "motivo": "control_programado",
            "error": "El tracking se controla una sola vez al cumplirse 7 días.",
            "reemision_id": int(reemision_id),
        }

    cliente_dhl = cliente_dhl or DHLClient()
    try:
        respuesta = cliente_dhl.track(str(candidato["tracking_anterior"]))
    except Exception as exc:
        respuesta = {
            "encontrado": False,
            "error": f"Consulta DHL fallida ({type(exc).__name__})",
        }
    normalizado = normalizar_consulta_reemplazada(respuesta)

    with get_conn() as conn:
        with conn.cursor() as cur:
            if normalizado.get("ok") and normalizado.get("movimiento"):
                cur.execute(
                    """
                    WITH previo AS (
                        SELECT id, riesgo_estado, alerta_movimiento_at
                        FROM solicitudes_guia_reemisiones
                        WHERE id=%s AND riesgo_estado='VIGILAR'
                        FOR UPDATE
                    )
                    UPDATE solicitudes_guia_reemisiones r
                    SET riesgo_estado='ALERTA_MOVIMIENTO',
                        tracking_anterior_consultado_at=NOW(),
                        tracking_anterior_estado_courier=%s,
                        tracking_anterior_descripcion=%s,
                        tracking_anterior_evento_fecha=%s,
                        tracking_anterior_actualizado_at=NOW(),
                        tracking_anterior_error=NULL,
                        tracking_anterior_error_at=NULL,
                        alerta_movimiento_at=COALESCE(
                            r.alerta_movimiento_at, NOW()
                        ),
                        updated_at=NOW()
                    FROM previo
                    WHERE r.id=previo.id
                    RETURNING r.id, previo.alerta_movimiento_at IS NULL
                              AS alerta_nueva
                    """,
                    (
                        int(reemision_id),
                        normalizado.get("estado_courier") or "",
                        normalizado.get("descripcion") or "",
                        normalizado.get("evento_fecha") or "",
                    ),
                )
                guardado = cur.fetchone()
                if guardado and guardado.get("alerta_nueva"):
                    registrar_evento_con_cursor(
                        cur,
                        event="dhl.guia_reemplazada_con_movimiento",
                        actor_type="sistema",
                        actor_ref="tracking_dhl",
                        ip=None,
                        method=None,
                        path=None,
                        status_code=200,
                        success=True,
                        request_id=None,
                        metadata={
                            "reemision_id": int(reemision_id),
                            "solicitud_anterior_id": int(
                                candidato["solicitud_anterior_id"]
                            ),
                            "solicitud_nueva_id": _id_opcional(
                                candidato.get("solicitud_nueva_id")
                            ),
                            "tracking_anterior": candidato["tracking_anterior"],
                            "estado_courier": normalizado.get("estado_courier"),
                        },
                    )
            elif normalizado.get("ok"):
                if confirmar_sin_movimiento:
                    cur.execute(
                        """
                        UPDATE solicitudes_guia_reemisiones
                        SET riesgo_estado='CERRADA',
                            tracking_anterior_consultado_at=NOW(),
                            tracking_anterior_error=NULL,
                            tracking_anterior_error_at=NULL,
                            riesgo_resuelto_at=NOW(),
                            riesgo_resuelto_nota=(
                                'Cancelación confirmada: DHL no registró ' ||
                                'recolección ni movimientos al control de 7 días.'
                            ),
                            updated_at=NOW()
                        WHERE id=%s AND riesgo_estado='VIGILAR'
                        RETURNING id
                        """,
                        (int(reemision_id),),
                    )
                    guardado = cur.fetchone()
                    if guardado:
                        registrar_evento_con_cursor(
                            cur,
                            event="dhl.cancelacion_reemplazada_confirmada",
                            actor_type="sistema",
                            actor_ref="tracking_dhl_7_dias",
                            ip=None,
                            method=None,
                            path=None,
                            status_code=200,
                            success=True,
                            request_id=None,
                            metadata={
                                "reemision_id": int(reemision_id),
                                "solicitud_anterior_id": int(
                                    candidato["solicitud_anterior_id"]
                                ),
                                "solicitud_nueva_id": _id_opcional(
                                    candidato.get("solicitud_nueva_id")
                                ),
                                "tracking_anterior": candidato[
                                    "tracking_anterior"
                                ],
                                "control_dias": 7,
                                "movimiento_detectado": False,
                            },
                        )
                else:
                    # La consulta manual informa el estado actual pero no
                    # adelanta la confirmación: el control definitivo sigue
                    # programado para cuando se cumplan los siete días.
                    cur.execute(
                        """
                        UPDATE solicitudes_guia_reemisiones
                        SET tracking_anterior_consultado_at=NOW(),
                            tracking_anterior_error=NULL,
                            tracking_anterior_error_at=NULL,
                            updated_at=NOW()
                        WHERE id=%s AND riesgo_estado <> 'CERRADA'
                        RETURNING id
                        """,
                        (int(reemision_id),),
                    )
                    guardado = cur.fetchone()
            else:
                cur.execute(
                    """
                    UPDATE solicitudes_guia_reemisiones
                    SET tracking_anterior_consultado_at=NOW(),
                        tracking_anterior_error=%s,
                        tracking_anterior_error_at=NOW(),
                        updated_at=NOW()
                    WHERE id=%s AND riesgo_estado <> 'CERRADA'
                    RETURNING id
                    """,
                    (_texto(normalizado.get("error"), 240), int(reemision_id)),
                )
                guardado = cur.fetchone()

    return {
        **normalizado,
        "reemision_id": int(reemision_id),
        "guardado": bool(guardado),
        "cancelacion_confirmada": bool(
            normalizado.get("ok")
            and not normalizado.get("movimiento")
            and confirmar_sin_movimiento
            and guardado
        ),
        "alerta_nueva": bool(
            guardado and isinstance(guardado, dict)
            and guardado.get("alerta_nueva")
        ),
    }


def actualizar_trackings_reemplazados_dhl(
    limite: Optional[int] = None,
) -> dict:
    """Busca sólo guías que cumplieron 7 días y ejecuta su control único.

    El scheduler puede correr diariamente sin consultar diariamente cada guía:
    una fila entra al lote sólo al vencer el plazo y sale definitivamente ante
    una respuesta concluyente. Los errores técnicos se reintentan al día
    siguiente porque no prueban que la etiqueta haya sido cancelada.
    """
    if limite is None:
        try:
            limite = int(os.getenv("DHL_REPLACED_TRACKING_DAILY_LIMIT", "500"))
        except ValueError:
            limite = 500
    limite = max(1, min(int(limite), 2000))

    cliente_dhl = DHLClient()
    error_config = cliente_dhl._error_configuracion()
    if error_config:
        return {
            "ok": False,
            "omitido": True,
            "motivo": "dhl_no_configurado",
            "error": _texto(error_config, 240),
        }

    with get_conn() as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS adquirido",
                (_LOCK_NAME,),
            )
            lock = cur.fetchone()
            if not lock or not lock.get("adquirido"):
                return {"ok": True, "omitido": True, "motivo": "job_en_curso"}
        try:
            with lock_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.id
                    FROM solicitudes_guia_reemisiones r
                    JOIN solicitudes_guia anterior
                      ON anterior.id=r.solicitud_anterior_id
                    WHERE r.estado='EMITIDA'
                      AND r.riesgo_estado='VIGILAR'
                      AND UPPER(anterior.courier)='DHL'
                      AND NULLIF(BTRIM(r.tracking_anterior), '') IS NOT NULL
                      AND r.completed_at <= NOW() - INTERVAL '7 days'
                    ORDER BY
                        r.completed_at ASC NULLS FIRST,
                        r.id ASC
                    LIMIT %s
                    """,
                    (limite,),
                )
                ids = [int(fila["id"]) for fila in cur.fetchall()]

            conteos = {
                "consultados": 0,
                "sin_movimiento": 0,
                "alertas": 0,
                "alertas_nuevas": 0,
                "errores": 0,
            }
            for reemision_id in ids:
                resultado = actualizar_tracking_reemplazado_dhl(
                    reemision_id,
                    cliente_dhl=cliente_dhl,
                    confirmar_sin_movimiento=True,
                )
                if resultado.get("omitido"):
                    continue
                conteos["consultados"] += 1
                if not resultado.get("ok"):
                    conteos["errores"] += 1
                elif resultado.get("movimiento"):
                    conteos["alertas"] += 1
                    if resultado.get("alerta_nueva"):
                        conteos["alertas_nuevas"] += 1
                else:
                    conteos["sin_movimiento"] += 1
                    if resultado.get("cancelacion_confirmada"):
                        conteos.setdefault("cancelaciones_confirmadas", 0)
                        conteos["cancelaciones_confirmadas"] += 1
            conteos.setdefault("cancelaciones_confirmadas", 0)
            return {"ok": True, "candidatos": len(ids), **conteos}
        finally:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))


def listar_reemisiones_admin(riesgo: str = "", limite: int = 500) -> list[dict]:
    riesgo = _texto(riesgo, 40).upper()
    params: list[Any] = []
    where = "WHERE r.estado='EMITIDA'"
    if riesgo in RIESGOS_VALIDOS:
        where += " AND r.riesgo_estado=%s"
        params.append(riesgo)
    params.append(max(1, min(int(limite), 2000)))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT r.*, c.nombre AS cliente_nombre,
                       anterior.courier,
                       anterior.estado AS estado_anterior,
                       anterior.label_pdf IS NOT NULL AS anterior_tiene_label,
                       nueva.estado AS estado_nuevo,
                       nueva.label_pdf IS NOT NULL AS nueva_tiene_label,
                       cargo_anterior.estado AS cargo_anterior_estado,
                       cargo_anterior.nro_fc AS cargo_anterior_fc,
                       cargo_nuevo.estado AS cargo_nuevo_estado,
                       cargo_nuevo.nro_fc AS cargo_nuevo_fc,
                       r.completed_at + INTERVAL '7 days'
                         AS control_programado_at
                FROM solicitudes_guia_reemisiones r
                JOIN clientes c ON c.cliente_id=r.cliente_id
                JOIN solicitudes_guia anterior
                  ON anterior.id=r.solicitud_anterior_id
                LEFT JOIN solicitudes_guia nueva
                  ON nueva.id=r.solicitud_nueva_id
                LEFT JOIN envios cargo_anterior
                  ON cargo_anterior.solicitud_id=r.solicitud_anterior_id
                LEFT JOIN envios cargo_nuevo
                  ON cargo_nuevo.solicitud_id=r.solicitud_nueva_id
                {where}
                ORDER BY
                    CASE r.riesgo_estado
                        WHEN 'ALERTA_MOVIMIENTO' THEN 0
                        WHEN 'VIGILAR' THEN 1 ELSE 2
                    END,
                    r.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            filas = []
            for fila in cur.fetchall():
                item = dict(fila)
                if item.get("operacion") == "CANCELACION":
                    item["cuenta_consistente"] = (
                        item.get("cargo_anterior_estado") in {None, "CANCELADO"}
                        and item.get("cargo_nuevo_estado") is None
                    )
                else:
                    item["cuenta_consistente"] = (
                        item.get("cargo_anterior_estado") == "CANCELADO"
                        and item.get("cargo_nuevo_estado") == "ACTIVO"
                    )
                filas.append(item)
            return filas


def resumen_reemisiones_admin() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE estado='EMITIDA' AND riesgo_estado='VIGILAR'
                    ) AS vigilar,
                    COUNT(*) FILTER (
                        WHERE estado='EMITIDA'
                          AND riesgo_estado='ALERTA_MOVIMIENTO'
                    ) AS alertas,
                    COUNT(*) FILTER (
                        WHERE estado='EMITIDA' AND riesgo_estado='CERRADA'
                    ) AS cerradas
                FROM solicitudes_guia_reemisiones
                """
            )
            fila = cur.fetchone() or {}
    return {k: int(fila.get(k) or 0) for k in ("vigilar", "alertas", "cerradas")}


def contar_alertas_reemplazadas() -> int:
    return resumen_reemisiones_admin()["alertas"]


def cerrar_control_reemision(reemision_id: int, nota: str) -> dict:
    nota = _texto(nota, 500)
    if len(nota) < 5:
        return {"ok": False, "error": "Indicá cómo se verificó o resolvió el caso."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitudes_guia_reemisiones
                SET riesgo_estado='CERRADA', riesgo_resuelto_at=NOW(),
                    riesgo_resuelto_nota=%s, updated_at=NOW()
                WHERE id=%s AND estado='EMITIDA'
                  AND riesgo_estado='ALERTA_MOVIMIENTO'
                RETURNING id, solicitud_anterior_id, solicitud_nueva_id,
                          tracking_anterior, alerta_movimiento_at
                """,
                (nota, int(reemision_id)),
            )
            fila = cur.fetchone()
            if not fila:
                return {
                    "ok": False,
                    "error": (
                        "Sólo se cierra manualmente una alerta con movimiento; "
                        "sin eventos el sistema confirma la cancelación a los 7 días."
                    ),
                }
            registrar_evento_con_cursor(
                cur,
                event="admin.cerrar_control_guia_reemplazada",
                actor_type="admin",
                actor_ref="admin",
                ip=None,
                method=None,
                path=None,
                status_code=200,
                success=True,
                request_id=None,
                metadata={
                    "reemision_id": int(reemision_id),
                    "solicitud_anterior_id": int(fila["solicitud_anterior_id"]),
                    "solicitud_nueva_id": _id_opcional(
                        fila.get("solicitud_nueva_id")
                    ),
                    "tracking_anterior": fila["tracking_anterior"],
                    "tuvo_movimiento": bool(fila.get("alerta_movimiento_at")),
                },
            )
    return {"ok": True}


def reabrir_control_reemision(reemision_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitudes_guia_reemisiones
                SET riesgo_estado=CASE
                        WHEN alerta_movimiento_at IS NULL THEN 'VIGILAR'
                        ELSE 'ALERTA_MOVIMIENTO'
                    END,
                    riesgo_resuelto_at=NULL, riesgo_resuelto_nota=NULL,
                    tracking_anterior_consultado_at=NULL, updated_at=NOW()
                WHERE id=%s AND estado='EMITIDA' AND riesgo_estado='CERRADA'
                RETURNING id, solicitud_anterior_id, solicitud_nueva_id
                """,
                (int(reemision_id),),
            )
            fila = cur.fetchone()
            if not fila:
                return {"ok": False, "error": "El control no está cerrado o no existe."}
            registrar_evento_con_cursor(
                cur,
                event="admin.reabrir_control_guia_reemplazada",
                actor_type="admin",
                actor_ref="admin",
                ip=None,
                method=None,
                path=None,
                status_code=200,
                success=True,
                request_id=None,
                metadata={
                    "reemision_id": int(reemision_id),
                    "solicitud_anterior_id": int(fila["solicitud_anterior_id"]),
                    "solicitud_nueva_id": _id_opcional(
                        fila.get("solicitud_nueva_id")
                    ),
                },
            )
    return {"ok": True}
