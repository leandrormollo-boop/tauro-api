# ============================================================
# Un solo lugar para "¿a dónde apunta el tracking de este courier?"
# ============================================================
# Antes esta lógica vivía copiada en tres templates y todo proveedor no
# reconocido terminaba apuntando a FedEx. Cuando entra un courier nuevo,
# había que acordarse de tocar los tres. Ahora es una función registrada
# como global de Jinja: se toca acá y cambia en todas las pantallas.
# ============================================================
from __future__ import annotations

import re
from urllib.parse import quote

_URLS = {
    "FEDEX": "https://www.fedex.com/fedextrack/?trknbr={t}",
    "ENVIA": "https://envia.com/es-AR/rastreo?label={t}",
    "UPS": "https://www.ups.com/track?tracknum={t}",
    "DHL": "https://www.dhl.com/ar-es/home/rastreo.html?tracking-id={t}",
    "ANDREANI": "https://www.andreani.com/envio/{t}",
    "OCA": "https://www.oca.com.ar/Busquedas/Envios?numero={t}",
    "CORREO": "https://www.correoargentino.com.ar/formularios/e-commerce?id={t}",
}

# ENVIA queda únicamente como identificador de filas históricas. Andreani,
# OCA y Correo son los proveedores nacionales directos. La división de los
# listados sale de acá, no de reglas sueltas en cada template.
_NACIONALES = {"ENVIA", "ANDREANI", "OCA", "CORREO"}


def url_tracking(courier: str, tracking: str) -> str:
    """URL de seguimiento oficial del courier. FedEx si no se lo conoce."""
    if not tracking:
        return ""
    c = (courier or "FEDEX").strip().upper()
    plantilla = _URLS.get(c, _URLS["FEDEX"])
    return plantilla.format(t=quote(str(tracking)))


def es_nacional(courier: str) -> bool:
    return (courier or "FEDEX").strip().upper() in _NACIONALES


def ambito_envio(envio: dict) -> str:
    """Clasifica por la ruta real; el courier no define nacionalidad.

    Las filas nuevas siempre tienen ambos países. El fallback por courier se
    conserva únicamente para historia nacional anterior a esa información.
    Si no hay evidencia suficiente, la fila queda en cuarentena en vez de
    aparecer mezclada como internacional.
    """
    from servicios.paises import normalizar as normalizar_pais

    origen = normalizar_pais(
        envio.get("ambito_origen")
        or envio.get("origen_iso")
        or envio.get("remitente_pais")
        or ""
    )
    destino = normalizar_pais(
        envio.get("ambito_destino")
        or envio.get("destino_iso")
        or envio.get("destino_pais")
        or ""
    )
    por_ruta = None
    if origen and destino:
        por_ruta = "nacional" if origen == "AR" and destino == "AR" else "internacional"

    persistido = str(envio.get("ambito") or "").strip().lower()
    if persistido in {"nacional", "internacional"}:
        # Una inconsistencia no se tapa eligiendo una de las dos fuentes: se
        # aísla hasta revisar la fila. La ruta es la evidencia canónica.
        if por_ruta and persistido != por_ruta:
            return "sin_clasificar"
        return persistido
    if por_ruta:
        return por_ruta
    if es_nacional(envio.get("courier")):
        return "nacional"
    return "sin_clasificar"


# Nombre lindo para mostrar (el chip "ver detalle en …").
_NOMBRES = {
    "FEDEX": "FedEx", "DHL": "DHL", "UPS": "UPS", "ENVIA": "Nacional histórico",
    "ANDREANI": "Andreani", "OCA": "OCA", "CORREO": "Correo Argentino",
}


def nombre_courier(courier: str) -> str:
    original = (courier or "").strip()
    return _NOMBRES.get(original.upper(), original)


def normalizar_tracking(nro: str) -> str:
    """Saca espacios y guiones: la gente copia '1Z 999 AA1 01 2345 6784'."""
    return re.sub(r"[\s\-]", "", (nro or "")).strip().upper()


def detectar_courier(nro: str) -> str | None:
    """
    Adivina el courier SÓLO por el formato del número, para cuando el envío no
    está en nuestra base (lo hicimos nosotros → ahí sabemos el courier exacto).
    Devuelve el código del courier o None si el formato no es concluyente.

    Es una heurística: los patrones inequívocos (UPS, DHL Express, el formato
    postal de Correo) se detectan con confianza; los dígitos puros se los queda
    FedEx, que es el courier principal y el de los envíos del mayorista. Ante la
    duda es mejor None que mandar al cliente al courier equivocado.
    """
    t = normalizar_tracking(nro)
    if not t:
        return None

    # UPS: 1Z + 16 alfanuméricos. Inequívoco.
    if re.fullmatch(r"1Z[0-9A-Z]{16}", t):
        return "UPS"
    # DHL Express: prefijos propios (JJD, JVGL) o exactamente 10 dígitos.
    if re.fullmatch(r"(JJD|JVGL)[0-9A-Z]+", t) or re.fullmatch(r"\d{10}", t):
        return "DHL"
    # Formato postal universal (Correo Argentino): 2 letras + 9 díg + país.
    if re.fullmatch(r"[A-Z]{2}\d{9}AR", t):
        return "CORREO"
    # Andreani: 15 dígitos que arrancan con 360 (su prefijo).
    if re.fullmatch(r"360\d{12}", t):
        return "ANDREANI"
    # FedEx: 12, 15, 20 o 22 dígitos. Es el default para dígitos puros.
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}", t):
        return "FEDEX"
    return None
