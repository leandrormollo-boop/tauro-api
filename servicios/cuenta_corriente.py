# ============================================================
# Servicio de cuenta corriente
# ============================================================
# Calcula saldo y movimientos del cliente cruzando:
#   - Hoja PAGOS (pagos recibidos, propia del API)
#   - Hoja ENVIOS 2026 del sheet operativo TAURO 2026 (facturado)
#
# El sheet operativo de Tauro tiene un ID distinto al del API.
# Lo abrimos con la misma cuenta de servicio (credenciales.json),
# que ya tiene permiso de lectura sobre ambos.
# ============================================================

import os
from datetime import datetime
from typing import List, Dict, Any

import gspread
from google.oauth2.service_account import Credentials

from core.sheets_client import _abrir_sheet


# ID del sheet operativo TAURO 2026 (el que llena el equipo de Tauro)
TAURO_2026_SHEET_ID = "1-c83aUq5LOUM5RkFrcaZaPhPDz3mC3Mf1blecJcrPGg"

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _abrir_sheet_operativo():
    """Abre el sheet TAURO 2026 con la cuenta de servicio del API."""
    creds_path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credenciales.json")
    creds = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(TAURO_2026_SHEET_ID)


def _parse_monto(valor) -> float:
    """Parsea valores en formato ARS con $ / coma / punto. Devuelve 0.0 si no se puede."""
    if valor is None or valor == "":
        return 0.0
    s = str(valor).strip()
    if not s or s in ("-", "N/A", "n/a"):
        return 0.0
    # Sacar $ y espacios
    s = s.replace("$", "").replace(" ", "").replace("\xa0", "")
    # Si tiene coma decimal estilo ES (1.234,56) → quitar puntos, cambiar coma por punto
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # Asumimos coma como decimal
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def get_facturado_real(cliente: str) -> float:
    """
    Suma col K=FACTURADO de ENVIOS 2026 matcheando por col V=EMPRESA.
    Filtra filas con ESTADO (col S o W) que sea CANCELADO o contenga NC.
    Si el sheet operativo no es accesible, devuelve 0.0 y loguea el error.
    """
    cliente = cliente.strip().upper()
    try:
        sh = _abrir_sheet_operativo()
        hoja = sh.worksheet("ENVIOS 2026")
        # Lectura por filas en bruto — la hoja tiene cols hasta W
        valores = hoja.get_all_values()
    except Exception as e:
        print(f"[cuenta_corriente] No se pudo leer TAURO 2026: {e}")
        return 0.0

    if len(valores) < 2:
        return 0.0

    # Indices fijos según ESTRUCTURA documentada (0-indexed)
    IDX_FACTURADO = 10  # K
    IDX_EMPRESA = 21    # V
    IDX_ESTADO_S = 18   # S
    IDX_ESTADO_W = 22   # W

    total = 0.0
    for fila in valores[1:]:  # skip header
        if len(fila) <= IDX_EMPRESA:
            continue
        empresa = str(fila[IDX_EMPRESA]).strip().upper()
        if empresa != cliente:
            continue

        # Filtrar canceladas / NC
        estado_s = str(fila[IDX_ESTADO_S]).strip().upper() if len(fila) > IDX_ESTADO_S else ""
        estado_w = str(fila[IDX_ESTADO_W]).strip().upper() if len(fila) > IDX_ESTADO_W else ""
        if any(x in estado_s for x in ("CANCEL", "NC")) or any(x in estado_w for x in ("CANCEL", "NC")):
            continue

        total += _parse_monto(fila[IDX_FACTURADO])

    return round(total, 2)


def get_facturas_recientes(cliente: str, limite: int = 10) -> List[Dict[str, Any]]:
    """
    Devuelve facturas del cliente desde ENVIOS 2026, ordenadas por fecha desc.
    Formato: [{fecha, nro_fc, monto_ars}, ...]
    """
    cliente = cliente.strip().upper()
    try:
        sh = _abrir_sheet_operativo()
        hoja = sh.worksheet("ENVIOS 2026")
        valores = hoja.get_all_values()
    except Exception as e:
        print(f"[cuenta_corriente] No se pudo leer TAURO 2026: {e}")
        return []

    if len(valores) < 2:
        return []

    IDX_FECHA = 1
    IDX_NRO_FC = 9
    IDX_FACTURADO = 10
    IDX_EMPRESA = 21
    IDX_ESTADO_S = 18
    IDX_ESTADO_W = 22

    facturas = []
    for fila in valores[1:]:
        if len(fila) <= IDX_EMPRESA:
            continue
        if str(fila[IDX_EMPRESA]).strip().upper() != cliente:
            continue
        estado_s = str(fila[IDX_ESTADO_S]).strip().upper() if len(fila) > IDX_ESTADO_S else ""
        estado_w = str(fila[IDX_ESTADO_W]).strip().upper() if len(fila) > IDX_ESTADO_W else ""
        if any(x in estado_s for x in ("CANCEL", "NC")) or any(x in estado_w for x in ("CANCEL", "NC")):
            continue
        monto = _parse_monto(fila[IDX_FACTURADO])
        if monto <= 0:
            continue
        facturas.append({
            "fecha": str(fila[IDX_FECHA]).strip(),
            "nro_fc": str(fila[IDX_NRO_FC]).strip(),
            "monto_ars": monto,
        })

    # Ordenar por fecha desc
    def _fecha_key(f):
        try:
            return datetime.strptime(f["fecha"], "%d/%m/%Y")
        except (ValueError, TypeError):
            return datetime.min

    facturas.sort(key=_fecha_key, reverse=True)
    return facturas[:limite]


def get_pagos(cliente: str) -> List[Dict[str, Any]]:
    """Lista de pagos recibidos del cliente."""
    cliente = cliente.strip().upper()
    sh = _abrir_sheet()
    hoja = sh.worksheet("PAGOS")
    rows = hoja.get_all_records()
    pagos = []
    for r in rows:
        if str(r.get("CLIENTE", "")).strip().upper() == cliente:
            try:
                monto = float(str(r.get("MONTO_ARS", 0)).replace(",", ""))
            except (ValueError, TypeError):
                continue
            pagos.append({
                "fecha": str(r.get("FECHA", "")),
                "monto_ars": monto,
                "metodo": str(r.get("METODO", "")),
                "referencia": str(r.get("REFERENCIA", "")),
                "nota": str(r.get("NOTA", "")),
            })
    return pagos


def total_pagado(cliente: str) -> float:
    """Suma total pagada por el cliente."""
    return sum(p["monto_ars"] for p in get_pagos(cliente))


def saldo(cliente: str, total_facturado_ars: float) -> Dict[str, float]:
    """
    Calcula saldo. Recibe el total facturado (que viene del sheet operativo)
    y devuelve el desglose.
    """
    pagado = total_pagado(cliente)
    return {
        "facturado_ars": round(total_facturado_ars, 2),
        "pagado_ars": round(pagado, 2),
        "saldo_pendiente_ars": round(total_facturado_ars - pagado, 2),
    }


def movimientos(cliente: str, facturas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Timeline de movimientos: facturas + pagos mezclados ordenados por fecha.

    facturas: lista que viene del sheet operativo, formato esperado:
        [{"fecha": "DD/MM/YYYY", "nro_fc": "0106-00921623", "monto_ars": 45300}, ...]

    Devuelve:
        [{"fecha": ..., "tipo": "FC"|"PAGO", "concepto": ..., "monto_ars": +/-}]
    """
    items = []

    for fc in facturas:
        items.append({
            "fecha": fc.get("fecha", ""),
            "tipo": "FC",
            "concepto": fc.get("nro_fc", ""),
            "monto_ars": float(fc.get("monto_ars", 0)),  # positivo = aumenta deuda
        })

    for p in get_pagos(cliente):
        items.append({
            "fecha": p["fecha"],
            "tipo": "PAGO",
            "concepto": f"{p['metodo']} {p['referencia']}".strip(),
            "monto_ars": -p["monto_ars"],  # negativo = baja deuda
        })

    # Ordenar por fecha (parseando DD/MM/YYYY)
    def _parse_fecha(s: str):
        try:
            return datetime.strptime(s, "%d/%m/%Y")
        except (ValueError, TypeError):
            return datetime.min

    items.sort(key=lambda x: _parse_fecha(x["fecha"]), reverse=True)
    return items
