from contextlib import contextmanager

import pytest

import core.database as database


def _resultado_listo():
    return {campo: True for campo in database._READINESS_CONTABLE_CAMPOS}


class _CursorReadiness:
    def __init__(self, resultado=None):
        self.resultado = _resultado_listo() if resultado is None else resultado
        self.consultas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.consultas.append((sql, params))

    def fetchone(self):
        return self.resultado


def test_readiness_contable_usa_una_consulta_de_catalogo_y_acepta_schema_listo():
    cursor = _CursorReadiness()

    resultado = database._verificar_readiness_contable(cursor)

    assert resultado == _resultado_listo()
    assert len(cursor.consultas) == 1
    sql = cursor.consultas[0][0]
    assert "information_schema.columns" in sql
    assert "numeric_precision = 14 AND numeric_scale = 2" in sql
    assert "TO_REGCLASS('pagos_aplicaciones')" in sql
    assert "trg_validar_pago_aplicacion" in sql
    assert "trg_validar_pago_con_aplicaciones" in sql
    assert "t.tgenabled IN ('O', 'A')" in sql
    assert "TO_REGCLASS('uq_envios_fc_normalizada')" in sql
    assert "i.indisunique AND i.indisvalid AND i.indisready" in sql
    assert "i.indnkeyatts = 1" in sql
    assert "ck_envios_nro_fc_valida" in sql
    assert sql.count("SELECT COUNT(*) = 1") == 2
    assert sql.count("confdeltype = 'r' AND convalidated") == 2
    assert "c.confdeltype = 'r' AND c.convalidated" in sql
    assert "c.conkey = ARRAY[(" in sql
    assert "c.confkey = ARRAY[(" in sql
    assert "TO_REGCLASS('password_reset_requests')" in sql
    assert "uq_password_reset_request_activa" in sql
    assert "TO_REGCLASS('cotizaciones_web')" in sql
    assert "uq_lead_cotizacion_email" in sql
    assert "VERIFICAR_EMAIL" in sql
    assert "TO_REGCLASS('envio_cotizacion_snapshots')" in sql
    assert "TO_REGCLASS('facturas_courier')" in sql
    assert "uq_factura_courier_documento" in sql
    assert "trg_validar_match_factura_courier" in sql
    assert "trg_validar_ajuste_cliente" in sql
    assert "trg_snapshot_inmutable" in sql
    assert "trg_validar_snapshot_cotizacion" in sql
    assert "trg_proteger_factura_con_items" in sql
    assert "trg_auditoria_courier_append_only" in sql
    assert "ck_conciliacion_formula_final" in sql


@pytest.mark.parametrize("campo", database._READINESS_CONTABLE_CAMPOS)
def test_readiness_contable_falla_cerrado_por_cada_invariante(campo):
    estado = _resultado_listo()
    estado[campo] = False
    cursor = _CursorReadiness(estado)

    with pytest.raises(RuntimeError, match=campo):
        database._verificar_readiness_contable(cursor)

    assert len(cursor.consultas) == 1


def test_readiness_contable_falla_si_catalogo_no_devuelve_fila():
    cursor = _CursorReadiness(resultado=None)
    cursor.resultado = None

    with pytest.raises(RuntimeError, match="sin resultado"):
        database._verificar_readiness_contable(cursor)


class _ConexionInit:
    def __init__(self, cursor):
        self.cursor_real = cursor
        self.exc_type = None

    def cursor(self):
        return self.cursor_real


@contextmanager
def _conexion_init(conexion):
    try:
        yield conexion
    except BaseException as exc:
        conexion.exc_type = type(exc)
        raise


def test_init_db_ejecuta_schema_y_readiness_en_la_misma_transaccion(monkeypatch):
    cursor = _CursorReadiness()
    conexion = _ConexionInit(cursor)
    monkeypatch.setattr(database, "get_conn", lambda: _conexion_init(conexion))

    database.init_db()

    assert len(cursor.consultas) == 2
    assert "CREATE TABLE IF NOT EXISTS pagos" in cursor.consultas[0][0]
    assert cursor.consultas[1][0] == database._READINESS_CONTABLE_SQL
    assert conexion.exc_type is None


def test_init_db_propaga_readiness_incompleta_para_abortar_startup(monkeypatch):
    estado = _resultado_listo()
    estado["indice_fc_global_correcto"] = False
    cursor = _CursorReadiness(estado)
    conexion = _ConexionInit(cursor)
    monkeypatch.setattr(database, "get_conn", lambda: _conexion_init(conexion))

    with pytest.raises(RuntimeError, match="indice_fc_global_correcto"):
        database.init_db()

    assert len(cursor.consultas) == 2
    assert conexion.exc_type is RuntimeError
