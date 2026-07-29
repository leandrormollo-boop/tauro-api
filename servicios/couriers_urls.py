# ============================================================
# Un solo lugar para "¿a dónde apunta el tracking de este courier?"
# ============================================================
# Antes esta lógica vivía copiada en tres templates, todos con la misma
# regla implícita "no-ENVIA = FedEx". Cuando entren UPS/DHL (los clientes
# ya están escritos, falta credencial) o un courier nacional directo,
# había que acordarse de tocar los tres. Ahora es una función registrada
# como global de Jinja: se toca acá y cambia en todas las pantallas.
# ============================================================
from __future__ import annotations

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

# Lo emitido por envia.com (y el día de mañana por los nacionales directos)
# es la pata NACIONAL; el resto es internacional. La división en listados
# sale de acá, no de reglas sueltas en cada template.
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
