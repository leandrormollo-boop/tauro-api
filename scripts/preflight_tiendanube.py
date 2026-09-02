#!/usr/bin/env python3
"""Imprime el preflight de Tiendanube en JSON y falla si hay bloqueadores."""
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servicios.tiendanube_preflight import evaluate_preflight


def main() -> int:
    result = evaluate_preflight(repository_root=ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready_for_release"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
