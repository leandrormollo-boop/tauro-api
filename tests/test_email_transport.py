from __future__ import annotations

import smtplib

from core import email_transport


class _SMTPFake:
    def __init__(self, host, port, timeout, *, error=None, refused=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.error = error
        self.refused = refused or {}
        self.message = None
        self.context = None
        self.logged = None
        self.ehlo_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        self.ehlo_calls += 1

    def starttls(self, *, context):
        self.context = context

    def login(self, user, password):
        self.logged = (user, password)
        if self.error:
            raise self.error

    def send_message(self, message, *, from_addr, to_addrs):
        self.message = message
        self.envelope = (from_addr, to_addrs)
        return self.refused


def _configurar(monkeypatch):
    monkeypatch.setenv("EMAIL_REMITENTE", "smtp@taurosolutions.ar")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_FROM", "TAURO Operaciones <operaciones@taurosolutions.ar>")
    monkeypatch.setenv("EMAIL_REPLY_TO", "operaciones@taurosolutions.ar")


def test_sin_credenciales_no_intenta_conectar(monkeypatch):
    monkeypatch.delenv("EMAIL_REMITENTE", raising=False)
    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    monkeypatch.setattr(
        email_transport.smtplib,
        "SMTP",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no conectar")),
    )

    result = email_transport.send_transactional_email(
        recipient="cliente@example.com",
        subject="Prueba",
        text_body="Texto",
        html_body="<p>Texto</p>",
    )

    assert result.accepted is False
    assert result.code == "SMTP_NOT_CONFIGURED"


def test_diagnostico_exige_remitente_visible_valido(monkeypatch):
    monkeypatch.setenv("EMAIL_REMITENTE", "smtp@taurosolutions.ar")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    assert email_transport.email_config_status()["configured"] is False

    monkeypatch.setenv("EMAIL_FROM", "TAURO Operaciones <operaciones@taurosolutions.ar>")
    estado = email_transport.email_config_status()
    assert estado["configured"] is True
    assert estado["sender_address"] == "operaciones@taurosolutions.ar"
    assert estado["sender_domain"] == "taurosolutions.ar"

    monkeypatch.setenv("EMAIL_FROM", "TAURO Cotizaciones <cotizaciones@taurosolutions.ar>")
    assert email_transport.email_config_status()["configured"] is False


def test_envia_multipart_con_tls_timeout_y_headers(monkeypatch):
    _configurar(monkeypatch)
    creado = {}

    def construir(host, port, timeout):
        creado["smtp"] = _SMTPFake(host, port, timeout)
        return creado["smtp"]

    monkeypatch.setattr(email_transport.smtplib, "SMTP", construir)
    result = email_transport.send_transactional_email(
        recipient="cliente@example.com",
        subject="Presupuesto TAURO",
        text_body="Versión texto",
        html_body="<html><body>Versión HTML</body></html>",
        dedupe_key="quote:ABC:cliente@example.com",
    )

    smtp = creado["smtp"]
    assert result.accepted is True
    assert result.code == "ACCEPTED"
    assert smtp.host == "smtp.gmail.com"
    assert smtp.port == 587
    assert smtp.timeout == 15.0
    assert smtp.context is not None
    assert smtp.ehlo_calls == 2
    assert smtp.logged == ("smtp@taurosolutions.ar", "secret")
    assert smtp.envelope == ("smtp@taurosolutions.ar", ["cliente@example.com"])
    assert smtp.message["From"] == "TAURO Operaciones <operaciones@taurosolutions.ar>"
    assert smtp.message["Reply-To"] == "operaciones@taurosolutions.ar"
    assert smtp.message["Date"]
    assert smtp.message["Message-ID"] == result.message_id
    assert smtp.message.is_multipart()
    assert smtp.message.get_body(preferencelist=("plain",)).get_content().strip() == "Versión texto"
    assert "Versión HTML" in smtp.message.get_body(preferencelist=("html",)).get_content()


def test_rechaza_destinatario_con_inyeccion_de_cabeceras(monkeypatch):
    _configurar(monkeypatch)
    result = email_transport.send_transactional_email(
        recipient="cliente@example.com\nBcc: atacante@example.com",
        subject="Prueba",
        text_body="Texto",
        html_body="<p>Texto</p>",
    )
    assert result.accepted is False
    assert result.code == "INVALID_RECIPIENT"


def test_no_envia_desde_un_alias_distinto_de_operaciones(monkeypatch):
    _configurar(monkeypatch)
    monkeypatch.setenv("EMAIL_FROM", "TAURO <cotizaciones@taurosolutions.ar>")
    monkeypatch.setattr(
        email_transport.smtplib,
        "SMTP",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no conectar")),
    )

    result = email_transport.send_transactional_email(
        recipient="cliente@example.com",
        subject="Prueba",
        text_body="Texto",
        html_body="<p>Texto</p>",
    )

    assert result.accepted is False
    assert result.code == "SMTP_SENDER_NOT_CONFIGURED"


def test_identidad_email_es_unica_y_no_reinterpreta_display_names_o_comentarios():
    assert email_transport.canonical_email_address(" CLIENTE@Example.COM ") == "cliente@example.com"
    for variante in (
        "victim@example.com(foo1)",
        "victim@example.com(foo2)",
        "name1 <victim@example.com>",
        "victim@example.com,other@example.com",
    ):
        assert email_transport.canonical_email_address(variante) == ""


def test_error_de_autenticacion_no_es_reintentable(monkeypatch):
    _configurar(monkeypatch)
    monkeypatch.setattr(
        email_transport.smtplib,
        "SMTP",
        lambda host, port, timeout: _SMTPFake(
            host,
            port,
            timeout,
            error=smtplib.SMTPAuthenticationError(535, b"bad credentials"),
        ),
    )
    result = email_transport.send_transactional_email(
        recipient="cliente@example.com",
        subject="Prueba",
        text_body="Texto",
        html_body="<p>Texto</p>",
    )
    assert result.accepted is False
    assert result.code == "SMTP_AUTH_FAILED"
    assert result.retryable is False


def test_message_id_es_estable_para_la_misma_dedupe_key(monkeypatch):
    _configurar(monkeypatch)
    mensajes = []

    def construir(host, port, timeout):
        smtp = _SMTPFake(host, port, timeout)
        mensajes.append(smtp)
        return smtp

    monkeypatch.setattr(email_transport.smtplib, "SMTP", construir)
    kwargs = dict(
        recipient="cliente@example.com",
        subject="Prueba",
        text_body="Texto",
        html_body="<p>Texto</p>",
        dedupe_key="misma-operacion",
    )
    primero = email_transport.send_transactional_email(**kwargs)
    segundo = email_transport.send_transactional_email(**kwargs)
    assert primero.message_id == segundo.message_id
