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

Los destinos frecuentes traen una ciudad y un CP de REFERENCIA. Sirven SOLO
para precargar una estimación: el cliente puede corregirlos y, para el resto
de los países, debe escribirlos. En un envío real siempre va la dirección del
remitente y del destinatario, porque los recargos por zona remota dependen
del CP exacto (ver `_destino_para_cotizar` en cotizador.py).
"""
from __future__ import annotations

import re
import unicodedata

from servicios.paises_iso import NOMBRES_ISO

# Referencias únicamente para precargar el cotizador. No definen cobertura.
# Si un país no está acá, el formulario pide ciudad y código postal reales.
_REFERENCIAS_PAISES = {
    "AR": ("BUENOS AIRES", "1043"),
    "US": ("MIAMI", "33101"),
    "CN": ("SHANGHAI", "200000"),
    "IN": ("MUMBAI", "400001"),
    "BD": ("DHAKA", "1000"),
    "VN": ("HO CHI MINH", "700000"),
    "PK": ("KARACHI", "74000"),
    "TH": ("BANGKOK", "10100"),
    "ID": ("JAKARTA", "10110"),
    "TR": ("ISTANBUL", "34000"),
    "KR": ("SEOUL", "04524"),
    "JP": ("TOKYO", "100-0001"),
    "HK": ("HONG KONG", ""),
    "TW": ("TAIPEI", "100"),
    "AE": ("DUBAI", ""),
    "ES": ("MADRID", "28001"),
    "IT": ("MILANO", "20121"),
    "DE": ("BERLIN", "10115"),
    "FR": ("PARIS", "75001"),
    "PT": ("LISBOA", "1000-001"),
    "NL": ("AMSTERDAM", "1011"),
    "GB": ("LONDON", "EC1A 1BB"),
    "BR": ("SAO PAULO", "01310100"),
    "CL": ("SANTIAGO", "8320000"),
    "UY": ("MONTEVIDEO", "11000"),
    "PY": ("ASUNCION", "1209"),
    "BO": ("LA PAZ", ""),
    "PE": ("LIMA", "15001"),
    "EC": ("QUITO", "170101"),
    "CO": ("BOGOTA", "110111"),
    "MX": ("CIUDAD DE MEXICO", "06600"),
    "CA": ("TORONTO", "M5H 2N2"),
    "AU": ("SYDNEY", "2000"),
    "IL": ("TEL AVIV", "6100000"),
    "ZA": ("JOHANNESBURG", "2000"),
}

# Capital (o ciudad principal) + un CP real de esa ciudad, SOLO para que el
# cotizador PÚBLICO —que no pide dirección— pueda pedirle tarifa al courier
# a cualquier país. No precarga el portal: ahí el cliente escribe la ciudad
# y el CP reales (ver referencia()). Antes, 214 países figuraban en el
# desplegable sin referencia y la cotización moría con "No se pudo obtener
# tarifas" (Polonia, Arabia Saudita, 08/10/2026). CP vacío = país sin código
# postal, o sin uno fiable: el courier decide.
_REFERENCIAS_ESTIMACION = {
    "AD": ("ANDORRA LA VELLA", "AD500"),
    "AF": ("KABUL", "1001"),
    "AG": ("ST JOHNS", ""),
    "AI": ("THE VALLEY", "AI-2640"),
    "AL": ("TIRANA", "1001"),
    "AM": ("YEREVAN", "0010"),
    "AO": ("LUANDA", ""),
    "AQ": ("MCMURDO STATION", ""),
    "AS": ("PAGO PAGO", "96799"),
    "AT": ("WIEN", "1010"),
    "AW": ("ORANJESTAD", ""),
    "AX": ("MARIEHAMN", "22100"),
    "AZ": ("BAKU", "AZ1000"),
    "BA": ("SARAJEVO", "71000"),
    "BB": ("BRIDGETOWN", "BB11000"),
    "BE": ("BRUSSELS", "1000"),
    "BF": ("OUAGADOUGOU", ""),
    "BG": ("SOFIA", "1000"),
    "BH": ("MANAMA", ""),
    "BI": ("BUJUMBURA", ""),
    "BJ": ("COTONOU", ""),
    "BL": ("GUSTAVIA", "97133"),
    "BM": ("HAMILTON", "HM 11"),
    "BN": ("BANDAR SERI BEGAWAN", "BS8711"),
    "BQ": ("KRALENDIJK", ""),
    "BS": ("NASSAU", ""),
    "BT": ("THIMPHU", "11001"),
    "BV": ("BOUVET ISLAND", ""),
    "BW": ("GABORONE", ""),
    "BY": ("MINSK", "220030"),
    "BZ": ("BELIZE CITY", ""),
    "CC": ("WEST ISLAND", "6799"),
    "CD": ("KINSHASA", ""),
    "CF": ("BANGUI", ""),
    "CG": ("BRAZZAVILLE", ""),
    "CH": ("ZURICH", "8001"),
    "CI": ("ABIDJAN", ""),
    "CK": ("AVARUA", ""),
    "CM": ("DOUALA", ""),
    "CR": ("SAN JOSE", "10101"),
    "CU": ("LA HABANA", "10100"),
    "CV": ("PRAIA", "7600"),
    "CW": ("WILLEMSTAD", ""),
    "CX": ("FLYING FISH COVE", "6798"),
    "CY": ("NICOSIA", "1010"),
    "CZ": ("PRAHA", "11000"),
    "DJ": ("DJIBOUTI", ""),
    "DK": ("COPENHAGEN", "1050"),
    "DM": ("ROSEAU", ""),
    "DO": ("SANTO DOMINGO", "10101"),
    "DZ": ("ALGIERS", "16000"),
    "EE": ("TALLINN", "10111"),
    "EG": ("CAIRO", "11511"),
    "EH": ("LAAYOUNE", "70000"),
    "ER": ("ASMARA", ""),
    "ET": ("ADDIS ABABA", "1000"),
    "FI": ("HELSINKI", "00100"),
    "FJ": ("SUVA", ""),
    "FK": ("STANLEY", "FIQQ 1ZZ"),
    "FM": ("PALIKIR", "96941"),
    "FO": ("TORSHAVN", "100"),
    "GA": ("LIBREVILLE", ""),
    "GD": ("ST GEORGES", ""),
    "GE": ("TBILISI", "0105"),
    "GF": ("CAYENNE", "97300"),
    "GG": ("ST PETER PORT", "GY1 1AA"),
    "GH": ("ACCRA", ""),
    "GI": ("GIBRALTAR", "GX11 1AA"),
    "GL": ("NUUK", "3900"),
    "GM": ("BANJUL", ""),
    "GN": ("CONAKRY", ""),
    "GP": ("POINTE-A-PITRE", "97110"),
    "GQ": ("MALABO", ""),
    "GR": ("ATHENS", "10431"),
    "GS": ("KING EDWARD POINT", "SIQQ 1ZZ"),
    "GT": ("GUATEMALA CITY", "01001"),
    "GU": ("HAGATNA", "96910"),
    "GW": ("BISSAU", "1000"),
    "GY": ("GEORGETOWN", ""),
    "HM": ("HEARD ISLAND", ""),
    "HN": ("TEGUCIGALPA", "11101"),
    "HR": ("ZAGREB", "10000"),
    "HT": ("PORT-AU-PRINCE", "HT6110"),
    "HU": ("BUDAPEST", "1051"),
    "IE": ("DUBLIN", ""),
    "IM": ("DOUGLAS", "IM1 1AA"),
    "IO": ("DIEGO GARCIA", "BBND 1ZZ"),
    "IQ": ("BAGHDAD", "10001"),
    "IR": ("TEHRAN", ""),
    "IS": ("REYKJAVIK", "101"),
    "JE": ("ST HELIER", "JE2 3AA"),
    "JM": ("KINGSTON", ""),
    "JO": ("AMMAN", "11118"),
    "KE": ("NAIROBI", "00100"),
    "KG": ("BISHKEK", "720001"),
    "KH": ("PHNOM PENH", "120101"),
    "KI": ("TARAWA", ""),
    "KM": ("MORONI", ""),
    "KN": ("BASSETERRE", ""),
    "KP": ("PYONGYANG", ""),
    "KW": ("KUWAIT CITY", "13001"),
    "KY": ("GEORGE TOWN", "KY1-1001"),
    "KZ": ("ALMATY", "050000"),
    "LA": ("VIENTIANE", "01000"),
    "LB": ("BEIRUT", ""),
    "LC": ("CASTRIES", ""),
    "LI": ("VADUZ", "9490"),
    "LK": ("COLOMBO", "00100"),
    "LR": ("MONROVIA", "1000"),
    "LS": ("MASERU", "100"),
    "LT": ("VILNIUS", "01100"),
    "LU": ("LUXEMBOURG", "1009"),
    "LV": ("RIGA", "LV-1050"),
    "LY": ("TRIPOLI", ""),
    "MA": ("CASABLANCA", "20000"),
    "MC": ("MONACO", "98000"),
    "MD": ("CHISINAU", "MD-2001"),
    "ME": ("PODGORICA", "81000"),
    "MF": ("MARIGOT", "97150"),
    "MG": ("ANTANANARIVO", "101"),
    "MH": ("MAJURO", "96960"),
    "MK": ("SKOPJE", "1000"),
    "ML": ("BAMAKO", ""),
    "MM": ("YANGON", "11181"),
    "MN": ("ULAANBAATAR", "14200"),
    "MO": ("MACAU", ""),
    "MP": ("SAIPAN", "96950"),
    "MQ": ("FORT-DE-FRANCE", "97200"),
    "MR": ("NOUAKCHOTT", ""),
    "MS": ("BRADES", "MSR1110"),
    "MT": ("VALLETTA", "VLT 1117"),
    "MU": ("PORT LOUIS", "11328"),
    "MV": ("MALE", "20026"),
    "MW": ("LILONGWE", ""),
    "MY": ("KUALA LUMPUR", "50450"),
    "MZ": ("MAPUTO", "1100"),
    "NA": ("WINDHOEK", ""),
    "NC": ("NOUMEA", "98800"),
    "NE": ("NIAMEY", "8001"),
    "NF": ("KINGSTON", "2899"),
    "NG": ("LAGOS", "101241"),
    "NI": ("MANAGUA", ""),
    "NO": ("OSLO", "0150"),
    "NP": ("KATHMANDU", "44600"),
    "NR": ("YAREN", ""),
    "NU": ("ALOFI", ""),
    "NZ": ("AUCKLAND", "1010"),
    "OM": ("MUSCAT", "100"),
    "PA": ("PANAMA CITY", ""),
    "PF": ("PAPEETE", "98714"),
    "PG": ("PORT MORESBY", "121"),
    "PH": ("MANILA", "1000"),
    "PL": ("WARSZAWA", "00-001"),
    "PM": ("SAINT-PIERRE", "97500"),
    "PN": ("ADAMSTOWN", "PCRN 1ZZ"),
    "PR": ("SAN JUAN", "00901"),
    "PS": ("RAMALLAH", ""),
    "PW": ("KOROR", "96940"),
    "QA": ("DOHA", ""),
    "RE": ("SAINT-DENIS", "97400"),
    "RO": ("BUCHAREST", "010011"),
    "RS": ("BELGRADE", "11000"),
    "RU": ("MOSCOW", "101000"),
    "RW": ("KIGALI", ""),
    "SA": ("RIYADH", "11564"),
    "SB": ("HONIARA", ""),
    "SC": ("VICTORIA", ""),
    "SD": ("KHARTOUM", "11111"),
    "SE": ("STOCKHOLM", "11120"),
    "SG": ("SINGAPORE", "018956"),
    "SH": ("JAMESTOWN", "STHL 1ZZ"),
    "SI": ("LJUBLJANA", "1000"),
    "SJ": ("LONGYEARBYEN", "9170"),
    "SK": ("BRATISLAVA", "81101"),
    "SL": ("FREETOWN", ""),
    "SM": ("SAN MARINO", "47890"),
    "SN": ("DAKAR", ""),
    "SO": ("MOGADISHU", ""),
    "SR": ("PARAMARIBO", ""),
    "SS": ("JUBA", ""),
    "ST": ("SAO TOME", ""),
    "SV": ("SAN SALVADOR", "1101"),
    "SX": ("PHILIPSBURG", ""),
    "SY": ("DAMASCUS", ""),
    "SZ": ("MBABANE", "H100"),
    "TC": ("PROVIDENCIALES", "TKCA 1ZZ"),
    "TD": ("NDJAMENA", ""),
    "TF": ("PORT-AUX-FRANCAIS", ""),
    "TG": ("LOME", ""),
    "TJ": ("DUSHANBE", "734000"),
    "TK": ("FAKAOFO", ""),
    "TL": ("DILI", ""),
    "TM": ("ASHGABAT", "744000"),
    "TN": ("TUNIS", "1000"),
    "TO": ("NUKUALOFA", ""),
    "TT": ("PORT OF SPAIN", ""),
    "TV": ("FUNAFUTI", ""),
    "TZ": ("DAR ES SALAAM", "11101"),
    "UA": ("KYIV", "01001"),
    "UG": ("KAMPALA", ""),
    "UM": ("WAKE ISLAND", "96898"),
    "UZ": ("TASHKENT", "100000"),
    "VA": ("VATICAN CITY", "00120"),
    "VC": ("KINGSTOWN", "VC0100"),
    "VE": ("CARACAS", "1010"),
    "VG": ("ROAD TOWN", "VG1110"),
    "VI": ("CHARLOTTE AMALIE", "00802"),
    "VU": ("PORT VILA", ""),
    "WF": ("MATA-UTU", "98600"),
    "WS": ("APIA", ""),
    "YE": ("SANAA", ""),
    "YT": ("MAMOUDZOU", "97600"),
    "ZM": ("LUSAKA", "10101"),
    "ZW": ("HARARE", ""),
}

# Contrato histórico: ISO-2 → (nombre, ciudad de referencia, CP de referencia).
# Ahora contiene los 249 códigos ISO vigentes; ciudad/CP quedan vacíos cuando
# no existe una referencia operativa validada por TAURO.
PAISES = {
    iso: (nombre, *_REFERENCIAS_PAISES.get(iso, ("", "")))
    for iso, nombre in NOMBRES_ISO.items()
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
    "bangladesh": "BD",
    "emiratos arabes": "AE",
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


def referencia_estimacion(valor: str) -> dict:
    """
    Como referencia(), pero nunca vuelve sin ciudad: si el país no tiene una
    referencia validada, usa la capital. Es para la estimación pública, donde
    no hay dirección; un envío real sigue yendo con la dirección de verdad.
    """
    ref = referencia(valor)
    if not ref:
        return {}
    if ref.get("city"):
        return ref
    ciudad, cp = _REFERENCIAS_ESTIMACION.get(ref["country"], ("", ""))
    return {"city": ciudad, "postal_code": cp, "country": ref["country"]}


def opciones() -> list:
    """Para los desplegables: [(iso2, nombre), ...] ordenado por nombre."""
    return sorted(((iso, datos[0]) for iso, datos in PAISES.items()),
                  key=lambda x: x[1])


def referencias_formulario() -> dict[str, dict[str, str]]:
    """Datos seguros para precargar ciudad/CP, sin implicar cobertura DHL."""
    return {
        iso: {"city": ciudad, "postal_code": cp}
        for iso, (_nombre, ciudad, cp) in PAISES.items()
    }
