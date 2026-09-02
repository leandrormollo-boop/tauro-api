#!/usr/bin/env python3
"""Genera el manifiesto revisado para importar MELCIOR 2026 al portal.

El archivo resultante no se versiona: contiene datos operativos del cliente.
La aplicación vuelve a validar la huella y todos los totales antes de escribir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from auditar_melcior_2026 import auditar
from preparar_importacion_melcior_2026 import leer_maestra, preparar


MESES = (
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
)


def _decimal(valor: object) -> Decimal:
    return Decimal(str(valor or "0")).quantize(Decimal("0.01"))


def _maestra_exacta(cliente: dict, maestras: list[dict]) -> dict | None:
    """Relaciona por los campos visibles sin elegir una fila por intuición."""
    candidatas = [
        fila for fila in maestras
        if _decimal(fila.get("facturado")) == _decimal(cliente.get("facturado"))
        and fila.get("fecha") == cliente.get("fecha")
        and str(fila.get("destinatario") or "").strip().casefold()
        == str(cliente.get("destinatario") or "").strip().casefold()
    ]
    return candidatas[0] if len(candidatas) == 1 else None


def _fila_manifiesto(
    fila: dict,
    *,
    tracking_portal: str,
    estado_portal: str = "DESPACHADO",
    genera_deuda: bool = True,
) -> dict:
    maestra = fila.get("maestra") or {}
    diferencia = _decimal(fila.get("diferencia"))
    costo_inicial = _decimal(maestra.get("costo_inicial")) if diferencia else Decimal("0")
    costo_real = _decimal(maestra.get("saldo_ars")) if diferencia else Decimal("0")
    costo_real_derivado = False
    if diferencia and not maestra.get("saldo_ars"):
        costo_real = costo_inicial + diferencia
        costo_real_derivado = True
    return {
        "source_key": f"MELCIOR-2026:{fila['mes']}:{fila['fila']}",
        "mes": fila["mes"],
        "fila_cliente": int(fila["fila"]),
        "fila_maestra": int(maestra["fila"]) if maestra.get("fila") else None,
        "fecha": fila["fecha"],
        "remitente": fila.get("remitente") or "JUAN PABLO MELCIOR",
        "destinatario": fila["destinatario"],
        "pais_fuente": fila.get("pais") or "",
        "peso_fuente": fila.get("peso") or "",
        "medidas_fuente": fila.get("medidas") or "",
        "tracking": tracking_portal,
        "tracking_fuente": fila.get("tracking") or "",
        "tracking_pendiente": not bool(tracking_portal),
        "tipo_fuente": fila.get("tipo") or "FLETE",
        "estado_portal": estado_portal,
        "genera_deuda": genera_deuda,
        "requiere_revision": (fila.get("tipo") or "").upper() == "REVISAR",
        "importe_inicial_ars": str(_decimal(fila["facturado"])),
        "diferencia_ars": str(diferencia),
        "costo_estimado_ars": str(costo_inicial) if diferencia else "",
        "costo_real_ars": str(costo_real) if diferencia else "",
        "costo_real_derivado": costo_real_derivado,
        "resolucion": fila.get("resolucion") or "",
    }


def generar(cliente_path: Path, maestra_path: Path) -> dict:
    lote = preparar(cliente_path, maestra_path)
    auditoria = auditar(cliente_path)
    maestra_completa = leer_maestra(maestra_path)
    envios = []
    for fila in lote["envios"]:
        envios.append(_fila_manifiesto(fila, tracking_portal=fila["tracking"]))

    # Los tres grupos en cuarentena no son seis cargos duplicados: la hoja
    # maestra confirma dos operaciones diferentes por importe/fecha/destino.
    # Conservamos ambas y dejamos sólo la segunda sin tracking para no violar
    # la unicidad global ni mostrar un número inventado al cliente.
    for grupo in lote["cuarentena"]:
        candidatas = sorted(
            grupo["cliente"],
            key=lambda fila: (MESES.index(fila["mes"]), int(fila["fila"])),
        )
        for indice, candidata in enumerate(candidatas):
            combinada = dict(candidata)
            combinada["maestra"] = _maestra_exacta(candidata, grupo["maestra"])
            if combinada["maestra"] is None:
                raise ValueError(
                    "No se pudo relacionar de forma unívoca "
                    f"{candidata['mes']}!{candidata['fila']} con TAURO 2026."
                )
            combinada["resolucion"] = (
                "TRACKING_CANONICO_GRUPO_REPETIDO"
                if indice == 0 else "TRACKING_DUPLICADO_PENDIENTE_CORRECCION"
            )
            envios.append(_fila_manifiesto(
                combinada,
                tracking_portal=candidata["tracking"] if indice == 0 else "",
            ))

    # La historia cancelada también debe verse en "Mis envíos", pero jamás
    # genera deuda. La única fila REVISAR sí integra el total aprobado por el
    # usuario; queda señalizada en observaciones para que el admin la controle.
    maestras_por_tracking = defaultdict(list)
    for fila in maestra_completa:
        if fila.get("tracking"):
            maestras_por_tracking[fila["tracking"]].append(fila)
    for fila in auditoria["filas"]:
        if fila.get("tipo") not in {"CANCELADO", "REVISAR"}:
            continue
        combinada = dict(fila)
        combinada["maestra"] = _maestra_exacta(
            fila, maestras_por_tracking.get(fila.get("tracking"), [])
        )
        if combinada["maestra"] is None:
            raise ValueError(
                "No se pudo relacionar el estado histórico de "
                f"{fila['mes']}!{fila['fila']} con TAURO 2026."
            )
        es_cancelado = fila["tipo"] == "CANCELADO"
        combinada["resolucion"] = "ESTADO_CANCELADO_FUENTE" if es_cancelado else "REVISAR_FUENTE"
        envios.append(_fila_manifiesto(
            combinada,
            tracking_portal=fila["tracking"],
            estado_portal="CANCELADO" if es_cancelado else "DESPACHADO",
            genera_deuda=not es_cancelado,
        ))

    envios.sort(key=lambda fila: (MESES.index(fila["mes"]), fila["fila_cliente"]))
    tracking_reales = [fila["tracking"] for fila in envios if fila["tracking"]]
    if len(tracking_reales) != len(set(tracking_reales)):
        raise ValueError("El manifiesto final todavía contiene trackings repetidos.")

    meses = {}
    for mes in MESES:
        filas = [fila for fila in envios if fila["mes"] == mes]
        if not filas:
            continue
        meses[mes] = {
            "envios": len(filas),
            "cargos": sum(fila["genera_deuda"] for fila in filas),
            "cancelados": sum(not fila["genera_deuda"] for fila in filas),
            "requieren_revision": sum(fila["requiere_revision"] for fila in filas),
            "tracking_pendiente": sum(fila["tracking_pendiente"] for fila in filas),
            "con_diferencia": sum(_decimal(fila["diferencia_ars"]) != 0 for fila in filas),
            "importe_inicial_ars": str(sum(
                (_decimal(fila["importe_inicial_ars"]) for fila in filas if fila["genera_deuda"]),
                Decimal("0"),
            )),
            "diferencias_ars": str(sum(
                (_decimal(fila["diferencia_ars"]) for fila in filas), Decimal("0")
            )),
        }

    pagos = []
    for pago in lote["pagos_sin_fecha"]:
        pagos.append({
            "source_key": f"MELCIOR-2026:PAGO:{pago['fila']}",
            "fila_cliente": int(pago["fila"]),
            "fecha": "2026-09-02",
            "monto_ars": str(_decimal(pago["monto_ars"])),
            "detalle_fuente": pago.get("detalle") or "",
            "fecha_original_informada": False,
        })

    contenido = {
        "schema_version": 1,
        "cliente_id": "MELCIOR",
        "periodo": 2026,
        "source_sha256": lote["source_sha256"],
        "source_files_sha256": {
            "melcior_2026": hashlib.sha256(cliente_path.read_bytes()).hexdigest(),
            "tauro_2026": hashlib.sha256(maestra_path.read_bytes()).hexdigest(),
        },
        "duplicados_descartados": lote["duplicados_resueltos"],
        "envios": envios,
        "saldo_pendiente_2025": {
            "source_key": "MELCIOR-2026:SALDO-PENDIENTE-2025",
            "fecha": "2025-12-31",
            "monto_ars": str(_decimal(lote["resumen"]["saldo_pendiente_2025_ars"])),
            "concepto": "SALDO PENDIENTE 2025",
        },
        "pagos": pagos,
        "resumen_mensual": meses,
        "resumen": {
            "envios": len(envios),
            "cargos": sum(fila["genera_deuda"] for fila in envios),
            "cancelados": sum(not fila["genera_deuda"] for fila in envios),
            "requieren_revision": sum(fila["requiere_revision"] for fila in envios),
            "tracking_pendiente": sum(fila["tracking_pendiente"] for fila in envios),
            "con_diferencia": sum(_decimal(fila["diferencia_ars"]) != 0 for fila in envios),
            "importe_inicial_ars": str(sum(
                (_decimal(fila["importe_inicial_ars"]) for fila in envios if fila["genera_deuda"]),
                Decimal("0"),
            )),
            "diferencias_ars": str(sum(
                (_decimal(fila["diferencia_ars"]) for fila in envios), Decimal("0")
            )),
            "saldo_pendiente_2025_ars": str(_decimal(
                lote["resumen"]["saldo_pendiente_2025_ars"]
            )),
            "pagos_ars": str(sum(
                (_decimal(pago["monto_ars"]) for pago in pagos), Decimal("0")
            )),
        },
    }
    contenido["resumen"]["saldo_resultante_ars"] = str(
        _decimal(contenido["resumen"]["importe_inicial_ars"])
        + _decimal(contenido["resumen"]["diferencias_ars"])
        + _decimal(contenido["resumen"]["saldo_pendiente_2025_ars"])
        - _decimal(contenido["resumen"]["pagos_ars"])
    )
    serializado = json.dumps(
        contenido, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    contenido["manifest_sha256"] = hashlib.sha256(serializado).hexdigest()
    return contenido


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cliente", type=Path, required=True)
    parser.add_argument("--maestra", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifiesto = generar(args.cliente, args.maestra)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "source_sha256": manifiesto["source_sha256"],
        "manifest_sha256": manifiesto["manifest_sha256"],
        "resumen": manifiesto["resumen"],
        "resumen_mensual": manifiesto["resumen_mensual"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
