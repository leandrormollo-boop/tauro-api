# ============================================================
# TOTP (RFC 6238) — segundo factor del admin, sólo stdlib
# ============================================================
# Sin dependencias nuevas a propósito: HMAC-SHA1 + base32, que es exactamente
# lo que hablan Google Authenticator, 1Password, Authy, etc.
#
# Se activa SOLO si ADMIN_TOTP_SECRET está cargada en Railway (generarla con
# scripts/generar_totp_admin.py). Sin la variable, el login del admin queda
# como siempre — imposible dejar al dueño afuera por accidente.
# ============================================================
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

PASO_SEGUNDOS = 30
DIGITOS = 6

# Anti-replay POR PASO DE TIEMPO, no por string. Un código vale ~90s (paso
# actual ±1 de tolerancia de reloj); guardar sólo "el último string aceptado"
# dejaba reusar un código dentro de su ventana apenas otro distinto lo pisara.
# Guardamos el número de paso ya consumido y no aceptamos ninguno <= ese: un
# código nunca entra dos veces, ni siquiera intercalado. En memoria del
# proceso (1 worker), igual que el rate limit.
_ultimo_paso: int | None = None


def _decodificar_secreto(secreto_b32: str) -> bytes:
    limpio = (secreto_b32 or "").strip().replace(" ", "").upper()
    faltante = (8 - len(limpio) % 8) % 8
    return base64.b32decode(limpio + "=" * faltante)


def _codigo(secreto_b32: str, momento: int) -> str:
    clave = _decodificar_secreto(secreto_b32)
    contador = struct.pack(">Q", momento // PASO_SEGUNDOS)
    resumen = hmac.new(clave, contador, hashlib.sha1).digest()
    offset = resumen[-1] & 0x0F
    numero = struct.unpack(">I", resumen[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(numero % (10 ** DIGITOS)).zfill(DIGITOS)


def paso_valido(secreto_b32: str, codigo: str, ventana: int = 1) -> int | None:
    """
    Devuelve el NÚMERO DE PASO de tiempo que matchea el código (o None si no
    matchea ninguno en la ventana). PURA: no toca estado global. Comparación
    de tiempo constante. El anti-replay lo decide el llamador con este número.
    """
    codigo = (codigo or "").strip().replace(" ", "")
    if len(codigo) != DIGITOS or not codigo.isdigit():
        return None
    try:
        ahora = int(time.time())
        matcheado = None
        # Recorre TODA la ventana (sin cortar en el primer match) para que el
        # tiempo no dependa de en qué paso cayó el código.
        for i in range(-ventana, ventana + 1):
            momento = ahora + i * PASO_SEGUNDOS
            if hmac.compare_digest(_codigo(secreto_b32, momento), codigo):
                matcheado = momento // PASO_SEGUNDOS
        return matcheado
    except Exception:
        # Secreto malformado en la env var: NO abrir la puerta por eso.
        return None


def verificar_codigo(secreto_b32: str, codigo: str, ventana: int = 1) -> bool:
    """
    Verifica y CONSUME el código: True si es válido y su paso no fue usado.
    Muta `_ultimo_paso`, así que sólo debe llamarse cuando el resto del login
    ya es correcto (para no 'quemar' un código en un intento con contraseña
    equivocada). Para verificar sin consumir, usar paso_valido().
    """
    global _ultimo_paso
    paso = paso_valido(secreto_b32, codigo, ventana)
    if paso is None:
        return False
    if _ultimo_paso is not None and paso <= _ultimo_paso:
        return False        # replay: ese paso (o uno anterior) ya se usó
    _ultimo_paso = paso
    return True


def generar_secreto() -> str:
    """Secreto base32 de 160 bits (el estándar de las apps autenticadoras)."""
    import secrets as _s
    return base64.b32encode(_s.token_bytes(20)).decode("ascii").rstrip("=")


def uri_otpauth(secreto_b32: str, cuenta: str = "admin@taurosolutions.ar",
                emisor: str = "TAURO Solutions") -> str:
    """URI para cargar la cuenta en Google Authenticator / 1Password."""
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(emisor)}:{quote(cuenta)}"
            f"?secret={secreto_b32}&issuer={quote(emisor)}"
            f"&algorithm=SHA1&digits={DIGITOS}&period={PASO_SEGUNDOS}")
