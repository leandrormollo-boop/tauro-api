"""Acuse de guardado para descartar sólo el borrador que creó la operación.

El token no autoriza ni identifica una operación en la base. Es un UUID
aleatorio de la pestaña, sin datos del cliente ni importes.
"""
from uuid import UUID
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def confirmar_borrador(url: str, token: str) -> str:
    try:
        token = str(UUID(token))
    except (ValueError, TypeError, AttributeError):
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("borrador_listo", token))
    return urlunsplit(parts._replace(query=urlencode(query)))
