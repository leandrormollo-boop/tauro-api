"""Cruce read-only de facturas courier contra la hoja madre TAURO 2026."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.database import get_conn
from jobs.sync_sheet_tauro import SHEET_ID_DEFAULT
from servicios.conciliacion_couriers import (
    ConciliacionCourierError,
    _registrar_auditoria,
    normalizar_identificador,
    normalizar_tracking,
    registrar_referencias_tauro_2026,
)


HOJA_ENVIOS = "ENVIOS 2026"
RANGO_COLUMNAS = "A:T"
_ENCABEZADOS_REQUERIDOS = {
    "EMPRESA", "FECHA", "DESTINATARIO", "PAIS", "PESO", "MEDIDAS", "TRACKING",
    "FLETE O TAX", "NRO FC", "FACTURADO", "DIF INICIAL VS FC",
    "SALDO ARS", "COSTOINICIAL",
}


def _encabezado(valor: Any) -> str:
    return re.sub(r"\s+", " ", str(valor or "").strip()).upper()


def _texto(valor: Any) -> str:
    return str(valor or "").strip()


def _valor(fila: list[Any], indice: int | None) -> Any:
    if indice is None or indice < 0 or indice >= len(fila):
        return None
    return fila[indice]


def _monto(valor: Any) -> str | None:
    if valor is None or _texto(valor) == "":
        return None
    texto = _texto(valor).replace("$", "").replace(" ", "")
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        return str(Decimal(texto).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return None


def _fecha(valor: Any) -> str | None:
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, (int, float)) and 1 <= float(valor) <= 100000:
        return (date(1899, 12, 30) + timedelta(days=int(valor))).isoformat()
    texto = _texto(valor)
    if not texto:
        return None
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto[:10], formato).date().isoformat()
        except ValueError:
            continue
    return texto[:40]


def _mapa_encabezados(encabezados: list[Any]) -> dict[str, int]:
    mapa = {
        _encabezado(valor): indice
        for indice, valor in enumerate(encabezados)
        if _encabezado(valor)
    }
    faltantes = sorted(_ENCABEZADOS_REQUERIDOS - set(mapa))
    if faltantes:
        raise ConciliacionCourierError(
            "TAURO 2026 cambió sus encabezados. Faltan: "
            + ", ".join(faltantes)
        )
    return mapa


def leer_referencias_factura(
    *,
    numero_factura: str,
    courier: str,
) -> tuple[list[dict[str, Any]], str]:
    """Lee únicamente las filas cuyo NRO FC coincide exactamente."""
    from core.sheets_client import get_cliente_sheets

    numero = normalizar_identificador(numero_factura)
    empresa = _texto(courier).upper()
    if not numero or not empresa:
        raise ConciliacionCourierError("Falta factura o courier para consultar TAURO 2026.")
    sheet_id = _texto(os.getenv("TAURO_SHEET_ID")) or SHEET_ID_DEFAULT
    libro = get_cliente_sheets().open_by_key(sheet_id)
    hoja = libro.worksheet(HOJA_ENVIOS)
    encabezados = hoja.get(
        "A1:T1", value_render_option="FORMATTED_VALUE"
    )[0]
    mapa = _mapa_encabezados(encabezados)
    columna_fc = mapa["NRO FC"] + 1
    celdas = hoja.findall(numero_factura, in_column=columna_fc)
    referencias: list[dict[str, Any]] = []
    evidencia_filas: list[dict[str, Any]] = []
    for celda in celdas:
        valores = hoja.get(
            f"A{celda.row}:T{celda.row}",
            value_render_option="UNFORMATTED_VALUE",
            date_time_render_option="FORMATTED_STRING",
        )
        if not valores:
            continue
        fila = valores[0]
        nro_fc = normalizar_identificador(_valor(fila, mapa["NRO FC"]))
        empresa_fila = _texto(_valor(fila, mapa["EMPRESA"])).upper()
        tracking = normalizar_tracking(_valor(fila, mapa["TRACKING"]))
        if nro_fc != numero or empresa_fila != empresa or not tracking:
            continue
        # En la hoja operativa, la columna E identifica al cliente aunque el
        # encabezado pueda estar temporalmente vacío por filtros/ediciones.
        cliente = _texto(_valor(fila, 4)).upper()
        if not cliente:
            raise ConciliacionCourierError(
                f"TAURO 2026 fila {celda.row} no identifica al cliente."
            )
        referencia = {
            "empresa": empresa_fila,
            "nro_fc": nro_fc,
            "tracking": tracking,
            "cliente": cliente,
            "concepto": _texto(_valor(fila, mapa["FLETE O TAX"])).upper(),
            "fecha_envio": _fecha(_valor(fila, mapa.get("FECHA"))),
            "remitente": cliente,
            "destinatario": _texto(_valor(fila, mapa["DESTINATARIO"])),
            "pais": _texto(_valor(fila, mapa["PAIS"])),
            "peso": _texto(_valor(fila, mapa["PESO"])),
            "medidas": _texto(_valor(fila, mapa["MEDIDAS"])),
            "facturado_ars": _monto(_valor(fila, mapa["FACTURADO"])),
            "saldo_ars": _monto(_valor(fila, mapa["SALDO ARS"])),
            "costo_inicial_ars": _monto(_valor(fila, mapa["COSTOINICIAL"])),
            "diferencia_ars": _monto(_valor(fila, mapa["DIF INICIAL VS FC"])),
            "fuente_libro": "TAURO 2026",
            "fuente_hoja": HOJA_ENVIOS,
            "fuente_fila": int(celda.row),
        }
        referencias.append(referencia)
        evidencia_filas.append({"fila": int(celda.row), "valores": fila})
    huella = hashlib.sha256(json.dumps(
        {"encabezados": encabezados, "filas": evidencia_filas},
        sort_keys=True, ensure_ascii=True, default=str,
    ).encode("utf-8")).hexdigest()
    return referencias, huella


def sincronizar_referencias_factura(
    factura_id: int,
    *,
    actor: str,
) -> dict[str, Any]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT numero, courier FROM facturas_courier WHERE id=%s",
            (int(factura_id),),
        )
        factura = cur.fetchone()
    if not factura:
        raise ConciliacionCourierError("La factura courier no existe.")
    referencias, huella = leer_referencias_factura(
        numero_factura=factura["numero"], courier=factura["courier"]
    )
    if not referencias:
        return {"estado": "SIN_REFERENCIAS", "factura_id": int(factura_id)}
    resultado = registrar_referencias_tauro_2026(
        int(factura_id), referencias=referencias,
        fuente_sha256=huella, actor=actor,
    )
    return {"estado": "OK", **resultado}


def sincronizar_referencias_factura_seguro(
    factura_id: int,
    *,
    actor: str,
) -> dict[str, Any]:
    """La caída de Sheets nunca invalida una FC ni mueve saldos."""
    try:
        return sincronizar_referencias_factura(factura_id, actor=actor)
    except Exception as exc:
        try:
            with get_conn() as conn, conn.cursor() as cur:
                _registrar_auditoria(
                    cur, evento="REFERENCIAS_TAURO_2026_ERROR", actor=actor,
                    factura_id=int(factura_id),
                    metadata={"error_tipo": type(exc).__name__},
                )
        except Exception:
            # Este wrapper existe para aislar la importación documental. Si
            # tampoco hay DB para auditar, no debe convertir una FC ya
            # registrada en un falso error de ingesta.
            pass
        return {
            "estado": "ERROR", "factura_id": int(factura_id),
            "error_tipo": type(exc).__name__,
        }
