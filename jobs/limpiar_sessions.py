# ============================================================
# Job: limpiar sesiones expiradas — PostgreSQL
# ============================================================
# Corre todos los días a las 3am (hora Argentina).
# Elimina de la tabla sessions las filas expiradas o usadas.
# ============================================================

from servicios.auth import limpiar_sessions_expiradas as _limpiar


def limpiar_sessions_expiradas() -> int:
    """Elimina sesiones expiradas o usadas. Devuelve la cantidad eliminada."""
    n = _limpiar()
    print(f"[limpiar_sessions] Eliminadas {n} sesiones.")
    return n


if __name__ == "__main__":
    limpiar_sessions_expiradas()
