# ============================================================
# Backup del cliente en Excel — SUS datos, para SU PC.
# ============================================================
# Pedido de Leandro (28/07): cada cliente puede bajarse desde el portal
# un .xlsx con sus envíos, su cuenta corriente y su catálogo, para
# controlarlo en su propia planilla. No confundir con /admin/backup.json,
# que es la copia de TODA la plataforma para recuperación de desastres.
#
# Sólo datos del cliente autenticado: nada de otros clientes, nada de
# secretos, nada de tokens.
# ============================================================
from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Identidad de marca en el archivo: violeta TAURO, no colores default.
_VIOLETA = "5B3AD4"
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill("solid", fgColor=_VIOLETA)


def _hoja(wb: Workbook, titulo: str, columnas: list[str], filas: list[list]) -> None:
    ws = wb.create_sheet(titulo)
    ws.append(columnas)
    for c in range(1, len(columnas) + 1):
        cel = ws.cell(row=1, column=c)
        cel.font = _HEADER_FONT
        cel.fill = _HEADER_FILL
        cel.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"

    for fila in filas:
        ws.append(fila)

    # Ancho por contenido, con tope: una observación de 300 caracteres no
    # puede dejar la columna ilegible.
    for c in range(1, len(columnas) + 1):
        mejor = len(str(columnas[c - 1]))
        for fila in filas[:200]:
            v = fila[c - 1] if c - 1 < len(fila) else ""
            mejor = max(mejor, len(str(v or "")))
        ws.column_dimensions[get_column_letter(c)].width = min(mejor + 3, 42)

    ws.auto_filter.ref = ws.dimensions


def generar_excel_cliente(cliente_id: str) -> bytes:
    """El archivo completo, en memoria. Nunca toca disco."""
    from servicios.catalogo import get_productos
    from servicios.cuenta_corriente import (
        get_facturado_real, get_facturas_recientes, get_pagos, movimientos, saldo,
    )
    from servicios.solicitudes_guia import listar_solicitudes_cliente

    cliente_id = cliente_id.strip().upper()
    wb = Workbook()
    wb.remove(wb.active)   # la hoja default vacía

    # ── Envíos ──────────────────────────────────────────────
    solicitudes = listar_solicitudes_cliente(cliente_id, limite=5000)

    def _nac_o_int(s: dict) -> str:
        # Regla vigente: lo emitido por envia.com es nacional, el resto
        # internacional. Si mañana entra un courier nacional directo,
        # revisar también servicios/nacional.py.
        return "Nacional" if (s.get("courier") or "").upper() == "ENVIA" else "Internacional"

    filas_envios = []
    for s in solicitudes:
        creado = s.get("created_at")
        filas_envios.append([
            creado.strftime("%d/%m/%Y") if creado else "",
            s.get("estado") or "",
            _nac_o_int(s),
            (s.get("courier") or "FEDEX").upper(),
            s.get("tracking") or "",
            s.get("producto_alias") or "",
            s.get("cantidad") or 1,
            s.get("dest_nombre") or "",
            s.get("dest_ciudad") or "",
            s.get("destino_pais") or "",
            float(s.get("peso_kg") or 0),
            float(s.get("valor_declarado_usd") or 0),
            float(s.get("precio_tauro_ars") or 0),
            float(s.get("precio_tauro_usd") or 0),
            s.get("observaciones") or "",
        ])
    _hoja(wb, "Envíos", [
        "Fecha", "Estado", "Tipo", "Courier", "Tracking", "Producto", "Cajas",
        "Destinatario", "Ciudad", "País", "Peso (kg)", "Valor declarado (USD)",
        "Costo (ARS)", "Costo (USD)", "Observaciones",
    ], filas_envios)

    # ── Cuenta corriente ────────────────────────────────────
    facturado = get_facturado_real(cliente_id)
    s = saldo(cliente_id, total_facturado_ars=facturado)
    movs = movimientos(cliente_id, get_facturas_recientes(cliente_id, limite=5000))
    filas_cc = [[m["fecha"], "Pago" if m["tipo"] == "PAGO" else "Factura",
                 m["concepto"], m["monto_ars"]] for m in movs]
    filas_cc.append([])
    filas_cc.append(["", "", "TOTAL FACTURADO", s["facturado_ars"]])
    filas_cc.append(["", "", "TOTAL PAGADO", -s["pagado_ars"]])
    filas_cc.append(["", "", "SALDO PENDIENTE", s["saldo_pendiente_ars"]])
    _hoja(wb, "Cuenta corriente",
          ["Fecha", "Tipo", "Concepto", "Monto (ARS)"], filas_cc)

    # ── Catálogo ────────────────────────────────────────────
    productos = get_productos(cliente_id, solo_activos=False)
    filas_prod = [[
        p.alias_interno, p.nombre_invoice, p.hs_code,
        p.largo_cm, p.ancho_cm, p.alto_cm, p.peso_kg,
        p.valor_usd_default, "Aprobado" if p.activo else "En revisión",
    ] for p in productos]
    _hoja(wb, "Mis productos", [
        "SKU", "Descripción invoice", "HS code", "Largo (cm)", "Ancho (cm)",
        "Alto (cm)", "Peso (kg)", "Valor (USD)", "Estado",
    ], filas_prod)

    # ── Portada mínima ──────────────────────────────────────
    ws = wb.create_sheet("Resumen", 0)
    ws["A1"] = f"TAURO Solutions — backup de {cliente_id}"
    ws["A1"].font = Font(bold=True, size=14, color=_VIOLETA)
    ws["A2"] = f"Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    ws["A4"] = "Envíos"
    ws["B4"] = len(filas_envios)
    ws["A5"] = "Saldo pendiente (ARS)"
    ws["B5"] = s["saldo_pendiente_ars"]
    ws["A6"] = "Productos en catálogo"
    ws["B6"] = len(filas_prod)
    ws.column_dimensions["A"].width = 26

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
