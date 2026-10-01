"""«Tipo de cuenta» en la configuración del cliente: una cuenta de prueba
se vuelve real desde el admin, queda auditado y reaparece en los totales."""
import pytest
from starlette.requests import Request

from endpoints import admin
from servicios import cuenta_corriente
from test_conciliacion_couriers_postgres import conciliacion_db, DATABASE_URL

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="requiere PostgreSQL aislado")


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    for modulo in (admin, cuenta_corriente):
        monkeypatch.setattr(modulo, "get_conn", conciliacion_db)
    monkeypatch.setattr(admin, "_is_auth", lambda token: token == "valido")
    return conciliacion_db


def _post(path):
    return Request(dict(
        type="http", method="POST", path=path, query_string=b"", headers=[],
        scheme="http", server=("testserver", 80), state={"csp_nonce": "test"},
        client=("127.0.0.1", 1234),
    ))


def _cliente(db, cliente_id, *, test):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO clientes (cliente_id, email, nombre, test) VALUES (%s, %s, %s, %s)",
            (cliente_id, f"{cliente_id.lower()}@example.invalid", cliente_id, test),
        )


def _editar(cliente_id, *, cuenta_prueba):
    # Llamada directa: los defaults de Form() no se resuelven fuera de FastAPI.
    return admin.admin_cliente_editar(
        _post(f"/admin/clientes/{cliente_id}/editar"), cliente_id,
        email=f"{cliente_id.lower()}@example.invalid", nombre=cliente_id,
        cuit="", direccion="", cp="", ciudad="", pais="AR", telefono="",
        markup_pct="25", markup_tipo="PCT", markup_valor="",
        markup_nac_tipo="", markup_nac_valor="", tax_paga="", notas="",
        activo="true", cuenta_prueba=cuenta_prueba, admin_token="valido",
    )


def _flag(db, cliente_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT test FROM clientes WHERE cliente_id=%s", (cliente_id,))
        return cur.fetchone()["test"]


def _auditoria(db, cliente_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT metadata FROM security_audit "
            "WHERE event='admin.cliente_cuenta_prueba' AND actor_ref=%s ORDER BY id",
            (cliente_id,),
        )
        return [dict(r)["metadata"] for r in cur.fetchall()]


def test_cuenta_de_prueba_se_vuelve_real_y_queda_auditado(db):
    _cliente(db, "PRUEBA_REAL", test=True)
    assert [c["cliente_id"] for c in admin._get_clientes_prueba()] == ["PRUEBA_REAL"]
    assert "PRUEBA_REAL" not in {
        c["cliente_id"] for c in cuenta_corriente.get_resumen_clientes_bulk(solo_activos=False)
    }

    respuesta = _editar("PRUEBA_REAL", cuenta_prueba="false")

    assert respuesta.status_code == 303
    assert _flag(db, "PRUEBA_REAL") is False
    assert admin._get_clientes_prueba() == []
    assert "PRUEBA_REAL" in {
        c["cliente_id"] for c in cuenta_corriente.get_resumen_clientes_bulk(solo_activos=False)
    }
    assert _auditoria(db, "PRUEBA_REAL") == [{"antes": True, "despues": False}]


def test_guardar_sin_cambiar_el_tipo_no_audita_ni_lo_toca(db):
    _cliente(db, "REAL", test=False)

    _editar("REAL", cuenta_prueba="false")

    assert _flag(db, "REAL") is False
    assert _auditoria(db, "REAL") == []


def test_marcar_como_prueba_la_saca_de_los_totales(db):
    _cliente(db, "DEMO", test=False)

    _editar("DEMO", cuenta_prueba="true")

    assert _flag(db, "DEMO") is True
    assert [c["cliente_id"] for c in admin._get_clientes_prueba()] == ["DEMO"]
    assert _auditoria(db, "DEMO") == [{"antes": False, "despues": True}]


def test_el_formulario_general_sigue_sin_tocar_permisos_operativos():
    import inspect
    fuente = inspect.getsource(admin.admin_cliente_editar)
    assert "test=%s" in fuente
    for campo in ("puede_emitir=%s", "puede_recolectar=%s", "tope_deuda_ars=%s"):
        assert campo not in fuente
