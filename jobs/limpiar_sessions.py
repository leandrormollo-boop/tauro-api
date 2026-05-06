# ============================================================
# Job: limpiar SESSIONS expiradas
# ============================================================
# Corre todos los días a las 3am (hora Argentina).
# Borra de la hoja SESSIONS las filas con EXPIRA < ahora,
# así la hoja no crece sin control.
#
# El borrado se hace de abajo hacia arriba para no desfasar
# los índices.
# ============================================================

from datetime import datetime
from typing import List

from core.sheets_client import _abrir_sheet


def _parse_fecha_flex(s: str):
    if not s:
        return None
    s = str(s).strip()
    formatos = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    ]
    for fmt in formatos:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def limpiar_sessions_expiradas() -> int:
    """
    Borra filas de SESSIONS donde EXPIRA < ahora.
    Devuelve la cantidad de filas borradas.
    """
    sh = _abrir_sheet()
    hoja = sh.worksheet("SESSIONS")
    valores = hoja.get_all_values()

    if len(valores) < 2:
        return 0

    header = valores[0]
    try:
        idx_expira = header.index("EXPIRA")
    except ValueError:
        print("[limpiar_sessions] Columna EXPIRA no encontrada, abortando.")
        return 0

    ahora = datetime.now()
    filas_a_borrar: List[int] = []

    for i, fila in enumerate(valores[1:], start=2):  # row index 1-based, header en 1
        if len(fila) <= idx_expira:
            continue
        expira = _parse_fecha_flex(fila[idx_expira])
        if expira and expira < ahora:
            filas_a_borrar.append(i)

    # Borrar de abajo hacia arriba para no desfasar índices
    for row in sorted(filas_a_borrar, reverse=True):
        try:
            hoja.delete_rows(row)
        except Exception as e:
            print(f"[limpiar_sessions] Error borrando fila {row}: {e}")

    n = len(filas_a_borrar)
    print(f"[limpiar_sessions] Borradas {n} sesiones expiradas.")
    return n


if __name__ == "__main__":
    limpiar_sessions_expiradas()
