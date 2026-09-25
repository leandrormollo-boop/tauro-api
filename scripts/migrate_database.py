"""Migración bloqueante para ejecutar antes de iniciar el proceso web."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.database import init_db, verificar_readiness_db  # noqa: E402
from servicios.api_b2b import migrar_api_keys_legacy  # noqa: E402
from servicios.shopify_app import (  # noqa: E402
    migrar_instalaciones_legacy,
    migrar_tokens_legacy as migrar_tokens_shopify,
)
from servicios.tiendanube_app import (  # noqa: E402
    migrar_tokens_legacy as migrar_tokens_tiendanube,
)


def main() -> None:
    # El schema debe entrar antes que los backfills legacy. La verificación
    # completa corre al final: si se hiciera dentro de init_db bloquearía, por
    # ejemplo, los access tokens históricos que este mismo predeploy cifra.
    init_db(verificar=False)
    migrar_api_keys_legacy()
    migrar_instalaciones_legacy()
    migrar_tokens_shopify()
    migrar_tokens_tiendanube()
    verificar_readiness_db()
    print("[migrate] Schema, API keys y secretos OAuth listos.")


if __name__ == "__main__":
    main()
