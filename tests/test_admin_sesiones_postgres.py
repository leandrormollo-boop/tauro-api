"""Revocación, vencimiento y límites concurrentes sobre PostgreSQL aislado."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import uuid

import psycopg2
import psycopg2.extras
import pytest

from core import database
from servicios import admin_sesiones, rate_limit, totp


@pytest.fixture
def seguridad_db(monkeypatch):
    url = os.getenv("TAURO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requiere PostgreSQL aislado")
    schema = "test_seguridad_" + uuid.uuid4().hex
    admin = psycopg2.connect(url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        schema_sql = (Path(__file__).resolve().parents[1] / "sql/schema.sql").read_text()
        for table in ("admin_recupero", "admin_sesiones", "admin_totp_uso", "auth_intentos"):
            cur.execute(re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \([\s\S]*?\n\);", schema_sql)[0])

    @contextmanager
    def conn():
        db = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            with db.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            with db:
                yield db
        finally:
            db.close()
    monkeypatch.setattr(admin_sesiones, "get_conn", conn)
    monkeypatch.setattr(database, "get_conn", conn)
    try:
        yield conn
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def test_sesiones_distintas_revocables_y_sin_bearer_en_db(seguridad_db):
    a = admin_sesiones.crear("clave", "")
    b = admin_sesiones.crear("clave", "")
    assert a != b
    assert admin_sesiones.verificar(a, "clave", "")
    assert not admin_sesiones.verificar(a, "nueva", "")
    assert not admin_sesiones.verificar(a, "clave", "segundo_factor")
    with seguridad_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM admin_sesiones")
        assert a not in str(cur.fetchall())
    admin_sesiones.revocar(a)
    assert not admin_sesiones.verificar(a, "clave", "")
    assert admin_sesiones.verificar(b, "clave", "")
    with seguridad_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE admin_sesiones SET vence=NOW()-INTERVAL '1 second'")
    assert not admin_sesiones.verificar(b, "clave", "")


def test_rate_durable_concurrente_no_depende_de_memoria(seguridad_db):
    with ThreadPoolExecutor(max_workers=8) as workers:
        result = list(workers.map(lambda _: rate_limit.check_auth_rate("admin", 5, 300), range(20)))
    assert sum(result) == 5
    rate_limit._hits.clear()
    assert not rate_limit.check_auth_rate("admin", 5, 300)
    with seguridad_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE auth_intentos SET vence=NOW()-INTERVAL '1 second'")
    assert rate_limit.check_auth_rate("admin", 5, 300)


def test_totp_concurrente_consumido_una_vez(seguridad_db, monkeypatch):
    monkeypatch.setattr(totp.time, "time", lambda: 1234567890)
    secret = totp.generar_secreto()
    code = totp._codigo(secret, 1234567890)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: admin_sesiones.verificar_totp(secret, code), range(8)))
    assert sum(results) == 1
    totp._ultimo_paso = None
    assert not admin_sesiones.verificar_totp(secret, code)


def test_logout_real_revoca_cookie_copiada(seguridad_db):
    from endpoints import admin
    token = admin_sesiones.crear(admin.ADMIN_PASSWORD, admin._totp_secret())
    assert admin._is_auth(token)
    response = admin.admin_logout(admin_token=token)
    assert response.status_code == 303
    assert not admin._is_auth(token)


def test_recuperar_por_email_no_elude_segundo_factor(seguridad_db, monkeypatch):
    from endpoints import admin
    secret = totp.generar_secreto()
    monkeypatch.setattr(admin, "_totp_secret", lambda: secret)
    monkeypatch.setattr(totp.time, "time", lambda: 1234567890)
    canjes = []
    monkeypatch.setattr(admin, "_canjear_token_recupero", lambda token: canjes.append(token) or True)
    assert admin.admin_recuperar_usar(token="correo-demo", codigo="").status_code == 401
    assert canjes == []
    code = totp._codigo(secret, 1234567890)
    response = admin.admin_recuperar_usar(token="correo-demo", codigo=code)
    assert response.status_code == 303 and canjes == ["correo-demo"]
    from http.cookies import SimpleCookie
    cookie = SimpleCookie(response.headers["set-cookie"])
    assert admin._is_auth(cookie["admin_token"].value)
    assert admin.admin_recuperar_usar(token="correo-demo", codigo=code).status_code == 401
    assert canjes == ["correo-demo"]


def test_login_sesiones_individuales_y_password_unicode(seguridad_db, monkeypatch):
    from endpoints import admin
    from servicios import auditoria
    from starlette.requests import Request
    from http.cookies import SimpleCookie
    monkeypatch.setattr(admin, "_totp_secret", lambda: "")
    monkeypatch.setattr(admin, "ADMIN_PASSWORD", "contraseña-local")
    monkeypatch.setattr(auditoria, "registrar_desde_request", lambda *args, **kwargs: None)
    request = Request({"type": "http", "method": "POST", "path": "/admin/login", "headers": [],
                       "client": ("127.0.0.1", 50000), "server": ("localhost", 80), "scheme": "http"})
    assert admin.admin_login(request, password="otra-contraseña", codigo="").status_code == 401
    responses = [admin.admin_login(request, password="contraseña-local", codigo="") for _ in range(2)]
    tokens = [SimpleCookie(r.headers["set-cookie"])["admin_token"].value for r in responses]
    assert tokens[0] != tokens[1] and all(admin._is_auth(t) for t in tokens)
    admin.admin_logout(admin_token=tokens[0])
    assert not admin._is_auth(tokens[0]) and admin._is_auth(tokens[1])


@pytest.fixture
def cliente_auth(seguridad_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import admin
    from servicios import auditoria

    app = FastAPI()
    app.include_router(admin.router)

    @app.middleware("http")
    async def nonce(request, call_next):
        request.state.csp_nonce = "nonce-qa"
        return await call_next(request)

    monkeypatch.setattr(auditoria, "registrar_desde_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin, "ADMIN_PASSWORD", "clave-local-de-prueba")
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client


def test_recuperacion_http_exige_totp_y_canje_unico(cliente_auth, seguridad_db, monkeypatch):
    from endpoints import admin
    from http.cookies import SimpleCookie

    secret = totp.generar_secreto()
    monkeypatch.setattr(admin, "_totp_secret", lambda: secret)
    monkeypatch.setattr(totp.time, "time", lambda: 1234567890)
    token = "a" * 43
    admin._guardar_token_recupero(token)
    page = cliente_auth.get("/admin/recuperar")
    assert page.status_code == 200 and 'name="codigo"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"

    rejected = cliente_auth.post("/admin/recuperar/canjear", data={"token": token})
    assert rejected.status_code == 401 and "set-cookie" not in rejected.headers
    with seguridad_db() as db, db.cursor() as cur:
        cur.execute("SELECT usado FROM admin_recupero")
        assert cur.fetchone()["usado"] is False

    code = totp._codigo(secret, 1234567890)
    response = cliente_auth.post("/admin/recuperar/canjear", data={"token": token, "codigo": code})
    assert response.status_code == 303 and response.headers["location"] == "/admin/home"
    cookie = SimpleCookie(response.headers["set-cookie"])["admin_token"]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "lax"
    assert admin._is_auth(cookie.value)
    assert not admin._is_auth(admin._ADMIN_TOKEN)

    replay = cliente_auth.post("/admin/recuperar/canjear", data={"token": token, "codigo": code})
    assert replay.status_code == 401 and "set-cookie" not in replay.headers
    monkeypatch.setattr(totp.time, "time", lambda: 1234567920)
    second = cliente_auth.post("/admin/recuperar/canjear", data={
        "token": token, "codigo": totp._codigo(secret, 1234567920),
    })
    assert second.headers["location"] == "/admin/login?error=link_vencido"
    assert "set-cookie" not in second.headers
    assert cliente_auth.get("/admin/logout").status_code == 303
    assert not admin._is_auth(cookie.value)


def test_codigo_no_se_reutiliza_entre_login_y_correo(cliente_auth, monkeypatch):
    from endpoints import admin

    secret = totp.generar_secreto()
    monkeypatch.setattr(admin, "_totp_secret", lambda: secret)
    monkeypatch.setattr(totp.time, "time", lambda: 1234567890)
    token = "b" * 43
    admin._guardar_token_recupero(token)
    code = totp._codigo(secret, 1234567890)
    login = cliente_auth.post("/admin/login", data={"password": admin.ADMIN_PASSWORD, "codigo": code})
    assert login.status_code == 303
    replay = cliente_auth.post("/admin/recuperar/canjear", data={"token": token, "codigo": code})
    assert replay.status_code == 401 and "set-cookie" not in replay.headers
    monkeypatch.setattr(totp.time, "time", lambda: 1234567920)
    recovered = cliente_auth.post("/admin/recuperar/canjear", data={
        "token": token, "codigo": totp._codigo(secret, 1234567920),
    })
    assert recovered.status_code == 303 and recovered.headers["location"] == "/admin/home"


def test_canje_limita_intentos_aunque_cambien_cabeceras_ip(cliente_auth, monkeypatch):
    from endpoints import admin

    monkeypatch.setattr(admin, "_totp_secret", lambda: totp.generar_secreto())
    for i in range(11):
        response = cliente_auth.post("/admin/recuperar/canjear", data={"token": "c" * 43}, headers={
            "X-Forwarded-For": f"192.0.2.{i + 1}", "CF-Connecting-IP": f"192.0.2.{i + 1}",
        })
        assert response.status_code == (401 if i < 10 else 429)
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize("expired", [False, True])
def test_recuperacion_sin_totp_respeta_vencimiento(cliente_auth, seguridad_db, monkeypatch, expired):
    from endpoints import admin

    monkeypatch.setattr(admin, "_totp_secret", lambda: "")
    token = "d" * 43
    admin._guardar_token_recupero(token)
    if expired:
        with seguridad_db() as db, db.cursor() as cur:
            cur.execute("UPDATE admin_recupero SET vence=NOW()-INTERVAL '1 second'")
    page = cliente_auth.get("/admin/recuperar")
    assert 'name="codigo"' not in page.text
    response = cliente_auth.post("/admin/recuperar/canjear", data={"token": token})
    assert response.status_code == 303
    assert response.headers["location"] == ("/admin/login?error=link_vencido" if expired else "/admin/home")
    assert ("set-cookie" in response.headers) is not expired
