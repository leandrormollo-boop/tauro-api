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
                SELECT fecha, nro_fc, monto_ars, descripcion
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
            # Los cargos automáticos de guías no tienen nro de factura: sin el
            # fallback a la descripción, el timeline mostraría filas mudas.
            "nro_fc": str(r["nro_fc"] or "") or str(r["descripcion"] or ""),
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


def get_resumen_clientes_bulk(solo_activos: bool = True) -> List[Dict[str, Any]]:
    """
    Devuelve facturado y pagado de TODOS los clientes en UNA sola query.
    Reemplaza el patrón N+1 de iterar y llamar get_facturado_real + total_pagado.

    Uso típico (admin_home):
        resumen = get_resumen_clientes_bulk(solo_activos=True)
        # cada item: {cliente_id, email, nombre, facturado, pagado, saldo}
    """
    where_activos = "WHERE c.activo = TRUE" if solo_activos else ""
    sql = f"""
        WITH fact AS (
            SELECT cliente_id, COALESCE(SUM(monto_ars), 0) AS facturado
            FROM envios
            WHERE estado NOT IN ('CANCELADO', 'NC')
            GROUP BY cliente_id
        ),
        pag AS (
            SELECT cliente_id, COALESCE(SUM(monto_ars), 0) AS pagado
            FROM pagos
            GROUP BY cliente_id
        )
        SELECT
            c.cliente_id,
            c.email,
            c.nombre,
            c.activo,
            COALESCE(fact.facturado, 0) AS facturado,
            COALESCE(pag.pagado, 0)    AS pagado
        FROM clientes c
        LEFT JOIN fact ON fact.cliente_id = c.cliente_id
        LEFT JOIN pag  ON pag.cliente_id  = c.cliente_id
        {where_activos}
        ORDER BY c.cliente_id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    resumen = []
    for r in rows:
        facturado = round(float(r["facturado"] or 0), 2)
        pagado = round(float(r["pagado"] or 0), 2)
        resumen.append({
            "cliente_id": r["cliente_id"],
            "email": r["email"] or "",
            "nombre": r["nombre"] or "",
            "activo": bool(r["activo"]),
            "facturado": facturado,
            "pagado": pagado,
            "saldo": round(facturado - pagado, 2),
        })
    return resumen


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


def cargar_guia_emitida(solicitud_id: int) -> bool:
    """
    Débito automático: al emitirse una guía, el cargo entra solo a la cuenta
    corriente del cliente por `precio_tauro_ars` (lo que TAURO le cobra, con
    su margen ya incluido — no confundir con `precio_cliente_final_ars`, que
    es lo que el cliente le cobra a SU comprador).

    Antes esto era doble carga manual: el admin emitía la guía y después
    tenía que acordarse de facturarla a mano. Si se olvidaba, el saldo del
    cliente mentía en silencio.

    Idempotente por el índice único sobre solicitud_id: reintentos, doble
    click o un reinicio a mitad de camino no pueden duplicar el cargo.
    Devuelve True si el cargo se insertó, False si ya existía o no se pudo.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cliente_id, precio_tauro_ars, tracking, courier,
                       producto_alias, destino_pais
                FROM solicitudes_guia WHERE id = %s
            """, (solicitud_id,))
            sol = cur.fetchone()

            if not sol:
                print(f"[cta_cte] solicitud {solicitud_id} no existe: sin cargo")
                return False
            monto = float(sol["precio_tauro_ars"] or 0)
            if monto <= 0:
                # Sin precio no se inventa un cargo: se avisa para que el
                # admin lo facture a mano, que es mejor que un débito en 0
                # que nadie revisaría jamás.
                print(f"[cta_cte] ATENCIÓN: la solicitud {solicitud_id} no tiene "
                      f"precio_tauro_ars — el cargo NO se generó, facturalo a mano")
                return False

            descripcion = (f"Guía {sol['tracking'] or 's/n'} · "
                           f"{sol['producto_alias'] or 'envío'} → {sol['destino_pais'] or ''} · "
                           f"{(sol['courier'] or 'FEDEX').upper()} · cargo automático")
            cur.execute("""
                INSERT INTO envios
                    (cliente_id, fecha, nro_fc, monto_ars, estado, descripcion,
                     tracking, solicitud_id)
                VALUES (%s, CURRENT_DATE, '', %s, 'ACTIVO', %s, %s, %s)
                ON CONFLICT (solicitud_id) WHERE solicitud_id IS NOT NULL
                DO NOTHING
                RETURNING id
            """, (sol["cliente_id"].upper(), monto, descripcion,
                  sol["tracking"] or "", solicitud_id))
            fila = cur.fetchone()

    if fila:
        print(f"[cta_cte] cargo automático: solicitud {solicitud_id} → "
              f"ARS {monto:,.0f} a {sol['cliente_id']}")
        return True
    print(f"[cta_cte] la solicitud {solicitud_id} ya tenía su cargo: no se duplica")
    return False


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
