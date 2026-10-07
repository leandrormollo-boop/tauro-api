"""Control de facturas de operadores: bandejas, resoluciones y cuadres.

Una factura de DHL o FedEx trae guías de varios clientes. La unidad que se
decide es ``factura + tracking``. Cada guía facturada cae en UNA bandeja:

- ``VIGENTE``: una sola solicitud viva con ese tracking. Se propone el match.
- ``REEMPLAZADA``: la guía fue corregida y reemplazada por otra.
- ``CANCELADA``: TAURO anuló la guía y el operador la facturó igual.
- ``SIN_DUENO``: el tracking no existe en el portal, es ambiguo o sólo apunta
  a una guía oculta sin precio ni cargo de una importación histórica.
- ``YA_FACTURADO``: el flete de ese tracking ya está en otra factura activa.

Sólo ``VIGENTE`` vincula costo a un envío. Las demás quedan bloqueadas hasta
que una persona decide el destino del renglón: la cuenta de un cliente, un
reclamo al operador o lo absorbe TAURO. Nunca se crean solicitudes ocultas.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from psycopg2.extras import Json

from servicios import conciliacion_couriers as cc
from servicios.conciliacion_couriers import (
    CENTAVO_CONTROL,
    CUATRO_DECIMALES,
    ConciliacionCourierError,
    _decimal,
    _json_seguro,
    _registrar_auditoria,
    _texto,
    normalizar_tracking,
)

BANDEJAS = ("VIGENTE", "REEMPLAZADA", "CANCELADA", "SIN_DUENO", "YA_FACTURADO")
BANDEJAS_BLOQUEADAS = frozenset({"REEMPLAZADA", "CANCELADA", "SIN_DUENO", "YA_FACTURADO"})
DESTINOS = ("CLIENTE", "RECLAMO_OPERADOR", "ABSORBE_TAURO", "ES_CORRECTO")
ESTADOS_ENVIO_NO_VIGENTES = ("CANCELADO", "REEMPLAZADO")
# Un envío vigente sin factura después de este plazo es una alerta: o no
# salió, o el operador lo facturó en otra cuenta.
DIAS_SIN_FACTURA = 30

ETIQUETAS_BANDEJA = {
    "VIGENTE": "Envío vigente",
    "REEMPLAZADA": "Guía reemplazada facturada",
    "CANCELADA": "Guía cancelada facturada",
    "SIN_DUENO": "Sin dueño",
    "YA_FACTURADO": "Ya facturado en otra factura",
}
ETIQUETAS_DESTINO = {
    "CLIENTE": "Cuenta del cliente",
    "RECLAMO_OPERADOR": "Reclamo al operador",
    "ABSORBE_TAURO": "Lo absorbe TAURO",
    "ES_CORRECTO": "Cobro correcto",
}


def _conn_o_nueva(_conn):
    # Se resuelve en tiempo de llamada para compartir la conexión (y los
    # tests aislados) del módulo de conciliación.
    return nullcontext(_conn) if _conn is not None else cc.get_conn()


def _solicitudes_por_tracking(cur, *, courier: str, tracking: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT s.id, s.cliente_id, s.estado, s.visible_cliente,
               s.precio_tauro_ars, s.tracking, s.servicio_courier,
               s.tracking_estado, s.tracking_descripcion, s.tracking_evento_at,
               e.estado AS cargo_estado, e.monto_ars AS cargo_monto_ars
          FROM solicitudes_guia s
          LEFT JOIN envios e ON e.solicitud_id = s.id
         WHERE UPPER(BTRIM(s.courier)) = UPPER(BTRIM(%s))
           AND NULLIF(REGEXP_REPLACE(
               UPPER(BTRIM(s.tracking)), '[^A-Z0-9]', '', 'g'
           ), '') = %s
         ORDER BY s.id
        """,
        (courier, tracking),
    )
    return [dict(fila) for fila in cur.fetchall()]


def _es_guia_oculta_sin_cargo(solicitud: dict[str, Any]) -> bool:
    """Guía creada por una importación histórica: oculta, sin precio ni cargo."""
    if solicitud.get("visible_cliente") is not False:
        return False
    precio = solicitud.get("precio_tauro_ars")
    sin_precio = precio is None or _decimal(precio, "Precio") <= CENTAVO_CONTROL
    sin_cargo = solicitud.get("cargo_estado") != "ACTIVO"
    return sin_precio and sin_cargo


def _flete_en_otra_factura(
    cur, *, factura_id: int, courier: str, tracking: str, solicitud_id: int | None = None,
) -> dict[str, Any] | None:
    """Devuelve la otra factura activa que ya tiene el FLETE de este tracking."""
    cur.execute(
        """
        SELECT f.id AS factura_id, f.numero, f.tipo_documento, f.estado AS factura_estado,
               m.estado AS match_estado, m.solicitud_id
          FROM facturas_courier_items i
          JOIN facturas_courier f ON f.id = i.factura_id
          JOIN factura_courier_item_matches m ON m.item_id = i.id
         WHERE i.factura_id <> %(factura_id)s
           AND UPPER(BTRIM(f.courier)) = UPPER(BTRIM(%(courier)s))
           AND f.estado <> 'ANULADA'
           AND f.tipo_documento IN ('FC','ND')
           AND i.estado <> 'IGNORADO'
           AND i.concepto_tipo = 'FLETE'
           AND i.tracking_normalizado = %(tracking)s
           AND m.estado IN ('PROPUESTO','CONFIRMADO')
           AND (%(solicitud_id)s::integer IS NULL OR m.solicitud_id = %(solicitud_id)s)
           -- Un control histórico lleva el número de la FC real adentro
           -- (CONTROL-<nro>-<tracking>). Es el mismo documento, no un
           -- doble cobro: lo resuelve el cruce con TAURO 2026.
           AND NOT EXISTS (
               SELECT 1 FROM facturas_courier propia
                WHERE propia.id = %(factura_id)s
                  AND (f.numero_normalizado LIKE '%%' || propia.numero_normalizado || '%%'
                       OR propia.numero_normalizado LIKE '%%' || f.numero_normalizado || '%%')
           )
         ORDER BY f.id
         LIMIT 1
        """,
        {"factura_id": int(factura_id), "courier": courier, "tracking": tracking,
         "solicitud_id": solicitud_id},
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def _reemision_de(cur, solicitud_id: int) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT r.id, r.operacion, r.estado, r.riesgo_estado,
               r.tracking_anterior, r.tracking_nuevo,
               r.solicitud_nueva_id, r.tracking_anterior_estado_courier,
               r.tracking_anterior_descripcion, r.tracking_anterior_evento_fecha,
               n.estado AS nueva_estado, n.cliente_id AS nueva_cliente_id
          FROM solicitudes_guia_reemisiones r
          LEFT JOIN solicitudes_guia n ON n.id = r.solicitud_nueva_id
         WHERE r.solicitud_anterior_id = %s
        """,
        (int(solicitud_id),),
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def clasificar_guia_facturada(
    cur,
    *,
    factura_id: int,
    courier: str,
    tracking: str,
    conceptos: set[str] | frozenset[str],
    cliente_referencia: str | None = None,
    tipo_documento: str = "FC",
) -> dict[str, Any]:
    """Decide la bandeja de una guía facturada. No escribe nada.

    Una nota de crédito nunca es un doble cobro: devuelve plata, así que no
    pasa por la bandeja YA_FACTURADO.
    """
    tracking = normalizar_tracking(tracking)
    tiene_flete = "FLETE" in {str(c).upper() for c in conceptos}
    if _texto(tipo_documento).upper() == "NC":
        tiene_flete = False
    candidatas = _solicitudes_por_tracking(cur, courier=courier, tracking=tracking)
    if cliente_referencia and candidatas:
        filtradas = [
            s for s in candidatas
            if cc._cliente_referencia_coincide(s.get("cliente_id"), cliente_referencia)
        ]
        if not filtradas:
            # TAURO 2026 dice que la guía es de otro cliente: nadie la toma
            # hasta que una persona lo aclare.
            return {
                "bandeja": "SIN_DUENO", "solicitud_id": None,
                "solicitud_referencia_id": None,
                "detalle": {
                    "causa": "CLIENTE_DISTINTO",
                    "cliente_referencia": cliente_referencia,
                    "candidatos": [
                        {"solicitud_id": int(s["id"]), "cliente_id": s["cliente_id"],
                         "estado": s["estado"]} for s in candidatas
                    ],
                },
            }
        candidatas = filtradas

    if not candidatas:
        return {
            "bandeja": "SIN_DUENO", "solicitud_id": None,
            "solicitud_referencia_id": None,
            "detalle": {"causa": "SIN_SOLICITUD", "tracking": tracking},
        }

    clientes = {_texto(s.get("cliente_id")).upper() for s in candidatas}
    if len(clientes) > 1:
        return {
            "bandeja": "SIN_DUENO", "solicitud_id": None,
            "solicitud_referencia_id": None,
            "detalle": {
                "causa": "AMBIGUO",
                "candidatos": [
                    {"solicitud_id": int(s["id"]), "cliente_id": s["cliente_id"],
                     "estado": s["estado"]} for s in candidatas
                ],
            },
        }

    vivas = [s for s in candidatas if s.get("estado") not in ESTADOS_ENVIO_NO_VIGENTES]
    if len(vivas) > 1:
        return {
            "bandeja": "SIN_DUENO", "solicitud_id": None,
            "solicitud_referencia_id": None,
            "detalle": {
                "causa": "AMBIGUO",
                "candidatos": [
                    {"solicitud_id": int(s["id"]), "cliente_id": s["cliente_id"],
                     "estado": s["estado"]} for s in vivas
                ],
            },
        }

    if not vivas:
        # Todas canceladas o reemplazadas: manda la más reciente.
        solicitud = candidatas[-1]
        reemision = _reemision_de(cur, int(solicitud["id"]))
        if solicitud.get("estado") == "REEMPLAZADO":
            nueva_facturada = None
            if reemision and reemision.get("tracking_nuevo"):
                nueva_facturada = _flete_en_otra_factura(
                    cur, factura_id=factura_id, courier=courier,
                    tracking=normalizar_tracking(reemision["tracking_nuevo"]),
                ) is not None
            return {
                "bandeja": "REEMPLAZADA", "solicitud_id": None,
                "solicitud_referencia_id": int(solicitud["id"]),
                "detalle": {
                    "cliente_id": solicitud["cliente_id"],
                    "tracking_nuevo": (reemision or {}).get("tracking_nuevo"),
                    "solicitud_nueva_id": (reemision or {}).get("solicitud_nueva_id"),
                    "nueva_facturada": nueva_facturada,
                    "tiene_flete": tiene_flete,
                },
            }
        return {
            "bandeja": "CANCELADA", "solicitud_id": None,
            "solicitud_referencia_id": int(solicitud["id"]),
            "detalle": {
                "cliente_id": solicitud["cliente_id"],
                "riesgo_estado": (reemision or {}).get("riesgo_estado"),
                "tracking_estado": solicitud.get("tracking_estado")
                    or (reemision or {}).get("tracking_anterior_estado_courier"),
                "tracking_descripcion": solicitud.get("tracking_descripcion")
                    or (reemision or {}).get("tracking_anterior_descripcion"),
                "tracking_evento_at": solicitud.get("tracking_evento_at")
                    or (reemision or {}).get("tracking_anterior_evento_fecha"),
                "tiene_flete": tiene_flete,
            },
        }

    solicitud = vivas[0]
    if _es_guia_oculta_sin_cargo(solicitud):
        return {
            "bandeja": "SIN_DUENO", "solicitud_id": None,
            "solicitud_referencia_id": int(solicitud["id"]),
            "detalle": {
                "causa": "GUIA_OCULTA",
                "cliente_id": solicitud["cliente_id"],
                "ayuda": "Completá la guía con 'Cargar envío realizado' y después volvé a vincular.",
            },
        }
    if tiene_flete:
        otra = _flete_en_otra_factura(
            cur, factura_id=factura_id, courier=courier, tracking=tracking,
        )
        if otra:
            return {
                "bandeja": "YA_FACTURADO", "solicitud_id": None,
                "solicitud_referencia_id": int(solicitud["id"]),
                "detalle": {
                    "cliente_id": solicitud["cliente_id"],
                    "factura_anterior_id": int(otra["factura_id"]),
                    "factura_anterior_numero": otra["numero"],
                    "factura_anterior_tipo": otra["tipo_documento"],
                    "match_anterior_estado": otra["match_estado"],
                },
            }
    return {
        "bandeja": "VIGENTE", "solicitud_id": int(solicitud["id"]),
        "solicitud_referencia_id": int(solicitud["id"]),
        "detalle": {"cliente_id": solicitud["cliente_id"]},
    }


def registrar_bandeja(
    cur,
    *,
    factura_id: int,
    tracking: str,
    clasificacion: dict[str, Any],
    actor: str,
) -> dict[str, Any] | None:
    """Deja constancia de una bandeja bloqueada. Idempotente por factura+guía."""
    if clasificacion["bandeja"] not in BANDEJAS_BLOQUEADAS:
        return None
    tracking = normalizar_tracking(tracking)
    cur.execute(
        """
        SELECT id, bandeja, estado, detalle
          FROM factura_courier_guia_resoluciones
         WHERE factura_id = %s AND tracking_normalizado = %s
         FOR UPDATE
        """,
        (int(factura_id), tracking),
    )
    existente = cur.fetchone()
    if existente:
        if existente["estado"] == "PENDIENTE" and existente["bandeja"] == clasificacion["bandeja"]:
            cur.execute(
                """
                UPDATE factura_courier_guia_resoluciones
                   SET detalle = %s, updated_at = NOW()
                 WHERE id = %s
                """,
                (Json(_json_seguro(clasificacion["detalle"])), int(existente["id"])),
            )
        return dict(existente)
    cur.execute(
        """
        INSERT INTO factura_courier_guia_resoluciones (
            factura_id, tracking_normalizado, bandeja,
            solicitud_referencia_id, detalle, estado
        ) VALUES (%s, %s, %s, %s, %s, 'PENDIENTE')
        RETURNING id, bandeja, estado, detalle
        """,
        (
            int(factura_id), tracking, clasificacion["bandeja"],
            clasificacion.get("solicitud_referencia_id"),
            Json(_json_seguro(clasificacion["detalle"])),
        ),
    )
    fila = dict(cur.fetchone())
    _registrar_auditoria(
        cur,
        evento="GUIA_FACTURADA_EN_BANDEJA",
        actor=actor,
        factura_id=int(factura_id),
        solicitud_id=clasificacion.get("solicitud_referencia_id"),
        metadata={
            "resolucion_id": int(fila["id"]),
            "tracking": tracking,
            "bandeja": clasificacion["bandeja"],
            "detalle": _json_seguro(clasificacion["detalle"]),
        },
    )
    return fila


def resolucion_de_guia(cur, *, factura_id: int, tracking: str) -> dict[str, Any] | None:
    """Cualquier bandeja registrada (pendiente o resuelta) de esa guía."""
    cur.execute(
        """
        SELECT id, bandeja, estado, destino, detalle, solicitud_referencia_id,
               solicitud_id, cliente_id
          FROM factura_courier_guia_resoluciones
         WHERE factura_id = %s AND tracking_normalizado = %s
        """,
        (int(factura_id), normalizar_tracking(tracking)),
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def bandeja_pendiente(cur, *, factura_id: int, tracking: str) -> dict[str, Any] | None:
    fila = resolucion_de_guia(cur, factura_id=factura_id, tracking=tracking)
    return fila if fila and fila["estado"] == "PENDIENTE" else None


def listar_resoluciones_factura(cur, factura_id: int) -> dict[str, dict[str, Any]]:
    cur.execute(
        """
        SELECT r.*, s.cliente_id AS referencia_cliente_id,
               s.estado AS referencia_estado, s.tracking AS referencia_tracking,
               d.cliente_id AS destino_cliente_id, d.tracking AS destino_tracking
          FROM factura_courier_guia_resoluciones r
          LEFT JOIN solicitudes_guia s ON s.id = r.solicitud_referencia_id
          LEFT JOIN solicitudes_guia d ON d.id = r.solicitud_id
         WHERE r.factura_id = %s
        """,
        (int(factura_id),),
    )
    return {fila["tracking_normalizado"]: dict(fila) for fila in cur.fetchall()}


def resumen_bandejas_factura(factura_id: int, *, _conn=None) -> dict[str, Any]:
    with _conn_o_nueva(_conn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bandeja, estado, COUNT(*) AS cantidad
                  FROM factura_courier_guia_resoluciones
                 WHERE factura_id = %s
                 GROUP BY bandeja, estado
                """,
                (int(factura_id),),
            )
            pendientes: dict[str, int] = {}
            resueltas: dict[str, int] = {}
            for fila in cur.fetchall():
                destino = pendientes if fila["estado"] == "PENDIENTE" else resueltas
                destino[fila["bandeja"]] = int(fila["cantidad"])
    return {
        "pendientes": pendientes,
        "resueltas": resueltas,
        "total_pendientes": sum(pendientes.values()),
    }


def _lineas_grupo(cur, *, factura_id: int, tracking: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT i.id, i.importe, i.importe_ars, i.tipo_cambio_ars, i.signo,
               i.concepto_tipo, f.courier,
               COALESCE((
                   SELECT SUM(m.monto_asignado)
                     FROM factura_courier_item_matches m
                    WHERE m.item_id = i.id
                      AND m.estado IN ('PROPUESTO','CONFIRMADO')
               ), 0) AS asignado
          FROM facturas_courier_items i
          JOIN facturas_courier f ON f.id = i.factura_id
         WHERE i.factura_id = %s
           AND i.tracking_normalizado = %s
           AND i.estado <> 'IGNORADO'
         ORDER BY i.linea_numero
         FOR UPDATE OF i
        """,
        (int(factura_id), tracking),
    )
    return [dict(fila) for fila in cur.fetchall()]


def _buscar_envio_destino(cur, *, courier: str, identificador: str) -> dict[str, Any]:
    identificador = _texto(identificador)
    if not identificador:
        raise ConciliacionCourierError("Indicá el tracking o #ID del envío que recibe el costo.")
    if identificador.startswith("#"):
        id_texto = identificador[1:].strip()
        if not id_texto.isdigit():
            raise ConciliacionCourierError("El ID interno debe escribirse como #123.")
        cur.execute(
            """
            SELECT s.id, s.cliente_id, s.estado, s.tracking, s.courier,
                   e.estado AS cargo_estado
              FROM solicitudes_guia s
              LEFT JOIN envios e ON e.solicitud_id = s.id
             WHERE s.id = %s AND UPPER(BTRIM(s.courier)) = UPPER(BTRIM(%s))
            """,
            (int(id_texto), courier),
        )
    else:
        tracking = normalizar_tracking(identificador)
        if not tracking:
            raise ConciliacionCourierError("El tracking no es válido.")
        cur.execute(
            """
            SELECT s.id, s.cliente_id, s.estado, s.tracking, s.courier,
                   e.estado AS cargo_estado
              FROM solicitudes_guia s
              LEFT JOIN envios e ON e.solicitud_id = s.id
             WHERE UPPER(BTRIM(s.courier)) = UPPER(BTRIM(%s))
               AND NULLIF(REGEXP_REPLACE(
                   UPPER(BTRIM(s.tracking)), '[^A-Z0-9]', '', 'g'
               ), '') = %s
             ORDER BY s.id
            """,
            (courier, tracking),
        )
    candidatos = [dict(fila) for fila in cur.fetchall()]
    if not candidatos:
        raise ConciliacionCourierError("No existe un envío de ese operador con ese tracking o ID.")
    ids = {int(fila["id"]) for fila in candidatos}
    if len(ids) != 1:
        raise ConciliacionCourierError("La referencia es ambigua. Usá el ID interno con formato #123.")
    envio = candidatos[0]
    if envio.get("estado") in ESTADOS_ENVIO_NO_VIGENTES:
        raise ConciliacionCourierError(
            "El envío elegido está cancelado o reemplazado. Elegí la guía vigente del cliente."
        )
    if envio.get("cargo_estado") != "ACTIVO":
        raise ConciliacionCourierError(
            "El envío elegido no tiene un cargo activo en cuenta corriente. "
            "Si es una guía histórica, completala con 'Cargar envío realizado'."
        )
    return envio


def _proponer_matches_del_grupo(
    cur, *, factura_id: int, tracking: str, solicitud_id: int, actor: str,
    metodo: str, evidencia: str | None, lineas: list[dict[str, Any]],
) -> list[int]:
    """Inserta los matches de todas las líneas de la guía dentro de la transacción."""
    match_ids: list[int] = []
    for linea in lineas:
        remanente = (
            _decimal(linea["importe"], "Importe") - _decimal(linea["asignado"], "Asignado")
        ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
        if remanente <= CENTAVO_CONTROL:
            continue
        monto_ars = (
            remanente * _decimal(linea["tipo_cambio_ars"], "Tipo de cambio")
        ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
        try:
            cur.execute(
                """
                INSERT INTO factura_courier_item_matches (
                    item_id, solicitud_id, monto_asignado, monto_asignado_ars,
                    metodo, confianza, estado, evidencia_uri, creado_por
                ) VALUES (%s, %s, %s, %s, %s, %s, 'PROPUESTO', %s, %s)
                RETURNING id
                """,
                (
                    int(linea["id"]), int(solicitud_id), remanente, monto_ars,
                    metodo, 1 if metodo == "EXACTO_TRACKING" else None,
                    evidencia, _texto(actor) or "admin",
                ),
            )
        except Exception as exc:
            if getattr(exc, "pgcode", None) == "23505":
                raise ConciliacionCourierError(
                    "La guía ya tiene un match histórico con el envío elegido."
                ) from exc
            raise
        match_ids.append(int(cur.fetchone()["id"]))
    if not match_ids:
        raise ConciliacionCourierError("La guía ya está totalmente asignada.")
    return match_ids


def resolver_bandeja(
    resolucion_id: int,
    *,
    actor: str,
    destino: str,
    motivo: str,
    identificador_envio: str | None = None,
    evidencia_uri: str | None = None,
    _conn=None,
) -> dict[str, Any]:
    """Resuelve una bandeja bloqueada. Una sola decisión por guía facturada.

    ``CLIENTE`` vincula el costo a un envío vigente del cliente (match MANUAL
    propuesto, que después se confirma y calcula como cualquier otro).
    ``RECLAMO_OPERADOR`` y ``ABSORBE_TAURO`` cierran el renglón sin cliente.
    ``ES_CORRECTO`` sólo aplica a YA_FACTURADO: acepta el segundo cobro y lo
    vincula al envío de referencia.
    """
    actor = _texto(actor)
    motivo = _texto(motivo)
    destino = _texto(destino).upper()
    if not actor:
        raise ConciliacionCourierError("Falta identificar al operador.")
    if destino not in DESTINOS:
        raise ConciliacionCourierError("Elegí un destino válido para el renglón.")
    if len(motivo) < 8:
        raise ConciliacionCourierError("Explicá el motivo de la decisión (mínimo 8 caracteres).")
    with _conn_o_nueva(_conn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.*, f.courier, f.estado AS factura_estado
                  FROM factura_courier_guia_resoluciones r
                  JOIN facturas_courier f ON f.id = r.factura_id
                 WHERE r.id = %s
                 FOR UPDATE OF r
                """,
                (int(resolucion_id),),
            )
            resolucion = cur.fetchone()
            if not resolucion:
                raise ConciliacionCourierError("La bandeja no existe.")
            resolucion = dict(resolucion)
            if resolucion["estado"] != "PENDIENTE":
                raise ConciliacionCourierError("Esta guía ya fue resuelta por otra persona.")
            if resolucion["factura_estado"] == "ANULADA":
                raise ConciliacionCourierError("Una factura anulada no se resuelve.")
            bandeja = resolucion["bandeja"]
            factura_id = int(resolucion["factura_id"])
            tracking = resolucion["tracking_normalizado"]
            courier = resolucion["courier"]
            lineas = _lineas_grupo(cur, factura_id=factura_id, tracking=tracking)
            tiene_flete = any(l["concepto_tipo"] == "FLETE" for l in lineas)

            cliente_id = None
            solicitud_destino_id = None
            match_ids: list[int] = []
            if destino == "ES_CORRECTO":
                if bandeja != "YA_FACTURADO":
                    raise ConciliacionCourierError(
                        "'Cobro correcto' sólo aplica a una guía ya facturada en otra factura."
                    )
                if not resolucion.get("solicitud_referencia_id"):
                    raise ConciliacionCourierError("La guía no tiene un envío de referencia.")
                cur.execute(
                    """
                    SELECT s.id, s.cliente_id, s.estado, e.estado AS cargo_estado
                      FROM solicitudes_guia s
                      LEFT JOIN envios e ON e.solicitud_id = s.id
                     WHERE s.id = %s
                    """,
                    (int(resolucion["solicitud_referencia_id"]),),
                )
                envio = dict(cur.fetchone())
                if envio.get("cargo_estado") != "ACTIVO":
                    raise ConciliacionCourierError("El envío de referencia no tiene un cargo activo.")
                cliente_id = envio["cliente_id"]
                solicitud_destino_id = int(envio["id"])
                match_ids = _proponer_matches_del_grupo(
                    cur, factura_id=factura_id, tracking=tracking,
                    solicitud_id=solicitud_destino_id, actor=actor, metodo="MANUAL",
                    evidencia=f"admin://bandeja/{int(resolucion_id)}", lineas=lineas,
                )
            elif destino == "CLIENTE":
                envio = _buscar_envio_destino(
                    cur, courier=courier, identificador=identificador_envio or "",
                )
                if bandeja in ("REEMPLAZADA", "CANCELADA"):
                    cliente_origen = _texto((resolucion.get("detalle") or {}).get("cliente_id")).upper()
                    if cliente_origen and _texto(envio["cliente_id"]).upper() != cliente_origen:
                        raise ConciliacionCourierError(
                            f"La guía era de {cliente_origen}. El costo sólo puede ir a un envío "
                            "vigente de ese cliente; si es de otro, reclamá al operador."
                        )
                if tiene_flete:
                    otra = _flete_en_otra_factura(
                        cur, factura_id=factura_id, courier=courier,
                        tracking=normalizar_tracking(envio.get("tracking")),
                    )
                    if otra and bandeja != "YA_FACTURADO":
                        raise ConciliacionCourierError(
                            "La guía elegida ya tiene su flete en la factura "
                            f"{otra['tipo_documento']} {otra['numero']}. "
                            "Si el operador cobró dos veces, reclamá al operador."
                        )
                cliente_id = envio["cliente_id"]
                solicitud_destino_id = int(envio["id"])
                match_ids = _proponer_matches_del_grupo(
                    cur, factura_id=factura_id, tracking=tracking,
                    solicitud_id=solicitud_destino_id, actor=actor, metodo="MANUAL",
                    evidencia=f"admin://bandeja/{int(resolucion_id)}", lineas=lineas,
                )
            cur.execute(
                """
                UPDATE factura_courier_guia_resoluciones
                   SET estado = 'RESUELTA', destino = %s, cliente_id = %s,
                       solicitud_id = %s, motivo = %s, evidencia_uri = %s,
                       resuelto_por = %s, resuelto_at = NOW(), updated_at = NOW()
                 WHERE id = %s AND estado = 'PENDIENTE'
                RETURNING id
                """,
                (
                    destino, cliente_id, solicitud_destino_id, motivo,
                    _texto(evidencia_uri) or None, actor, int(resolucion_id),
                ),
            )
            if not cur.fetchone():
                raise ConciliacionCourierError("Esta guía ya fue resuelta por otra persona.")
            _registrar_auditoria(
                cur,
                evento="BANDEJA_RESUELTA",
                actor=actor,
                factura_id=factura_id,
                item_id=int(lineas[0]["id"]) if lineas else None,
                solicitud_id=solicitud_destino_id or resolucion.get("solicitud_referencia_id"),
                metadata={
                    "resolucion_id": int(resolucion_id),
                    "tracking": tracking,
                    "bandeja": bandeja,
                    "destino": destino,
                    "cliente_id": cliente_id,
                    "match_ids_lineas": match_ids,
                    "motivo": motivo,
                    "importe": str(sum(
                        (_decimal(l["importe"], "Importe") * Decimal(int(l["signo"])) for l in lineas),
                        Decimal("0"),
                    )),
                },
            )
            cc._actualizar_estado_factura(cur, factura_id)
            return {
                "id": int(resolucion_id),
                "factura_id": factura_id,
                "bandeja": bandeja,
                "destino": destino,
                "cliente_id": cliente_id,
                "solicitud_id": solicitud_destino_id,
                "match_ids": match_ids,
            }


def reintentar_bandeja(resolucion_id: int, *, actor: str, _conn=None) -> dict[str, Any]:
    """Vuelve a clasificar una bandeja pendiente.

    Si la guía ahora está vigente (por ejemplo, se completó la guía oculta con
    'Cargar envío realizado'), propone el match exacto y resuelve la bandeja
    como CLIENTE. Si no, actualiza el detalle y la deja pendiente.
    """
    actor = _texto(actor) or "admin"
    with _conn_o_nueva(_conn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.*, f.courier, f.tipo_documento
                  FROM factura_courier_guia_resoluciones r
                  JOIN facturas_courier f ON f.id = r.factura_id
                 WHERE r.id = %s
                 FOR UPDATE OF r
                """,
                (int(resolucion_id),),
            )
            resolucion = cur.fetchone()
            if not resolucion:
                raise ConciliacionCourierError("La bandeja no existe.")
            resolucion = dict(resolucion)
            if resolucion["estado"] != "PENDIENTE":
                return {"id": int(resolucion_id), "bandeja": resolucion["bandeja"],
                        "estado": resolucion["estado"], "cambio": False}
            factura_id = int(resolucion["factura_id"])
            tracking = resolucion["tracking_normalizado"]
            lineas = _lineas_grupo(cur, factura_id=factura_id, tracking=tracking)
            clasificacion = clasificar_guia_facturada(
                cur, factura_id=factura_id, courier=resolucion["courier"],
                tracking=tracking, conceptos={l["concepto_tipo"] for l in lineas},
                tipo_documento=resolucion.get("tipo_documento") or "FC",
            )
            if clasificacion["bandeja"] != "VIGENTE":
                cur.execute(
                    """
                    UPDATE factura_courier_guia_resoluciones
                       SET detalle = %s, updated_at = NOW()
                     WHERE id = %s
                    """,
                    (Json(_json_seguro({**clasificacion["detalle"],
                                        "reclasificada_como": clasificacion["bandeja"]})),
                     int(resolucion_id)),
                )
                return {"id": int(resolucion_id), "bandeja": resolucion["bandeja"],
                        "bandeja_actual": clasificacion["bandeja"],
                        "estado": "PENDIENTE", "cambio": False}
            solicitud_id = int(clasificacion["solicitud_id"])
            match_ids = _proponer_matches_del_grupo(
                cur, factura_id=factura_id, tracking=tracking,
                solicitud_id=solicitud_id, actor=actor, metodo="EXACTO_TRACKING",
                evidencia=None, lineas=lineas,
            )
            cur.execute(
                """
                UPDATE factura_courier_guia_resoluciones
                   SET estado = 'RESUELTA', destino = 'CLIENTE', cliente_id = %s,
                       solicitud_id = %s, motivo = %s, resuelto_por = %s,
                       resuelto_at = NOW(), updated_at = NOW()
                 WHERE id = %s AND estado = 'PENDIENTE'
                """,
                (
                    clasificacion["detalle"].get("cliente_id"), solicitud_id,
                    "Reclasificada: la guía ya está vigente en el portal.", actor,
                    int(resolucion_id),
                ),
            )
            _registrar_auditoria(
                cur,
                evento="BANDEJA_RESUELTA",
                actor=actor,
                factura_id=factura_id,
                item_id=int(lineas[0]["id"]) if lineas else None,
                solicitud_id=solicitud_id,
                metadata={
                    "resolucion_id": int(resolucion_id), "tracking": tracking,
                    "bandeja": resolucion["bandeja"], "destino": "CLIENTE",
                    "reclasificada": True, "match_ids_lineas": match_ids,
                },
            )
            cc._actualizar_estado_factura(cur, factura_id)
            return {"id": int(resolucion_id), "bandeja": resolucion["bandeja"],
                    "estado": "RESUELTA", "cambio": True,
                    "solicitud_id": solicitud_id, "match_ids": match_ids}


def cuadres_factura(cur, factura_id: int) -> dict[str, Any]:
    """Los dos cuadres de la factura, en moneda del documento.

    1. Renglones (sin ignorados) = total; y subtotal + impuestos = total
       cuando el documento informa subtotal.
    2. Asignado a clientes + reclamado + absorbido + cargos generales = total,
       sin ningún renglón sin destino.
    """
    cur.execute(
        """
        SELECT courier, tipo_documento, numero, numero_normalizado, moneda,
               subtotal, impuestos, total, estado, archivo_sha256
          FROM facturas_courier WHERE id = %s
        """,
        (int(factura_id),),
    )
    factura = cur.fetchone()
    if not factura:
        raise ConciliacionCourierError("La factura courier no existe.")
    factura = dict(factura)
    cur.execute(
        """
        SELECT i.id, i.tracking_normalizado, i.concepto_tipo, i.signo, i.importe,
               i.estado,
               COALESCE((
                   SELECT SUM(m.monto_asignado)
                     FROM factura_courier_item_matches m
                    WHERE m.item_id = i.id AND m.estado IN ('PROPUESTO','CONFIRMADO')
               ), 0) AS asignado,
               COALESCE((
                   SELECT SUM(m.monto_asignado)
                     FROM factura_courier_item_matches m
                    WHERE m.item_id = i.id AND m.estado = 'CONFIRMADO'
               ), 0) AS confirmado,
               EXISTS (
                   SELECT 1 FROM factura_courier_item_matches m
                    WHERE m.item_id = i.id AND m.estado = 'PROPUESTO'
               ) AS tiene_propuesto
          FROM facturas_courier_items i
         WHERE i.factura_id = %s
         ORDER BY i.linea_numero
        """,
        (int(factura_id),),
    )
    items = [dict(fila) for fila in cur.fetchall() if fila["estado"] != "IGNORADO"]
    resoluciones = listar_resoluciones_factura(cur, int(factura_id))

    cero = Decimal("0")
    suma_renglones = cero
    generales = cero
    asignado = cero
    reclamado = cero
    absorbido = cero
    pendiente_bandeja = cero
    sin_destino = cero
    grupos: dict[str, dict[str, Any]] = {}
    for item in items:
        monto = _decimal(item["importe"], "Importe") * Decimal(int(item["signo"]))
        suma_renglones += monto
        tracking = _texto(item.get("tracking_normalizado"))
        if not tracking:
            generales += monto
            continue
        grupo = grupos.setdefault(tracking, {
            "importe": cero, "asignado": cero, "confirmado": cero, "propuesto": False,
        })
        grupo["importe"] += monto
        grupo["asignado"] += _decimal(item["asignado"], "Asignado") * Decimal(int(item["signo"]))
        grupo["confirmado"] += _decimal(item["confirmado"], "Confirmado") * Decimal(int(item["signo"]))
        grupo["propuesto"] = grupo["propuesto"] or bool(item["tiene_propuesto"])

    guias_sin_destino: list[str] = []
    guias_pendientes: list[str] = []
    for tracking, grupo in grupos.items():
        resolucion = resoluciones.get(tracking)
        importe = grupo["importe"]
        if resolucion and resolucion["estado"] == "PENDIENTE":
            pendiente_bandeja += importe
            guias_pendientes.append(tracking)
            continue
        if resolucion and resolucion["estado"] == "RESUELTA" and resolucion["destino"] == "RECLAMO_OPERADOR":
            reclamado += importe
            continue
        if resolucion and resolucion["estado"] == "RESUELTA" and resolucion["destino"] == "ABSORBE_TAURO":
            absorbido += importe
            continue
        if abs(grupo["asignado"] - importe) <= CENTAVO_CONTROL and abs(importe) > cero:
            asignado += importe
            continue
        if abs(importe) <= CENTAVO_CONTROL:
            continue
        asignado += grupo["asignado"]
        sin_destino += importe - grupo["asignado"]
        guias_sin_destino.append(tracking)

    total = _decimal(factura["total"], "Total")
    subtotal = _decimal(factura.get("subtotal") or 0, "Subtotal")
    impuestos = _decimal(factura.get("impuestos") or 0, "Impuestos")
    q = lambda v: v.quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)  # noqa: E731
    diferencia_documento = q(suma_renglones - total) if items else cero
    cuadre_documento_ok = bool(items) and abs(diferencia_documento) <= CENTAVO_CONTROL and (
        subtotal <= cero or abs(subtotal + impuestos - total) <= CENTAVO_CONTROL
    )
    suma_destinos = asignado + reclamado + absorbido + generales
    diferencia_destinos = q(suma_destinos - total)
    cuadre_destinos_ok = (
        bool(items)
        and not guias_pendientes
        and not guias_sin_destino
        and abs(diferencia_destinos) <= CENTAVO_CONTROL
    )
    hay_propuestos = any(g["propuesto"] for g in grupos.values())
    return {
        "moneda": factura["moneda"],
        "total": q(total),
        "subtotal": q(subtotal),
        "impuestos_documento": q(impuestos),
        "suma_renglones": q(suma_renglones),
        "diferencia_documento": diferencia_documento,
        "cuadre_documento_ok": cuadre_documento_ok,
        "asignado_clientes": q(asignado),
        "reclamado_operador": q(reclamado),
        "absorbido_tauro": q(absorbido),
        "cargos_generales": q(generales),
        "pendiente_bandeja": q(pendiente_bandeja),
        "sin_destino": q(sin_destino),
        "diferencia_destinos": diferencia_destinos,
        "cuadre_destinos_ok": cuadre_destinos_ok,
        "guias_pendientes": guias_pendientes,
        "guias_sin_destino": guias_sin_destino,
        "hay_propuestos": hay_propuestos,
        "cantidad_guias": len(grupos),
    }


def posibles_duplicadas(cur, factura_id: int) -> list[dict[str, Any]]:
    """Otras facturas que parecen el mismo documento.

    La restricción única cubre courier + tipo + número normalizado. Acá se
    buscan los huecos: el mismo número con otro tipo, el número sin ceros a la
    izquierda, el mismo PDF o el mismo conjunto de guías e importes.
    """
    cur.execute(
        """
        SELECT id, courier, tipo_documento, numero, numero_normalizado,
               archivo_sha256, total
          FROM facturas_courier WHERE id = %s
        """,
        (int(factura_id),),
    )
    factura = cur.fetchone()
    if not factura:
        return []
    factura = dict(factura)
    cur.execute(
        """
        WITH guias AS (
            SELECT i.factura_id,
                   STRING_AGG(i.tracking_normalizado || ':' || i.importe::text,
                              ',' ORDER BY i.tracking_normalizado, i.importe) AS firma
              FROM facturas_courier_items i
             WHERE i.tracking_normalizado IS NOT NULL AND i.estado <> 'IGNORADO'
             GROUP BY i.factura_id
        ), propia AS (
            SELECT firma FROM guias WHERE factura_id = %(id)s
        )
        SELECT f.id, f.courier, f.tipo_documento, f.numero, f.estado, f.total,
               f.moneda, f.fecha_emision,
               ARRAY_REMOVE(ARRAY[
                   CASE WHEN f.numero_normalizado = %(numero)s THEN 'MISMO_NUMERO_OTRO_TIPO' END,
                   CASE WHEN f.numero_normalizado <> %(numero)s
                         AND LTRIM(f.numero_normalizado, '0') = LTRIM(%(numero)s, '0')
                        THEN 'MISMO_NUMERO_SIN_CEROS' END,
                   CASE WHEN f.numero_normalizado <> %(numero)s
                         AND LTRIM(f.numero_normalizado, '0') <> LTRIM(%(numero)s, '0')
                         AND (f.numero_normalizado LIKE '%%' || %(numero)s || '%%'
                              OR %(numero)s LIKE '%%' || f.numero_normalizado || '%%')
                        THEN 'CONTROL_HISTORICO_MISMA_FC' END,
                   CASE WHEN %(sha)s IS NOT NULL AND f.archivo_sha256 = %(sha)s THEN 'MISMO_PDF' END,
                   CASE WHEN g.firma IS NOT NULL AND g.firma = (SELECT firma FROM propia)
                        THEN 'MISMAS_GUIAS_E_IMPORTES' END
               ], NULL) AS motivos
          FROM facturas_courier f
          LEFT JOIN guias g ON g.factura_id = f.id
         WHERE f.id <> %(id)s
           AND UPPER(BTRIM(f.courier)) = UPPER(BTRIM(%(courier)s))
           AND f.estado <> 'ANULADA'
           AND (
               f.numero_normalizado = %(numero)s
               OR LTRIM(f.numero_normalizado, '0') = LTRIM(%(numero)s, '0')
               OR f.numero_normalizado LIKE '%%' || %(numero)s || '%%'
               OR %(numero)s LIKE '%%' || f.numero_normalizado || '%%'
               OR (%(sha)s IS NOT NULL AND f.archivo_sha256 = %(sha)s)
               OR (g.firma IS NOT NULL AND g.firma = (SELECT firma FROM propia))
           )
         ORDER BY f.id
        """,
        {
            "id": int(factura_id),
            "numero": factura["numero_normalizado"],
            "sha": factura.get("archivo_sha256"),
            "courier": factura["courier"],
        },
    )
    return [dict(fila) for fila in cur.fetchall()]


def regla_diferencia_cliente(cur, cliente_id: str | None) -> str:
    """Política de diferencias del cliente. Sin fila: la regla de Leandro."""
    if not _texto(cliente_id):
        return "COBRAR_SOLO_SI_MAYOR"
    cur.execute(
        "SELECT regla_diferencia_courier FROM clientes WHERE cliente_id = %s",
        (_texto(cliente_id).upper(),),
    )
    fila = cur.fetchone()
    regla = _texto((fila or {}).get("regla_diferencia_courier")).upper()
    return regla if regla in ("COBRAR_SOLO_SI_MAYOR", "COBRAR_SIEMPRE") else "COBRAR_SOLO_SI_MAYOR"


def fecha_limite_sin_factura(hoy: date | None = None) -> date:
    return (hoy or date.today()) - timedelta(days=DIAS_SIN_FACTURA)
