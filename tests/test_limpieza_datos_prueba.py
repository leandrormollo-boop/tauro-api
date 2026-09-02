from pathlib import Path

import servicios.solicitudes_guia as solicitudes


RAIZ = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self):
        self.consultas = []
        self._filas = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.consultas.append((" ".join(sql.split()), params))
        if "UPDATE solicitudes_guia s" in sql:
            self._filas = [{"id": 51, "cliente_id": "WAIMAO"}]
        else:
            self._filas = []

    def fetchall(self):
        return list(self._filas)


class _Conexion:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cursor


def test_regla_cancela_y_audita_solicitud_con_cargo_cancelado(monkeypatch, capsys):
    cursor = _Cursor()
    monkeypatch.setattr(solicitudes, "get_conn", lambda: _Conexion(cursor))

    cantidad = solicitudes.reconciliar_solicitudes_con_cargo_cancelado("waimao")

    assert cantidad == 1
    update_sql, params = cursor.consultas[0]
    assert "s.estado NOT IN ('CANCELADO', 'REEMPLAZADO')" in update_sql
    assert "e.estado='CANCELADO'" in update_sql
    assert params == ("WAIMAO",)
    assert any(
        "INSERT INTO security_audit" in sql
        and args[0] == "sistema.solicitud_cancelada_por_cargo"
        for sql, args in cursor.consultas
    )
    assert "solicitud=51 cliente=WAIMAO" in capsys.readouterr().out


def test_schema_y_script_preservan_historia_con_marca_test():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    script = (
        RAIZ / "scripts" / "limpiar_datos_prueba_20260902.sql"
    ).read_text(encoding="utf-8")

    assert "test         BOOLEAN NOT NULL DEFAULT FALSE" in schema
    assert "ADD COLUMN IF NOT EXISTS test BOOLEAN NOT NULL DEFAULT FALSE" in schema
    assert "UPDATE clientes" in script
    assert "SET test=TRUE" in script
    assert "UPDATE solicitudes_guia" in script
    assert "SET estado='CANCELADO'" in script
    assert "DELETE FROM clientes" not in script
    assert "DELETE FROM solicitudes_guia" not in script


def test_listados_y_dashboard_excluyen_datos_de_prueba():
    admin = (RAIZ / "endpoints" / "admin.py").read_text(encoding="utf-8")
    cuenta = (RAIZ / "servicios" / "cuenta_corriente.py").read_text(
        encoding="utf-8"
    )
    bandeja = (RAIZ / "servicios" / "bandeja_admin.py").read_text(
        encoding="utf-8"
    )
    guias = (RAIZ / "servicios" / "solicitudes_guia.py").read_text(
        encoding="utf-8"
    )

    assert "WHERE test=FALSE ORDER BY cliente_id" in admin
    assert "WHERE activo=TRUE AND test=FALSE" in admin
    assert 'where_activos = "WHERE c.test = FALSE"' in cuenta
    assert "WHERE c.test = FALSE" in bandeja
    assert guias.count("s.test=FALSE") >= 7
    assert 'condiciones = ["cliente_id=%s", "test=FALSE"]' in guias


def test_tracking_fedex_queda_oculto_y_sus_errores_son_genericos():
    menu = (RAIZ / "templates" / "admin" / "base_admin.html").read_text(
        encoding="utf-8"
    )
    admin = (RAIZ / "endpoints" / "admin.py").read_text(encoding="utf-8")

    assert 'href="/admin/tracking-fedex"' not in menu
    assert '@router.get("/tracking-fedex"' in admin
    assert 'summary_error = str(e)' not in admin
    assert '"output": f"Error ejecutando tracking FedEx: {e}"' not in admin
    assert "No se expusieron detalles internos" in admin
