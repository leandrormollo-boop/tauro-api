"""Catálogo canónico de jurisdicciones argentinas para envíos nacionales.

El portal guarda un código interno estable y cada adapter (OCA, Andreani,
etc.) lo traducirá al valor que exija su API. Nunca se compara texto libre de
la pantalla directamente contra un courier.
"""
from __future__ import annotations

import re
import unicodedata


# Código de jurisdicción usado también como primera letra del CPA.
PROVINCIAS = {
    "C": "Ciudad Autónoma de Buenos Aires",
    "B": "Buenos Aires",
    "K": "Catamarca",
    "H": "Chaco",
    "U": "Chubut",
    "X": "Córdoba",
    "W": "Corrientes",
    "E": "Entre Ríos",
    "P": "Formosa",
    "Y": "Jujuy",
    "L": "La Pampa",
    "F": "La Rioja",
    "M": "Mendoza",
    "N": "Misiones",
    "Q": "Neuquén",
    "R": "Río Negro",
    "A": "Salta",
    "J": "San Juan",
    "D": "San Luis",
    "Z": "Santa Cruz",
    "S": "Santa Fe",
    "G": "Santiago del Estero",
    "V": "Tierra del Fuego",
    "T": "Tucumán",
}


def _clave(valor) -> str:
    texto = str(valor or "").strip()
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", texto).casefold().split())


_POR_NOMBRE = {_clave(nombre): codigo for codigo, nombre in PROVINCIAS.items()}
_ALIASES = {
    "caba": "C",
    "capital": "C",
    "capital federal": "C",
    "ciudad de buenos aires": "C",
    "ciudad autonoma buenos aires": "C",
    "provincia de buenos aires": "B",
    "pcia buenos aires": "B",
    "pcia de buenos aires": "B",
    "tierra del fuego antartida e islas del atlantico sur": "V",
}


def normalizar_provincia(valor) -> str:
    """Devuelve el código canónico o ``""`` si la provincia no existe."""
    texto = str(valor or "").strip().upper()
    if texto.startswith("AR-"):
        texto = texto[3:]
    if texto in PROVINCIAS:
        return texto
    clave = _clave(valor)
    return _POR_NOMBRE.get(clave) or _ALIASES.get(clave, "")


def nombre_provincia(valor) -> str:
    codigo = normalizar_provincia(valor)
    return PROVINCIAS.get(codigo, "")


def opciones() -> list[tuple[str, str]]:
    """Opciones ordenadas para los dos desplegables del cotizador."""
    return sorted(PROVINCIAS.items(), key=lambda item: item[1])


def descomponer_codigo_postal(valor, provincia=None) -> dict | None:
    """Normaliza un CP de 4 dígitos o un CPA oficial de 8 caracteres.

    Los adapters nacionales reciben el CP numérico de cuatro dígitos. El CPA
    completo se conserva como evidencia, pero nunca se manda entero a OCA o
    Andreani. No se aceptan abreviaturas como ``C1425``: no son un CPA oficial
    y podrían ocultar un dato incompleto antes de crear una guía.
    """
    codigo = re.sub(r"[\s-]+", "", str(valor or "").strip().upper())
    es_cp4 = bool(re.fullmatch(r"\d{4}", codigo))
    es_cpa8 = bool(re.fullmatch(r"[A-Z]\d{4}[A-Z]{3}", codigo))
    if not (es_cp4 or es_cpa8):
        return None

    provincia_codigo = normalizar_provincia(provincia) if provincia else ""
    if es_cpa8 and provincia_codigo and codigo[0] != provincia_codigo:
        return None

    return {
        "cp_input": codigo,
        "cp4": codigo if es_cp4 else codigo[1:5],
        "cpa8": codigo if es_cpa8 else None,
    }


def normalizar_codigo_postal(valor, provincia=None) -> str:
    """Compatibilidad: devuelve el CP/CPA normalizado o ``""`` si falla."""
    partes = descomponer_codigo_postal(valor, provincia)
    return partes["cp_input"] if partes else ""


def normalizar_localidad(valor) -> str:
    """Limpia espacios y rechaza texto vacío, de control o excesivo."""
    localidad = " ".join(str(valor or "").strip().split())
    if not 2 <= len(localidad) <= 100:
        return ""
    if any(ord(c) < 32 for c in localidad) or "<" in localidad or ">" in localidad:
        return ""
    return localidad
