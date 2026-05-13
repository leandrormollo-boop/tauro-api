# ============================================================
# Servicio de cuenta corriente — PostgreSQL
# ============================================================
# Lee envíos/facturas y pagos directamente de la base de datos.
# El admin carga los datos desde el panel.
# ============================================================

from datetime import datetime
from typing import List, Dict, Any

from core.database import get_conn


def _parse_monto(valor) -> float:
    if valor is None or valor == "":
        return 0.0
    try:
        return float(valor)
    except (ValueError, TypeError):
        return 0.0


def get_facturado_real(cliente: str) -> float:
    """
    Suma el monto de envíos activos del cliente (estado != CANCELADO / NC).
    """
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(monto_ars), 0) AS total
                FROM envios
                WHERE cliente_id = %s
                  AND estado NOT IN ('CANCELADO', 'NC')
                """,
                (cliente,),
            )
            row = cur.fetchone()
    return round(float(row["total"]) if row else 0.0, 2)


def get_facturas_recientes(cliente: str, limite: int = 10) -> List[Dict[str, Any]]:
    """Envíos del cliente ordenados por fecha descendente."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fecha, nro_fc, monto_ars
                FROM envios
                WHERE cliente_id = %s
                  AND estado NOT IN ('CANCELADO', 'NC')
                  AND monto_ars > 0
                ORDER BY fecha DESC
                LIMIT %s
                """,
                (cliente, limite),
            )
            rows = cur.fetchall()

    facturas = []
    for r in rows:
        facturas.append({
            "fecha": r["fecha"].strftime("%d/%m/%Y") if r["fecha"] else "",
            "nro_fc": str(r["nro_fc"] or ""),
            "monto_ars": float(r["monto_ars"] or 0),
        })
    return facturas


def get_pagos(cliente: str) -> List[Dict[str, Any]]:
    """Lista de pagos recibidos del cliente, ordenados por fecha asc."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fecha, monto_ars, metodo, referencia, nota
                FROM pagos
                WHERE cliente_id = %s
                ORDER BY fecha ASC
                """,
                (cliente,),
            )
            rows = cur.fetchall()

    pagos = []
    for r in rows:
        pagos.append({
            "fecha": r["fecha"].strftime("%d/%m/%Y") if r["fecha"] else "",
            "monto_ars": float(r["monto_ars"] or 0),
            "metodo": str(r["metodo"] or ""),
            "referencia": str(r["referencia"] or ""),
            "nota": str(r["nota"] or ""),
        })
    return pagos


def total_pagado(cliente: str) -> float:
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(monto_ars), 0) AS total FROM pagos WHERE cliente_id = %s",
                (cliente,),
            )
            row = cur.fetchone()
    return round(float(row["total"]) if row else 0.0, 2)


def saldo(cliente: str, total_facturado_ars: float) -> Dict[str, float]:
    pagado = total_pagado(cliente)
    return {
        "facturado_ars": round(total_facturado_ars, 2),
        "pagado_ars": round(pagado, 2),
        "saldo_pendiente_ars": round(total_facturado_ars - pagado, 2),
    }


def movimientos(cliente: str, facturas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Timeline mezclado: facturas + pagos ordenados por fecha desc."""
    items = []
    for fc in facturas:
        items.append({
            "fecha": fc.get("fecha", ""),
            "tipo": "FC",
            "concepto": fc.get("nro_fc", ""),
            "monto_ars": float(fc.get("monto_ars", 0)),
        })
    for p in get_pagos(cliente):
        items.append({
            "fecha": p["fecha"],
            "tipo": "PAGO",
            "concepto": f"{p['metodo']} {p['referencia']}".strip(),
            "monto_ars": -p["monto_ars"],
        })

    def _parse_fecha(s: str):
        try:
            return datetime.strptime(s, "%d/%m/%Y")
        except (ValueError, TypeError):
            return datetime.min

    items.sort(key=lambda x: _parse_fecha(x["fecha"]), reverse=True)
    return items


# ── Funciones de escritura (para el admin) ───────────────────

def registrar_pago(
    cliente_id: str,
    fecha: str,        # "YYYY-MM-DD"
    monto_ars: float,
    metodo: str,
    referencia: str = "",
    nota: str = "",
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pagos (cliente_id, fecha, monto_ars, metodo, referencia, nota)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (cliente_id.upper(), fecha, monto_ars, metodo, referencia, nota),
            )


def registrar_envio(
    cliente_id: str,
    fecha: str,        # "YYYY-MM-DD"
    monto_ars: float,
    nro_fc: str = "",
    estado: str = "ACTIVO",
    descripcion: str = "",
    tracking: str = "",
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO envios
                    (cliente_id, fecha, nro_fc, monto_ars, estado, descripcion, tracking)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (cliente_id.upper(), fecha, nro_fc, monto_ars, estado.upper(), descripcion, tracking),
            )


def cancelar_envio(envio_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE envios SET estado = 'CANCELADO' WHERE id = %s", (envio_id,))


def get_envios_cliente(cliente: str) -> List[Dict[str, Any]]:
    """Todos los envíos del cliente — para el admin."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM envios WHERE cliente_id = %s ORDER BY fecha DESC",
                (cliente,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]
