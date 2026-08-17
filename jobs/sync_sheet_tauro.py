# ============================================================
# Espejo automático en el Google Sheet TAURO 2026
# ============================================================
# Pedido de Leandro (28/07): que la info de la plataforma "se cargue
# automáticamente en un sheet como el que hoy tenemos de TAURO 2026".
#
# REGLA DE ORO: este job escribe SOLO en la pestaña "PLATAFORMA", que es
# 100% propiedad del sistema y se reescribe entera en cada corrida. Las
# pestañas que Leandro maneja a mano NO SE TOCAN NUNCA — pisarle una
# columna con filtro activo ya desalineó datos una vez (está documentado).
#
# Se enciende solo cuando existen las dos variables en Railway:
#   GOOGLE_CREDENTIALS_JSON  (el JSON del service account)
#   TAURO_SHEET_ID           (opcional: default el TAURO 2026 conocido)
# Además hay que COMPARTIR el sheet con el mail del service account,
# permiso Editor — sin eso Google rechaza la escritura.
# ============================================================
from __future__ import annotations

import os
from datetime import datetime

from servicios.couriers_urls import ambito_envio

# El de "TAURO 2026" (documentado en CONTEXTO_PROYECTO.md / memoria).
SHEET_ID_DEFAULT = "1-c83aUq5LOUM5RkFrcaZaPhPDz3mC3Mf1blecJcrPGg"
PESTANA = "PLATAFORMA"

ENCABEZADOS = [
    "Fecha", "Cliente", "Estado", "Tipo", "Courier", "Tracking", "Producto",
    "Cajas", "Destinatario", "Ciudad", "País", "Peso (kg)",
    "Valor declarado (USD)", "Precio cliente (ARS)", "Precio cliente (USD)",
]


def configurado() -> bool:
    return bool(os.getenv("GOOGLE_CREDENTIALS_JSON"))


def sincronizar() -> None:
    """
    Reescribe la pestaña PLATAFORMA con todas las solicitudes de todos los
    clientes. Reescritura completa a propósito: es idempotente, se banca
    correcciones retroactivas (un tracking editado en el admin aparece
    corregido en el sheet) y no hay que razonar sobre deltas.
    """
    if not configurado():
        return   # sin credenciales no hay nada que hacer, y no es un error

    from core.database import get_conn
    from core.sheets_client import get_cliente_sheets

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT created_at, cliente_id, estado, courier, tracking,
                       ambito, remitente_pais,
                       producto_alias, cantidad, dest_nombre, dest_ciudad,
                       destino_pais, peso_kg, valor_declarado_usd,
                       precio_tauro_ars, precio_tauro_usd
                FROM solicitudes_guia
                ORDER BY created_at DESC
                LIMIT 5000
            """)
            filas_db = cur.fetchall()

    filas = [ENCABEZADOS]
    for s in filas_db:
        courier = (s["courier"] or "FEDEX").upper()
        filas.append([
            s["created_at"].strftime("%d/%m/%Y") if s["created_at"] else "",
            s["cliente_id"] or "",
            s["estado"] or "",
            {
                "nacional": "Nacional",
                "internacional": "Internacional",
            }.get(ambito_envio(dict(s)), "Sin clasificar"),
            courier,
            s["tracking"] or "",
            s["producto_alias"] or "",
            int(s["cantidad"] or 1),
            s["dest_nombre"] or "",
            s["dest_ciudad"] or "",
            s["destino_pais"] or "",
            float(s["peso_kg"] or 0),
            float(s["valor_declarado_usd"] or 0),
            float(s["precio_tauro_ars"] or 0),
            float(s["precio_tauro_usd"] or 0),
        ])

    sheet_id = os.getenv("TAURO_SHEET_ID", SHEET_ID_DEFAULT)
    doc = get_cliente_sheets().open_by_key(sheet_id)

    try:
        hoja = doc.worksheet(PESTANA)
    except Exception:
        hoja = doc.add_worksheet(title=PESTANA, rows=max(len(filas) + 50, 200),
                                 cols=len(ENCABEZADOS) + 2)
        print(f"[sync_sheet] pestaña {PESTANA} creada en el sheet")

    # clear + update en la MISMA pestaña. batch_update sería más elegante,
    # pero clear/update son dos llamadas y esto corre pocas veces al día:
    # la simpleza le gana a la sofisticación acá.
    hoja.clear()
    hoja.update(filas, "A1")
    hoja.format("A1:O1", {"textFormat": {"bold": True}})

    print(f"[sync_sheet] {len(filas) - 1} envío(s) espejados en '{PESTANA}' "
          f"({datetime.now().strftime('%H:%M')})")


def sincronizar_seguro() -> None:
    """Para el scheduler: nunca deja que un fallo del sheet tire nada."""
    try:
        sincronizar()
    except Exception as e:
        # El sheet es un espejo, no la fuente de verdad: si Google rebota
        # (cuota, permisos, red), la plataforma sigue y se reintenta en la
        # próxima corrida.
        print(f"[sync_sheet] sincronización falló (se reintenta sola): {e}")
