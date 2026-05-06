# ============================================================
# Servicio de autenticación — Login mágico por email
# ============================================================
# Flujo:
#   1. Cliente pone su email en /portal/login.
#   2. generar_token() crea token random, lo guarda en SESSIONS.
#   3. enviar_link_magico() manda email con tauro.app/portal/auth?token=X.
#   4. validar_token(token) devuelve el cliente (str) o None si inválido/expirado.
#   5. Endpoint setea cookie httponly con el token.
# ============================================================

import secrets
from datetime import datetime, timedelta
from typing import Optional

from core.sheets_client import _abrir_sheet


SESSION_DAYS = 7


def _now() -> datetime:
    return datetime.now()


def buscar_cliente_por_email(email: str) -> Optional[str]:
    """
    Busca en PERFILES y devuelve el ID del cliente (UPPERCASE) o None.

    Esquema flexible — soporta:
      - CLIENTE_ID (el actual de Tauro)
      - CLIENTE (esquema futuro)
      - NOMBRE_COMPLETO (fallback)
    Si no existe columna ACTIVO, asume que todos están activos.
    """
    email = email.strip().lower()
    sh = _abrir_sheet()
    hoja = sh.worksheet("PERFILES")
    rows = hoja.get_all_records()
    for r in rows:
        email_row = str(r.get("EMAIL", "")).strip().lower()
        if email_row != email:
            continue
        # Si hay columna ACTIVO, respetarla
        activo = str(r.get("ACTIVO", "TRUE")).strip().upper()
        if activo not in ("TRUE", "SI", "1", ""):
            continue
        cliente = r.get("CLIENTE_ID") or r.get("CLIENTE") or r.get("NOMBRE_COMPLETO") or ""
        return str(cliente).strip().upper()
    return None


def generar_token(email: str, cliente: str) -> str:
    """Crea token y lo guarda en SESSIONS. Devuelve el token."""
    token = secrets.token_urlsafe(32)
    creado = _now()
    expira = creado + timedelta(days=SESSION_DAYS)

    sh = _abrir_sheet()
    hoja = sh.worksheet("SESSIONS")
    hoja.append_row([
        token, email.lower(), cliente,
        creado.isoformat(timespec="seconds"),
        expira.isoformat(timespec="seconds"),
        "FALSE",  # USADO
    ], value_input_option="USER_ENTERED")
    return token


def _parse_fecha_flexible(s: str) -> Optional[datetime]:
    """Google Sheets a veces re-formatea fechas. Probamos varios formatos."""
    if not s:
        return None
    s = str(s).strip()
    formatos = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %-H:%M:%S",  # sin leading zero
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    ]
    for fmt in formatos:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # último intento: fromisoformat clásico
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def validar_token(token: str) -> Optional[str]:
    """Valida token. Si es válido y no expiró, devuelve cliente. Si no, None."""
    if not token or len(token) < 20:
        return None

    sh = _abrir_sheet()
    hoja = sh.worksheet("SESSIONS")
    rows = hoja.get_all_records()
    ahora = _now()

    for r in rows:
        if r.get("TOKEN") != token:
            continue
        expira = _parse_fecha_flexible(r.get("EXPIRA", ""))
        if not expira:
            return None
        if ahora > expira:
            return None
        return str(r.get("CLIENTE", "")).strip().upper() or None

    return None


def revocar_token(token: str) -> bool:
    """Marca un token como USADO=TRUE (logout)."""
    sh = _abrir_sheet()
    hoja = sh.worksheet("SESSIONS")
    cell = hoja.find(token)
    if not cell:
        return False
    # Columna USADO es la 6
    hoja.update_cell(cell.row, 6, "TRUE")
    return True


def link_magico_url(base_url: str, token: str) -> str:
    """URL que va en el email del login mágico."""
    return f"{base_url.rstrip('/')}/portal/auth?token={token}"


def get_markup_pct(cliente: str, default: float = 25.0) -> float:
    """
    Lee MARKUP_PCT del cliente desde PERFILES.
    Si no existe la columna o el valor está vacío/inválido, devuelve default (25%).
    """
    cliente = cliente.strip().upper()
    try:
        sh = _abrir_sheet()
        hoja = sh.worksheet("PERFILES")
        rows = hoja.get_all_records()
        for r in rows:
            cliente_row = (
                r.get("CLIENTE_ID") or r.get("CLIENTE") or r.get("NOMBRE_COMPLETO") or ""
            )
            if str(cliente_row).strip().upper() != cliente:
                continue
            valor = r.get("MARKUP_PCT", "")
            if valor in (None, "", " "):
                return default
            try:
                return float(str(valor).replace(",", "."))
            except (ValueError, TypeError):
                return default
    except Exception as e:
        print(f"[auth] Error leyendo MARKUP_PCT: {e}")
    return default
