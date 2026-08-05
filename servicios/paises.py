"""
Catálogo de países del cotizador.

Regla de Leandro (05/08/2026): TAURO cotiza cualquier par de países.
  AR → CN, CN → AR, AR → AR, y también CN → IN, donde Argentina ni aparece.

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


def nombre(iso2: str) -> str:
    return PAISES.get((iso2 or "").upper(), ((iso2 or "").upper(),))[0]


def existe(iso2: str) -> bool:
    return (iso2 or "").upper() in PAISES


def referencia(iso2: str) -> dict:
    """
    Ciudad y CP de referencia del país, para una estimación sin dirección.
    En un envío real NO se usa: va la dirección de verdad.
    """
    iso2 = (iso2 or "").upper()
    if iso2 not in PAISES:
        return {}
    _, ciudad, cp = PAISES[iso2]
    return {"city": ciudad, "postal_code": cp, "country": iso2}


def opciones() -> list:
    """Para los desplegables: [(iso2, nombre), ...] ordenado por nombre."""
    return sorted(((iso, datos[0]) for iso, datos in PAISES.items()),
                  key=lambda x: x[1])
