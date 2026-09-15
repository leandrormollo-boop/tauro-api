"""Inicio de visualización del control, sin borrar ni reiniciar la cuenta real.

El saldo anterior usa los estados actuales de los movimientos registrados.
No reconstruye un cierre histórico: una aprobación tardía de un pago puede
recalcularlo. Facturas y NC documentales no se suman como cargos adicionales.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from core.database import get_conn


_CERO = Decimal("0.00")
_INICIO_WAIMAO = date(2026, 9, 1)


def inicio_cuenta_cliente(cliente: str) -> date | None:
    """Política de presentación del piloto; nunca cambia permisos ni deuda."""
    return _INICIO_WAIMAO if str(cliente or "").strip().upper() == "WAIMAO" else None


_PERIODO_SQL = """
WITH asientos AS (
    SELECT e.fecha,
           CASE WHEN e.ambito IN ('NACIONAL','INTERNACIONAL')
                THEN e.ambito ELSE 'SIN_CLASIFICAR' END AS ambito,
           'ENVIO'::text AS clase, e.monto_ars AS importe
    FROM envios e
    WHERE e.cliente_id=%s AND e.estado NOT IN ('CANCELADO','NC')
      AND (e.monto_ars>0 OR e.monto_ars IS NULL)
    UNION ALL
    SELECT (a.aplicado_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date,
           CASE WHEN e.ambito IN ('NACIONAL','INTERNACIONAL')
                THEN e.ambito ELSE 'SIN_CLASIFICAR' END,
           a.tipo, ABS(a.monto_ars)
    FROM ajustes_cliente a
    JOIN envios e ON e.solicitud_id=a.solicitud_id
    WHERE e.cliente_id=%s AND e.estado='ACTIVO' AND a.estado='APLICADO'
    UNION ALL
    SELECT p.fecha, 'CONSOLIDADO', 'PAGO', p.monto_ars
    FROM pagos p
    WHERE p.cliente_id=%s AND COALESCE(p.estado,'APROBADO')='APROBADO'
), periodos AS (
    SELECT *, CASE WHEN fecha IS NULL OR fecha<%s THEN 'ANTERIOR' ELSE 'DESDE' END AS periodo
    FROM asientos
)
SELECT periodo, ambito, clase, SUM(importe) AS importe,
       SUM(ROUND(importe,2)) AS importe_presentado,
       COUNT(*) FILTER (WHERE fecha IS NULL) AS sin_fecha_cantidad,
       COUNT(*) FILTER (WHERE importe IS NULL) AS sin_importe_cantidad
FROM periodos
GROUP BY periodo, ambito, clase
ORDER BY periodo, ambito, clase
LIMIT 21
"""


def _importe_exacto(valor) -> Decimal:
    try:
        importe = Decimal(str(valor))
        if not importe.is_finite() or importe < 0:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Hay movimientos con un importe no válido para separar el período.") from None
    return importe


def _resumen_grupos(grupos: dict[tuple[str, str], Decimal]) -> dict:
    # El resumen vigente redondea cada grupo ámbito/tipo, no cada ajuste
    # NUMERIC(18,4) por separado. Conservar ese criterio para no cambiar saldo.
    cargos = sum((m.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for (_, clase), m in grupos.items()
                  if clase in {"ENVIO", "DEBITO"}), _CERO)
    creditos = sum((m.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for (_, clase), m in grupos.items()
                    if clase == "CREDITO"), _CERO)
    pagos = sum((m.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for (_, clase), m in grupos.items()
                 if clase == "PAGO"), _CERO)
    return {"cargos": cargos, "creditos": creditos, "pagos": pagos,
            "neto": cargos - creditos - pagos}


def _presentar_periodo(filas: list[dict], inicio: date) -> dict:
    if len(filas)>20:
        raise ValueError("Hay movimientos que no se pueden clasificar en el período.")
    grupos = {"ANTERIOR": {}, "DESDE": {}, "TOTAL": {}}
    sin_fecha = 0
    for fila in filas:
        if int(fila.get("sin_importe_cantidad") or 0):
            raise ValueError("Hay movimientos sin importe registrado. No se puede calcular el saldo anterior.")
        periodo, ambito, clase = fila["periodo"], fila["ambito"], fila["clase"]
        if periodo not in {"ANTERIOR", "DESDE"} or clase not in {"ENVIO", "DEBITO", "CREDITO", "PAGO"}:
            raise ValueError("Hay movimientos que no se pueden clasificar en el período.")
        clave = (ambito, clase)
        importe = _importe_exacto(fila["importe"])
        # Antes/después suman los mismos centavos presentados en lista/Excel.
        # El total conserva la precisión y regla del resumen vigente. Cualquier
        # diferencia entre ambos criterios queda explícita en redondeo_ars.
        presentado = _importe_exacto(fila.get("importe_presentado", importe.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )))
        grupos[periodo][clave] = grupos[periodo].get(clave, _CERO) + presentado
        grupos["TOTAL"][clave] = grupos["TOTAL"].get(clave, _CERO) + importe
        sin_fecha += int(fila.get("sin_fecha_cantidad") or 0)
    anterior, desde, total = (_resumen_grupos(grupos[k]) for k in ("ANTERIOR", "DESDE", "TOTAL"))
    redondeo = total["neto"] - anterior["neto"] - desde["neto"]
    return {
        "saldo_anterior_ars": anterior["neto"],
        "cargos_desde_ars": desde["cargos"],
        "creditos_desde_ars": desde["creditos"],
        "pagos_desde_ars": desde["pagos"],
        "neto_desde_ars": desde["neto"],
        "saldo_total_ars": total["neto"],
        "redondeo_ars": redondeo,
        "sin_fecha_cantidad": sin_fecha,
        "fecha_inicio": inicio.isoformat(),
        "fecha_inicio_visible": inicio.strftime("%d/%m/%Y"),
        "criterio": "Según movimientos registrados y sus estados actuales; no es un cierre histórico auditado.",
        "advertencia_sin_fecha": (
            f"Hay {sin_fecha} movimientos sin fecha: se incluyen en el saldo anterior para conservar el saldo total."
            if sin_fecha else ""
        ),
    }


def obtener_periodo_cuenta(cliente: str, inicio: date) -> dict:
    """Separa un saldo consolidado en una lectura agregada, propia y acotada.

    No filtra pagos por la fecha del envío que cancelan, ni incluye pendientes
    como haber. El límite de emisión y la selección de documentos se conservan
    en sus servicios actuales, independientemente de esta presentación.
    """
    cliente = str(cliente or "").strip().upper()
    if not cliente or len(cliente)>80 or type(inicio) is not date:
        raise ValueError("El cliente o la fecha de inicio no son válidos.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(_PERIODO_SQL, (cliente, cliente, cliente, inicio))
            filas = [dict(f) for f in cur.fetchall()]
    return _presentar_periodo(filas, inicio)
