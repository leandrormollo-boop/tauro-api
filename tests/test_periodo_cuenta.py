"""Un corte visual conserva el saldo, pagos antiguos y centavos del libro."""

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import os
from urllib.parse import urlsplit
import uuid

import psycopg2
import psycopg2.extras
import pytest

from servicios import cuenta_corriente as cc
from servicios import periodo_cuenta as pc


INICIO = date(2026, 9, 1)


@pytest.mark.parametrize("cliente,esperado", [
    ("WAIMAO", INICIO), (" waimao ", INICIO), ("WAIMAO2", None),
    ("WAIMAO CLIENTE", None), ("OTRO", None), (None, None), ("", None),
])
def test_politica_solo_cliente_exacto_sin_base(cliente, esperado, monkeypatch):
    monkeypatch.setattr(pc, "get_conn", lambda: pytest.fail("La política no abre DB"))
    assert pc.inicio_cuenta_cliente(cliente) == esperado


@pytest.mark.parametrize("cliente,inicio", [("", INICIO), ("A"*81, INICIO), ("WAIMAO", "2026-09-01"), ("WAIMAO", datetime(2026, 9, 1))])
def test_parametros_invalidos_no_abren_base(cliente, inicio, monkeypatch):
    monkeypatch.setattr(pc, "get_conn", lambda: pytest.fail("No abrir DB"))
    with pytest.raises(ValueError):
        pc.obtener_periodo_cuenta(cliente, inicio)


def _grupo(periodo, ambito, clase, importe, **extra):
    return {"periodo": periodo, "ambito": ambito, "clase": clase,
            "importe": Decimal(importe), **extra}


@pytest.mark.parametrize("importe,anterior,neto,total,redondeo", [
    ("0.005", "0.01", "0.01", "0.01", "-0.01"),
    ("0.004", "0.00", "0.00", "0.01", "0.01"),
    ("0.010", "0.01", "0.01", "0.02", "0.00"),
])
def test_redondeo_explicito_conserva_regla_del_resumen(importe, anterior, neto, total, redondeo):
    filas = [_grupo(p, "NACIONAL", "DEBITO", importe) for p in ("ANTERIOR", "DESDE")]
    r = pc._presentar_periodo(filas, INICIO)
    assert r["saldo_anterior_ars"] == Decimal(anterior)
    assert r["neto_desde_ars"] == Decimal(neto)
    assert r["saldo_total_ars"] == Decimal(total)
    assert r["redondeo_ars"] == Decimal(redondeo)
    assert r["saldo_anterior_ars"] + r["neto_desde_ars"] + r["redondeo_ars"] == r["saldo_total_ars"]


def test_redondeo_por_ambito_no_compensa_creditos_ni_desaparece():
    r = pc._presentar_periodo([
        _grupo("DESDE", "NACIONAL", "DEBITO", "0.005"),
        _grupo("DESDE", "INTERNACIONAL", "DEBITO", "0.005"),
        _grupo("DESDE", "NACIONAL", "CREDITO", "0.005"),
    ], INICIO)
    assert r["cargos_desde_ars"] == Decimal("0.02")
    assert r["creditos_desde_ars"] == Decimal("0.01")
    assert r["neto_desde_ars"] == Decimal("0.01")


@pytest.mark.parametrize("importe", [None, "NaN", "Infinity", "-1"])
def test_importe_desconocido_no_se_convierte_en_cero(importe):
    fila = {"periodo": "ANTERIOR", "ambito": "NACIONAL", "clase": "ENVIO", "importe": importe}
    with pytest.raises(ValueError):
        pc._presentar_periodo([fila], INICIO)
    fila["importe"] = Decimal("100")
    fila["sin_importe_cantidad"] = 1
    with pytest.raises(ValueError):
        pc._presentar_periodo([fila], INICIO)


def test_sin_fecha_queda_anterior_y_se_advierte():
    r = pc._presentar_periodo([
        _grupo("ANTERIOR", "NACIONAL", "ENVIO", "100", sin_fecha_cantidad=2),
        _grupo("DESDE", "CONSOLIDADO", "PAGO", "25"),
    ], INICIO)
    assert r["saldo_anterior_ars"] == Decimal("100")
    assert r["neto_desde_ars"] == Decimal("-25")
    assert r["saldo_total_ars"] == Decimal("75")
    assert r["sin_fecha_cantidad"] == 2
    assert "sin fecha" in r["advertencia_sin_fecha"]
    assert "estados actuales" in r["criterio"]


@pytest.fixture
def periodo_db(monkeypatch):
    url = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("requiere TAURO_TEST_DATABASE_URL local y aislada")
    assert urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
    schema = "test_periodo_" + uuid.uuid4().hex
    admin = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute("""
            CREATE TABLE envios(id integer PRIMARY KEY,cliente_id text,fecha date,
                monto_ars numeric(14,2),estado text,solicitud_id integer,ambito text,nro_fc text);
            CREATE TABLE pagos(id integer PRIMARY KEY,cliente_id text,fecha date,
                monto_ars numeric(14,2),estado text);
            CREATE TABLE ajustes_cliente(id integer PRIMARY KEY,solicitud_id integer,
                monto_ars numeric(18,4),tipo text,estado text,aplicado_at timestamptz);
            CREATE TABLE pagos_aplicaciones(pago_id integer,ambito text,monto_ars numeric,
                estado text,envio_id integer);
            CREATE TABLE facturas_cliente(id integer,estado text);
            CREATE TABLE facturas_cliente_items(factura_id integer,envio_id integer,ajuste_id integer);
        """)

    @contextmanager
    def conectar():
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(pc, "get_conn", conectar)
    monkeypatch.setattr(cc, "get_conn", conectar)
    try:
        yield conectar
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def test_particion_sql_reconcilia_con_resumen_real_y_pago_a_envio_anterior(periodo_db):
    with periodo_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO envios(id,cliente_id,fecha,monto_ars,estado,solicitud_id,ambito) VALUES
                    (1,'WAIMAO','2026-08-10',100,'ACTIVO',1,'NACIONAL'),
                    (2,'WAIMAO','2026-09-10',120,'ACTIVO',2,'INTERNACIONAL'),
                    (3,'WAIMAO','2026-08-10',90,'CANCELADO',3,'NACIONAL'),
                    (4,'WAIMAO','2026-09-10',80,'NC',4,'NACIONAL'),
                    (5,'WAIMAO','2026-09-10',10,'ACTIVO',5,NULL),
                    (6,'WAIMAO',NULL,7,'ACTIVO',6,NULL),
                    (7,'AJENO','2026-09-10',90000,'ACTIVO',7,'NACIONAL');
                INSERT INTO pagos VALUES
                    (1,'WAIMAO','2026-08-10',30,NULL),
                    (2,'WAIMAO','2026-09-10',40,'APROBADO'),
                    (3,'WAIMAO','2026-09-10',60,'PENDIENTE'),
                    (4,'WAIMAO','2026-08-10',70,'RECHAZADO'),
                    (5,'AJENO','2026-09-10',5000,'APROBADO');
                INSERT INTO pagos_aplicaciones VALUES (2,'NACIONAL',40,'APLICADA',1);
                INSERT INTO ajustes_cliente VALUES
                    (1,1,5.005,'DEBITO','APLICADO','2026-09-01 01:00+00'),
                    (2,1,10.005,'DEBITO','APLICADO','2026-09-01 03:00+00'),
                    (3,1,-3.005,'CREDITO','APLICADO','2026-09-10 12:00+00'),
                    (4,1,9999,'DEBITO','PROPUESTO',NULL),
                    (5,2,-2,'CREDITO','APLICADO','2026-09-10 12:00+00'),
                    (6,1,1,'DEBITO','APLICADO',NULL),
                    (7,3,100,'DEBITO','APLICADO','2026-09-10 12:00+00');
            """)
    r = pc.obtener_periodo_cuenta(" waimao ", INICIO)
    actual = cc.resumen_cuenta_por_ambito("WAIMAO")
    assert r["saldo_anterior_ars"] == Decimal("83.01")
    assert r["cargos_desde_ars"] == Decimal("140.01")
    assert r["creditos_desde_ars"] == Decimal("5.01")
    assert r["pagos_desde_ars"] == Decimal("40.00")
    assert r["neto_desde_ars"] == Decimal("95.00")
    assert r["redondeo_ars"] == Decimal("-0.01")
    assert r["saldo_total_ars"] == actual["consolidado"]["saldo_ars"] == Decimal("178.00")
    assert r["sin_fecha_cantidad"] == 2
    assert r["saldo_anterior_ars"] + r["neto_desde_ars"] + r["redondeo_ars"] == r["saldo_total_ars"]
    assert pc.obtener_periodo_cuenta("WAIMAO' OR '1'='1", INICIO)["saldo_total_ars"] == 0


def test_fecha_corte_no_reescribe_estados_y_aprobacion_posterior_recalcula_anterior(periodo_db):
    with periodo_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO envios VALUES (1,'WAIMAO','2026-08-10',100,'ACTIVO',1,'NACIONAL',NULL)")
            cur.execute("INSERT INTO pagos VALUES (1,'WAIMAO','2026-08-30',25,'PENDIENTE')")
    antes = pc.obtener_periodo_cuenta("WAIMAO", INICIO)
    assert antes["saldo_anterior_ars"] == Decimal("100")
    with periodo_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pagos SET estado='APROBADO' WHERE id=1")
    despues = pc.obtener_periodo_cuenta("WAIMAO", INICIO)
    assert despues["saldo_anterior_ars"] == Decimal("75")
    assert despues["neto_desde_ars"] == 0
    assert "no es un cierre histórico auditado" in despues["criterio"]


def test_dos_ajustes_medios_centavos_concilian_periodo_lista_y_excel(periodo_db):
    from io import BytesIO
    from openpyxl import load_workbook
    from servicios.export_cuenta import generar_excel_cuenta

    with periodo_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE envios ADD COLUMN created_at timestamptz DEFAULT NOW(),
                    ADD COLUMN tracking text, ADD COLUMN descripcion text, ADD COLUMN factura_pdf bytea;
                ALTER TABLE pagos ADD COLUMN created_at timestamptz DEFAULT NOW(),
                    ADD COLUMN metodo text, ADD COLUMN referencia text, ADD COLUMN comprobante bytea;
                ALTER TABLE pagos_aplicaciones ADD COLUMN id integer,
                    ADD COLUMN updated_at timestamptz DEFAULT NOW(), ADD COLUMN factura_id integer;
                ALTER TABLE facturas_cliente ADD COLUMN cliente_id text, ADD COLUMN tipo text,
                    ADD COLUMN punto_venta integer, ADD COLUMN numero integer, ADD COLUMN pdf bytea;
                ALTER TABLE ajustes_cliente ADD COLUMN conciliacion_id integer,
                    ADD COLUMN motivo text, ADD COLUMN precio_nuevo_ars numeric DEFAULT 100;
                CREATE TABLE solicitudes_guia(id integer,cliente_id text,tracking text,
                    visible_cliente boolean DEFAULT true,test boolean DEFAULT false,
                    dest_nombre text,remitente_nombre text,etiqueta_cliente text,
                    remitente_ciudad text,remitente_pais text,dest_ciudad text,destino_pais text,
                    created_at timestamptz DEFAULT NOW(),ambito text,estado text,
                    numero_guia_tauro bigint,producto_alias text,cantidad integer DEFAULT 1);
                CREATE TABLE conciliaciones_envio(id integer,tax_cliente_ars numeric DEFAULT 0,
                    diferencia_flete_ars numeric DEFAULT 0,motivo_diferencia text DEFAULT 'OTRO',
                    precio_cliente_inicial_ars numeric DEFAULT 100,peso_cotizado_kg numeric,
                    peso_final_facturado_kg numeric,peso_base_facturado text);
                CREATE TABLE factura_courier_item_matches(solicitud_id integer,item_id integer,estado text);
                CREATE TABLE facturas_courier_items(id integer,descripcion text,concepto_tipo text);
                INSERT INTO envios(id,cliente_id,fecha,monto_ars,estado,solicitud_id,ambito)
                    VALUES (1,'WAIMAO','2026-08-10',100,'ACTIVO',1,'NACIONAL');
                INSERT INTO solicitudes_guia(id,cliente_id) VALUES (1,'WAIMAO');
                INSERT INTO conciliaciones_envio(id) VALUES (1),(2);
                INSERT INTO ajustes_cliente(id,solicitud_id,monto_ars,tipo,estado,aplicado_at,conciliacion_id)
                    VALUES (1,1,0.005,'DEBITO','APLICADO','2026-09-10 12:00+00',1),
                           (2,1,0.005,'DEBITO','APLICADO','2026-09-10 12:00+00',2);
            """)
    periodo = pc.obtener_periodo_cuenta("WAIMAO", INICIO)
    actual = cc.resumen_cuenta_por_ambito("WAIMAO")
    lista = cc.movimientos_cuenta_paginados("WAIMAO", desde="2026-09-01")
    neto_filas = sum((m["debe_ars"]-m["haber_ars"] for m in lista["items"]), Decimal("0"))
    libro = load_workbook(BytesIO(generar_excel_cuenta("WAIMAO", desde="2026-09-01")))
    filas = list(libro["Movimientos"].values)[1:]
    neto_excel = sum((Decimal(str(f[7]))-Decimal(str(f[8])) for f in filas), Decimal("0"))
    assert periodo["saldo_anterior_ars"] == Decimal("100.00")
    assert periodo["cargos_desde_ars"] == periodo["neto_desde_ars"] == neto_filas == neto_excel == Decimal("0.02")
    assert periodo["saldo_total_ars"] == actual["consolidado"]["saldo_ars"] == Decimal("100.01")
    assert periodo["redondeo_ars"] == Decimal("-0.01")
    assert periodo["saldo_anterior_ars"] + periodo["neto_desde_ars"] + periodo["redondeo_ars"] == periodo["saldo_total_ars"]

    # La misma regla aplica a los movimientos anteriores, no sólo a los nuevos.
    with periodo_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ajustes_cliente SET aplicado_at='2026-08-10 12:00+00'")
    anterior = pc.obtener_periodo_cuenta("WAIMAO", INICIO)
    assert anterior["saldo_anterior_ars"] == Decimal("100.02")
    assert anterior["neto_desde_ars"] == 0
    assert anterior["saldo_total_ars"] == Decimal("100.01")
    assert anterior["redondeo_ars"] == Decimal("-0.01")
