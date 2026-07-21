# ============================================================
# Servicio de solicitudes de guía — PostgreSQL
# ============================================================

import json
from typing import Optional

import psycopg2

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


def _sin_label(row: dict) -> dict:
    """Reemplaza el PDF (bytea) por un booleano en los listados, para no cargar
    los bytes del label en cada fila de la tabla."""
    row["tiene_label"] = bool(row.get("label_pdf"))
    row.pop("label_pdf", None)
    return row


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
    bultos: Optional[list] = None,
) -> dict:
    """Crea una solicitud de guía pendiente para gestión operativa.

    bultos (multi-bulto): lista [{producto_alias, cantidad, peso_kg, largo_cm,
    ancho_cm, alto_cm, valor_unitario_usd, hs_code, descripcion_en}, ...].
    Los campos legacy (producto_alias, cantidad, peso/dims/valor) guardan el
    primer bulto + totales para que listados y admin sigan andando."""
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
                    precio_tauro_usd, precio_cliente_final_ars, bultos
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s
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
                    json.dumps(bultos) if bultos else None,
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
            return [_sin_label(dict(r)) for r in cur.fetchall()]


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
            return [_sin_label(dict(r)) for r in cur.fetchall()]


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


def obtener_solicitud_de_cliente(solicitud_id: int, cliente_id: str) -> Optional[dict]:
    """Una solicitud del cliente logueado (para la página de detalle del
    portal). Chequea pertenencia y no carga los bytes del label."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM solicitudes_guia
                WHERE id = %s AND cliente_id = %s
                """,
                (solicitud_id, cliente_id.strip().upper()),
            )
            row = cur.fetchone()
    return _sin_label(dict(row)) if row else None


# ── Emisión de guía real (FedEx Ship API) ───────────────────

def obtener_solicitud(solicitud_id: int) -> Optional[dict]:
    """Una solicitud con los datos del cliente (para el remitente por defecto)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*,
                       c.nombre AS cliente_nombre, c.telefono AS cliente_telefono,
                       c.direccion AS cliente_direccion, c.ciudad AS cliente_ciudad,
                       c.cp AS cliente_cp, c.pais AS cliente_pais
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                WHERE s.id = %s
                """,
                (solicitud_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def guardar_guia_generada(solicitud_id: int, tracking: str, label_pdf: Optional[bytes],
                          courier: str = "FEDEX") -> None:
    """Persiste la guía emitida: tracking, label PDF y estado GUIA_LISTA."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitudes_guia
                SET estado='GUIA_LISTA', tracking=%s, label_pdf=%s, courier=%s,
                    guia_generada_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (tracking, psycopg2.Binary(label_pdf) if label_pdf else None, courier, solicitud_id),
            )


def obtener_label_pdf(solicitud_id: int, cliente_id: Optional[str] = None) -> Optional[bytes]:
    """
    Devuelve los bytes del label PDF de una solicitud, o None si no existe.
    Si se pasa cliente_id, verifica que la solicitud le pertenezca (para el portal).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if cliente_id:
                cur.execute(
                    "SELECT label_pdf FROM solicitudes_guia WHERE id=%s AND cliente_id=%s",
                    (solicitud_id, cliente_id.strip().upper()),
                )
            else:
                cur.execute(
                    "SELECT label_pdf FROM solicitudes_guia WHERE id=%s",
                    (solicitud_id,),
                )
            row = cur.fetchone()
    if not row or not row["label_pdf"]:
        return None
    return bytes(row["label_pdf"])


def generar_guia_fedex(solicitud_id: int) -> dict:
    """
    Emite la guía real en FedEx para una solicitud y guarda tracking + label PDF.
    Devuelve {ok, tracking, tiene_label} o {ok: False, error}.
    """
    sol = obtener_solicitud(solicitud_id)
    if not sol:
        return {"ok": False, "error": "Solicitud no encontrada."}
    if sol.get("estado") == "CANCELADO":
        return {"ok": False, "error": "La solicitud está cancelada."}
    if sol.get("tracking") and sol.get("label_pdf"):
        return {"ok": False, "error": "Esta solicitud ya tiene una guía generada."}

    # Bultos de la solicitud (multi-bulto). Si no hay, cae al camino legacy
    # de un solo bulto con los campos históricos.
    bultos = sol.get("bultos") or []
    if isinstance(bultos, str):
        try:
            import json as _json
            bultos = _json.loads(bultos)
        except Exception:
            bultos = []

    # Producto del catálogo → HS code y descripción en inglés para la aduana.
    hs_code, descripcion_en = "", "Merchandise"
    try:
        from servicios.catalogo import get_producto
        prod = get_producto(sol["cliente_id"], sol.get("producto_alias") or "")
        if prod:
            hs_code = prod.hs_code or ""
            descripcion_en = prod.nombre_invoice or "Merchandise"
    except Exception as e:
        print(f"[guia] no se pudo leer el producto: {e}")

    from servicios.rutas import pais_a_iso2

    shipper = {
        "nombre": sol.get("remitente_nombre") or sol.get("cliente_nombre") or sol["cliente_id"],
        "empresa": sol.get("cliente_nombre") or "",
        "telefono": sol.get("remitente_telefono") or sol.get("cliente_telefono") or "",
        "calle": sol.get("remitente_direccion") or sol.get("cliente_direccion") or "",
        "ciudad": sol.get("remitente_ciudad") or sol.get("cliente_ciudad") or "Buenos Aires",
        "estado": sol.get("remitente_estado") or "",
        "zip": sol.get("remitente_zip") or sol.get("cliente_cp") or "",
        "pais": pais_a_iso2(sol.get("remitente_pais") or sol.get("cliente_pais") or "AR"),
    }
    recipient = {
        "nombre": sol.get("dest_nombre") or "",
        "telefono": sol.get("dest_telefono") or "",
        "calle": sol.get("dest_direccion") or "",
        "ciudad": sol.get("dest_ciudad") or "",
        "estado": sol.get("dest_estado") or "",
        "zip": sol.get("dest_zip") or "",
        "pais": pais_a_iso2(sol.get("destino_pais") or "US"),
    }
    datos_envio = {
        "shipper": shipper,
        "recipient": recipient,
    }
    if bultos:
        # Multi-bulto: cada caja del envío como pieza propia, con su label.
        datos_envio["bultos"] = [
            {
                "peso_kg": b.get("peso_kg") or 0.5,
                "largo": b.get("largo_cm") or 30,
                "ancho": b.get("ancho_cm") or 20,
                "alto": b.get("alto_cm") or 10,
                "valor_unitario_usd": b.get("valor_unitario_usd") or 100,
                "unidades": max(int(b.get("cantidad") or 1), 1),
                "hs_code": b.get("hs_code") or "",
                "descripcion_en": b.get("descripcion_en") or "Merchandise",
                "pais_origen": "AR",
            }
            for b in bultos
        ]
    else:
        # OJO: valor_declarado_usd viene TOTALIZADO (unitario × cantidad) desde
        # el portal. create_shipment vuelve a multiplicar unitario × cantidad,
        # así que acá se pasa el UNITARIO real para no declarar de más en aduana.
        cantidad_sol = max(int(sol.get("cantidad") or 1), 1)
        valor_total_sol = float(sol.get("valor_declarado_usd") or 100)
        datos_envio["package"] = {
            "peso_kg": sol.get("peso_kg") or 0.5,
            "largo": sol.get("largo_cm") or 30,
            "ancho": sol.get("ancho_cm") or 20,
            "alto": sol.get("alto_cm") or 10,
        }
        datos_envio["commodity"] = {
            "descripcion": descripcion_en,
            "hs_code": hs_code,
            "cantidad": cantidad_sol,
            "valor_unitario_usd": round(valor_total_sol / cantidad_sol, 2),
            "pais_origen": "AR",
        }

    from core.fedex_client import FedExClient
    resultado = FedExClient().create_shipment(datos_envio)

    if not resultado.get("encontrado"):
        return {"ok": False, "error": resultado.get("error", "FedEx no emitió la guía.")}

    guardar_guia_generada(
        solicitud_id,
        resultado["tracking"],
        resultado.get("label_pdf"),
        courier="FEDEX",
    )
    return {
        "ok": True,
        "tracking": resultado["tracking"],
        "tiene_label": bool(resultado.get("label_pdf")),
    }
