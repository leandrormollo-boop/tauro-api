"""El acceso de emergencia admin no expone bearer tokens en DB ni URLs."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import database
from endpoints import admin


ROOT = Path(__file__).resolve().parents[1]


def test_token_admin_se_guarda_como_hash_determinista():
    raw = "x" * 43
    digest = admin._hash_token_recupero(raw)
    assert len(digest) == 64
    assert digest != raw
    assert digest == admin._hash_token_recupero(raw)


def test_link_admin_usa_fragmento_y_canje_por_body():
    source = (ROOT / "endpoints/admin.py").read_text(encoding="utf-8")
    template = (ROOT / "templates/admin/recuperar.html").read_text(encoding="utf-8")

    assert 'link = f"{base}/admin/recuperar#token={token}"' in source
    assert '@router.get("/recuperar/{token}")' not in source
    assert '@router.post("/recuperar/canjear")' in source
    assert "window.location.hash" in template
    assert 'replaceState(null, document.title, "/admin/recuperar")' in template
    assert 'method="POST" action="/admin/recuperar/canjear"' in template
    assert 'name="token"' in template


def test_schema_admin_documenta_que_la_columna_contiene_sha256():
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    bloque = schema[schema.index("CREATE TABLE IF NOT EXISTS admin_recupero") :]
    assert "token   TEXT PRIMARY KEY" in bloque
    assert "token !~ '^[0-9a-f]{64}$'" in bloque
    assert "idx_admin_recupero_vence" in bloque
    assert "idx_admin_recupero_creado" in bloque


def test_cupo_admin_es_durable_atomico_y_no_depende_de_ip(monkeypatch):
    consultas = []

    class Cursor:
        def execute(self, sql, params=None):
            consultas.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {"total": 6}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            raise AssertionError("no debe confirmar una solicitud excedida")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(database, "get_conn", lambda: Conn())
    monkeypatch.setenv("EMAIL_ADMIN_RECOVERY_MAX_HORA", "6")

    with pytest.raises(admin.AdminRecoveryRateLimited):
        admin._guardar_token_recupero("x" * 43)

    sql = "\n".join(q for q, _params in consultas)
    assert "pg_advisory_xact_lock" in sql
    assert "creado >= NOW() - interval '1 hour'" in sql
    assert "INSERT INTO admin_recupero" not in sql


def test_cupo_admin_tiene_default_acotado(monkeypatch):
    monkeypatch.setenv("EMAIL_ADMIN_RECOVERY_MAX_HORA", "invalido")
    assert admin._recupero_max_hora() == 6
    monkeypatch.setenv("EMAIL_ADMIN_RECOVERY_MAX_HORA", "10000")
    assert admin._recupero_max_hora() == 100
