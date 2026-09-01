"""Rastreo persistido de envíos del portal.

La API del courier se consulta desde un job diario. Las páginas del cliente
leen el último snapshot confirmado en PostgreSQL: abrir o refrescar el portal
no consume cuota de DHL ni vuelve inestable la interfaz.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any, Optional

from core.database import get_conn
from core.dhl_client import DHLClient


PROCESO_ENTREGA = "PROCESO_ENTREGA"
ENTREGADO = "ENTREGADO"
RETENIDO = "RETENIDO"
ESTADOS_TRACKING = {PROCESO_ENTREGA, ENTREGADO, RETENIDO}
ESTADOS_FINALES = {ENTREGADO}

_LOCK_NAME = "tauro:tracking:dhl:diario:v1"
_DELIVERY_CODES = {"OK", "DEL", "DL"}
_HOLD_CODES = {"OH"}
_DELIVERY_WORDS = (
    "DELIVERED",
    "ENTREGADO",
    "DELIVERY COMPLETED",
    "SHIPMENT DELIVERED",
)
_HOLD_WORDS = (
    "ON HOLD",
    "HELD",
    "RETENIDO",
    "RETENIDA",
    "CLEARANCE DELAY",
    "CUSTOMS DELAY",
    "DELIVERY EXCEPTION",
    "SHIPMENT EXCEPTION",
    "UNSUCCESSFUL DELIVERY",
    "DELIVERY ATTEMPT",
    "ADDRESS INFORMATION NEEDED",
    "PAYMENT REQUIRED",
    "CONTACT DHL",
    "SHIPMENT HAS BEEN STOPPED",
    "REFUSED",
    "DAMAGED",
    "MISSING",
)


def _texto(valor: Any, maximo: int = 300) -> str:
    return " ".join(str(valor or "").strip().split())[:maximo]


def _texto_clasificacion(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", _texto(valor, 2000))
    return "".join(c for c in texto if not unicodedata.combining(c)).upper()


def _clave_evento(par: tuple[int, dict]) -> tuple[str, int]:
    indice, evento = par
    timestamp = _texto(
        evento.get("timestamp")
        or evento.get("dateTime")
        or evento.get("gmtDateTime"),
        80,
    )
    if not timestamp:
        timestamp = (
            f"{_texto(evento.get('date'), 20)}"
            f"T{_texto(evento.get('time'), 20)}"
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


def _detalle_evento(evento: dict) -> str:
    partes = [
        evento.get("typeCode"),
        evento.get("statusCode"),
        evento.get("eventType"),
        evento.get("description"),
    ]
    for observacion in evento.get("remarks") or []:
        if isinstance(observacion, dict):
            partes.extend((observacion.get("value"), observacion.get("details")))
        else:
            partes.append(observacion)
    return " ".join(_texto(p, 500) for p in partes if _texto(p, 500))


def normalizar_respuesta_dhl(respuesta: dict) -> dict:
    """Reduce la respuesta DHL a los tres estados visibles del portal.

    Nunca usa ``shipments[].status=Success`` como estado logístico: según el
    contrato MyDHL, el avance está en ``events[].typeCode/description``.
    """
    if not isinstance(respuesta, dict) or not respuesta.get("encontrado"):
        return {
            "ok": False,
            "error": _texto(
                (respuesta or {}).get("error") if isinstance(respuesta, dict)
                else "Respuesta inválida de DHL",
                240,
            ) or "DHL no devolvió información de rastreo.",
        }

    evento = _ultimo_evento(respuesta)
    if not evento:
        return {
            "ok": False,
            "error": "DHL todavía no informó eventos para esta guía.",
        }
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
    clasificacion = _texto_clasificacion(_detalle_evento(evento))

    if codigo in _DELIVERY_CODES or any(
        palabra in clasificacion for palabra in _DELIVERY_WORDS
    ):
        estado = ENTREGADO
    elif codigo in _HOLD_CODES or any(
        palabra in clasificacion for palabra in _HOLD_WORDS
    ):
        estado = RETENIDO
    else:
        estado = PROCESO_ENTREGA

    return {
        "ok": True,
        "estado": estado,
        "estado_courier": codigo,
        "descripcion": descripcion,
    }


def _candidato_dhl(solicitud_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, tracking
                FROM solicitudes_guia
                WHERE id = %s
                  AND UPPER(courier) = 'DHL'
                  AND NULLIF(BTRIM(tracking), '') IS NOT NULL
                  AND estado NOT IN ('CANCELADO', 'ENTREGADO')
                  AND estado <> 'REEMPLAZADO'
                  AND tracking_estado IS DISTINCT FROM 'ENTREGADO'
                """,
                (int(solicitud_id),),
            )
            fila = cur.fetchone()
    return dict(fila) if fila else None


def actualizar_tracking_dhl(
    solicitud_id: int,
    *,
    cliente_dhl: Optional[DHLClient] = None,
) -> dict:
    """Consulta una guía DHL y guarda sólo un snapshot mínimo y saneado."""
    candidato = _candidato_dhl(solicitud_id)
    if not candidato:
        return {"ok": True, "omitido": True, "solicitud_id": int(solicitud_id)}

    cliente_dhl = cliente_dhl or DHLClient()
    try:
        respuesta = cliente_dhl.track(str(candidato["tracking"]))
    except Exception as exc:
        respuesta = {
            "encontrado": False,
            "error": f"Consulta DHL fallida ({type(exc).__name__})",
        }
    normalizado = normalizar_respuesta_dhl(respuesta)

    with get_conn() as conn:
        with conn.cursor() as cur:
            if normalizado.get("ok"):
                cur.execute(
                    """
                    UPDATE solicitudes_guia
                    SET tracking_estado = %s,
                        tracking_estado_courier = %s,
                        tracking_descripcion = %s,
                        tracking_consultado_at = NOW(),
                        tracking_actualizado_at = NOW(),
                        tracking_finalizado_at = CASE
                            WHEN %s = 'ENTREGADO'
                                THEN COALESCE(tracking_finalizado_at, NOW())
                            ELSE tracking_finalizado_at
                        END,
                        tracking_error = NULL,
                        tracking_error_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                      AND estado NOT IN ('CANCELADO', 'ENTREGADO')
                      AND estado <> 'REEMPLAZADO'
                      AND tracking_estado IS DISTINCT FROM 'ENTREGADO'
                    RETURNING id
                    """,
                    (
                        normalizado["estado"],
                        normalizado.get("estado_courier") or "",
                        normalizado.get("descripcion") or "",
                        normalizado["estado"],
                        int(solicitud_id),
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE solicitudes_guia
                    SET tracking_consultado_at = NOW(),
                        tracking_error = %s,
                        tracking_error_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                      AND estado NOT IN ('CANCELADO', 'ENTREGADO')
                      AND estado <> 'REEMPLAZADO'
                      AND tracking_estado IS DISTINCT FROM 'ENTREGADO'
                    RETURNING id
                    """,
                    (_texto(normalizado.get("error"), 240), int(solicitud_id)),
                )
            guardado = cur.fetchone()
    return {
        **normalizado,
        "solicitud_id": int(solicitud_id),
        "guardado": bool(guardado),
    }


def actualizar_trackings_diarios_dhl(limite: Optional[int] = None) -> dict:
    """Actualiza una vez por día las guías DHL todavía no entregadas.

    Un advisory lock impide que dos procesos web ejecuten el mismo lote. Los
    envíos entregados quedan excluidos para siempre; los errores se vuelven a
    intentar al día siguiente, sin martillar la API al refrescar la página.
    """
    if limite is None:
        try:
            limite = int(os.getenv("DHL_TRACKING_DAILY_LIMIT", "1000"))
        except ValueError:
            limite = 1000
    limite = max(1, min(int(limite), 5000))
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
                    SELECT id
                    FROM solicitudes_guia
                    WHERE UPPER(courier) = 'DHL'
                      AND NULLIF(BTRIM(tracking), '') IS NOT NULL
                      AND estado NOT IN ('CANCELADO', 'ENTREGADO')
                      AND estado <> 'REEMPLAZADO'
                      AND tracking_estado IS DISTINCT FROM 'ENTREGADO'
                      AND (
                          tracking_consultado_at IS NULL
                          OR (
                              tracking_consultado_at AT TIME ZONE
                                  'America/Argentina/Buenos_Aires'
                          )::date < (
                              NOW() AT TIME ZONE
                                  'America/Argentina/Buenos_Aires'
                          )::date
                      )
                    ORDER BY tracking_consultado_at ASC NULLS FIRST, id ASC
                    LIMIT %s
                    """,
                    (limite,),
                )
                ids = [int(fila["id"]) for fila in cur.fetchall()]

            conteos = {
                "consultados": 0,
                "actualizados": 0,
                "entregados": 0,
                "retenidos": 0,
                "errores": 0,
            }
            for solicitud_id in ids:
                resultado = actualizar_tracking_dhl(
                    solicitud_id, cliente_dhl=cliente_dhl
                )
                if resultado.get("omitido"):
                    continue
                conteos["consultados"] += 1
                if resultado.get("ok"):
                    conteos["actualizados"] += 1
                    if resultado.get("estado") == ENTREGADO:
                        conteos["entregados"] += 1
                    elif resultado.get("estado") == RETENIDO:
                        conteos["retenidos"] += 1
                else:
                    conteos["errores"] += 1
            return {"ok": True, "candidatos": len(ids), **conteos}
        finally:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))


def actualizar_trackings_diarios_seguro() -> dict:
    """Wrapper APScheduler: rastreo vigente + riesgo de guías reemplazadas."""
    try:
        resultado = actualizar_trackings_diarios_dhl()
    except Exception as exc:
        print(f"[tracking-dhl] job diario falló: {type(exc).__name__}")
        resultado = {"ok": False, "error": type(exc).__name__}
    try:
        # Una guía REEMPLAZADA queda afuera del tracking normal. El control
        # separado consulta cada tracking descartado una sola vez al cumplir
        # 7 días: sin eventos confirma la cancelación; con actividad alerta.
        from servicios.monitoreo_guias_reemplazadas import (
            actualizar_trackings_reemplazados_dhl,
        )
        reemplazadas = actualizar_trackings_reemplazados_dhl()
    except Exception as exc:
        print(
            "[tracking-dhl] vigilancia de reemplazadas falló: "
            f"{type(exc).__name__}"
        )
        reemplazadas = {"ok": False, "error": type(exc).__name__}

    try:
        print(
            "[tracking-dhl] diario: "
            f"consultados={resultado.get('consultados', 0)} "
            f"actualizados={resultado.get('actualizados', 0)} "
            f"entregados={resultado.get('entregados', 0)} "
            f"retenidos={resultado.get('retenidos', 0)} "
            f"errores={resultado.get('errores', 0)} "
            f"omitido={resultado.get('motivo', '')}"
        )
        print(
            "[tracking-dhl] reemplazadas: "
            f"consultados={reemplazadas.get('consultados', 0)} "
            f"sin_movimiento={reemplazadas.get('sin_movimiento', 0)} "
            f"confirmadas={reemplazadas.get('cancelaciones_confirmadas', 0)} "
            f"alertas={reemplazadas.get('alertas', 0)} "
            f"alertas_nuevas={reemplazadas.get('alertas_nuevas', 0)} "
            f"errores={reemplazadas.get('errores', 0)} "
            f"omitido={reemplazadas.get('motivo', '')}"
        )
        return {**resultado, "reemplazadas": reemplazadas}
    except Exception as exc:
        print(f"[tracking-dhl] registro del job falló: {type(exc).__name__}")
        return {
            **resultado,
            "reemplazadas": reemplazadas,
            "error_log": type(exc).__name__,
        }
