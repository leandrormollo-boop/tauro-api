"""Control financiero de un envío desde ADMIN.

La cancelación de este módulo es comercial y local: quita el cargo de la
cuenta del cliente y conserva toda la historia. No llama al courier ni afirma
que la guía haya sido anulada fuera de TAURO.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from typing import Any

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor


ACTIVOS_RECOLECCION = (
    "AGENDANDO", "AGENDADA", "CANCELANDO", "VERIFICAR_COURIER",
)
ESTADOS_EMISION_EN_CURSO = ("EN_PROCESO", "EMITIENDO", "VERIFICAR_COURIER")


class ControlEnvioAdminError(ValueError):
    """Bloqueo esperado del control, con un código estable para la interfaz."""

    def __init__(self, mensaje: str, *, codigo: str = "OPERACION_NO_DISPONIBLE"):
        super().__init__(mensaje)
        self.codigo = codigo


def _cliente(valor: Any) -> str | None:
    limpio = " ".join(str(valor or "").strip().split()).upper()
    return limpio or None


def _motivo(valor: Any) -> str:
    return " ".join(str(valor or "").strip().split())[:500]


def _decimal(valor: Any) -> Decimal:
    return Decimal(str(valor or 0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _identificadores(
    solicitud_id: int | None,
    envio_id: int | None,
) -> tuple[int | None, int | None]:
    try:
        sid = int(solicitud_id) if solicitud_id is not None else None
        eid = int(envio_id) if envio_id is not None else None
    except (TypeError, ValueError):
        raise ControlEnvioAdminError(
            "El identificador del envío no es válido.", codigo="IDENTIFICADOR_INVALIDO"
        ) from None
    if sid is None and eid is None:
        raise ControlEnvioAdminError(
            "Falta identificar el envío.", codigo="IDENTIFICADOR_REQUERIDO"
        )
    if (sid is not None and sid <= 0) or (eid is not None and eid <= 0):
        raise ControlEnvioAdminError(
            "El identificador del envío no es válido.", codigo="IDENTIFICADOR_INVALIDO"
        )
    return sid, eid


def _resolver(cur, *, solicitud_id=None, envio_id=None, cliente_id=None) -> dict[str, Any]:
    sid, eid = _identificadores(solicitud_id, envio_id)
    cliente = _cliente(cliente_id)
    if eid is not None:
        cur.execute(
            """
            SELECT e.id AS envio_id, e.cliente_id, e.solicitud_id,
                   e.estado AS cargo_estado, e.monto_ars, e.ambito,
                   e.nro_fc, e.fecha AS cargo_fecha, e.created_at AS cargo_created_at,
                   s.estado AS solicitud_estado, s.updated_at AS solicitud_updated_at,
                   s.created_at AS solicitud_created_at, s.courier, s.tracking,
                   s.tracking_estado, s.cargo_pendiente, s.test,
                   s.visible_cliente, s.guia_url, s.guia_generada_at,
                   (s.label_pdf IS NOT NULL) AS tiene_label
              FROM envios e
         LEFT JOIN solicitudes_guia s
                ON s.id=e.solicitud_id AND s.cliente_id=e.cliente_id
             WHERE e.id=%s
            """,
            (eid,),
        )
    else:
        cur.execute(
            """
            SELECT e.id AS envio_id, s.cliente_id, s.id AS solicitud_id,
                   e.estado AS cargo_estado, e.monto_ars, e.ambito,
                   e.nro_fc, e.fecha AS cargo_fecha, e.created_at AS cargo_created_at,
                   s.estado AS solicitud_estado, s.updated_at AS solicitud_updated_at,
                   s.created_at AS solicitud_created_at, s.courier, s.tracking,
                   s.tracking_estado, s.cargo_pendiente, s.test,
                   s.visible_cliente, s.guia_url, s.guia_generada_at,
                   (s.label_pdf IS NOT NULL) AS tiene_label
              FROM solicitudes_guia s
         LEFT JOIN envios e
                ON e.solicitud_id=s.id AND e.cliente_id=s.cliente_id
             WHERE s.id=%s
            """,
            (sid,),
        )
    fila = cur.fetchone()
    if not fila:
        raise ControlEnvioAdminError(
            "El envío no existe.", codigo="NO_ENCONTRADO"
        )
    fila = dict(fila)
    if sid is not None and int(fila.get("solicitud_id") or 0) != sid:
        raise ControlEnvioAdminError(
            "La solicitud y el cargo no corresponden al mismo envío.",
            codigo="IDENTIDAD_INCONSISTENTE",
        )
    if eid is not None and int(fila.get("envio_id") or 0) != eid:
        raise ControlEnvioAdminError(
            "La solicitud y el cargo no corresponden al mismo envío.",
            codigo="IDENTIDAD_INCONSISTENTE",
        )
    if cliente is not None and fila.get("cliente_id") != cliente:
        raise ControlEnvioAdminError(
            "El envío no pertenece al cliente indicado.", codigo="NO_ENCONTRADO"
        )
    return fila


def _historial(cur, fila: dict[str, Any]) -> dict[str, Any]:
    sid = fila.get("solicitud_id")
    eid = fila.get("envio_id")
    ajustes: list[dict[str, Any]] = []
    if sid is not None:
        cur.execute(
            """
            SELECT id, tipo, monto_ars, precio_anterior_ars, precio_nuevo_ars,
                   estado, origen, motivo, aplicado_at, created_at
              FROM ajustes_cliente
             WHERE solicitud_id=%s
             ORDER BY created_at, id
            """,
            (int(sid),),
        )
        ajustes = [dict(item) for item in cur.fetchall()]

    condiciones = []
    parametros: list[Any] = []
    if eid is not None:
        condiciones.append("i.envio_id=%s")
        parametros.append(int(eid))
    if sid is not None:
        condiciones.append("a.solicitud_id=%s")
        parametros.append(int(sid))
    facturas: list[dict[str, Any]] = []
    if condiciones:
        cur.execute(
            f"""
            SELECT f.id, f.tipo, f.estado, f.punto_venta, f.numero,
                   f.fecha_emision, f.total, SUM(i.monto) AS item_monto,
                   ARRAY_AGG(i.envio_id) FILTER (
                       WHERE i.envio_id IS NOT NULL
                   ) AS envio_ids,
                   ARRAY_AGG(i.ajuste_id) FILTER (
                       WHERE i.ajuste_id IS NOT NULL
                   ) AS ajuste_ids
              FROM facturas_cliente_items i
              JOIN facturas_cliente f ON f.id=i.factura_id
         LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
             WHERE ({' OR '.join(condiciones)})
             GROUP BY f.id, f.tipo, f.estado, f.punto_venta, f.numero,
                      f.fecha_emision, f.total
             ORDER BY f.fecha_emision, f.id
            """,
            tuple(parametros),
        )
        facturas = [dict(item) for item in cur.fetchall()]

    aplicaciones: list[dict[str, Any]] = []
    factura_ids = sorted({int(item["id"]) for item in facturas})
    if eid is not None:
        cur.execute(
            """
            SELECT pa.id, pa.pago_id, pa.estado, pa.monto_ars, pa.ambito,
                   pa.envio_id, pa.factura_id,
                   p.estado AS pago_estado, p.fecha
              FROM pagos_aplicaciones pa
              JOIN pagos p ON p.id=pa.pago_id
             WHERE (pa.envio_id=%s
                    OR (%s::bigint[] <> '{}'::bigint[]
                        AND pa.factura_id=ANY(%s::bigint[])))
               AND p.cliente_id=%s
             ORDER BY pa.id
            """,
            (int(eid), factura_ids, factura_ids, fila["cliente_id"]),
        )
        aplicaciones = [dict(item) for item in cur.fetchall()]

    recolecciones: list[dict[str, Any]] = []
    controles: list[dict[str, Any]] = []
    if sid is not None:
        cur.execute(
            """
            SELECT id, estado, courier, fecha, confirmation_code
              FROM recolecciones
             WHERE solicitud_id=%s
               AND estado=ANY(%s)
             ORDER BY id
            """,
            (int(sid), list(ACTIVOS_RECOLECCION)),
        )
        recolecciones = [dict(item) for item in cur.fetchall()]
        cur.execute(
            """
            SELECT id, operacion, estado, riesgo_estado, solicitud_nueva_id
              FROM solicitudes_guia_reemisiones
             WHERE solicitud_anterior_id=%s
             ORDER BY id
            """,
            (int(sid),),
        )
        controles = [dict(item) for item in cur.fetchall()]
    cur.execute(
        """SELECT created_at, actor_ref, metadata->>'motivo' AS motivo
             FROM security_audit
            WHERE event='admin.envio_cancelacion_comercial' AND success
              AND metadata->>'cliente_id'=%s
              AND ((%s IS NOT NULL AND metadata->>'envio_id'=%s)
                OR (%s IS NOT NULL AND metadata->>'solicitud_id'=%s))
            ORDER BY id DESC LIMIT 1""",
        (fila["cliente_id"], str(eid) if eid is not None else None,
         str(eid) if eid is not None else None, str(sid) if sid is not None else None,
         str(sid) if sid is not None else None),
    )
    cancelacion = cur.fetchone()
    return {
        "ajustes": ajustes,
        "facturas": facturas,
        "aplicaciones_pago": aplicaciones,
        "recolecciones_activas": recolecciones,
        "controles_previos": controles,
        "cancelacion": dict(cancelacion) if cancelacion else None,
    }


def _revision(fila: dict[str, Any], historial: dict[str, Any]) -> str:
    firma = {
        "cliente_id": fila.get("cliente_id"),
        "solicitud_id": fila.get("solicitud_id"),
        "envio_id": fila.get("envio_id"),
        "solicitud_estado": fila.get("solicitud_estado"),
        "solicitud_updated_at": str(fila.get("solicitud_updated_at") or ""),
        "cargo_estado": fila.get("cargo_estado"),
        "monto_ars": str(fila.get("monto_ars") or ""),
        "cargo_pendiente": bool(fila.get("cargo_pendiente")),
        "ajustes": [
            (item["id"], item["estado"], str(item["monto_ars"]))
            for item in historial["ajustes"]
        ],
        "facturas": [
            (item["id"], item["tipo"], item["estado"])
            for item in historial["facturas"]
        ],
        "aplicaciones": [
            (item["id"], item["estado"], item["pago_estado"], str(item["monto_ars"]))
            for item in historial["aplicaciones_pago"]
        ],
        "recolecciones": [
            (item["id"], item["estado"])
            for item in historial["recolecciones_activas"]
        ],
        "controles": [
            (item["id"], item["operacion"], item["estado"])
            for item in historial["controles_previos"]
        ],
    }
    canonica = json.dumps(firma, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonica.encode("utf-8")).hexdigest()


def _retiros_agendados(historial: dict[str, Any]) -> list[dict[str, Any]]:
    """Retiros confirmados por el courier: la cancelación los anula primero."""
    return [
        item for item in historial["recolecciones_activas"]
        if str(item.get("estado") or "").upper() == "AGENDADA"
    ]


def _retiros_en_curso(historial: dict[str, Any]) -> list[dict[str, Any]]:
    """Retiros a mitad de camino (agendando, cancelando, por verificar): nadie
    puede anularlos a ciegas; se resuelven en Recolecciones."""
    return [
        item for item in historial["recolecciones_activas"]
        if str(item.get("estado") or "").upper() != "AGENDADA"
    ]


def _bloqueo_cancelacion(fila: dict[str, Any], historial: dict[str, Any]) -> tuple[str | None, str | None]:
    estado_solicitud = str(fila.get("solicitud_estado") or "").upper()
    estado_cargo = str(fila.get("cargo_estado") or "").upper()
    if estado_solicitud == "CANCELADO" or estado_cargo == "CANCELADO":
        return "YA_CANCELADO", "El envío ya está cancelado."
    if estado_solicitud == "REEMPLAZADO":
        return "ENVIO_REEMPLAZADO", "El envío fue reemplazado y conserva su historia."
    if estado_solicitud in ESTADOS_EMISION_EN_CURSO:
        return (
            "EMISION_EN_CURSO",
            "La emisión todavía está en curso o requiere verificar al courier.",
        )
    if fila.get("cargo_pendiente"):
        return "CARGO_PENDIENTE", "El cargo todavía se está registrando."
    if _retiros_en_curso(historial):
        return (
            "RECOLECCION_ACTIVA",
            "Hay una recolección en curso o por verificar con el courier. "
            "Resolvela en Recolecciones antes de cancelar el cargo.",
        )
    if historial["controles_previos"]:
        return (
            "CONTROL_PREVIO",
            "El envío ya tiene una corrección o cancelación operativa registrada.",
        )
    if fila.get("envio_id") is None:
        sin_emitir = (
            estado_solicitud == "SOLICITADO"
            and not str(fila.get("tracking") or "").strip()
            and not str(fila.get("guia_url") or "").strip()
            and not fila.get("tiene_label")
            and not fila.get("guia_generada_at")
        )
        if sin_emitir:
            return None, None
        return (
            "SOLICITUD_NO_CANCELABLE",
            "La solicitud ya avanzó y no tiene un cargo que pueda revertirse desde este control.",
        )
    if estado_cargo != "ACTIVO":
        return "CARGO_NO_ACTIVO", "El cargo ya no está activo."
    if str(fila.get("nro_fc") or "").strip():
        return (
            "REQUIERE_NOTA_CREDITO",
            "El cargo tiene una factura anterior. Documentá la nota de crédito antes de cancelarlo.",
        )
    aplicaciones_vigentes = [
        item for item in historial["aplicaciones_pago"]
        if item["estado"] in ("APLICADA", "SOLICITADA")
        and item["pago_estado"] in ("APROBADO", "PENDIENTE")
    ]
    if aplicaciones_vigentes:
        return (
            "PAGO_IMPUTADO",
            "El cargo tiene un pago imputado. Desimputalo con trazabilidad antes de cancelarlo.",
        )
    emitidas = [f for f in historial["facturas"] if f["estado"] == "EMITIDA"]
    if emitidas:
        total_fc = sum(
            (Decimal(str(f["item_monto"])) for f in emitidas if f["tipo"] == "FC"),
            Decimal("0"),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_nc = sum(
            (Decimal(str(f["item_monto"])) for f in emitidas if f["tipo"] == "NC"),
            Decimal("0"),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ajustes = sum(
            (
                Decimal(str(a["monto_ars"]))
                for a in historial["ajustes"] if a["estado"] == "APLICADO"
            ),
            Decimal("0"),
        )
        vigente = (_decimal(fila.get("monto_ars")) + ajustes).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        cobertura_exacta = (
            total_fc > 0 and total_nc == total_fc and vigente == 0
            and all(f["tipo"] in ("FC", "NC") for f in emitidas)
        )
        if not cobertura_exacta:
            if total_fc > 0 and total_nc == 0:
                return (
                    "REQUIERE_NOTA_CREDITO",
                    "El cargo o uno de sus ajustes ya fue facturado. Requiere una nota de crédito vinculada antes de cancelarlo.",
                )
            return (
                "COBERTURA_FISCAL_INCOMPLETA",
                "Las partidas de factura y nota de crédito no se compensan exactamente para este envío. Requiere revisión contable.",
            )
    return None, None


def _armar_contexto(cur, fila: dict[str, Any]) -> dict[str, Any]:
    historial = _historial(cur, fila)
    ajustes_aplicados = [a for a in historial["ajustes"] if a["estado"] == "APLICADO"]
    suma_ajustes = sum((Decimal(str(a["monto_ars"])) for a in ajustes_aplicados), Decimal("0"))
    original = _decimal(fila.get("monto_ars")) if fila.get("envio_id") is not None else None
    vigente = (
        (original + suma_ajustes).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if original is not None else None
    )
    ajustes_redondeados = suma_ajustes.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    codigo, motivo = _bloqueo_cancelacion(fila, historial)
    retiros_agendados = _retiros_agendados(historial) if codigo is None else []
    precio_habilitado = (
        fila.get("envio_id") is not None
        and fila.get("solicitud_id") is not None
        and fila.get("cargo_estado") == "ACTIVO"
        and fila.get("solicitud_estado") not in ("CANCELADO", "REEMPLAZADO")
        and not bool(fila.get("test"))
    )
    return {
        "solicitud": {
            "id": fila.get("solicitud_id"),
            "cliente_id": fila["cliente_id"],
            "estado": fila.get("solicitud_estado"),
            "courier": fila.get("courier"),
            "tracking": fila.get("tracking"),
            "tracking_estado": fila.get("tracking_estado"),
            "cargo_pendiente": bool(fila.get("cargo_pendiente")),
        } if fila.get("solicitud_id") is not None else None,
        "cargo": {
            "id": fila.get("envio_id"),
            "cliente_id": fila["cliente_id"],
            "estado": fila.get("cargo_estado"),
            "monto_ars": original,
            "ambito": fila.get("ambito"),
            "nro_fc": fila.get("nro_fc"),
        } if fila.get("envio_id") is not None else None,
        "cancelacion": {
            "habilitada": codigo is None,
            "codigo": codigo,
            "motivo": motivo,
            "modo": (
                "SOLICITUD_SIN_CARGO" if fila.get("envio_id") is None
                else "CANCELACION_COMERCIAL"
            ),
            # Un solo botón: si hay retiro agendado, cancelar lo anula primero.
            "retiros": [
                {
                    "id": item["id"],
                    "courier": item.get("courier"),
                    "fecha": item.get("fecha"),
                    "confirmation_code": item.get("confirmation_code"),
                }
                for item in retiros_agendados
            ],
        },
        "precio": {
            "habilitada": precio_habilitado,
            "motivo": None if precio_habilitado else (
                "Este envío no admite cambios de precio."
            ),
            # Nombres cortos para formularios y sufijo explícito para APIs.
            "original": original,
            "vigente": vigente,
            "ajustes": ajustes_redondeados,
            "original_ars": original,
            "vigente_ars": vigente,
            "ajustes_ars": ajustes_redondeados,
        },
        "revision": _revision(fila, historial),
        "historial": historial,
        "aviso_alcance": (
            "La cancelación anula primero el retiro agendado ante el courier y "
            "después quita el cargo de la cuenta del cliente. La guía no se anula."
            if retiros_agendados else
            "La cancelación quita el cargo de la cuenta del cliente. "
            "No cancela la guía ante el courier."
        ),
    }


def obtener_control_envio_admin(
    *,
    solicitud_id: int | None = None,
    envio_id: int | None = None,
    cliente_id: str | None = None,
) -> dict[str, Any]:
    """Devuelve el estado financiero y los bloqueos sin modificar datos."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            fila = _resolver(
                cur, solicitud_id=solicitud_id, envio_id=envio_id,
                cliente_id=cliente_id,
            )
            return _armar_contexto(cur, fila)


def cancelar_envio_admin(
    *,
    solicitud_id: int | None = None,
    envio_id: int | None = None,
    cliente_id: str | None = None,
    motivo: str,
    actor: str,
    revision: str,
) -> dict[str, Any]:
    """Quita un cargo de la cuenta con locks, stale guard y auditoría atómica."""
    motivo_limpio = _motivo(motivo)
    actor_limpio = _motivo(actor)[:120] or "admin"
    if len(motivo_limpio) < 8:
        raise ControlEnvioAdminError(
            "Explicá el motivo de la cancelación con al menos 8 caracteres.",
            codigo="MOTIVO_REQUERIDO",
        )
    revision_limpia = str(revision or "").strip().lower()
    if len(revision_limpia) != 64:
        raise ControlEnvioAdminError(
            "El control quedó desactualizado. Recargá antes de cancelar.",
            codigo="CAMBIO_CONCURRENTE",
        )
    sid, eid = _identificadores(solicitud_id, envio_id)
    revision_limpia, retiros_cancelados = _anular_retiros_agendados(
        sid=sid, eid=eid, cliente_id=cliente_id, revision=revision_limpia,
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            preliminar = _resolver(
                cur, solicitud_id=sid, envio_id=eid, cliente_id=cliente_id,
            )
            sid_real = preliminar.get("solicitud_id")
            eid_real = preliminar.get("envio_id")
            # Orden común con emisión, ajustes y cancelación del portal.
            if sid_real is not None:
                cur.execute(
                    "SELECT id FROM solicitudes_guia WHERE id=%s FOR UPDATE",
                    (int(sid_real),),
                )
                if not cur.fetchone():
                    raise ControlEnvioAdminError(
                        "El envío ya no existe.", codigo="CAMBIO_CONCURRENTE"
                    )
            if eid_real is not None:
                cur.execute(
                    "SELECT id FROM envios WHERE id=%s FOR UPDATE",
                    (int(eid_real),),
                )
                if not cur.fetchone():
                    raise ControlEnvioAdminError(
                        "El cargo ya no existe.", codigo="CAMBIO_CONCURRENTE"
                    )
            fila = _resolver(
                cur, solicitud_id=sid_real, envio_id=eid_real,
                cliente_id=cliente_id,
            )
            contexto = _armar_contexto(cur, fila)
            if contexto["revision"] != revision_limpia:
                raise ControlEnvioAdminError(
                    "El envío cambió desde que abriste el control. Recargá antes de cancelar.",
                    codigo="CAMBIO_CONCURRENTE",
                )
            if not contexto["cancelacion"]["habilitada"]:
                raise ControlEnvioAdminError(
                    contexto["cancelacion"]["motivo"],
                    codigo=contexto["cancelacion"]["codigo"],
                )

            if eid_real is not None:
                cur.execute(
                    """
                    UPDATE envios SET estado='CANCELADO'
                     WHERE id=%s AND cliente_id=%s AND estado='ACTIVO'
                    RETURNING id
                    """,
                    (int(eid_real), fila["cliente_id"]),
                )
                if not cur.fetchone():
                    raise ControlEnvioAdminError(
                        "El cargo cambió mientras se procesaba la cancelación.",
                        codigo="CAMBIO_CONCURRENTE",
                    )
            if sid_real is not None:
                cur.execute(
                    """
                    UPDATE solicitudes_guia
                       SET estado='CANCELADO', cargo_pendiente=FALSE,
                           cancelacion_comercial=TRUE, updated_at=NOW()
                     WHERE id=%s AND cliente_id=%s
                       AND estado NOT IN ('CANCELADO','REEMPLAZADO')
                    RETURNING id
                    """,
                    (int(sid_real), fila["cliente_id"]),
                )
                if not cur.fetchone():
                    raise ControlEnvioAdminError(
                        "El envío cambió mientras se procesaba la cancelación.",
                        codigo="CAMBIO_CONCURRENTE",
                    )

            registrar_evento_con_cursor(
                cur,
                event="admin.envio_cancelacion_comercial",
                actor_type="admin",
                actor_ref=actor_limpio,
                ip=None,
                method=None,
                path=None,
                status_code=200,
                success=True,
                request_id=None,
                metadata={
                    "cliente_id": fila["cliente_id"],
                    "solicitud_id": sid_real,
                    "envio_id": eid_real,
                    "motivo": motivo_limpio,
                    "modo": contexto["cancelacion"]["modo"],
                    "precio_vigente_ars": (
                        str(contexto["precio"]["vigente_ars"])
                        if contexto["precio"]["vigente_ars"] is not None else None
                    ),
                    "ajustes_preservados": len(contexto["historial"]["ajustes"]),
                    "alcance": "CUENTA_CLIENTE",
                    "courier_cancelado": False,
                    "retiros_cancelados": retiros_cancelados,
                },
            )
            return {
                "ok": True,
                "cliente_id": fila["cliente_id"],
                "solicitud_id": sid_real,
                "envio_id": eid_real,
                "estado": "CANCELADO",
                "modo": contexto["cancelacion"]["modo"],
                "monto_anulado_ars": contexto["precio"]["vigente_ars"],
                "alcance": "CUENTA_CLIENTE",
                "courier_cancelado": False,
                "retiros_cancelados": retiros_cancelados,
                "aviso": contexto["aviso_alcance"],
            }


def _anular_retiros_agendados(
    *, sid: int | None, eid: int | None, cliente_id: str | None, revision: str,
) -> tuple[str, list[str]]:
    """Paso 0 de la cancelación: anular ante el courier los retiros AGENDADOS.

    Va fuera de la transacción del cargo porque llama al courier (DHL) y esa
    llamada no se puede deshacer con un rollback. Si el courier no confirma,
    se corta acá y el cargo no se toca: el retiro queda en VERIFICAR_COURIER y
    el control pasa a bloquear la cancelación hasta resolverlo.

    La revisión que vio el admin incluye los retiros; después de anularlos se
    recalcula, exigiendo que NADA más haya cambiado en el medio.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            fila = _resolver(cur, solicitud_id=sid, envio_id=eid, cliente_id=cliente_id)
            contexto = _armar_contexto(cur, fila)
    if contexto["revision"] != revision:
        raise ControlEnvioAdminError(
            "El envío cambió desde que abriste el control. Recargá antes de cancelar.",
            codigo="CAMBIO_CONCURRENTE",
        )
    if not contexto["cancelacion"]["habilitada"]:
        raise ControlEnvioAdminError(
            contexto["cancelacion"]["motivo"], codigo=contexto["cancelacion"]["codigo"],
        )
    retiros = contexto["cancelacion"]["retiros"]
    if not retiros:
        return revision, []
    from servicios import recolecciones as recolecciones_srv
    cancelados: list[str] = []
    for rec in retiros:
        etiqueta = str(rec.get("confirmation_code") or rec["id"])
        resultado = recolecciones_srv.cancelar(int(rec["id"]))
        if not resultado.get("ok"):
            raise ControlEnvioAdminError(
                f"No se pudo anular el retiro {etiqueta} ante "
                f"{rec.get('courier') or 'el courier'}: "
                f"{resultado.get('error') or 'sin confirmación'}. El cargo no se tocó.",
                codigo="RECOLECCION_NO_CANCELADA",
            )
        cancelados.append(etiqueta)
    with get_conn() as conn:
        with conn.cursor() as cur:
            fila = _resolver(cur, solicitud_id=sid, envio_id=eid, cliente_id=cliente_id)
            historial = _historial(cur, fila)
    sin_cambio_de_retiros = {
        **historial,
        "recolecciones_activas": contexto["historial"]["recolecciones_activas"],
    }
    if _revision(fila, sin_cambio_de_retiros) != revision:
        raise ControlEnvioAdminError(
            "El envío cambió mientras se anulaba el retiro. Recargá antes de cancelar.",
            codigo="CAMBIO_CONCURRENTE",
        )
    return _revision(fila, historial), cancelados
