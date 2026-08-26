"""Transporte único y observable para correos transaccionales de TAURO.

El contrato es deliberadamente pequeño: acepta texto + HTML ya renderizados y
devuelve un resultado seguro. Nunca propaga credenciales, destinatarios ni el
contenido del mensaje a logs o respuestas públicas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid, parseaddr
import hashlib
import os
import re
import smtplib
import socket
import ssl


_EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+$", re.IGNORECASE)
OPERATIONS_EMAIL = "operaciones@taurosolutions.ar"


def canonical_email_address(value: str) -> str:
    """Devuelve un addr-spec simple o vacío; nunca reinterpreta display names.

    La misma identidad canónica se usa para rate-limit, persistencia y SMTP.
    Así, variantes con comentarios o ``Nombre <mail>`` no pueden representar
    el mismo buzón con claves distintas.
    """
    raw = (value or "").strip()
    if not raw or len(raw) > 254 or not _EMAIL_RE.fullmatch(raw):
        return ""
    if raw.count("@") != 1:
        return ""
    local, domain = raw.rsplit("@", 1)
    if not local or len(local) > 64 or local.startswith(".") or local.endswith("."):
        return ""
    if ".." in local or domain.startswith(".") or domain.endswith("."):
        return ""
    labels = domain.split(".")
    if len(labels) < 2 or len(labels[-1]) < 2:
        return ""
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?", label, re.IGNORECASE)
        for label in labels
    ):
        return ""
    return f"{local.lower()}@{domain.lower()}"


@dataclass(frozen=True)
class EmailDeliveryResult:
    accepted: bool
    code: str
    retryable: bool = False
    message_id: str = ""


def email_config_status() -> dict[str, object]:
    """Diagnóstico sin secretos para el panel de administración."""
    login = (os.getenv("EMAIL_REMITENTE") or "").strip()
    password = os.getenv("EMAIL_PASSWORD") or ""
    visible = (os.getenv("EMAIL_FROM") or "").strip()
    _nombre, visible_address = parseaddr(visible)
    visible_canonico = canonical_email_address(visible_address)
    return {
        # Es un preflight de variables, no un health check contra SMTP. Exigir
        # también el remitente visible evita mostrar verde cuando Gmail no
        # tiene una identidad utilizable para el mensaje.
        "configured": bool(
            login and password and _EMAIL_RE.fullmatch(login)
            and visible_canonico == OPERATIONS_EMAIL
        ),
        "sender_address": visible_canonico,
        "sender_domain": (
            visible_address.rsplit("@", 1)[-1].lower()
            if "@" in visible_address
            else (login.rsplit("@", 1)[-1].lower() if "@" in login else "")
        ),
        "host": (os.getenv("EMAIL_SMTP_HOST") or "smtp.gmail.com").strip(),
        "port": _smtp_port(),
    }


def _smtp_port() -> int:
    try:
        port = int((os.getenv("EMAIL_SMTP_PORT") or "587").strip())
    except (TypeError, ValueError):
        return 587
    return port if 1 <= port <= 65535 else 587


def _smtp_timeout() -> float:
    try:
        timeout = float((os.getenv("EMAIL_SMTP_TIMEOUT") or "15").strip())
    except (TypeError, ValueError):
        return 15.0
    return min(max(timeout, 3.0), 30.0)


def _recipient_address(value: str) -> str:
    return canonical_email_address(value)


def operations_visible_sender() -> str:
    """Identidad visible obligatoria de todo correo transaccional TAURO."""
    raw = (os.getenv("EMAIL_FROM") or "").strip()
    name, address = parseaddr(raw)
    if (
        canonical_email_address(address) == OPERATIONS_EMAIL
        and "\r" not in raw
        and "\n" not in raw
    ):
        return formataddr((name or "TAURO Operaciones", OPERATIONS_EMAIL))
    return ""


def _message_id(dedupe_key: str = "") -> str:
    domain = (os.getenv("EMAIL_MESSAGE_ID_DOMAIN") or "taurosolutions.ar").strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+", domain):
        domain = "taurosolutions.ar"
    if dedupe_key:
        digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]
        return f"<tauro.{digest}@{domain}>"
    return make_msgid(domain=domain)


def send_transactional_email(
    *,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
    reply_to: str = "",
    dedupe_key: str = "",
) -> EmailDeliveryResult:
    """Envía un multipart/alternative y confirma aceptación SMTP.

    ``accepted`` significa que el servidor SMTP aceptó el mensaje. No implica
    que el proveedor del destinatario lo haya colocado en la bandeja principal.
    """
    login = (os.getenv("EMAIL_REMITENTE") or "").strip()
    password = os.getenv("EMAIL_PASSWORD") or ""
    if not login or not password or not _EMAIL_RE.fullmatch(login):
        return EmailDeliveryResult(False, "SMTP_NOT_CONFIGURED")
    visible_sender = operations_visible_sender()
    if not visible_sender:
        return EmailDeliveryResult(False, "SMTP_SENDER_NOT_CONFIGURED")

    address = _recipient_address(recipient)
    if not address:
        return EmailDeliveryResult(False, "INVALID_RECIPIENT")
    if not subject or "\r" in subject or "\n" in subject:
        return EmailDeliveryResult(False, "INVALID_SUBJECT")
    if not text_body.strip() or not html_body.strip():
        return EmailDeliveryResult(False, "EMPTY_CONTENT")

    message_id = _message_id(dedupe_key)
    msg = EmailMessage()
    msg["From"] = visible_sender
    msg["To"] = address
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = message_id

    reply_address = _recipient_address(reply_to or os.getenv("EMAIL_REPLY_TO", ""))
    if reply_address:
        msg["Reply-To"] = reply_address

    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    host = (os.getenv("EMAIL_SMTP_HOST") or "smtp.gmail.com").strip()
    try:
        with smtplib.SMTP(host, _smtp_port(), timeout=_smtp_timeout()) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(login, password)
            refused = server.send_message(msg, from_addr=login, to_addrs=[address])
        if refused:
            return EmailDeliveryResult(False, "SMTP_RECIPIENT_REFUSED", False, message_id)
        return EmailDeliveryResult(True, "ACCEPTED", False, message_id)
    except smtplib.SMTPAuthenticationError:
        return EmailDeliveryResult(False, "SMTP_AUTH_FAILED", False, message_id)
    except smtplib.SMTPRecipientsRefused:
        return EmailDeliveryResult(False, "SMTP_RECIPIENT_REFUSED", False, message_id)
    except smtplib.SMTPResponseException as exc:
        retryable = 400 <= int(getattr(exc, "smtp_code", 0) or 0) < 500
        return EmailDeliveryResult(
            False,
            "SMTP_TEMPORARY" if retryable else "SMTP_REJECTED",
            retryable,
            message_id,
        )
    except (TimeoutError, socket.timeout):
        return EmailDeliveryResult(False, "SMTP_TIMEOUT", True, message_id)
    except (ConnectionError, OSError, smtplib.SMTPServerDisconnected):
        return EmailDeliveryResult(False, "SMTP_NETWORK", True, message_id)
    except Exception:
        return EmailDeliveryResult(False, "SMTP_ERROR", False, message_id)
