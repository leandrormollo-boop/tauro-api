# ============================================================
# Servicio de rutas predefinidas
# ============================================================
# Lee la hoja RUTAS_DEFAULT.
# El portal usa get_rutas_activas() para llenar el dropdown del cliente.
# ============================================================

from typing import List, Optional
from core.sheets_client import _abrir_sheet
from modelos.ruta import Ruta


# ── Mapeos para FedEx (que pide ISO-2 + state code) ─────────
# El sheet guarda nombres legibles ("Argentina"); FedEx pide "AR".
# Si agregás un país nuevo a RUTAS_DEFAULT, sumalo acá también.
PAIS_A_ISO = {
    "ARGENTINA": "AR",
    "ESTADOS UNIDOS": "US",
    "USA": "US",
    "EE.UU.": "US",
    "BRASIL": "BR",
    "CHILE": "CL",
    "URUGUAY": "UY",
}

# Provincia/Estado por ciudad (FedEx lo pide en stateOrProvinceCode).
CIUDAD_A_STATE = {
    "BUENOS AIRES": "B",   # Provincia de Buenos Aires (FedEx AR usa código provincia)
    "CABA": "C",
    "MIAMI": "FL",
    "NEW YORK": "NY",
    "LOS ANGELES": "CA",
    "MENDOZA": "M",
    "CORDOBA": "X",
}


def pais_a_iso2(nombre: str) -> str:
    """Devuelve el código ISO-2 de un país. Si no lo conoce, devuelve el input."""
    return PAIS_A_ISO.get((nombre or "").strip().upper(), (nombre or "").strip().upper())


def ciudad_a_state(ciudad: str) -> str:
    """Devuelve el código de estado/provincia para una ciudad. '' si no se conoce."""
    return CIUDAD_A_STATE.get((ciudad or "").strip().upper(), "")


def _row_a_ruta(r: dict) -> Ruta:
    return Ruta(
        ruta_id=str(r.get("RUTA_ID", "")).strip(),
        origen_pais=str(r.get("ORIGEN_PAIS", "")).strip(),
        origen_ciudad=str(r.get("ORIGEN_CITY", "")).strip(),
        origen_zip=str(r.get("ORIGEN_ZIP", "")).strip(),
        destino_pais=str(r.get("DESTINO_PAIS", "")).strip(),
        destino_ciudad=str(r.get("DESTINO_CITY", "")).strip(),
        destino_zip=str(r.get("DESTINO_ZIP", "")).strip(),
        dias_estimados=int(r.get("DIAS_ESTIMADOS", 5) or 5),
        activa=str(r.get("ACTIVA", "")).strip().upper() == "TRUE",
    )


def get_rutas_activas() -> List[Ruta]:
    """Solo rutas con ACTIVA=TRUE. Para el dropdown del portal."""
    sh = _abrir_sheet()
    hoja = sh.worksheet("RUTAS_DEFAULT")
    rows = hoja.get_all_records()
    rutas = []
    for r in rows:
        try:
            ruta = _row_a_ruta(r)
            if ruta.activa and ruta.ruta_id:
                rutas.append(ruta)
        except Exception:
            continue  # fila mal formada, ignorar
    return rutas


def get_ruta(ruta_id: str) -> Optional[Ruta]:
    """Busca una ruta por su ID. Devuelve None si no existe o está inactiva."""
    ruta_id = ruta_id.strip().upper()
    sh = _abrir_sheet()
    hoja = sh.worksheet("RUTAS_DEFAULT")
    rows = hoja.get_all_records()
    for r in rows:
        if str(r.get("RUTA_ID", "")).strip().upper() == ruta_id:
            try:
                ruta = _row_a_ruta(r)
                if ruta.activa:
                    return ruta
            except Exception:
                return None
    return None


def get_paises_origen() -> List[str]:
    """Países distintos de origen entre las rutas activas."""
    rutas = get_rutas_activas()
    vistos = []
    for r in rutas:
        if r.origen_pais and r.origen_pais not in vistos:
            vistos.append(r.origen_pais)
    return vistos


def get_paises_destino() -> List[str]:
    """Países distintos de destino entre las rutas activas."""
    rutas = get_rutas_activas()
    vistos = []
    for r in rutas:
        if r.destino_pais and r.destino_pais not in vistos:
            vistos.append(r.destino_pais)
    return vistos


def find_ruta_por_paises(origen_pais: str, destino_pais: str) -> Optional[Ruta]:
    """
    Encuentra la primera ruta activa que matchea origen+destino.
    El portal usa esto: el cliente solo elige países, el sistema resuelve
    internamente ciudad/ZIP de la ruta predefinida.
    """
    origen_pais = (origen_pais or "").strip().upper()
    destino_pais = (destino_pais or "").strip().upper()
    for r in get_rutas_activas():
        if r.origen_pais.upper() == origen_pais and r.destino_pais.upper() == destino_pais:
            return r
    return None
