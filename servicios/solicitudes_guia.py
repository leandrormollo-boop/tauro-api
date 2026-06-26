# ============================================================
# Servicio de solicitudes de guía — PostgreSQL
# ============================================================

from typing import Optional

from core.database import get_conn


ESTADOS_SOLICITUD = [
    "SOLICITADO",
    "EN_PROCESO",
    "GUIA_LISTA",
    "DESPACHADO",
    "CANCELADO",
]


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def crear_solicitud_guia(
    *,
    cliente_id: str,
    producto_alias: str,
    cantidad: int,
    destino_pais: str,
    dest_nombre: str,
    dest_documento: str,
    dest_email: str,
    dest_telefono: str,
    dest_direccion: str,
    dest_ciudad: str,
    dest_estado: str,
    dest_zip: str,
    observaciones: str,
    peso_kg: float,
    largo_cm: float,
    ancho_cm: float,
    alto_cm: float,
    valor_declarado_usd: float,
    ruta_id: str,
    coti_id: str,
    precio_tauro_ars: float,
    precio_tauro_usd: float,
    remitente_alias: str = "",
    remitente_nombre: str = "",
    remitente_documento: str = "",
    remitente_email: str = "",
    remitente_telefono: str = "",
    remitente_direccion: str = "",
    remitente_ciudad: str = "",
    remitente_estado: str = "",
    remitente_zip: str = "",
    remitente_pais: str = "",
    precio_cliente_final_ars: Optional[float] = None,
) -> dict:
    """Crea una solicitud de guía pendiente para gestión operativa."""
    cliente_id = cliente_id.strip().upper()
    cantidad = max(int(cantidad or 1), 1)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, cantidad, destino_pais,
                    remitente_alias, remitente_nombre, remitente_documento,
                    remitente_email, remitente_telefono, remitente_direccion,
                    remitente_ciudad, remitente_estado, remitente_zip, remitente_pais,
                    dest_nombre, dest_documento, dest_email, dest_telefono,
                    dest_direccion, dest_ciudad, dest_estado, dest_zip,
                    observaciones, peso_kg, largo_cm, ancho_cm, alto_cm,
                    valor_declarado_usd, ruta_id, coti_id, precio_tauro_ars,
                    precio_tauro_usd, precio_cliente_final_ars
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
                )
                RETURNING *
                """,
                (
                    cliente_id,
                    producto_alias.strip(),
                    cantidad,
                    destino_pais.strip().upper(),
                    _clean(remitente_alias),
                    _clean(remitente_nombre),
                    _clean(remitente_documento),
                    _clean(remitente_email),
                    _clean(remitente_telefono),
                    _clean(remitente_direccion),
                    _clean(remitente_ciudad),
                    _clean(remitente_estado),
                    _clean(remitente_zip),
                    _clean(remitente_pais) or "AR",
                    dest_nombre.strip(),
                    _clean(dest_documento),
                    _clean(dest_email),
                    _clean(dest_telefono),
                    dest_direccion.strip(),
                    dest_ciudad.strip(),
                    _clean(dest_estado),
                    dest_zip.strip(),
                    _clean(observaciones),
                    peso_kg,
                    largo_cm,
                    ancho_cm,
                    alto_cm,
                    valor_declarado_usd,
                    ruta_id,
                    coti_id,
                    precio_tauro_ars,
                    precio_tauro_usd,
                    precio_cliente_final_ars,
                ),
            )
            return dict(cur.fetchone())


def listar_solicitudes_cliente(cliente_id: str, limite: int = 100) -> list[dict]:
    """Solicitudes de guía de un cliente, últimas primero."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM solicitudes_guia
                WHERE cliente_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (cliente_id.strip().upper(), limite),
            )
            return [dict(r) for r in cur.fetchall()]


def listar_solicitudes_admin(estado: str = "", limite: int = 300) -> list[dict]:
    """Solicitudes para la bandeja operativa del admin."""
    estado = (estado or "").strip().upper()
    params: list = []
    where = ""
    if estado:
        where = "WHERE s.estado = %s"
        params.append(estado)
    params.append(limite)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.*, c.nombre AS cliente_nombre, c.email AS cliente_email
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                {where}
                ORDER BY
                    CASE s.estado
                        WHEN 'SOLICITADO' THEN 1
                        WHEN 'EN_PROCESO' THEN 2
                        WHEN 'GUIA_LISTA' THEN 3
                        WHEN 'DESPACHADO' THEN 4
                        ELSE 5
                    END,
                    s.created_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]


def actualizar_solicitud_guia(
    solicitud_id: int,
    *,
    estado: str,
    tracking: str = "",
    guia_url: str = "",
) -> None:
    """Actualiza estado operativo, tracking y URL/documento de guía."""
    estado = (estado or "").strip().upper()
    if estado not in ESTADOS_SOLICITUD:
        raise ValueError(f"Estado inválido: {estado}")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitudes_guia
                SET estado=%s, tracking=%s, guia_url=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (estado, _clean(tracking), _clean(guia_url), solicitud_id),
            )


def contar_solicitudes_pendientes() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM solicitudes_guia
                WHERE estado IN ('SOLICITADO', 'EN_PROCESO')
                """
            )
            row = cur.fetchone()
    return int(row["n"] if row else 0)
