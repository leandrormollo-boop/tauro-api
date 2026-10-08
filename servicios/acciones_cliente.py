"""Política única para acciones pendientes del cliente en el portal."""

from __future__ import annotations

import re
from typing import Any

from core.database import get_conn
from servicios.estados_envio import es_envio_realizado, estado_principal_envio


_ALIAS_SQL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ACCION_DESCARGAR_GUIA = {
    "codigo": "descargar_guia",
    "titulo": "Descargar guía",
    "detalle": "Descargá la guía para preparar el envío.",
}


def _texto(valor: Any) -> str:
    return str(valor or "").strip()


def requiere_accion_cliente(envio: dict) -> bool:
    """Indica si al cliente sólo le falta descargar una guía disponible.

    Las filas que ya vienen de los listados del portal no incluyen las
    columnas ``test`` y ``visible_cliente`` porque la consulta ya las filtra.
    Por eso la ausencia de esas claves conserva los defaults del esquema.
    """
    if bool(envio.get("test", False)):
        return False
    if not bool(envio.get("visible_cliente", True)):
        return False
    if _texto(envio.get("cargo_estado")).upper() == "CANCELADO":
        return False
    if es_envio_realizado(envio):
        return False
    if envio.get("reemplaza_solicitud_id") and (
        bool(envio.get("cargo_pendiente"))
        or _texto(envio.get("reemision_estado")).upper() != "EMITIDA"
    ):
        return False

    estado = estado_principal_envio(
        envio.get("estado"),
        envio.get("tracking_estado"),
    )
    if estado != "GUIA_LISTA":
        return False
    if _texto(envio.get("tracking_estado")):
        return False
    if envio.get("guia_descargada_at") is not None:
        return False

    return bool(envio.get("tiene_label"))


def accion_pendiente_cliente(envio: dict) -> dict | None:
    """Devuelve la única acción explícita hoy modelada para el cliente."""
    if not requiere_accion_cliente(envio):
        return None
    return dict(_ACCION_DESCARGAR_GUIA)


def accion_cliente_sql(alias: str = "s") -> str:
    """Predicado SQL equivalente a :func:`requiere_accion_cliente`.

    El alias forma parte de la consulta y no puede enviarse como parámetro de
    PostgreSQL. Sólo se aceptan identificadores simples para impedir que un
    caller convierta este helper en una vía de inyección SQL.
    """
    if not isinstance(alias, str) or not _ALIAS_SQL.fullmatch(alias):
        raise ValueError("El alias SQL no es válido.")

    return f"""(
        {alias}.test IS FALSE
        AND {alias}.visible_cliente IS TRUE
        AND UPPER(BTRIM(COALESCE({alias}.estado, ''))) = 'GUIA_LISTA'
        AND {alias}.guia_descargada_at IS NULL
        AND NULLIF(BTRIM(COALESCE({alias}.tracking_estado, '')), '') IS NULL
        AND LEFT(COALESCE(BTRIM({alias}.coti_id), ''), 4) <> 'EXT-'
        AND {alias}.label_pdf IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM solicitudes_guia_reemisiones accion_reemision
            WHERE accion_reemision.solicitud_nueva_id = {alias}.id
              AND (
                  accion_reemision.estado <> 'EMITIDA'
                  OR {alias}.cargo_pendiente = TRUE
              )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM envios accion_cargo_cancelado
            WHERE accion_cargo_cancelado.solicitud_id = {alias}.id
              AND accion_cargo_cancelado.cliente_id = {alias}.cliente_id
              AND UPPER(BTRIM(accion_cargo_cancelado.estado)) = 'CANCELADO'
        )
    )"""


def contar_acciones_cliente(cliente_id: str) -> int:
    """Cuenta acciones de envíos para un cliente sin cargar sus documentos."""
    cliente = _texto(cliente_id).upper()
    if not cliente:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM solicitudes_guia s
                WHERE s.cliente_id = %s
                  AND {accion_cliente_sql('s')}
                """,
                (cliente,),
            )
            fila = cur.fetchone()
    return int(fila["n"] if fila else 0)
