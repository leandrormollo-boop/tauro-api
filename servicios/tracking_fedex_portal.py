"""Rastreo FedEx persistido para el portal, con el mismo contrato que DHL.

Hasta ahora sólo las guías DHL se actualizaban solas: las FedEx quedaban
"en tránsito" para siempre aunque ya estuvieran entregadas. Este job consulta
FedEx una vez por día (en lotes de 30, el máximo de la API) y guarda el mismo
snapshot mínimo que DHL: estado, código, mensaje, fecha real del evento y
momento en que cambió. Sigue consultando hasta que FedEx confirme la entrega.

Nunca escribe con credenciales de sandbox: un estado de prueba no puede
marcar como entregado un envío real.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from core.database import get_conn
from servicios.tracking_envios import ENTREGADO, PROCESO_ENTREGA, RETENIDO, _texto

_LOCK_NAME = "tauro:tracking:fedex:diario:v1"
_LOTE = 30
_CODIGOS_ENTREGA = {"DL"}
# DE: excepción de entrega · SE: excepción del envío · CD: demora en aduana.
_CODIGOS_RETENCION = {"DE", "SE", "CD"}
# OC: FedEx recibió la información de la etiqueta, pero todavía no el paquete.
_CODIGOS_PRE_RETIRO = {"OC"}
# Cancelado o sin datos: no es un avance logístico; no tocamos el estado.
_CODIGOS_SIN_AVANCE = {"CA"}


def _instante(valor: Any) -> Optional[datetime]:
    texto = _texto(valor, 60)
    if not texto:
        return None
    try:
        instante = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    return instante if instante.tzinfo else None


def normalizar_respuesta_fedex(resultado: dict) -> dict:
    """Reduce un trackResult de FedEx a los estados visibles del portal."""
    if not isinstance(resultado, dict) or not resultado:
        return {"ok": False, "error": "FedEx no devolvió información de rastreo."}
    error = resultado.get("error") or {}
    if error:
        return {"ok": False,
                "error": _texto(error.get("message") or error.get("code"), 240)
                or "FedEx no devolvió información de rastreo."}
    ultimo = resultado.get("latestStatusDetail") or {}
    codigo = _texto(ultimo.get("code") or ultimo.get("derivedCode"), 20).upper()
    descripcion = _texto(
        ultimo.get("statusByLocale") or ultimo.get("description"), 300
    )
    if not codigo and not descripcion:
        return {"ok": False, "error": "FedEx todavía no informó eventos para esta guía."}
    if codigo in _CODIGOS_SIN_AVANCE:
        return {"ok": False, "error": f"FedEx informa {descripcion or codigo}."}

    if codigo in _CODIGOS_ENTREGA:
        estado = ENTREGADO
    elif codigo in _CODIGOS_RETENCION:
        estado = RETENIDO
    elif codigo in _CODIGOS_PRE_RETIRO:
        # Es una respuesta válida para guardar y volver a consultar, pero no
        # demuestra que FedEx haya recibido físicamente el paquete.
        estado = None
    else:
        estado = PROCESO_ENTREGA

    eventos = [e for e in (resultado.get("scanEvents") or []) if isinstance(e, dict)]
    instantes = [i for i in (_instante(e.get("date")) for e in eventos) if i]
    evento_at = max(instantes) if instantes else None
    if estado == ENTREGADO:
        for item in resultado.get("dateAndTimes") or []:
            if _texto(item.get("type"), 40).upper() in {"ACTUAL_DELIVERY", "ACTUAL_DELIVERY_DATE"}:
                evento_at = _instante(item.get("dateTime")) or evento_at
                break
    return {
        "ok": True,
        "estado": estado,
        "estado_courier": codigo,
        "descripcion": descripcion,
        "evento_at": evento_at,
    }


def _configuracion_fedex():
    """Cliente FedEx listo para producción, o el motivo para no consultar."""
    from core.fedex_client import FedExClient

    cliente = FedExClient()
    if not (cliente.api_key and cliente.secret_key):
        return None, "fedex_no_configurado"
    if cliente.environment == "sandbox" and os.getenv("FEDEX_TRACKING_PERMITIR_SANDBOX") != "1":
        return None, "fedex_sandbox"
    return cliente, ""


def _guardar(cur, solicitud_id: int, tracking: str, normalizado: dict) -> bool:
    if normalizado.get("ok"):
        cur.execute(
            """
            UPDATE solicitudes_guia
            SET estado = CASE
                    WHEN %s = 'ENTREGADO' AND estado IN ('GUIA_LISTA', 'DESPACHADO')
                        THEN 'ENTREGADO'
                    WHEN %s = 'PROCESO_ENTREGA' AND estado = 'GUIA_LISTA'
                        THEN 'DESPACHADO'
                    ELSE estado
                END,
                tracking_estado = %s,
                tracking_estado_courier = %s,
                tracking_descripcion = %s,
                tracking_consultado_at = NOW(),
                tracking_actualizado_at = NOW(),
                tracking_evento_at = COALESCE(%s, tracking_evento_at),
                tracking_cambio_at = CASE
                    WHEN tracking_cambio_at IS NULL
                         OR tracking_estado IS DISTINCT FROM %s
                         OR tracking_estado_courier IS DISTINCT FROM %s
                         OR tracking_descripcion IS DISTINCT FROM %s
                        THEN NOW()
                    ELSE tracking_cambio_at
                END,
                tracking_vigilancia_desde = CASE WHEN %s = 'RETENIDO'
                    THEN COALESCE(tracking_vigilancia_desde, NOW())
                    ELSE tracking_vigilancia_desde END,
                tracking_finalizado_at = CASE WHEN %s = 'ENTREGADO'
                    THEN COALESCE(tracking_finalizado_at, NOW())
                    ELSE tracking_finalizado_at END,
                tracking_error = NULL,
                tracking_error_at = NULL,
                updated_at = NOW()
            WHERE id = %s AND tracking = %s AND UPPER(courier) = 'FEDEX'
              AND (
                  (estado NOT IN ('CANCELADO', 'ENTREGADO')
                   AND estado <> 'REEMPLAZADO')
                  OR (estado='CANCELADO' AND cancelacion_comercial=TRUE)
              )
              AND tracking_estado IS DISTINCT FROM 'ENTREGADO'
            RETURNING id
            """,
            (
                normalizado["estado"], normalizado["estado"], normalizado["estado"],
                normalizado.get("estado_courier") or "", normalizado.get("descripcion") or "",
                normalizado.get("evento_at"),
                normalizado["estado"], normalizado.get("estado_courier") or "",
                normalizado.get("descripcion") or "",
                normalizado["estado"], normalizado["estado"],
                int(solicitud_id), tracking,
            ),
        )
    else:
        cur.execute(
            """
            UPDATE solicitudes_guia
            SET tracking_consultado_at = NOW(), tracking_error = %s,
                tracking_error_at = NOW(), updated_at = NOW()
            WHERE id = %s AND tracking = %s AND UPPER(courier) = 'FEDEX'
              AND (
                  (estado NOT IN ('CANCELADO', 'ENTREGADO')
                   AND estado <> 'REEMPLAZADO')
                  OR (estado='CANCELADO' AND cancelacion_comercial=TRUE)
              )
              AND tracking_estado IS DISTINCT FROM 'ENTREGADO'
            RETURNING id
            """,
            (_texto(normalizado.get("error"), 240), int(solicitud_id), tracking),
        )
    return bool(cur.fetchone())


def actualizar_trackings_diarios_fedex(limite: Optional[int] = None, *, cliente_fedex=None) -> dict:
    """Una consulta diaria por guía FedEx vigente, hasta confirmar la entrega."""
    try:
        limite = int(limite if limite is not None else os.getenv("FEDEX_TRACKING_DAILY_LIMIT", "600"))
    except ValueError:
        limite = 600
    limite = max(1, min(limite, 3000))
    if cliente_fedex is None:
        cliente_fedex, motivo = _configuracion_fedex()
        if cliente_fedex is None:
            return {"ok": False, "omitido": True, "motivo": motivo}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS adquirido", (_LOCK_NAME,))
            lock = cur.fetchone()
            if not lock or not lock.get("adquirido"):
                return {"ok": True, "omitido": True, "motivo": "job_en_curso"}
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, BTRIM(tracking) AS tracking
                    FROM solicitudes_guia
                    WHERE UPPER(courier) = 'FEDEX'
                      AND NULLIF(BTRIM(tracking), '') IS NOT NULL
                      AND (
                          (estado NOT IN ('CANCELADO', 'ENTREGADO')
                           AND estado <> 'REEMPLAZADO')
                          OR (estado='CANCELADO' AND cancelacion_comercial=TRUE)
                      )
                      AND tracking_estado IS DISTINCT FROM 'ENTREGADO'
                      AND test = FALSE
                      AND EXISTS (SELECT 1 FROM clientes c
                                  WHERE c.cliente_id = solicitudes_guia.cliente_id
                                    AND c.test = FALSE)
                      AND (tracking_consultado_at IS NULL
                           OR (tracking_consultado_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
                              < (NOW() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date)
                    ORDER BY tracking_consultado_at ASC NULLS FIRST, id ASC
                    LIMIT %s
                    """,
                    (limite,),
                )
                candidatos = [(int(f["id"]), str(f["tracking"])) for f in cur.fetchall()]
            conn.commit()

            conteos = {"consultados": 0, "actualizados": 0, "entregados": 0,
                       "retenidos": 0, "errores": 0}
            for inicio in range(0, len(candidatos), _LOTE):
                lote = candidatos[inicio:inicio + _LOTE]
                try:
                    respuestas = cliente_fedex.track_many([t for _, t in lote])
                except Exception as exc:
                    respuestas = {t: {"error": {"message": f"Consulta FedEx fallida ({type(exc).__name__})"}}
                                  for _, t in lote}
                with conn.cursor() as cur:
                    for solicitud_id, tracking in lote:
                        normalizado = normalizar_respuesta_fedex(respuestas.get(tracking) or {})
                        if not _guardar(cur, solicitud_id, tracking, normalizado):
                            continue
                        conteos["consultados"] += 1
                        if normalizado.get("ok"):
                            conteos["actualizados"] += 1
                            if normalizado["estado"] == ENTREGADO:
                                conteos["entregados"] += 1
                            elif normalizado["estado"] == RETENIDO:
                                conteos["retenidos"] += 1
                        else:
                            conteos["errores"] += 1
                conn.commit()
            return {"ok": True, "candidatos": len(candidatos), **conteos}
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))
            conn.commit()


def actualizar_trackings_fedex_seguro() -> dict:
    try:
        resultado = actualizar_trackings_diarios_fedex()
    except Exception as exc:
        resultado = {"ok": False, "error": type(exc).__name__}
    print("[tracking-fedex] diario: "
          f"consultados={resultado.get('consultados', 0)} "
          f"entregados={resultado.get('entregados', 0)} "
          f"errores={resultado.get('errores', 0)} "
          f"omitido={resultado.get('motivo', '')} error={resultado.get('error', '')}")
    return resultado
