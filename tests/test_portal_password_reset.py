"""Contrato de seguridad del restablecimiento de contraseña del portal."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import time

from starlette.requests import Request


RAIZ = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.queries = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.queries.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def _request(path="/portal/password/forgot", query_string=b""):
    request = Request({
        "type": "http", "method": "POST", "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": query_string,
        "headers": [], "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    })
    request.state.csp_nonce = "nonce-test"
    return request


def test_schema_guarda_solo_hash_y_exige_confirmacion_de_email():
    schema = (RAIZ / "sql/schema.sql").read_text(encoding="utf-8")
    bloque = schema[schema.index("CREATE TABLE IF NOT EXISTS password_reset_tokens") :]
    bloque = bloque[:bloque.index("CREATE TABLE IF NOT EXISTS password_reset_requests")]

    assert "token_hash" in bloque
    assert "email_enviado_at" in bloque
    assert "usado_at" in bloque
    assert "REFERENCES clientes(cliente_id) ON DELETE CASCADE" in bloque
    assert " token " not in bloque.lower()


def test_schema_incluye_cola_durable_claims_y_estado_incierto():
    schema = (RAIZ / "sql/schema.sql").read_text(encoding="utf-8")
    bloque = schema[schema.index("CREATE TABLE IF NOT EXISTS password_reset_requests") :]
    bloque = bloque[:bloque.index("CREATE TABLE IF NOT EXISTS rutas")]

    assert "REFERENCES clientes(cliente_id) ON DELETE CASCADE" in bloque
    assert "VERIFICAR_EMAIL" in bloque
    assert "ck_password_reset_request_claim" in bloque
    assert "uq_password_reset_request_activa" in bloque
    assert "WHERE estado IN ('PENDIENTE','PROCESANDO')" in bloque
    assert "email TEXT" not in bloque
    assert "token TEXT" not in bloque


def test_creacion_persiste_sha256_no_token_crudo_y_ttl_30_min(monkeypatch):
    from servicios import auth

    token = "secreto-no-persistir-abcdefghijklmnopqrstuvwxyz"
    ahora = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
    cursor = _Cursor()
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _n: token)
    monkeypatch.setattr(auth, "_now", lambda: ahora)
    monkeypatch.setattr(auth, "get_conn", lambda: _Conn(cursor))

    devuelto = auth.crear_password_reset_token("melcior")

    assert devuelto == token
    insert_sql, params = cursor.queries[0]
    assert "INSERT INTO password_reset_tokens" in insert_sql
    assert params[0] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(cursor.queries)
    assert (params[3] - params[2]).total_seconds() == 30 * 60


def test_token_no_es_valido_hasta_confirmar_smtp(monkeypatch):
    from servicios import auth

    cursor = _Cursor(rows=[None])
    monkeypatch.setattr(auth, "get_conn", lambda: _Conn(cursor))

    assert auth.password_reset_token_valido("x" * 43) is False
    sql, _params = cursor.queries[0]
    assert "email_enviado_at IS NOT NULL" in sql
    assert "usado_at IS NULL" in sql
    assert "expira_at >" in sql


def test_activacion_invalida_links_anteriores(monkeypatch):
    from servicios import auth

    cursor = _Cursor(rows=[{"cliente_id": "MELCIOR"}])
    monkeypatch.setattr(auth, "get_conn", lambda: _Conn(cursor))

    assert auth.activar_password_reset_token("x" * 43) is True
    consultas = "\n".join(sql for sql, _params in cursor.queries)
    assert "FOR UPDATE" in consultas
    assert "token_hash <>" in consultas
    assert "SET email_enviado_at" in consultas


def test_canje_es_one_use_y_revoca_todas_las_sesiones(monkeypatch):
    from servicios import auth

    cursores = [
        _Cursor(rows=[{"cliente_id": "MELCIOR"}]),
        _Cursor(rows=[None]),
    ]
    monkeypatch.setattr(auth, "get_conn", lambda: _Conn(cursores.pop(0)))
    monkeypatch.setattr(auth, "hash_password", lambda _password: "bcrypt-nuevo")

    assert auth.consumir_password_reset_token("z" * 43, "NuevaSegura2026") is True

    # Segundo canje del mismo secreto: la fila ya no cumple usado_at IS NULL.
    assert auth.consumir_password_reset_token("z" * 43, "NuevaSegura2026") is False


def test_canje_sql_bloquea_fila_actualiza_password_y_revoca_sessions(monkeypatch):
    from servicios import auth

    cursor = _Cursor(rows=[{"cliente_id": "WAIMAO"}])
    monkeypatch.setattr(auth, "get_conn", lambda: _Conn(cursor))
    monkeypatch.setattr(auth, "hash_password", lambda _password: "bcrypt-nuevo")

    assert auth.consumir_password_reset_token("y" * 43, "NuevaSegura2026") is True
    consultas = "\n".join(sql for sql, _params in cursor.queries)
    assert "FOR UPDATE OF r" in consultas
    assert "UPDATE clientes SET password_hash" in consultas
    assert "UPDATE password_reset_tokens SET usado_at" in consultas
    assert "UPDATE sessions SET usado = TRUE" in consultas


def test_validador_password_longitud_bytes_confirmacion_y_calidad():
    from servicios.auth import validar_nueva_password

    assert validar_nueva_password("Corta123", "Corta123")
    assert validar_nueva_password("NuevaSegura2026", "otra")
    assert validar_nueva_password("sindigitoslargos", "sindigitoslargos")
    assert validar_nueva_password("  NuevaSegura2026", "  NuevaSegura2026")
    assert validar_nueva_password("á" * 36 + "1A", "á" * 36 + "1A")  # >72 bytes
    assert validar_nueva_password("NuevaSegura2026", "NuevaSegura2026") is None


def _preparar_forgot(monkeypatch, portal):
    monkeypatch.setattr(portal, "check_rate", lambda *_a, **_k: True)
    monkeypatch.setattr(portal, "_auditar_password_reset", lambda *_a, **_k: None)
    monkeypatch.setattr(
        portal, "uniformar_password_reset_inexistente", lambda *_a, **_k: None,
    )


def test_forgot_no_enumera_cuenta_existente(monkeypatch):
    from endpoints import portal_cliente as portal

    _preparar_forgot(monkeypatch, portal)
    encoladas = []
    monkeypatch.setattr(
        portal, "encolar_password_reset",
        lambda cid, _quote="": (encoladas.append(cid) or SimpleNamespace(
            accepted=True, code="QUEUED",
        )),
    )

    monkeypatch.setattr(portal, "buscar_cliente_para_password_reset", lambda _i: None)
    ausente = portal.password_forgot(_request(), "nadie@example.com")
    monkeypatch.setattr(
        portal, "buscar_cliente_para_password_reset",
        lambda _i: {"cliente_id": "MELCIOR", "email": "cliente@example.com"},
    )
    existente = portal.password_forgot(_request(), "cliente@example.com")

    assert ausente.status_code == existente.status_code == 200
    assert ausente.body == existente.body
    assert b"cliente@example.com" not in existente.body
    assert encoladas == ["MELCIOR"]


def test_forgot_no_crea_token_ni_llama_smtp_aunque_el_transporte_sea_lento(monkeypatch):
    from endpoints import portal_cliente as portal
    from core import email_sender
    from servicios import auth

    _preparar_forgot(monkeypatch, portal)
    llamadas_smtp = []

    def smtp_lento(*_args, **_kwargs):
        llamadas_smtp.append(True)
        time.sleep(0.25)
        raise AssertionError("el endpoint no debe invocar SMTP")

    monkeypatch.setattr(email_sender, "enviar_restablecimiento_password", smtp_lento)
    monkeypatch.setattr(
        auth, "crear_password_reset_token",
        lambda *_a: (_ for _ in ()).throw(AssertionError("no crear token en HTTP")),
    )
    monkeypatch.setattr(
        portal, "buscar_cliente_para_password_reset",
        lambda _i: {"cliente_id": "MELCIOR", "email": "cliente@example.com"},
    )
    monkeypatch.setattr(
        portal, "encolar_password_reset",
        lambda _cid, _quote="": SimpleNamespace(accepted=True, code="QUEUED"),
    )

    respuesta = portal.password_forgot(_request(), "cliente@example.com")

    assert respuesta.status_code == 200
    assert llamadas_smtp == []
    assert b"Si la cuenta existe" in respuesta.body


def test_fallo_de_cola_mantiene_respuesta_generica(monkeypatch):
    from endpoints import portal_cliente as portal

    _preparar_forgot(monkeypatch, portal)
    monkeypatch.setattr(
        portal, "buscar_cliente_para_password_reset",
        lambda _i: {"cliente_id": "MELCIOR", "email": "cliente@example.com"},
    )
    monkeypatch.setattr(
        portal, "encolar_password_reset",
        lambda _cid, _quote="": (_ for _ in ()).throw(RuntimeError("db")),
    )

    respuesta = portal.password_forgot(_request(), "cliente@example.com")

    assert respuesta.status_code == 200
    assert b"Si la cuenta existe" in respuesta.body
    assert b"cliente@example.com" not in respuesta.body


def test_auditoria_no_recibe_pii_ni_token(monkeypatch):
    from endpoints import portal_cliente as portal
    from servicios import auditoria

    capturado = {}
    monkeypatch.setattr(auditoria, "registrar_desde_request", lambda _r, **kw: capturado.update(kw))
    portal._auditar_password_reset(
        _request(), "portal.password_reset.request",
        success=False, status_code=200, estado="email_no_enviado",
    )

    assert capturado["actor_ref"] is None
    assert capturado["metadata"] == {"estado": "email_no_enviado"}
    serializado = repr(capturado)
    assert "@" not in serializado
    assert "token" not in serializado.lower()


def test_rate_key_del_identificador_es_irreversible():
    from endpoints.portal_cliente import _password_reset_rate_ref

    email = "Cliente@Example.com"
    ref = _password_reset_rate_ref(email)
    assert len(ref) == 64
    assert email.casefold() not in ref
    assert ref == _password_reset_rate_ref("  cliente@example.com  ")


def test_base_url_del_reset_no_admite_dominio_ajeno(monkeypatch):
    from endpoints import portal_cliente as portal

    monkeypatch.setattr(portal, "BASE_URL", "https://atacante.example/robo")
    assert portal._password_reset_base_url() == "https://taurosolutions.ar"
    monkeypatch.setattr(portal, "BASE_URL", "https://www.taurosolutions.ar")
    assert portal._password_reset_base_url() == "https://www.taurosolutions.ar"


def test_get_reset_no_acepta_query_token_y_bootstrappea_desde_fragmento():
    from endpoints import portal_cliente as portal

    token = "v" * 43
    respuesta = portal.password_reset_form(_request(
        "/portal/password/reset", f"token={token}".encode(),
    ))

    assert respuesta.status_code == 200
    assert respuesta.headers["referrer-policy"] == "no-referrer"
    assert respuesta.headers["cache-control"] == "no-store, max-age=0"
    assert respuesta.headers["pragma"] == "no-cache"
    assert token.encode() not in respuesta.body
    assert b"window.location.hash" in respuesta.body
    assert b"URLSearchParams" in respuesta.body
    assert b"history.replaceState" in respuesta.body
    assert b'location.search' not in respuesta.body
    assert b"Guardar nueva contrase" in respuesta.body


def test_submit_exitoso_revoca_cookie_y_redirige_al_login(monkeypatch):
    from endpoints import portal_cliente as portal

    consumidos = []
    monkeypatch.setattr(portal, "check_rate", lambda *_a, **_k: True)
    monkeypatch.setattr(portal, "reset_rate", lambda *_a, **_k: None)
    monkeypatch.setattr(portal, "_auditar_password_reset", lambda *_a, **_k: None)
    monkeypatch.setattr(portal, "password_reset_token_valido", lambda _t: True)
    monkeypatch.setattr(portal, "validar_nueva_password", lambda *_a: None)
    monkeypatch.setattr(
        portal, "consumir_password_reset_token",
        lambda token, password: (consumidos.append((token, password)) or True),
    )

    respuesta = portal.password_reset_submit(
        _request("/portal/password/reset"),
        "s" * 43, "NuevaSegura2026", "NuevaSegura2026",
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/portal/login?password_reset=ok"
    assert consumidos == [("s" * 43, "NuevaSegura2026")]
    assert "token=" in respuesta.headers["set-cookie"]
    assert "Max-Age=0" in respuesta.headers["set-cookie"]


def test_password_invalida_no_consume_el_token(monkeypatch):
    from endpoints import portal_cliente as portal

    monkeypatch.setattr(portal, "check_rate", lambda *_a, **_k: True)
    monkeypatch.setattr(portal, "_auditar_password_reset", lambda *_a, **_k: None)
    monkeypatch.setattr(portal, "password_reset_token_valido", lambda _t: True)
    monkeypatch.setattr(portal, "validar_nueva_password", lambda *_a: "Usá una clave mejor.")
    monkeypatch.setattr(
        portal, "consumir_password_reset_token",
        lambda *_a: (_ for _ in ()).throw(AssertionError("no consumir")),
    )

    respuesta = portal.password_reset_submit(
        _request("/portal/password/reset"), "s" * 43, "mala", "mala",
    )

    assert respuesta.status_code == 400
    assert b"clave mejor" in respuesta.body


def test_email_reset_usa_transporte_unico_y_no_conecta_real(monkeypatch):
    from core import email_sender, email_transport

    capturado = {}
    monkeypatch.setattr(
        email_transport,
        "send_transactional_email",
        lambda **kw: (capturado.update(kw) or SimpleNamespace(accepted=True, code="ACCEPTED")),
    )
    link = "https://taurosolutions.ar/portal/password/reset#token=secreto"

    resultado = email_sender.enviar_restablecimiento_password(
        "cliente@example.com", link,
    )
    assert resultado.accepted is True
    assert capturado["recipient"] == "cliente@example.com"
    assert "Restablec" in capturado["subject"]
    assert "30 minutos" in capturado["text_body"]
    assert "TAURO" in capturado["html_body"]
    assert "Crear nueva contraseña" in capturado["html_body"]
    assert capturado["dedupe_key"].startswith("password-reset:")
    assert "?token=" not in capturado["text_body"]


def test_template_reset_no_expone_identidad_y_explica_reglas():
    template = (RAIZ / "templates/portal/password_reset.html").read_text(encoding="utf-8")
    assert "name=\"token\"" in template
    assert "autocomplete=\"new-password\"" in template
    assert "Mínimo 12 caracteres" in template
    assert "cerrar" in template.lower() and "sesiones" in template.lower()
    assert "email" not in template.lower()


def test_encolado_es_durable_atomico_y_no_persiste_email_ni_token(monkeypatch):
    from servicios import password_reset_queue as cola

    cursor = _Cursor(rows=[
        {"activa": False, "recientes": 0},
        {"id": 91},
    ])
    monkeypatch.setattr(cola, "get_conn", lambda: _Conn(cursor))

    resultado = cola.encolar_password_reset("melcior")

    assert resultado.accepted is True
    assert resultado.code == "QUEUED"
    consultas = "\n".join(sql for sql, _params in cursor.queries)
    assert "pg_advisory_xact_lock" in consultas
    assert "INSERT INTO password_reset_requests" in consultas
    assert "ON CONFLICT (cliente_id)" in consultas
    assert "email" not in consultas.lower()
    assert "token" not in consultas.lower()


def test_camino_inexistente_uniforma_db_sin_persistir_identificador(monkeypatch):
    from servicios import password_reset_queue as cola

    cursor = _Cursor(rows=[{"activa": False, "recientes": 0}, None])
    monkeypatch.setattr(cola, "get_conn", lambda: _Conn(cursor))

    cola.uniformar_password_reset_inexistente("a" * 64)

    assert len(cursor.queries) == 3
    consultas = "\n".join(sql for sql, _params in cursor.queries)
    assert "pg_advisory_xact_lock" in consultas
    assert "INSERT" not in consultas
    assert "a" * 64 not in consultas


def test_worker_usa_fragmento_activa_despues_de_aceptacion_y_marca_enviado(monkeypatch):
    from servicios import password_reset_queue as cola

    eventos = []
    filas = [{
        "id": 7, "cliente_id": "MELCIOR", "intentos": 1,
        "email": "cliente@example.com", "claim_id": "claim-7",
    }, None]
    monkeypatch.setattr(cola, "recuperar_password_reset_claims_stale", lambda: 0)
    monkeypatch.setattr(cola, "expirar_password_reset_pendientes", lambda: 0)
    monkeypatch.setattr(cola, "_reclamar_siguiente", lambda: filas.pop(0))
    monkeypatch.setattr(
        cola, "crear_password_reset_token",
        lambda cid: (eventos.append(("crear", cid)) or "x" * 43),
    )

    def enviar(_email, link):
        eventos.append(("enviar", link))
        assert "/portal/password/reset#token=" in link
        assert "?token=" not in link
        return SimpleNamespace(
            accepted=True, retryable=False, code="ACCEPTED", message_id="<m@tauro>",
        )

    monkeypatch.setattr(cola, "enviar_restablecimiento_password", enviar)
    monkeypatch.setattr(
        cola, "finalizar_password_reset_entregado",
        lambda token, **kw: (eventos.append(("finalizar", token, kw)) or True),
    )

    resumen = cola.procesar_password_reset_requests()

    assert resumen["enviadas"] == 1
    assert [e[0] for e in eventos] == ["crear", "enviar", "finalizar"]
    assert eventos[-1][2] == {
        "request_id": 7,
        "claim_id": "claim-7",
        "message_id": "<m@tauro>",
    }


def test_worker_revoca_y_aisla_timeout_sin_reenvio_ciego(monkeypatch):
    from servicios import password_reset_queue as cola

    fila = {
        "id": 8, "cliente_id": "WAIMAO", "intentos": 1,
        "email": "cliente@example.com", "claim_id": "claim-8",
    }
    revocados = []
    inciertos = []
    monkeypatch.setattr(cola, "crear_password_reset_token", lambda _cid: "r" * 43)
    monkeypatch.setattr(
        cola, "enviar_restablecimiento_password",
        lambda *_a: SimpleNamespace(
            accepted=False, retryable=True, code="SMTP_TIMEOUT", message_id="",
        ),
    )
    monkeypatch.setattr(cola, "revocar_password_reset_token", revocados.append)
    monkeypatch.setattr(
        cola, "finalizar_password_reset_entregado",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no activar")),
    )
    monkeypatch.setattr(
        cola, "_marcar_incierto",
        lambda *a, **kw: (inciertos.append((a, kw)) or "VERIFICAR_EMAIL"),
    )

    assert cola._procesar_reclamada(fila) == "VERIFICAR_EMAIL"
    assert revocados == ["r" * 43]
    assert inciertos[0][1]["code"] == "SMTP_TIMEOUT"


def test_worker_no_revoca_si_smtp_acepto_y_commit_quedo_incierto(monkeypatch):
    from servicios import password_reset_queue as cola

    fila = {
        "id": 81, "cliente_id": "MELCIOR", "intentos": 1,
        "email": "cliente@example.com", "claim_id": "claim-81",
    }
    revocados = []
    inciertos = []
    monkeypatch.setattr(cola, "crear_password_reset_token", lambda _cid: "s" * 43)
    monkeypatch.setattr(
        cola, "enviar_restablecimiento_password",
        lambda *_a: SimpleNamespace(
            accepted=True, retryable=False, code="ACCEPTED",
            message_id="<accepted@tauro>",
        ),
    )
    monkeypatch.setattr(
        cola, "finalizar_password_reset_entregado",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("commit ack perdido")),
    )
    monkeypatch.setattr(cola, "revocar_password_reset_token", revocados.append)
    monkeypatch.setattr(
        cola, "_marcar_incierto",
        lambda *a, **kw: (inciertos.append((a, kw)) or "VERIFICAR_EMAIL"),
    )

    assert cola._procesar_reclamada(fila) == "VERIFICAR_EMAIL"
    assert revocados == []
    assert inciertos == [((81, "claim-81"), {"code": "INTERNAL_ERROR"})]


def test_worker_reprograma_unicamente_rechazo_smtp_4xx_confirmado(monkeypatch):
    from servicios import password_reset_queue as cola

    fila = {
        "id": 9, "cliente_id": "WAIMAO", "intentos": 1,
        "email": "cliente@example.com", "claim_id": "claim-9",
    }
    fallos = []
    monkeypatch.setattr(cola, "crear_password_reset_token", lambda _cid: "r" * 43)
    monkeypatch.setattr(
        cola, "enviar_restablecimiento_password",
        lambda *_a: SimpleNamespace(
            accepted=False, retryable=True, code="SMTP_TEMPORARY", message_id="",
        ),
    )
    monkeypatch.setattr(cola, "revocar_password_reset_token", lambda _t: True)
    monkeypatch.setattr(
        cola, "_marcar_fallo",
        lambda *a, **kw: (fallos.append((a, kw)) or "PENDIENTE"),
    )

    assert cola._procesar_reclamada(fila) == "PENDIENTE"
    assert fallos[0][1]["code"] == "SMTP_TEMPORARY"
    assert fallos[0][1]["retryable"] is True


def test_recuperacion_de_claims_stale_los_aisla_como_inciertos(monkeypatch):
    from servicios import password_reset_queue as cola

    cursor = _Cursor()
    cursor.rowcount = 2
    monkeypatch.setattr(cola, "get_conn", lambda: _Conn(cursor))

    assert cola.recuperar_password_reset_claims_stale() == 2
    sql, params = cursor.queries[0]
    assert "estado = 'VERIFICAR_EMAIL'" in sql
    assert "CLAIM_EXPIRED" in sql
    assert "claimed_at < NOW()" in sql
    assert params == (cola.CLAIM_STALE_MINUTOS,)


def test_pedidos_pendientes_expiran_y_no_pueden_enviarse_meses_despues(monkeypatch):
    from servicios import password_reset_queue as cola

    cursor = _Cursor()
    cursor.rowcount = 3
    monkeypatch.setattr(cola, "get_conn", lambda: _Conn(cursor))

    assert cola.expirar_password_reset_pendientes() == 3
    sql, params = cursor.queries[0]
    assert "estado = 'FALLIDO'" in sql
    assert "REQUEST_EXPIRED" in sql
    assert "creado_at <= NOW()" in sql
    assert params == (cola.REQUEST_TTL_MINUTOS,)


def test_finalizacion_reset_bloquea_claim_antes_de_invalidar_tokens(monkeypatch):
    from servicios import auth

    cursor = _Cursor(rows=[
        {"cliente_id": "MELCIOR"},
        {"cliente_id": "MELCIOR"},
    ])
    monkeypatch.setattr(auth, "get_conn", lambda: _Conn(cursor))

    assert auth.finalizar_password_reset_entregado(
        "x" * 43,
        request_id=91,
        claim_id="claim-91",
        message_id="<m@tauro>",
    ) is True

    consultas = [sql for sql, _params in cursor.queries]
    assert "FROM password_reset_requests" in consultas[0]
    assert "FOR UPDATE" in consultas[0]
    assert "FROM password_reset_tokens" in consultas[1]
    assert "token_hash <>" in consultas[2]
    assert "SET email_enviado_at" in consultas[3]
    assert "SET estado = 'ENVIADO'" in consultas[4]


def test_retencion_reset_no_toca_trabajos_activos(monkeypatch):
    from servicios import password_reset_queue as cola

    cursor = _Cursor()
    cursor.rowcount = 2
    monkeypatch.setattr(cola, "get_conn", lambda: _Conn(cursor))

    resultado = cola.limpiar_retencion_password_reset(
        solicitudes_dias=30, tokens_dias=7,
    )

    assert resultado == {"solicitudes_eliminadas": 2, "tokens_eliminados": 2}
    solicitudes_sql = cursor.queries[0][0]
    assert "estado NOT IN ('PENDIENTE', 'PROCESANDO')" in solicitudes_sql
    assert "DELETE FROM password_reset_tokens" in cursor.queries[1][0]
