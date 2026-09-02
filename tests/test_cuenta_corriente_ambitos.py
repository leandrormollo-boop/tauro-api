from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import servicios.cuenta_corriente as cc


RAIZ = Path(__file__).resolve().parents[1]


def test_schema_aplicaciones_es_explicito_concurrente_y_sin_backfill():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    bloque = schema[schema.index("CREATE TABLE IF NOT EXISTS pagos_aplicaciones") :]

    assert "monto_ars   NUMERIC(14,2) NOT NULL" in bloque
    assert "REFERENCES pagos(id) ON DELETE RESTRICT ON UPDATE RESTRICT" in bloque
    assert "UNIQUE (pago_id, ambito)" in bloque
    assert "ambito IN ('NACIONAL', 'INTERNACIONAL')" in bloque
    assert "FOR UPDATE" in bloque
    assert "SUM(monto_ars)" in bloque
    assert "pago_estado <> 'APROBADO'" in bloque
    assert "estado IN ('SOLICITADA', 'APLICADA')" in bloque
    assert "IF ya_aplicado + NEW.monto_ars > pago_monto" in bloque
    assert "INSERT INTO pagos_aplicaciones SELECT" not in schema


def test_migracion_decimal_incluye_tope_deuda_y_markup_nacional():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    migracion = (RAIZ / "scripts" / "migrar_dinero_numeric.sql").read_text(
        encoding="utf-8"
    )
    preflight = (RAIZ / "scripts" / "preflight_cuenta_ambitos.sql").read_text(
        encoding="utf-8"
    )
    postflight = (RAIZ / "scripts" / "postflight_cuenta_ambitos.sql").read_text(
        encoding="utf-8"
    )

    assert "markup_nac_valor NUMERIC(14,4)" in schema
    assert "tope_deuda_ars NUMERIC(14,2)" in schema
    assert "['clientes','markup_nac_valor','14,4']" in migracion
    assert "['clientes','tope_deuda_ars','14,2']" in migracion
    assert "('clientes', 'markup_nac_valor')" in preflight
    assert "('clientes', 'tope_deuda_ars')" in preflight
    assert "('clientes', 'markup_nac_valor', 14, 4)" in postflight
    assert "('clientes', 'tope_deuda_ars', 14, 2)" in postflight


def test_schema_idempotencia_y_fc_unica_global_normalizada_con_preflight():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    preflight = (RAIZ / "scripts" / "preflight_cuenta_ambitos.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS idempotency_key TEXT" in schema
    assert "uq_pagos_cliente_idempotency" in schema
    assert "uq_envios_cliente_idempotency" in schema
    assert "ck_pagos_idempotency_key" in schema
    assert "ck_envios_idempotency_key" in schema
    assert "^[A-Za-z0-9_-]{32,128}$" in schema
    assert schema.count("WHERE idempotency_key IS NOT NULL") >= 2
    assert "uq_envios_fc_normalizada" in schema
    assert "ck_envios_nro_fc_valida" in schema
    assert "VALIDATE CONSTRAINT ck_envios_nro_fc_valida" in schema
    assert "DROP CONSTRAINT ck_envios_nro_fc_valido" in schema
    assert "OR BTRIM(nro_fc) = ''" in schema
    assert "REGEXP_REPLACE(UPPER(BTRIM(nro_fc))" in schema
    assert "<> 'NC'" in schema
    indice_fc = schema[
        schema.index("CREATE UNIQUE INDEX IF NOT EXISTS uq_envios_fc_normalizada"):
        schema.index("-- Índice compuesto para queries de facturación")
    ]
    assert "cliente_id," not in indice_fc
    assert "tracking" not in indice_fc
    assert "DROP INDEX IF EXISTS uq_envios_cliente_fc_normalizada" in indice_fc
    assert "Facturas potencialmente duplicadas" in preflight
    assert "ARRAY_AGG(id ORDER BY id)" in preflight
    assert "<> 'NC'" in preflight


def test_schema_fks_contables_restringen_borrado_en_nuevas_y_existentes():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    pagos = schema[
        schema.index("CREATE TABLE IF NOT EXISTS pagos ("):
        schema.index("-- Aplicación contable explícita")
    ]
    envios = schema[
        schema.index("CREATE TABLE IF NOT EXISTS envios ("):
        schema.index("-- Factura emitida con su PDF adjunto")
    ]

    assert "REFERENCES clientes(cliente_id) ON DELETE RESTRICT" in pagos
    assert "REFERENCES clientes(cliente_id) ON DELETE RESTRICT" in envios
    reconciliacion_pagos = pagos[
        pagos.index("-- Preserva el libro contable"):
        pagos.index("CREATE INDEX IF NOT EXISTS idx_pagos_cliente")
    ]
    reconciliacion_envios = envios[envios.index("DO $$"):]
    for bloque, tabla, constraint in (
        (reconciliacion_pagos, "pagos", "pagos_cliente_id_fkey"),
        (reconciliacion_envios, "envios", "envios_cliente_id_fkey"),
    ):
        assert "FROM pg_constraint c" in bloque
        assert "FROM pg_attribute a" in bloque
        assert "c.contype = 'f'" in bloque
        assert f"c.conrelid = '{tabla}'::regclass" in bloque
        assert "c.confrelid = 'clientes'::regclass" in bloque
        assert "c.conkey = ARRAY[(" in bloque
        assert "c.confkey = ARRAY[(" in bloque
        assert bloque.count("a.attname = 'cliente_id'") >= 4
        assert "SELECT COUNT(*), COALESCE(BOOL_AND(" in bloque
        assert f"conname = '{constraint}'" in bloque
        assert "confdeltype = 'r'" in bloque
        assert "c.convalidated" in bloque
        assert "IF cantidad_exactas <> 1 OR NOT todas_canonicas THEN" in bloque
        assert "FOR fk IN" in bloque
        assert "'ALTER TABLE %s DROP CONSTRAINT %I'" in bloque
        assert "fk.conname" in bloque
        assert f"ADD CONSTRAINT {constraint}" in bloque
        assert "ON DELETE RESTRICT" in bloque


def test_preflight_inventaria_y_postflight_valida_fks_sin_confiar_en_nombre():
    preflight = (RAIZ / "scripts" / "preflight_cuenta_ambitos.sql").read_text(
        encoding="utf-8"
    )
    postflight = (RAIZ / "scripts" / "postflight_cuenta_ambitos.sql").read_text(
        encoding="utf-8"
    )

    inventario_pre = preflight[preflight.index("-- 9) Inventario") :]
    assert "PG_GET_CONSTRAINTDEF(c.oid) AS definicion" in inventario_pre
    assert "c.confdeltype" in inventario_pre
    assert "c.conname AS constraint_nombre" in inventario_pre
    assert "c.conkey = ARRAY[e.cliente_attnum]::smallint[]" in inventario_pre
    assert "c.confkey = ARRAY[e.clientes_cliente_attnum]::smallint[]" in inventario_pre

    control_post = postflight[
        postflight.index("-- 4b) Cada libro") : postflight.index("-- 5) Triggers")
    ]
    assert "total_fks = 1 AND fks_canonicas_seguras = 1" in control_post
    assert "FK_EXTRA_INSEGURA" in control_post
    assert "ELSE 'REVISAR'" in control_post
    assert "c.confdeltype = 'r'" in control_post
    assert "c.convalidated" in control_post
    assert "c.conkey = ARRAY[e.cliente_attnum]::smallint[]" in control_post
    assert "c.confkey = ARRAY[e.clientes_cliente_attnum]::smallint[]" in control_post
    assert "pagos_cliente_id_fkey" not in control_post
    assert "envios_cliente_id_fkey" not in control_post


def test_pre_y_postflight_bloquean_fc_no_vacia_con_normalizacion_vacia():
    preflight = (RAIZ / "scripts" / "preflight_cuenta_ambitos.sql").read_text(
        encoding="utf-8"
    )
    postflight = (RAIZ / "scripts" / "postflight_cuenta_ambitos.sql").read_text(
        encoding="utf-8"
    )

    control_datos = (
        "NULLIF(BTRIM(COALESCE(nro_fc, '')), '') IS NOT NULL",
        "UPPER(BTRIM(nro_fc)), '[^A-Z0-9]', '', 'g'",
        ") = ''",
    )
    for fragmento in control_datos:
        assert fragmento in preflight
        assert fragmento in postflight

    assert "Debe devolver 0 filas antes de validar ck_envios_nro_fc_valida" in preflight
    assert "c.conname = 'ck_envios_nro_fc_valida'" in postflight
    assert "c.contype = 'c'" in postflight
    assert "WHEN NOT c.convalidated THEN 'NO_VALIDADA'" in postflight


def test_resumen_decimal_cierra_aplicado_sin_imputar_y_consolidado():
    resumen = cc._armar_resumen_ambitos({
        "debe_nacional": Decimal("100.00"),
        "debe_internacional": Decimal("200.00"),
        "debe_sin_clasificar": Decimal("50.00"),
        "facturado_nacional": Decimal("80.00"),
        "facturado_internacional": Decimal("150.00"),
        "facturado_sin_clasificar": Decimal("20.00"),
        "haber_nacional": Decimal("60.00"),
        "haber_internacional": Decimal("40.00"),
        "pagos_aprobados": Decimal("150.00"),
        "pagos_pendientes": Decimal("999.00"),
    })

    assert resumen["nacional"]["saldo_ars"] == Decimal("40.00")
    assert resumen["internacional"]["saldo_ars"] == Decimal("160.00")
    assert resumen["consolidado"]["haber_aplicado_ars"] == Decimal("100.00")
    assert resumen["consolidado"]["pagos_aprobados_ars"] == Decimal("150.00")
    assert resumen["consolidado"]["saldo_ars"] == Decimal("200.00")
    assert resumen["pagos_pendientes_ars"] == Decimal("999.00")
    assert resumen["nacional"]["haber_ars"] == Decimal("60.00")
    assert resumen["nacional"]["facturado_ars"] == Decimal("80.00")
    assert resumen["nacional"]["pendiente_facturacion_ars"] == Decimal("20.00")
    assert resumen["internacional"]["haber_ars"] == Decimal("40.00")
    assert resumen["consolidado"]["facturado_ars"] == Decimal("250.00")
    assert resumen["consolidado"]["pendiente_facturacion_ars"] == Decimal("100.00")
    assert resumen["consolidado"]["haber_ars"] == Decimal("150.00")
    assert resumen["credito_sin_imputar_ars"] == Decimal("50.00")
    assert resumen["cargos_sin_clasificar_ars"] == Decimal("50.00")


def test_resumen_falla_cerrado_si_aplicaciones_superan_aprobado():
    with pytest.raises(RuntimeError, match="superan los pagos aprobados"):
        cc._armar_resumen_ambitos({
            "haber_nacional": "80",
            "haber_internacional": "30",
            "pagos_aprobados": "100",
        })


def test_diferencias_aplicadas_suman_debito_o_credito_sin_reescribir_envio():
    resumen = cc._armar_resumen_ambitos({
        "debe_internacional": "10000",
        "ajuste_debito_internacional": "10000",
        "ajuste_credito_internacional": "2500",
        "haber_internacional": "5000",
        "pagos_aprobados": "5000",
    })

    assert resumen["internacional"]["envios_ars"] == Decimal("10000.00")
    assert resumen["internacional"]["debe_ars"] == Decimal("20000.00")
    assert resumen["internacional"]["haber_ars"] == Decimal("7500.00")
    assert resumen["internacional"]["saldo_ars"] == Decimal("12500.00")
    assert resumen["consolidado"]["diferencias_netas_ars"] == Decimal("7500.00")


class _CursorResolver:
    def __init__(self, *, estado="PENDIENTE", monto="100.00", aplicaciones=None):
        self.estado = estado
        self.monto = Decimal(monto)
        self.aplicaciones = dict(aplicaciones or {})
        self.one = None
        self.all = []
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = None
        self.all = []
        if "FROM pagos WHERE id = %s FOR UPDATE" in compacto:
            self.one = {
                "id": params[0], "cliente_id": "TEST",
                "monto_ars": self.monto, "estado": self.estado,
            }
        elif "UPDATE pagos SET estado = 'APROBADO'" in compacto:
            self.estado = "APROBADO"
            self.one = {"id": params[0]}
        elif "UPDATE pagos SET estado = 'RECHAZADO'" in compacto:
            self.estado = "RECHAZADO"
            self.one = {"id": params[0]}
        elif "FROM pagos_aplicaciones" in compacto and compacto.startswith("SELECT"):
            self.all = [
                {"ambito": ambito, "monto_ars": monto, "estado": "APLICADA"}
                for ambito, monto in sorted(self.aplicaciones.items())
            ]
        elif compacto.startswith("DELETE FROM pagos_aplicaciones"):
            self.aplicaciones.clear()
        elif compacto.startswith("INSERT INTO pagos_aplicaciones"):
            _pago_id, ambito, monto = params
            self.aplicaciones[ambito] = Decimal(monto)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all


@contextmanager
def _conexion(cursor):
    class Conn:
        def cursor(self):
            return cursor

    yield Conn()


def test_resolver_aprueba_y_aplica_por_ambito_en_una_transaccion(monkeypatch):
    cursor = _CursorResolver()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(cc, "registrar_evento_con_cursor", lambda *_a, **_k: None)

    cambio = cc.resolver_pago(
        7,
        aprobar=True,
        aplicaciones={"nacional": "60.10", "INTERNACIONAL": Decimal("30.20")},
    )

    assert cambio is True
    assert cursor.estado == "APROBADO"
    assert cursor.aplicaciones == {
        "NACIONAL": Decimal("60.10"),
        "INTERNACIONAL": Decimal("30.20"),
    }
    sql = " ".join(sentencia for sentencia, _ in cursor.ejecutadas)
    assert "FOR UPDATE" in sql
    assert sql.index("UPDATE pagos SET estado = 'APROBADO'") < sql.index(
        "INSERT INTO pagos_aplicaciones"
    )


def test_resolver_aprobado_es_inmutable_aun_con_decision_explicita(monkeypatch):
    cursor = _CursorResolver(
        estado="APROBADO", aplicaciones={"NACIONAL": Decimal("20.00")}
    )
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(cc, "registrar_evento_con_cursor", lambda *_a, **_k: None)

    assert cc.resolver_pago(7, aprobar=True) is False
    assert cursor.aplicaciones == {"NACIONAL": Decimal("20.00")}

    assert cc.resolver_pago(
        7, aprobar=True, aplicaciones={"INTERNACIONAL": "75"}
    ) is False
    assert cursor.aplicaciones == {"NACIONAL": Decimal("20.00")}
    assert not any(
        sql.startswith("DELETE FROM pagos_aplicaciones")
        for sql, _ in cursor.ejecutadas
    )


def test_resolver_rechaza_suma_mayor_antes_de_borrar(monkeypatch):
    cursor = _CursorResolver(
        estado="PENDIENTE", monto="100", aplicaciones={"NACIONAL": Decimal("20")}
    )
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))

    with pytest.raises(ValueError, match="superan el monto"):
        cc.resolver_pago(
            7,
            aprobar=True,
            aplicaciones={"NACIONAL": "80.01", "INTERNACIONAL": "20"},
        )
    assert cursor.aplicaciones == {"NACIONAL": Decimal("20")}
    assert not any(
        sql.startswith("DELETE FROM pagos_aplicaciones")
        for sql, _ in cursor.ejecutadas
    )


class _CursorEnvio:
    def __init__(self):
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params

    def fetchone(self):
        return {"id": 44}


def test_cargo_manual_exige_ambito_y_persiste_decimal(monkeypatch):
    with pytest.raises(ValueError, match="NACIONAL o INTERNACIONAL"):
        cc.registrar_envio("TEST", "2026-08-17", 100, ambito="")
    with pytest.raises(ValueError, match="NC no es FC"):
        cc.registrar_envio("TEST", "2026-08-17", 100, ambito="NACIONAL", estado="NC")
    with pytest.raises(ValueError, match="número de factura válido"):
        cc.registrar_envio(
            "TEST", "2026-08-17", 100, nro_fc="---", ambito="NACIONAL"
        )

    cursor = _CursorEnvio()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(cc, "registrar_evento_con_cursor", lambda *_a, **_k: None)
    cc.registrar_envio(
        "TEST", "2026-08-17", "100.105", nro_fc="   ",
        descripcion="Cargo", ambito="nacional"
    )

    assert "factura_nombre, ambito" in cursor.sql
    assert cursor.params[2] == ""
    assert cursor.params[3] == Decimal("100.11")
    assert cursor.params[-2] == "NACIONAL"
    assert cursor.params[-1] is None


class _CursorRegistrarPago:
    def __init__(self):
        self.one = None
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = {"id": 81} if compacto.startswith("INSERT INTO pagos ") else None

    def fetchone(self):
        return self.one


@pytest.mark.parametrize(
    "estado,esperado",
    [("PENDIENTE", "SOLICITADA"), ("APROBADO", "APLICADA")],
)
def test_registrar_pago_distingue_solicitud_de_haber(monkeypatch, estado, esperado):
    cursor = _CursorRegistrarPago()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(cc, "registrar_evento_con_cursor", lambda *_a, **_k: None)

    pago_id = cc.registrar_pago(
        "TEST", "2026-08-17", "100.00", "transferencia",
        estado=estado, aplicaciones={"NACIONAL": "40.00"},
    )

    assert pago_id == 81
    insercion = next(
        (sql, params) for sql, params in cursor.ejecutadas
        if sql.startswith("INSERT INTO pagos_aplicaciones")
    )
    assert insercion[1] == (81, "NACIONAL", Decimal("40.00"), esperado)


def test_registrar_pago_rechaza_solicitud_sobregirada_sin_escribir(monkeypatch):
    cursor = _CursorRegistrarPago()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    with pytest.raises(ValueError, match="superan el monto"):
        cc.registrar_pago(
            "TEST", "2026-08-17", "100", "transferencia",
            estado="PENDIENTE",
            aplicaciones={"NACIONAL": "90", "INTERNACIONAL": "10.01"},
        )
    assert cursor.ejecutadas == []


class _CursorPagoIdempotente:
    def __init__(self):
        self.insertado = False
        self.one = None
        self.all = []
        self.ejecutadas = []
        self.aplicaciones = {}
        self.fila = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = None
        self.all = []
        if compacto.startswith("INSERT INTO pagos "):
            if not self.insertado:
                self.insertado = True
                self.fila = {
                    "id": 81, "fecha": date.fromisoformat(params[1]),
                    "monto_ars": params[2], "metodo": params[3],
                    "referencia": params[4], "estado": params[6],
                }
                self.one = {"id": 81}
        elif compacto.startswith("SELECT id, fecha, monto_ars, metodo"):
            self.one = dict(self.fila)
        elif compacto.startswith("INSERT INTO pagos_aplicaciones"):
            _pago, ambito, monto, estado = params
            self.aplicaciones[ambito] = (monto, estado)
        elif compacto.startswith("SELECT ambito, monto_ars, estado"):
            self.all = [
                {"ambito": ambito, "monto_ars": monto, "estado": estado}
                for ambito, (monto, estado) in sorted(self.aplicaciones.items())
            ]

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all


def test_registrar_pago_idempotente_no_duplica_aplicacion_ni_auditoria(monkeypatch):
    cursor = _CursorPagoIdempotente()
    auditorias = []
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(
        cc, "registrar_evento_con_cursor",
        lambda *_a, **kwargs: auditorias.append(kwargs),
    )
    clave = "A" * 32
    argumentos = dict(
        cliente_id="test", fecha="2026-08-17", monto_ars="100.00",
        metodo="transferencia", referencia="REF",
        estado="PENDIENTE", aplicaciones={"NACIONAL": "40"},
        idempotency_key=clave,
    )

    assert cc.registrar_pago(**argumentos) == 81
    assert cc.registrar_pago(**argumentos) == 81
    assert len(auditorias) == 1
    assert sum(
        sql.startswith("INSERT INTO pagos_aplicaciones")
        for sql, _ in cursor.ejecutadas
    ) == 1
    assert any("ON CONFLICT (cliente_id, idempotency_key)" in sql
               for sql, _ in cursor.ejecutadas)

    with pytest.raises(ValueError, match="otra aplicación contable"):
        cc.registrar_pago(
            **{**argumentos, "aplicaciones": {"INTERNACIONAL": "40"}}
        )
    assert len(auditorias) == 1


def test_idempotency_key_rechaza_formato_inseguro_sin_escribir(monkeypatch):
    cursor = _CursorRegistrarPago()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    with pytest.raises(ValueError, match="32 y 128"):
        cc.registrar_pago(
            "TEST", "2026-08-17", "10", "transferencia",
            idempotency_key="corta",
        )
    assert cursor.ejecutadas == []


class _CursorPendientes:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())

    def fetchall(self):
        return [{
            "id": 3, "monto_nacional": Decimal("25.00"),
            "monto_internacional": Decimal("0"),
        }]


def test_pagos_pendientes_precarga_solicitud_en_una_query(monkeypatch):
    cursor = _CursorPendientes()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    pagos = cc.pagos_pendientes()
    assert "LEFT JOIN pagos_aplicaciones" in cursor.sql
    assert "GROUP BY p.id" in cursor.sql
    assert pagos[0]["aplicaciones"] == {"NACIONAL": Decimal("25.00")}


class _CursorMovimientos:
    def __init__(self):
        self.one = None
        self.all = []
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = None
        self.all = []
        if "SELECT COUNT(*) AS total FROM filtrados" in compacto:
            self.one = {"total": 1}
        else:
            self.all = [{
                "fecha": date(2026, 8, 17),
                "created_at": datetime(2026, 8, 17, tzinfo=timezone.utc),
                "tipo_orden": 40, "origen_id": 9, "tipo": "FC",
                "ambito": "NACIONAL", "concepto": "FC-1", "referencia": None,
                "debe_ars": Decimal("10.25"), "haber_ars": Decimal("0"),
                "monto_ars": Decimal("10.25"), "estado": "ACTIVO",
                "facturado": True, "envio_id": 9, "pago_id": None,
                "solicitud_id": 19,
                "archivo_url": "/portal/facturas/9/pdf",
                "numero_guia": "TRACK-9", "destinatario": "Destino SA",
                "remitente": "Origen SA",
                "valor_envio_ars": Decimal("10.2500"),
                "numero_factura": "FC-1",
            }]

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all


def test_movimientos_paginados_filtra_en_sql_y_conserva_factura(monkeypatch):
    cursor = _CursorMovimientos()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    pagina = cc.movimientos_cuenta_paginados(
        "test", "nacional", "cargos", 1, 25
    )

    assert pagina["total_resultados"] == 1
    assert pagina["items"][0]["tipo"] == "FC"
    assert pagina["items"][0]["debe_ars"] == Decimal("10.25")
    assert pagina["items"][0]["valor_envio_ars"] == Decimal("10.25")
    assert pagina["items"][0]["numero_guia"] == "TRACK-9"
    assert pagina["items"][0]["destinatario"] == "Destino SA"
    assert pagina["items"][0]["remitente"] == "Origen SA"
    assert pagina["items"][0]["archivo_url"] == "/portal/facturas/9/pdf"
    sql = cursor.ejecutadas[0][0]
    assert "WHERE estado = 'APLICADA'" in sql
    assert "p.estado = 'PENDIENTE'" in sql
    assert "0::numeric, 0::numeric, p.monto_ars, 'PENDIENTE'" in sql
    assert "LEFT JOIN solicitudes_guia s" in sql
    assert "AS numero_guia" in sql
    assert "AS destinatario" in sql
    assert "AS remitente" in sql
    assert "AS valor_envio_ars" in sql
    assert cursor.ejecutadas[0][1][5:7] == ("NACIONAL", "NACIONAL")


class _CursorClasificar:
    def __init__(self, ambito=None):
        self.ambito = ambito
        self.one = None
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = None
        if compacto.startswith("SELECT id, cliente_id, monto_ars, ambito"):
            if params[1] == "DUENO":
                self.one = {
                    "id": params[0], "cliente_id": "DUENO",
                    "monto_ars": Decimal("500.00"), "ambito": self.ambito,
                }
        elif compacto.startswith("UPDATE envios SET ambito"):
            self.ambito = params[0]
            self.one = {
                "id": params[1], "cliente_id": params[2],
                "monto_ars": Decimal("500.00"), "ambito": self.ambito,
            }

    def fetchone(self):
        return self.one


def test_clasificar_cargo_preserva_dueno_y_no_reclasifica(monkeypatch):
    cursor = _CursorClasificar()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(cc, "registrar_evento_con_cursor", lambda *_a, **_k: None)

    assert cc.clasificar_cargo_sin_ambito(
        9, "NACIONAL", cliente_id="OTRO"
    ) is False
    assert cursor.ambito is None

    resultado = cc.clasificar_cargo_sin_ambito(
        9, "internacional", cliente_id="dueno"
    )
    assert resultado == {
        "id": 9, "cliente_id": "DUENO", "monto_ars": Decimal("500.00"),
        "ambito": "INTERNACIONAL",
    }
    assert any("FOR UPDATE" in sql for sql, _ in cursor.ejecutadas)
    cantidad_updates = sum(
        sql.startswith("UPDATE envios SET ambito") for sql, _ in cursor.ejecutadas
    )

    assert cc.clasificar_cargo_sin_ambito(
        9, "NACIONAL", cliente_id="DUENO"
    ) is False
    assert cursor.ambito == "INTERNACIONAL"
    assert sum(
        sql.startswith("UPDATE envios SET ambito") for sql, _ in cursor.ejecutadas
    ) == cantidad_updates


class _CursorEnvioIdempotente:
    def __init__(self):
        self.insertado = False
        self.one = None
        self.ejecutadas = []
        self.fila = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = None
        if compacto.startswith("INSERT INTO envios"):
            if not self.insertado:
                self.insertado = True
                self.fila = {
                    "id": 44, "fecha": date.fromisoformat(params[1]),
                    "nro_fc": params[2], "monto_ars": params[3],
                    "estado": params[4], "descripcion": params[5],
                    "tracking": params[6], "ambito": params[9],
                }
                self.one = {"id": 44}
        elif compacto.startswith("SELECT id, fecha, nro_fc"):
            self.one = dict(self.fila)

    def fetchone(self):
        return self.one


def test_registrar_envio_idempotente_no_duplica_auditoria_y_valida_payload(
    monkeypatch,
):
    cursor = _CursorEnvioIdempotente()
    auditorias = []
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(
        cc, "registrar_evento_con_cursor",
        lambda *_a, **kwargs: auditorias.append(kwargs),
    )
    clave = "B" * 32
    argumentos = dict(
        cliente_id="test", fecha="2026-08-17", monto_ars="150.00",
        nro_fc="FC-001", estado="ACTIVO", descripcion="Servicio",
        tracking="TRACK-1", ambito="INTERNACIONAL",
        idempotency_key=clave,
    )

    assert cc.registrar_envio(**argumentos) == 44
    assert cc.registrar_envio(**argumentos) == 44
    assert len(auditorias) == 1
    assert any("ON CONFLICT (cliente_id, idempotency_key)" in sql
               for sql, _ in cursor.ejecutadas)

    with pytest.raises(ValueError, match="otro cargo"):
        cc.registrar_envio(**{**argumentos, "estado": "CANCELADO"})
    assert len(auditorias) == 1


class _FcDuplicada(cc.psycopg2.errors.UniqueViolation):
    class _Diag:
        constraint_name = "uq_envios_fc_normalizada"

    @property
    def diag(self):
        return self._Diag()


class _CursorFcDuplicada:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql, _params=None):
        raise _FcDuplicada()


def test_registrar_envio_traduce_conflicto_de_fc_sin_usar_tracking(monkeypatch):
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(_CursorFcDuplicada()))
    with pytest.raises(ValueError, match="factura con ese número"):
        cc.registrar_envio(
            "TEST", "2026-08-17", "100", nro_fc="fc-001",
            tracking="TRACK-PUEDE-REPETIR", ambito="NACIONAL",
        )


class _CursorFacturar:
    def __init__(self, *, estado="ACTIVO"):
        self.estado = estado
        self.nro_fc = None
        self.factura_pdf = None
        self.factura_nombre = None
        self.monto = Decimal("500.00")
        self.ambito = "INTERNACIONAL"
        self.one = None
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = None
        if compacto.startswith("SELECT id, cliente_id, monto_ars, estado"):
            if params[1] == "DUENO":
                self.one = {
                    "id": params[0], "cliente_id": "DUENO",
                    "monto_ars": self.monto, "estado": self.estado,
                    "ambito": self.ambito, "nro_fc": self.nro_fc,
                    "factura_pdf": self.factura_pdf,
                    "factura_nombre": self.factura_nombre,
                }
        elif compacto.startswith("UPDATE envios SET nro_fc"):
            fc, binario, nombre, envio_id, cliente_id = params
            if (
                self.estado == "ACTIVO" and not self.nro_fc
                and cliente_id == "DUENO"
            ):
                self.nro_fc = fc
                self.factura_pdf = bytes(getattr(binario, "adapted", binario))
                self.factura_nombre = nombre
                self.one = {
                    "id": envio_id, "cliente_id": cliente_id,
                    "monto_ars": self.monto, "ambito": self.ambito,
                    "nro_fc": fc, "factura_nombre": nombre,
                }

    def fetchone(self):
        return self.one


def test_facturar_cargo_es_atomico_idempotente_y_preserva_importes(monkeypatch):
    cursor = _CursorFacturar()
    auditorias = []
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(
        cc, "registrar_evento_con_cursor",
        lambda *_a, **kwargs: auditorias.append(kwargs),
    )
    pdf = b"%PDF-1.4\nTAURO"

    assert cc.facturar_cargo(
        9, "OTRO", "FC-900", pdf, "fc-900.pdf"
    ) is False
    resultado = cc.facturar_cargo(
        9, "dueno", "FC-900", pdf, "fc-900.pdf",
        actor_tipo="admin", actor_ref="operador",
    )
    assert resultado == {
        "id": 9, "cliente_id": "DUENO", "monto_ars": Decimal("500.00"),
        "ambito": "INTERNACIONAL", "nro_fc": "FC-900",
        "factura_nombre": "fc-900.pdf",
    }
    assert cursor.monto == Decimal("500.00")
    assert cursor.ambito == "INTERNACIONAL"
    assert len(auditorias) == 1
    assert auditorias[0]["event"] == "cuenta.facturar_cargo"

    # El mismo payload garantiza la misma factura, sin UPDATE ni audit extra.
    updates_antes = sum(
        sql.startswith("UPDATE envios SET nro_fc")
        for sql, _ in cursor.ejecutadas
    )
    assert cc.facturar_cargo(
        9, "DUENO", "FC-900", pdf, "fc-900.pdf"
    ) == resultado
    assert len(auditorias) == 1
    assert sum(
        sql.startswith("UPDATE envios SET nro_fc")
        for sql, _ in cursor.ejecutadas
    ) == updates_antes

    with pytest.raises(ValueError, match="otro comprobante"):
        cc.facturar_cargo(
            9, "DUENO", "FC-900", b"%PDF-1.4\nDISTINTO", "fc-900.pdf"
        )
    with pytest.raises(ValueError, match="otro comprobante"):
        cc.facturar_cargo(
            9, "DUENO", "FC-901", pdf, "fc-900.pdf"
        )

    select_sql = next(
        sql for sql, _ in cursor.ejecutadas
        if sql.startswith("SELECT id, cliente_id, monto_ars, estado")
    )
    update_sql = next(
        sql for sql, _ in cursor.ejecutadas
        if sql.startswith("UPDATE envios SET nro_fc")
    )
    assert "FOR UPDATE" in select_sql
    asignacion = update_sql.split("WHERE", 1)[0]
    assert "monto_ars" not in asignacion
    assert "ambito" not in asignacion


class _CursorFacturarFcDuplicada(_CursorFacturar):
    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        if compacto.startswith("UPDATE envios SET nro_fc"):
            raise _FcDuplicada()
        super().execute(sql, params)


def test_facturar_cargo_traduce_fc_global_duplicada(monkeypatch):
    cursor = _CursorFacturarFcDuplicada()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    with pytest.raises(ValueError, match="factura con ese número"):
        cc.facturar_cargo(
            9, "DUENO", "FC-REPETIDA", b"%PDF-1.4\nTAURO", "fc.pdf"
        )


def test_facturar_cargo_exige_pdf_antes_de_bloquear(monkeypatch):
    cursor = _CursorFacturar()
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    with pytest.raises(ValueError, match="formato PDF"):
        cc.facturar_cargo(
            9, "DUENO", "FC-1", b"\x89PNGcontenido", "factura.png"
        )
    assert cursor.ejecutadas == []


class _CursorCancelar:
    def __init__(self):
        self.estado = "ACTIVO"
        self.nro_fc = None
        self.one = None
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.ejecutadas.append((compacto, params))
        self.one = None
        if compacto.startswith("UPDATE envios SET estado = 'CANCELADO'"):
            _envio_id, ownership, _ownership_repetido = params
            if (
                self.estado == "ACTIVO" and not self.nro_fc
                and ownership in (None, "DUENO")
            ):
                self.estado = "CANCELADO"
                self.one = {
                    "id": 9, "cliente_id": "DUENO",
                    "monto_ars": Decimal("500.00"),
                    "ambito": "NACIONAL", "estado": "CANCELADO",
                }

    def fetchone(self):
        return self.one


def test_cancelar_envio_es_transicion_unica_con_ownership_y_auditoria(monkeypatch):
    cursor = _CursorCancelar()
    auditorias = []
    monkeypatch.setattr(cc, "get_conn", lambda: _conexion(cursor))
    monkeypatch.setattr(
        cc, "registrar_evento_con_cursor",
        lambda *_a, **kwargs: auditorias.append(kwargs),
    )

    assert cc.cancelar_envio(9, cliente_id="OTRO") is False
    cursor.nro_fc = "FC-900"
    assert cc.cancelar_envio(9, cliente_id="DUENO") is False
    assert cursor.estado == "ACTIVO"
    cursor.nro_fc = None
    resultado = cc.cancelar_envio(
        9, cliente_id="dueno", actor_tipo="admin", actor_ref="operador"
    )
    assert resultado["estado"] == "CANCELADO"
    assert resultado["monto_ars"] == Decimal("500.00")
    assert cc.cancelar_envio(9, cliente_id="DUENO") is False
    assert len(auditorias) == 1
    assert auditorias[0]["event"] == "cuenta.cancelar_cargo"
    sql = cursor.ejecutadas[0][0]
    assert "estado = 'ACTIVO'" in sql
    assert "NULLIF(BTRIM(nro_fc), '') IS NULL" in sql
    assert "RETURNING id, cliente_id" in sql
