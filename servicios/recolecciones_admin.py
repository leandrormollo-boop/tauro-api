"""Programación segura de recolecciones desde ADMIN.

El formulario administrativo sólo aporta fecha, ventana e instrucciones. El
cliente, courier, origen y piezas se vuelven a leer de la guía emitida para no
convertir campos del navegador en datos operativos del retiro.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date
from typing import Any

import psycopg2
from psycopg2.extras import Json

from core.database import get_conn
from servicios import recolecciones as recolecciones
from servicios.auditoria import registrar_evento_con_cursor
from servicios.carrier_contract import (
    Ambito,
    Capacidad,
    operation_implemented,
    public_catalog,
)
from servicios.numeros_humanos import parse_float_formulario


_IDEMPOTENCIA_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_ESTADO_EMITIDO_APTO = "GUIA_LISTA"


def _texto(valor: Any) -> str:
    return " ".join(str(valor or "").strip().split())


def _referencia_idempotente(clave: str) -> str:
    digest = hashlib.sha256(clave.encode("utf-8")).hexdigest()[:32]
    return f"tauro-admin-pick-{digest}"


def _courier_habilitado_para_retiro(courier: str) -> tuple[bool, str]:
    """Comprueba catálogo, implementación y configuración productiva.

    No usa permisos del cliente: ésta es una acción de ADMIN sobre una guía
    real. Sí falla cerrado si TAURO no publicó o no tiene operativa la
    integración que puede crear el retiro.
    """
    courier_id = _texto(courier).lower()
    publicado = next(
        (
            item
            for item in public_catalog(Ambito.INTERNACIONAL, canal="cuenta")
            if item["id"] == courier_id
        ),
        None,
    )
    if (
        not publicado
        or publicado.get("estado") == "integracion_pendiente"
        or Capacidad.RECOLECTAR.value not in publicado.get("capacidades", ())
        or not operation_implemented(
            courier_id,
            Capacidad.RECOLECTAR,
            ambito=Ambito.INTERNACIONAL,
        )
    ):
        return False, "El operador de esta guía no admite recolecciones publicadas."

    from servicios.configuracion_couriers_cliente import estado_integracion

    try:
        integracion = estado_integracion(courier_id)
    except Exception:
        return False, "No se pudo comprobar la integración productiva del operador."
    if not integracion.get("operativa"):
        return False, "La integración productiva del operador no está habilitada."
    return True, ""


def _cargar_solicitud(cur, solicitud_id: int, *, bloquear: bool) -> dict | None:
    cur.execute(
        """
        SELECT s.*, c.activo AS cliente_activo
        FROM solicitudes_guia s
        JOIN clientes c ON c.cliente_id=s.cliente_id
        WHERE s.id=%s
        FOR UPDATE
        """
        if bloquear
        else """
        SELECT s.*, c.activo AS cliente_activo
        FROM solicitudes_guia s
        JOIN clientes c ON c.cliente_id=s.cliente_id
        WHERE s.id=%s
        """,
        (int(solicitud_id),),
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def _recoleccion_abierta(cur, solicitud_id: int) -> dict | None:
    cur.execute(
        """
        SELECT id, solicitud_id, estado, courier, fecha, ready_time, close_time,
               confirmation_code, error_operativo, courier_message_reference
        FROM recolecciones
        WHERE solicitud_id=%s
          AND estado IN ('AGENDANDO', 'AGENDADA', 'CANCELANDO',
                         'VERIFICAR_COURIER')
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(solicitud_id),),
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def _preparar_solicitud(sol: dict | None) -> tuple[dict | None, str]:
    if not sol:
        return None, "El envío no existe."
    if bool(sol.get("test")):
        return None, "Los envíos de prueba no admiten recolecciones reales."
    if not bool(sol.get("cliente_activo")):
        return None, "La cuenta del cliente está suspendida y no admite operaciones reales."
    if _texto(sol.get("estado")).upper() != _ESTADO_EMITIDO_APTO:
        return None, "Sólo se programa el retiro de una guía emitida que todavía está lista."
    if not _texto(sol.get("tracking")):
        return None, "El envío todavía no tiene una guía real emitida."

    from servicios.couriers_urls import ambito_envio

    if ambito_envio(sol) != "internacional":
        return None, "La recolección administrativa sólo está disponible para guías internacionales."

    courier = _texto(sol.get("courier") or "FEDEX").upper()
    habilitado, motivo = _courier_habilitado_para_retiro(courier)
    if not habilitado:
        return None, motivo

    try:
        retiro = recolecciones.datos_retiro_desde_solicitud(sol)
        peso_total = parse_float_formulario(
            retiro.get("peso_kg"),
            "Peso total",
            minimo=0.001,
            maximo=recolecciones.MAX_PESO_RECOLECCION_KG,
        )
        for paquete in retiro.get("paquetes") or ():
            peso_bulto = parse_float_formulario(
                paquete.get("peso_kg"),
                "Peso de bulto",
                minimo=0.001,
                maximo=recolecciones.MAX_KG_POR_BULTO,
            )
            if not math.isfinite(peso_bulto):
                raise ValueError("El peso de un bulto no es válido.")
    except (TypeError, ValueError) as exc:
        return None, f"La guía no tiene cajas válidas para el retiro: {exc}"

    origen = retiro["origen"]
    if not all(_texto(origen.get(campo)) for campo in ("calle", "ciudad", "pais")):
        return None, "La guía no conserva un origen completo para pedir el retiro."
    retiro["peso_kg"] = peso_total
    retiro["courier"] = courier
    retiro["cliente_id"] = _texto(sol.get("cliente_id")).upper()
    retiro["tracking"] = _texto(sol.get("tracking"))
    return retiro, ""


def contexto_recoleccion_admin(solicitud_id: int) -> dict[str, Any]:
    """Contexto de sólo lectura para decidir si ADMIN muestra el formulario."""
    try:
        solicitud = int(solicitud_id)
    except (TypeError, ValueError):
        return {"habilitada": False, "motivo": "El envío no es válido."}

    recolecciones._ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            sol = _cargar_solicitud(cur, solicitud, bloquear=False)
            activa = _recoleccion_abierta(cur, solicitud) if sol else None

    retiro, motivo = _preparar_solicitud(sol)
    if activa:
        motivo = "La guía ya tiene una recolección activa o pendiente de verificación."
    contexto: dict[str, Any] = {
        "habilitada": bool(retiro and not activa),
        "motivo": motivo,
        "solicitud_id": solicitud,
        "recoleccion_activa": activa,
    }
    if retiro:
        contexto.update(
            cliente_id=retiro["cliente_id"],
            courier=retiro["courier"],
            tracking=retiro["tracking"],
            origen=dict(retiro["origen"]),
            cajas={
                "bultos": int(retiro["bultos"]),
                "peso_kg": retiro["peso_kg"],
                "paquetes": [dict(p) for p in (retiro.get("paquetes") or ())],
            },
        )
    return contexto


def _coincide_reintento(
    existente: dict,
    *,
    solicitud_id: int,
    fecha: str,
    ready_time: str,
    close_time: str,
    instrucciones: str,
) -> bool:
    fecha_existente = existente.get("fecha")
    if isinstance(fecha_existente, date):
        fecha_existente = fecha_existente.isoformat()
    return (
        int(existente.get("solicitud_id") or 0) == int(solicitud_id)
        and str(fecha_existente or "") == fecha
        and str(existente.get("ready_time") or "") == ready_time
        and str(existente.get("close_time") or "") == close_time
        and str(existente.get("instrucciones") or "") == instrucciones
    )


def _respuesta_reintento(existente: dict) -> dict[str, Any]:
    estado = _texto(existente.get("estado")).upper()
    base = {
        "id": int(existente["id"]),
        "solicitud_id": int(existente["solicitud_id"]),
        "duplicado": True,
        "estado": estado,
    }
    if estado == "AGENDADA":
        return {
            **base,
            "ok": True,
            "confirmation_code": existente.get("confirmation_code"),
        }
    if estado in {"AGENDANDO", "CANCELANDO", "VERIFICAR_COURIER"}:
        return {
            **base,
            "ok": False,
            "incierto": True,
            "error": (
                "La operación ya fue recibida y todavía requiere confirmación. "
                "No programes otro retiro."
            ),
        }
    return {
        **base,
        "ok": False,
        "error": "La clave de operación ya corresponde a una recolección cerrada.",
    }


def _auditar(
    cur,
    *,
    actor: str,
    referencia: str,
    solicitud_id: int,
    recoleccion_id: int | None,
    cliente_id: str,
    courier: str,
    resultado: str,
    success: bool,
    event: str = "admin.programar_recoleccion",
) -> None:
    registrar_evento_con_cursor(
        cur,
        event=event,
        actor_type="admin",
        actor_ref=actor,
        ip=None,
        method="POST",
        path=None,
        status_code=200 if success else 409,
        success=success,
        request_id=None,
        metadata={
            "referencia": referencia,
            "solicitud_id": solicitud_id,
            "recoleccion_id": recoleccion_id,
            "cliente_id": cliente_id,
            "courier": courier,
            "resultado": resultado,
        },
    )


def _conflicto_solicitud(solicitud_id: int) -> dict[str, Any]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                existente = _recoleccion_abierta(cur, solicitud_id)
    except Exception:
        existente = None
    salida: dict[str, Any] = {
        "ok": False,
        "error": "La guía ya tiene una recolección activa o pendiente de verificación.",
    }
    if existente:
        salida["recoleccion_conflicto_id"] = int(existente["id"])
        salida["estado"] = existente["estado"]
    return salida


def programar_recoleccion_admin(
    *,
    solicitud_id: int,
    fecha: str,
    ready_time: str,
    close_time: str,
    instrucciones: str = "",
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Programa un retiro usando exclusivamente el snapshot de la guía."""
    try:
        solicitud = int(solicitud_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "El envío no es válido."}
    actor_limpio = _texto(actor)[:120]
    clave = _texto(idempotency_key)
    fecha_limpia = _texto(fecha)
    inicio = _texto(ready_time)
    cierre = _texto(close_time)
    notas = _texto(instrucciones)[:255]
    if not actor_limpio:
        return {"ok": False, "error": "Falta identificar al administrador."}
    if not _IDEMPOTENCIA_RE.fullmatch(clave):
        return {"ok": False, "error": "La clave de la operación no es válida."}
    referencia = _referencia_idempotente(clave)

    recolecciones._ensure_tabla()
    retiro: dict[str, Any] | None = None
    rec_id: int | None = None
    origen_retiro: dict[str, str] | None = None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Serializa el mismo token aun cuando todavía no existe fila.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"tauro:admin-pickup:{referencia}",),
                )
                cur.execute(
                    """
                    SELECT * FROM recolecciones
                    WHERE courier_message_reference=%s
                    ORDER BY id DESC LIMIT 1
                    FOR UPDATE
                    """,
                    (referencia,),
                )
                repetida = cur.fetchone()
                if repetida:
                    repetida = dict(repetida)
                    if not _coincide_reintento(
                        repetida,
                        solicitud_id=solicitud,
                        fecha=fecha_limpia,
                        ready_time=inicio,
                        close_time=cierre,
                        instrucciones=notas,
                    ):
                        return {
                            "ok": False,
                            "error": "La clave de operación ya fue usada con otros datos.",
                        }
                    return _respuesta_reintento(repetida)

                # El lock de la solicitud hace que cancelar y programar se
                # observen en un orden único. La cancelación que continúe
                # después verá la fila AGENDANDO insertada en esta transacción.
                sol = _cargar_solicitud(cur, solicitud, bloquear=True)
                retiro, motivo = _preparar_solicitud(sol)
                if not retiro:
                    return {"ok": False, "error": motivo}
                abierta = _recoleccion_abierta(cur, solicitud)
                if abierta:
                    return {
                        "ok": False,
                        "error": (
                            "La guía ya tiene una recolección activa o "
                            "pendiente de verificación."
                        ),
                        "recoleccion_conflicto_id": int(abierta["id"]),
                        "estado": abierta["estado"],
                    }

                motivo = recolecciones._dias_habiles_validos(fecha_limpia)
                if motivo:
                    return {"ok": False, "error": motivo}
                motivo = recolecciones._ventana_horaria_valida(inicio, cierre)
                if motivo:
                    return {"ok": False, "error": motivo}

                origen = retiro["origen"]
                origen_retiro = {
                    campo: origen.get(campo) or ""
                    for campo in ("pais", "estado", "ciudad", "zip", "calle")
                }
                cur.execute(
                    """
                    INSERT INTO recolecciones (
                        cliente_id, fecha, ready_time, close_time, bultos,
                        peso_kg, direccion, instrucciones, estado, courier,
                        solicitud_id, origen_retiro, courier_message_reference
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, 'AGENDANDO',
                        %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        retiro["cliente_id"], fecha_limpia, inicio, cierre,
                        retiro["bultos"], retiro["peso_kg"],
                        f"{origen.get('calle', '')}, {origen.get('ciudad', '')}".strip(", "),
                        notas, retiro["courier"], solicitud,
                        Json(origen_retiro), referencia,
                    ),
                )
                rec_id = int(cur.fetchone()["id"])
                _auditar(
                    cur,
                    actor=actor_limpio,
                    referencia=referencia,
                    solicitud_id=solicitud,
                    recoleccion_id=rec_id,
                    cliente_id=retiro["cliente_id"],
                    courier=retiro["courier"],
                    resultado="AGENDANDO",
                    success=True,
                    event="admin.programar_recoleccion_iniciada",
                )
    except psycopg2.IntegrityError as exc:
        if exc.pgcode == "23505":
            constraint = getattr(exc.diag, "constraint_name", "") or ""
            if constraint == "uq_recoleccion_solicitud_abierta_v2":
                return _conflicto_solicitud(solicitud)
            if constraint in {
                "uq_recoleccion_origen_fecha_abierta_v3",
                "uq_recoleccion_origen_pendiente_v3",
            } and retiro and origen_retiro:
                return recolecciones._conflicto_reserva(
                    retiro["cliente_id"],
                    fecha_limpia,
                    retiro["courier"],
                    solicitud,
                    origen_retiro,
                    constraint,
                )
            return {
                "ok": False,
                "error": (
                    "No se pudo reservar el retiro porque otro registro "
                    "cambió al mismo tiempo. Recargá antes de intentar nuevamente."
                ),
            }
        if exc.pgcode == "23514":
            return {"ok": False, "error": "Los datos del retiro no son válidos."}
        raise

    assert retiro is not None and rec_id is not None
    cliente_api = recolecciones._cliente_pickup(retiro["courier"])
    if cliente_api is None:
        resultado = {
            "encontrado": False,
            "error": "El operador no tiene un adaptador de recolecciones disponible.",
        }
    else:
        origen = retiro["origen"]
        try:
            resultado = cliente_api.create_pickup(
                {
                    "origen": {
                        "nombre": origen.get("nombre") or retiro["cliente_id"],
                        "empresa": origen.get("empresa") or "",
                        "telefono": origen.get("telefono") or "",
                        "calle": origen.get("calle") or "",
                        "ciudad": origen.get("ciudad") or "",
                        "estado": origen.get("estado") or "",
                        "zip": origen.get("zip") or "",
                        "pais": origen.get("pais") or "AR",
                    },
                    "fecha": fecha_limpia,
                    "ready_time": inicio,
                    "close_time": cierre,
                    "peso_kg": retiro["peso_kg"],
                    "bultos": retiro["bultos"],
                    "instrucciones": notas,
                    "paquetes": retiro.get("paquetes") or None,
                    "message_reference": referencia,
                }
            )
        except Exception as exc:
            print(
                f"[recolecciones-admin] respuesta incierta de "
                f"{retiro['courier']} para reserva {rec_id}: {type(exc).__name__}"
            )
            resultado = {
                "encontrado": False,
                "incierto": True,
                "error": "No se pudo confirmar la recolección con el operador.",
            }

    confirmacion = _texto(resultado.get("confirmation_code"))
    incierto = bool(resultado.get("incierto")) or (
        bool(resultado.get("encontrado")) and not confirmacion
    )
    if not resultado.get("encontrado") and not incierto:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM recolecciones
                    WHERE id=%s AND estado='AGENDANDO'
                    RETURNING id
                    """,
                    (rec_id,),
                )
                eliminado = cur.fetchone()
                _auditar(
                    cur,
                    actor=actor_limpio,
                    referencia=referencia,
                    solicitud_id=solicitud,
                    recoleccion_id=rec_id,
                    cliente_id=retiro["cliente_id"],
                    courier=retiro["courier"],
                    resultado="RECHAZADA" if eliminado else "ESTADO_CAMBIO",
                    success=False,
                )
        if not eliminado:
            return {
                "ok": False,
                "incierto": True,
                "error": "El retiro cambió de estado mientras respondía el operador; requiere verificación.",
            }
        return {
            "ok": False,
            "error": resultado.get("error") or "El operador rechazó la recolección.",
        }

    if incierto:
        error = _texto(resultado.get("error")) or "La respuesta del operador fue incierta."
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recolecciones
                    SET estado='VERIFICAR_COURIER', error_operativo=%s,
                        confirmation_code=COALESCE(NULLIF(%s, ''), confirmation_code),
                        updated_at=NOW()
                    WHERE id=%s AND estado='AGENDANDO'
                    RETURNING id
                    """,
                    (error[:500], confirmacion, rec_id),
                )
                cur.fetchone()
                _auditar(
                    cur,
                    actor=actor_limpio,
                    referencia=referencia,
                    solicitud_id=solicitud,
                    recoleccion_id=rec_id,
                    cliente_id=retiro["cliente_id"],
                    courier=retiro["courier"],
                    resultado="VERIFICAR_COURIER",
                    success=False,
                )
        return {
            "ok": False,
            "incierto": True,
            "id": rec_id,
            "error": error + " Verificala antes de programar otro retiro.",
        }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE recolecciones
                SET estado='AGENDADA', confirmation_code=%s, ubicacion=%s,
                    error_operativo=NULL, updated_at=NOW()
                WHERE id=%s AND estado='AGENDANDO'
                RETURNING id
                """,
                (
                    confirmacion,
                    resultado.get("ubicacion"),
                    rec_id,
                ),
            )
            actualizada = cur.fetchone()
            _auditar(
                cur,
                actor=actor_limpio,
                referencia=referencia,
                solicitud_id=solicitud,
                recoleccion_id=rec_id,
                cliente_id=retiro["cliente_id"],
                courier=retiro["courier"],
                resultado="AGENDADA" if actualizada else "ESTADO_CAMBIO",
                success=bool(actualizada),
            )
    if not actualizada:
        return {
            "ok": False,
            "incierto": True,
            "id": rec_id,
            "error": "El retiro fue aceptado pero el registro cambió; requiere verificación.",
        }
    return {
        "ok": True,
        "id": rec_id,
        "solicitud_id": solicitud,
        "estado": "AGENDADA",
        "duplicado": False,
        "confirmation_code": confirmacion,
    }
