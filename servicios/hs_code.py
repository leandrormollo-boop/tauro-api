"""Búsqueda asistida HS6 sobre referencia versionada; nunca aprueba aduana.

El ranking es léxico, no una probabilidad ni una decisión arancelaria. No hay
llamadas a terceros, aprendizaje entre clientes ni sufijos nacionales inventados.
"""
from collections import Counter
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import unicodedata

BASE = Path(__file__).resolve().parents[1] / "datos" / "hs2022.json"
VERSION_MOTOR = "hs-search-1"

# Vocabulario de búsqueda, no reglas de clasificación. Los textos de referencia
# permanecen en su idioma original y cada resultado conserva su código real.
SINONIMOS = {
    "remera": "t-shirt", "camiseta": "t-shirt", "camisa": "shirt",
    "pantalon": "trousers", "jean": "denim trousers", "vestido": "dress",
    "buzo": "sweatshirt", "sueter": "pullover", "tejido de punto": "knitted",
    "tejido plano": "woven", "algodon": "cotton", "poliester": "synthetic fibre",
    "lana": "wool", "seda": "silk", "lino": "flax", "cuero": "leather",
    "sintetico": "synthetic", "plastico": "plastic", "madera": "wood",
    "acero inoxidable": "stainless steel", "acero": "steel", "aluminio": "aluminium",
    "cobre": "copper", "vidrio": "glass", "ceramica": "ceramic",
    "bolso": "handbag", "cartera": "handbag", "mochila": "rucksack",
    "valija": "suitcase", "calzado": "footwear", "zapato": "footwear",
    "zapatilla": "sports footwear", "sandalia": "sandal",
    "reel": "fishing reel", "carrete de pesca": "fishing reel",
    "cana de pescar": "fishing rod", "cana de pesca": "fishing rod",
    "anzuelo": "fish hook", "senuelo": "fishing tackle",
    "celular": "smartphone", "telefono inteligente": "smartphone",
    "computadora": "computer", "portatil": "portable", "bateria": "battery",
    "litio": "lithium", "recargable": "accumulator", "juguete": "toy", "pelota": "ball",
    "tornillo": "screw", "tuerca": "nut", "herramienta": "tool",
    "rodamiento": "bearing", "motor": "motor", "electrico": "electric",
    "cafe": "coffee", "tostado": "roasted", "molido": "ground",
    "hombre": "men", "mujer": "women", "bebe": "baby", "nino": "children",
}
STOP = set("a an and or of for the with in to not other than whether including from by all de del la el los las un una para con en y o sin por producto product merchandise mercaderia mercancia articulo articles new nuevo nueva uso use made fabricado fabricada ciento percent pesca".split())


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto).lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _singular(token):
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "ous")) and len(token) > 3:
        return token[:-1]
    return token


def _tokens(texto, traducir=False):
    texto = normalizar(texto)
    texto = re.sub(r"\bt[ -]?shirts?\b", "tshirt", texto)
    if traducir:
        # Plurales españoles corrientes, antes de expandir frases completas.
        texto = re.sub(r"\b(remeras|camisetas|camisas|carteras|mochilas|reels|zapatos|baterias|juguetes|recargables)\b", lambda m: m[0][:-1], texto)
        for original, ingles in sorted(SINONIMOS.items(), key=lambda p: -len(p[0])):
            texto = re.sub(r"\b" + re.escape(original) + r"\b", ingles, texto)
    texto = re.sub(r"\bt[ -]?shirts?\b", "tshirt", texto)
    texto = texto.replace("man-made", "synthetic").replace("polyester", "synthetic").replace('fibers', 'fibres')
    texto = texto.replace("rechargeable", "accumulator").replace("batteries", "battery")
    return {_singular(t) for t in re.findall(r"[a-z]{2,}", texto) if t not in STOP}


@lru_cache(maxsize=1)
def _indice():
    data = json.loads(BASE.read_text())
    if data.get("edition") != "HS2022":
        raise ValueError("Versión HS no soportada")
    entries = data["entries"]
    codes = {r["code"]: r for r in entries}
    if len(codes) != len(entries) or not 5500 <= len(codes) <= 6000:
        raise ValueError("Referencia HS incompleta o duplicada")
    if any(not re.fullmatch(r"[0-9]{6}", c) or int(c[:2]) >= 98 for c in codes):
        raise ValueError("Referencia HS no internacional")
    documents = [(r, _tokens(r["description"])) for r in entries]
    frequency = Counter(t for _, tokens in documents for t in tokens)
    idf = {t: math.log(1 + len(entries) / (1 + n)) for t, n in frequency.items()}
    return data, codes, documents, idf


def formato_hs(valor):
    """Admite HS6 y extensiones existentes de 8/10, sin agregar dígitos."""
    v = str(valor).strip()
    if not re.fullmatch(r"(?:[0-9]{6}|[0-9]{8}|[0-9]{10}|[0-9]{4}\.[0-9]{2}(?:\.[0-9]{2}(?:\.[0-9]{2})?)?)", v):
        raise ValueError("HS inválido: usá 6, 8 o 10 dígitos (ej.: 6109.10).")
    d = v.replace(".", "")
    return d[:4] + "." + ".".join(d[i:i+2] for i in range(4, len(d), 2))


def _preguntas(tokens):
    preguntas = []
    if tokens & {"shirt", "tshirt", "trouser", "dress", "sweatshirt", "pullover", "apparel", "clothe"}:
        if not tokens & {"cotton", "wool", "silk", "synthetic", "polyester", "flax"}:
            preguntas.append("¿Cuál es la composición textil y el porcentaje de cada fibra?")
        if not tokens & {"knitted", "crocheted", "woven"}:
            preguntas.append("¿Es tejido de punto o tejido plano?")
        if len(tokens & {"cotton", "wool", "silk", "synthetic", "flax"}) > 1:
            preguntas.append("Indicá los porcentajes de la mezcla de fibras.")
        if tokens & {"shirt", "trouser", "dress"} and not tokens & {"men", "women", "boy", "girl", "children", "baby"}:
            preguntas.append("¿Es para hombre, mujer o bebé? Ese detalle puede cambiar la subpartida.")
    if tokens & {"handbag", "rucksack", "suitcase"}:
        preguntas.append("Confirmá el material de la superficie exterior: cuero, plástico o textil.")
    if tokens & {"footwear", "sandal"}:
        preguntas.append("Indicá por separado el material de la suela y del exterior del calzado.")
    if tokens & {"part", "accessory", "spare"}:
        preguntas.append("¿De qué equipo es la pieza y qué función cumple? Indicá marca/modelo si ayuda a identificarla.")
    if tokens & {"battery", "batterie"}:
        preguntas.append("¿Cuál es su composición química? ¿Es recargable y va suelta o integrada al equipo?")
    return preguntas


def sugerir_hs(descripcion, detalle=""):
    if not isinstance(descripcion, str) or not 3 <= len(descripcion.strip()) <= 1000:
        raise ValueError("Describí el producto entre 3 y 1.000 caracteres.")
    if not isinstance(detalle, str) or len(detalle) > 700:
        raise ValueError("El detalle admite hasta 700 caracteres.")
    data, codes, documents, idf = _indice()
    query = (descripcion.strip() + " " + detalle.strip()).strip()
    tokens = _tokens(query, traducir=True)
    preguntas = _preguntas(tokens)
    response = dict(edition=data["edition"], source=data["source"], source_url=data["source_index"],
                    retrieved_at=data["retrieved_at"], engine=VERSION_MOTOR,
                    requires_confirmation=True, candidates=[], questions=preguntas)
    # Una consulta de código devuelve su referencia, no certifica su uso.
    digits = descripcion.strip().replace(".", "")
    if re.fullmatch(r"[0-9]{6}", digits) and digits in codes:
        ranked = [(1, codes[digits], set())]
    else:
        meaningful = tokens - {"cotton", "wool", "synthetic", "polyester", "plastic", "leather", "steel", "wood", "men", "women", "children", "knitted", "woven"}
        if not meaningful:
            ranked = []
        else:
            ranked = []
            weight = sum(idf.get(t, 5) for t in tokens) or 1
            for row, words in documents:
                matched = tokens & words
                if not matched & meaningful:
                    continue
                coverage = sum(idf.get(t, 0) for t in matched) / weight
                if coverage < 0.45:
                    continue
                score = sum(idf[t] for t in matched) * coverage / (1 + .012 * len(words))
                # Una mención excluida no debe ganar por coincidir textualmente.
                leaf = row.get('path', [row['description']])[-1]
                excluded = re.findall(r"(?:other than|excluding|not) ([^;.)]+)", normalizar(row["description"]))
                if any(_tokens(part) & tokens for part in excluded):
                    score *= .18
                if 'woven' in tokens and 'knitted' in words:
                    score *= 2 if 'not knitted' in row['description'].lower() else .15
                # Intención expresada: la denominación principal pesa más que
                # menciones accesorias (p. ej. objetos que se llevan EN carteras).
                if 'handbag' in tokens and row['code'].startswith('42022'):
                    score *= 3
                if {'fishing', 'reel'} <= tokens and row['code'] == '950730':
                    score *= 3
                if 'accumulator' in tokens and row['code'].startswith('8507'):
                    score *= 3
                score *= 1 + len(_tokens(leaf) & tokens)
                ranked.append((score, row, matched))
            ranked.sort(key=lambda r: (-r[0], r[1]["code"]))
            if ranked:
                ranked = [r for r in ranked if r[0] >= ranked[0][0] * .35]
    for _, row, matched in ranked[:4]:
        response["candidates"].append(dict(code=row["code"], formatted=formato_hs(row["code"]),
            description=row["description"], summary='; '.join(row.get('path', [row['description']])[-2:]),
            matched_terms=sorted(matched), source="reference"))
    if not response["candidates"]:
        response.update(status="needs_details", message="Necesitamos más detalle para buscar una clasificación útil.")
        if not preguntas:
            preguntas.append("Indicá qué producto es, de qué está hecho y para qué se usa. Evitá descripciones como ‘ropa’, ‘repuestos’ o ‘muestras’.")
    else:
        response.update(status="suggestions", message="Posibles códigos HS de 6 dígitos. Compará la descripción antes de elegir.")
    return response
