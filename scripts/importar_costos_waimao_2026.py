#!/usr/bin/env python3
"""Importa COSTOINICIAL de TAURO 2026 a snapshots históricos de WAIMAO.

El modo predeterminado es sólo lectura. Para escribir se exige ``--aplicar``.
La conexión se toma exclusivamente de ``DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servicios.importacion_costos_waimao import (
    importar_costos_waimao,
    planificar_importacion_costos_waimao,
)


def _json(valor):
    if isinstance(valor, Decimal):
        return str(valor)
    raise TypeError(type(valor).__name__)


def _resumen(resultado: dict, aplicado: bool) -> dict:
    return {
        "modo": "APLICADO" if aplicado else "DRY_RUN",
        "source_sha256": resultado["source_sha256"],
        "candidatos": len(resultado["candidatos"]),
        "ya_existentes_compatibles": len(resultado["existentes"]),
        "aplicados": len(resultado.get("aplicados", [])),
        "sin_evidencia_costo_inicial": len(resultado["sin_evidencia"]),
        "no_aplicables": len(resultado["no_aplicables"]),
        "tracking_aplicados": [x["tracking"] for x in resultado.get("aplicados", [])],
        "tracking_sin_evidencia": [x["tracking"] for x in resultado["sin_evidencia"]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", type=Path, help="Copia XLSX autenticada de TAURO 2026")
    parser.add_argument("--aplicar", action="store_true", help="Inserta snapshots faltantes")
    parser.add_argument("--actor", default="script:importar_costos_waimao_2026")
    args = parser.parse_args()
    if args.aplicar:
        resultado = importar_costos_waimao(args.xlsx, actor=args.actor)
    else:
        resultado = planificar_importacion_costos_waimao(args.xlsx)
    print(json.dumps(_resumen(resultado, args.aplicar), ensure_ascii=False, indent=2, default=_json))


if __name__ == "__main__":
    main()
