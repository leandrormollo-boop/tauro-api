"""
Catálogo de países del cotizador internacional.

TAURO cotiza ambos sentidos y rutas entre terceros países: AR → CN, CN → AR
y también CN → IN, donde Argentina ni aparece. AR → AR es un envío nacional
y queda reservado al circuito directo de OCA/Andreani.

Eso rompe el modelo de la tabla `rutas`, que guarda pares cargados a mano:
serían cientos de filas y cada país nuevo un pedido al admin antes de poder
cotizar. Acá el catálogo es una constante y **la cobertura la decide el
courier**: si DHL cotiza Bangladesh → India, se vende; si no, el cliente ve
que ese courier no llega, que es la verdad y sale de ellos.

Cada país trae una ciudad y un CP de REFERENCIA. Sirven SOLO para una
estimación sin dirección cargada: en un envío real va la dirección del
remitente y del destinatario, porque los recargos por zona remota dependen
del CP exacto (ver `_destino_para_cotizar` en cotizador.py).
"""
from __future__ import annotations

import re
import unicodedata

# ISO-2 → (nombre, ciudad de referencia, CP de referencia)
PAISES = {
    "AR": ("Argentina",        "BUENOS AIRES", "1043"),
    "US": ("Estados Unidos",   "MIAMI",        "33101"),
    "CN": ("China",            "SHANGHAI",     "200000"),
    "IN": ("India",            "MUMBAI",       "400001"),
    "BD": ("Bangladesh",       "DHAKA",        "1000"),
    "VN": ("Vietnam",          "HO CHI MINH",  "700000"),
    "PK": ("Pakistán",         "KARACHI",      "74000"),
    "TH": ("Tailandia",        "BANGKOK",      "10100"),
    "ID": ("Indonesia",        "JAKARTA",      "10110"),
    "TR": ("Turquía",          "ISTANBUL",     "34000"),
    "KR": ("Corea del Sur",    "SEOUL",        "04524"),
    "JP": ("Japón",            "TOKYO",        "100-0001"),
    "HK": ("Hong Kong",        "HONG KONG",    ""),
    "TW": ("Taiwán",           "TAIPEI",       "100"),
    "AE": ("Emiratos Árabes",  "DUBAI",        ""),
    "ES": ("España",           "MADRID",       "28001"),
    "IT": ("Italia",           "MILANO",       "20121"),
    "DE": ("Alemania",         "BERLIN",       "10115"),
    "FR": ("Francia",          "PARIS",        "75001"),
    "PT": ("Portugal",         "LISBOA",       "1000-001"),
    "NL": ("Países Bajos",     "AMSTERDAM",    "1011"),
    "GB": ("Reino Unido",      "LONDON",       "EC1A 1BB"),
    "BR": ("Brasil",           "SAO PAULO",    "01310100"),
    "CL": ("Chile",            "SANTIAGO",     "8320000"),
    "UY": ("Uruguay",          "MONTEVIDEO",   "11000"),
    "PY": ("Paraguay",         "ASUNCION",     "1209"),
    "BO": ("Bolivia",          "LA PAZ",       ""),
    "PE": ("Perú",             "LIMA",         "15001"),
    "EC": ("Ecuador",          "QUITO",        "170101"),
    "CO": ("Colombia",         "BOGOTA",       "110111"),
    "MX": ("México",           "CIUDAD DE MEXICO", "06600"),
    "CA": ("Canadá",           "TORONTO",      "M5H 2N2"),
    "AU": ("Australia",        "SYDNEY",       "2000"),
    "IL": ("Israel",           "TEL AVIV",     "6100000"),
    "ZA": ("Sudáfrica",        "JOHANNESBURG", "2000"),
}


def _clave_pais(valor: str) -> str:
    """Clave estable para comparar nombres sin depender de tildes o puntos."""
    texto = str(valor or "").strip()
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(
        caracter for caracter in texto
        if not unicodedata.combining(caracter)
    ).casefold()
    # ``EE.UU.`` -> ``ee uu`` y ``U.S.A.`` -> ``u s a``. No se eliminan
    # caracteres pegándolos porque eso podría hacer coincidir textos que en
    # realidad son distintos.
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texto).split())


# Los nombres oficiales del catálogo se registran automáticamente. Así cada
# país nuevo agregado a PAISES queda normalizable sin mantener dos tablas.
_ISO_POR_NOMBRE = {
    _clave_pais(datos[0]): iso
    for iso, datos in PAISES.items()
}
_ISO_POR_ALIAS = {
    # Estados Unidos aparece con estas variantes en formularios, APIs de
    # tiendas y archivos históricos de clientes.
    "usa": "US",
    "u s a": "US",
    "eeuu": "US",
    "ee uu": "US",
    "estados unidos de america": "US",
    "united states": "US",
    "united states of america": "US",
    # Alias internacionales frecuentes que no coinciden con el nombre
    # comercial en español del catálogo.
    "brazil": "BR",
    "spain": "ES",
    "germany": "DE",
    "france": "FR",
    "italy": "IT",
    "japan": "JP",
    "united kingdom": "GB",
    "uk": "GB",
    "u k": "GB",
    "england": "GB",
    "inglaterra": "GB",
    "netherlands": "NL",
    "holanda": "NL",
    "south korea": "KR",
    "united arab emirates": "AE",
}


def normalizar_iso2(valor: str) -> str:
    """Devuelve el ISO-2 canónico de un código, nombre o alias conocido.

    Falla cerrado: un valor desconocido devuelve ``""``. En particular no
    corta los dos primeros caracteres; ``ESTADOS UNIDOS`` debe convertirse
    en ``US`` y jamás en ``ES``.
    """
    texto = str(valor or "").strip()
    if not texto:
        return ""

    candidato_iso = texto.upper()
    if len(candidato_iso) == 2 and candidato_iso in PAISES:
        return candidato_iso

    clave = _clave_pais(texto)
    return _ISO_POR_NOMBRE.get(clave) or _ISO_POR_ALIAS.get(clave, "")


def normalizar(valor: str) -> str:
    """Alias público breve para consumidores que normalizan rutas completas."""
    return normalizar_iso2(valor)


def nombre(valor: str) -> str:
    iso2 = normalizar_iso2(valor)
    return PAISES[iso2][0] if iso2 else ""


def existe(valor: str) -> bool:
    return bool(normalizar_iso2(valor))


def referencia(valor: str) -> dict:
    """
    Ciudad y CP de referencia del país, para una estimación sin dirección.
    En un envío real NO se usa: va la dirección de verdad.
    """
    iso2 = normalizar_iso2(valor)
    if not iso2:
        return {}
    _, ciudad, cp = PAISES[iso2]
    return {"city": ciudad, "postal_code": cp, "country": iso2}


def opciones() -> list:
    """Para los desplegables: [(iso2, nombre), ...] ordenado por nombre."""
    return sorted(((iso, datos[0]) for iso, datos in PAISES.items()),
                  key=lambda x: x[1])
