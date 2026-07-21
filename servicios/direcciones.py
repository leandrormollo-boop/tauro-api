from __future__ import annotations

from typing import Optional

from core.database import get_conn


TIPO_REMITENTE = "REMITENTE"
TIPO_DESTINATARIO = "DESTINATARIO"
TIPOS_VALIDOS = {TIPO_REMITENTE, TIPO_DESTINATARIO}


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def _tipo(tipo: str) -> str:
    tipo = (tipo or "").strip().upper()
    if tipo not in TIPOS_VALIDOS:
        raise ValueError("Tipo de dirección inválido.")
    return tipo


def _cliente(cliente_id: str) -> str:
    return (cliente_id or "").strip().upper()


def _normalizar_row(row: dict) -> dict:
    data = dict(row)
    data["label"] = data.get("alias") or data.get("nombre") or f"Dirección #{data.get('id')}"
    return data


def _cliente_como_remitente(cliente_id: str) -> Optional[dict]:
    cliente_id = _cliente(cliente_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cliente_id, nombre, cuit, email, telefono, direccion, ciudad, cp, pais
                FROM clientes
                WHERE cliente_id = %s
                LIMIT 1
                """,
                (cliente_id,),
            )
            row = cur.fetchone()
    if not row or not row.get("direccion") or not row.get("cp") or not row.get("ciudad"):
        return None
    return {
        "id": None,
        "tipo": TIPO_REMITENTE,
        "alias": "Remitente principal",
        "label": "Remitente principal",
        "nombre": row.get("nombre") or cliente_id,
        "documento": row.get("cuit"),
        "email": row.get("email"),
        "telefono": row.get("telefono"),
        "direccion": row.get("direccion"),
        "ciudad": row.get("ciudad"),
        "estado": "",
        "cp": row.get("cp"),
        "pais": row.get("pais") or "AR",
        "predeterminada": True,
        "notas": "Datos cargados en el perfil del cliente.",
        "virtual": True,
    }


def listar_direcciones(cliente_id: str, tipo: Optional[str] = None) -> list[dict]:
    cliente_id = _cliente(cliente_id)
    params: list = [cliente_id]
    where_tipo = ""
    if tipo:
        where_tipo = "AND tipo = %s"
        params.append(_tipo(tipo))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM direcciones
                WHERE cliente_id = %s {where_tipo}
                ORDER BY predeterminada DESC, updated_at DESC, id DESC
                """,
                params,
            )
            rows = cur.fetchall()
    return [_normalizar_row(r) for r in rows]


def obtener_direccion(cliente_id: str, direccion_id: int) -> Optional[dict]:
    cliente_id = _cliente(cliente_id)
    if not direccion_id:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM direcciones
                WHERE cliente_id = %s AND id = %s
                LIMIT 1
                """,
                (cliente_id, direccion_id),
            )
            row = cur.fetchone()
    return _normalizar_row(row) if row else None


def obtener_remitente_para_envio(cliente_id: str, remitente_id: Optional[int] = None) -> Optional[dict]:
    if remitente_id:
        row = obtener_direccion(cliente_id, remitente_id)
        if row and row["tipo"] == TIPO_REMITENTE:
            return row

    remitentes = listar_direcciones(cliente_id, TIPO_REMITENTE)
    if remitentes:
        return remitentes[0]
    return _cliente_como_remitente(cliente_id)


def crear_direccion(
    *,
    cliente_id: str,
    tipo: str,
    alias: str = "",
    nombre: str,
    documento: str = "",
    email: str = "",
    telefono: str = "",
    direccion: str,
    ciudad: str,
    estado: str = "",
    cp: str,
    pais: str = "AR",
    predeterminada: bool = False,
    notas: str = "",
) -> dict:
    cliente_id = _cliente(cliente_id)
    tipo = _tipo(tipo)
    pais = (pais or "AR").strip().upper()

    with get_conn() as conn:
        with conn.cursor() as cur:
            if predeterminada:
                cur.execute(
                    """
                    UPDATE direcciones
                    SET predeterminada = FALSE, updated_at = NOW()
                    WHERE cliente_id = %s AND tipo = %s
                    """,
                    (cliente_id, tipo),
                )

            cur.execute(
                """
                INSERT INTO direcciones (
                    cliente_id, tipo, alias, nombre, documento, email, telefono,
                    direccion, ciudad, estado, cp, pais, predeterminada, notas
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    cliente_id,
                    tipo,
                    _clean(alias),
                    nombre.strip(),
                    _clean(documento),
                    _clean(email),
                    _clean(telefono),
                    direccion.strip(),
                    ciudad.strip(),
                    _clean(estado),
                    cp.strip(),
                    pais,
                    bool(predeterminada),
                    _clean(notas),
                ),
            )
            return _normalizar_row(cur.fetchone())


def actualizar_direccion(
    direccion_id: int,
    *,
    cliente_id: str,
    tipo: str,
    alias: str = "",
    nombre: str,
    documento: str = "",
    email: str = "",
    telefono: str = "",
    direccion: str,
    ciudad: str,
    estado: str = "",
    cp: str,
    pais: str = "AR",
    predeterminada: bool = False,
    notas: str = "",
) -> Optional[dict]:
    """Actualiza una dirección del cliente. El WHERE por cliente_id garantiza
    que nadie edite direcciones ajenas. Devuelve la fila o None si no existe."""
    cliente_id = _cliente(cliente_id)
    tipo = _tipo(tipo)
    pais = (pais or "AR").strip().upper()

    with get_conn() as conn:
        with conn.cursor() as cur:
            if predeterminada:
                cur.execute(
                    """
                    UPDATE direcciones
                    SET predeterminada = FALSE, updated_at = NOW()
                    WHERE cliente_id = %s AND tipo = %s AND id <> %s
                    """,
                    (cliente_id, tipo, direccion_id),
                )
            cur.execute(
                """
                UPDATE direcciones
                SET tipo=%s, alias=%s, nombre=%s, documento=%s, email=%s,
                    telefono=%s, direccion=%s, ciudad=%s, estado=%s, cp=%s,
                    pais=%s, predeterminada=%s, notas=%s, updated_at=NOW()
                WHERE id = %s AND cliente_id = %s
                RETURNING *
                """,
                (
                    tipo, _clean(alias), nombre.strip(), _clean(documento),
                    _clean(email), _clean(telefono), direccion.strip(),
                    ciudad.strip(), _clean(estado), cp.strip(), pais,
                    bool(predeterminada), _clean(notas),
                    direccion_id, cliente_id,
                ),
            )
            row = cur.fetchone()
    return _normalizar_row(row) if row else None


def eliminar_direccion(cliente_id: str, direccion_id: int) -> bool:
    """Borra una dirección del cliente (solo las propias). True si borró algo."""
    cliente_id = _cliente(cliente_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM direcciones WHERE id = %s AND cliente_id = %s",
                (direccion_id, cliente_id),
            )
            return cur.rowcount > 0


def contar_direcciones(cliente_id: str) -> dict:
    cliente_id = _cliente(cliente_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tipo, COUNT(*) AS total
                FROM direcciones
                WHERE cliente_id = %s
                GROUP BY tipo
                """,
                (cliente_id,),
            )
            rows = cur.fetchall()
    data = {TIPO_REMITENTE: 0, TIPO_DESTINATARIO: 0}
    for row in rows:
        data[row["tipo"]] = int(row["total"])
    if data[TIPO_REMITENTE] == 0 and _cliente_como_remitente(cliente_id):
        data[TIPO_REMITENTE] = 1
    return data
