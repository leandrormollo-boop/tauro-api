# ============================================================
# Servicio de rutas predefinidas — PostgreSQL
# ============================================================

from typing import List, Optional
from core.database import get_conn
from modelos.ruta import Ruta


# ── Mapeos para FedEx ────────────────────────────────────────
PAIS_A_ISO = {
    "ARGENTINA": "AR",
    "ESTADOS UNIDOS": "US",
    "USA": "US",
    "EE.UU.": "US",
    "BRASIL": "BR",
    "CHILE": "CL",
    "URUGUAY": "UY",
}

CIUDAD_A_STATE = {
    "BUENOS AIRES": "B",
    "CABA": "C",
    "MIAMI": "FL",
    "NEW YORK": "NY",
    "LOS ANGELES": "CA",
    "MENDOZA": "M",
    "CORDOBA": "X",
}


def pais_a_iso2(nombre: str) -> str:
    return PAIS_A_ISO.get((nombre or "").strip().upper(), (nombre or "").strip().upper())


def ciudad_a_state(ciudad: str) -> str:
    return CIUDAD_A_STATE.get((ciudad or "").strip().upper(), "")


def _row_a_ruta(r: dict) -> Ruta:
    return Ruta(
        ruta_id=str(r["ruta_id"]).strip(),
        origen_pais=str(r["origen_pais"]).strip(),
        origen_ciudad=str(r["origen_ciudad"]).strip(),
        origen_zip=str(r["origen_zip"]).strip(),
        destino_pais=str(r["destino_pais"]).strip(),
        destino_ciudad=str(r["destino_ciudad"]).strip(),
        destino_zip=str(r["destino_zip"]).strip(),
        dias_estimados=int(r["dias_estimados"] or 5),
        activa=bool(r["activa"]),
    )


def get_rutas_activas() -> List[Ruta]:
    """Rutas con activa=TRUE."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rutas WHERE activa = TRUE ORDER BY ruta_id")
            rows = cur.fetchall()
    return [_row_a_ruta(r) for r in rows]


def get_todas_las_rutas() -> List[Ruta]:
    """Todas las rutas (admin)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rutas ORDER BY ruta_id")
            rows = cur.fetchall()
    return [_row_a_ruta(r) for r in rows]


def get_ruta(ruta_id: str) -> Optional[Ruta]:
    """Busca una ruta activa por su ID."""
    ruta_id = ruta_id.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM rutas WHERE ruta_id = %s AND activa = TRUE",
                (ruta_id,),
            )
            row = cur.fetchone()
    return _row_a_ruta(row) if row else None


def get_paises_origen() -> List[str]:
    rutas = get_rutas_activas()
    vistos = []
    for r in rutas:
        if r.origen_pais and r.origen_pais not in vistos:
            vistos.append(r.origen_pais)
    return vistos


def get_paises_destino() -> List[str]:
    rutas = get_rutas_activas()
    vistos = []
    for r in rutas:
        if r.destino_pais and r.destino_pais not in vistos:
            vistos.append(r.destino_pais)
    return vistos


def find_ruta_por_paises(origen_pais: str, destino_pais: str) -> Optional[Ruta]:
    origen_pais = (origen_pais or "").strip().upper()
    destino_pais = (destino_pais or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM rutas
                WHERE UPPER(origen_pais) = %s
                  AND UPPER(destino_pais) = %s
                  AND activa = TRUE
                LIMIT 1
                """,
                (origen_pais, destino_pais),
            )
            row = cur.fetchone()
    return _row_a_ruta(row) if row else None


def upsert_ruta(ruta: Ruta) -> None:
    """Crea o actualiza una ruta."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rutas
                    (ruta_id, origen_pais, origen_ciudad, origen_zip,
                     destino_pais, destino_ciudad, destino_zip, dias_estimados, activa)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ruta_id) DO UPDATE SET
                    origen_pais    = EXCLUDED.origen_pais,
                    origen_ciudad  = EXCLUDED.origen_ciudad,
                    origen_zip     = EXCLUDED.origen_zip,
                    destino_pais   = EXCLUDED.destino_pais,
                    destino_ciudad = EXCLUDED.destino_ciudad,
                    destino_zip    = EXCLUDED.destino_zip,
                    dias_estimados = EXCLUDED.dias_estimados,
                    activa         = EXCLUDED.activa
                """,
                (
                    ruta.ruta_id.upper(),
                    ruta.origen_pais, ruta.origen_ciudad, ruta.origen_zip,
                    ruta.destino_pais, ruta.destino_ciudad, ruta.destino_zip,
                    ruta.dias_estimados, ruta.activa,
                ),
            )


def toggle_ruta(ruta_id: str, activa: bool) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rutas SET activa = %s WHERE ruta_id = %s",
                (activa, ruta_id.strip().upper()),
            )
