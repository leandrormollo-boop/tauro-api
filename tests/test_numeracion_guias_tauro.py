"""Número interno del PDF: estable, global y aislado del tracking del courier."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from servicios import solicitudes_guia as sg
from test_portal_pdf_unificado import _pdf_con_paginas


@pytest.mark.parametrize("pais,esperado", [("China", "CN"), ("ar", "AR"),
                                         ("Estados Unidos", "US"), (None, "ORIGEN SIN DATO")])
def test_nombre_utiliza_origen_canonico_sin_adivinar_pais(pais, esperado):
    nombre = sg.nombre_archivo_documentos_envio(
        cliente_id="WAIMAO", dest_nombre="María González",
        remitente_pais=pais, numero_guia_tauro=50300,
    )
    assert nombre == f"TAURO - WAIMAO - MARIA GONZALEZ - {esperado} - 50300.pdf"


@pytest.mark.parametrize("numero", [None, 0, 50299, "50300\r\nX: malicious", True, 50300.5])
def test_no_inventa_numero_o_copia_datos_arbitrarios_al_header(numero):
    with pytest.raises(ValueError, match="número interno"):
        sg.nombre_archivo_documentos_envio(cliente_id="WAIMAO", dest_nombre="Demo",
                                         remitente_pais="CN", numero_guia_tauro=numero)


@pytest.fixture
def guias_db(monkeypatch):
    url = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("requiere TAURO_TEST_DATABASE_URL aislada")
    schema = f"test_numero_guia_{uuid.uuid4().hex}"
    schema_sql = (Path(__file__).resolve().parents[1] / "sql/schema.sql").read_text()
    admin = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)
            cur.execute("""INSERT INTO clientes (cliente_id,email,nombre) VALUES
                ('WAIMAO','waimao@example.invalid','Razón social distinta'),
                ('DEMO','demo@example.invalid','Otra razón social')""")

        @contextmanager
        def conexion():
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

        monkeypatch.setattr(sg, "get_conn", conexion)
        monkeypatch.setattr(sg, "_avisar_tienda_origen", lambda *_: None)
        monkeypatch.setattr("servicios.cuenta_corriente.cargar_guia_emitida", lambda *_: True)
        yield conexion, schema_sql
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def crear(conexion, *, cliente="WAIMAO", estado="GUIA_LISTA", test=False,
          visible=True, label=True, invoice=None):
    pdf = _pdf_con_paginas((100, 200)) if label else None
    with conexion() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO solicitudes_guia (
                    cliente_id,producto_alias,destino_pais,remitente_pais,
                    dest_nombre,dest_direccion,dest_ciudad,dest_zip,
                    estado,test,visible_cliente,label_pdf,commercial_invoice_pdf,
                    tracking,courier,precio_tauro_ars
                ) VALUES (%s,'CARGA','UY','CN','MARSANTEX','Calle de prueba',
                          'Montevideo','11000',%s,%s,%s,%s,%s,%s,'DHL',100)
                RETURNING id
            """, (cliente, estado, test, visible, psycopg2.Binary(pdf) if pdf else None,
                  psycopg2.Binary(invoice) if invoice else None, "TEST-" + uuid.uuid4().hex))
            return cur.fetchone()["id"]


def numeros(conexion):
    with conexion() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, numero_guia_tauro, tracking FROM solicitudes_guia ORDER BY id")
            return cur.fetchall()


def test_secuencia_empieza_en_50300_y_es_global_para_las_cuentas(guias_db):
    conexion, _ = guias_db
    uno = crear(conexion)
    dos = crear(conexion, cliente="DEMO")
    primero = sg.preparar_documentos_envio_portal(uno, "waimao")
    segundo = sg.preparar_documentos_envio_portal(dos, "DEMO")
    assert primero["filename"] == "TAURO - WAIMAO - MARSANTEX - CN - 50300.pdf"
    assert segundo["filename"] == "TAURO - DEMO - MARSANTEX - CN - 50301.pdf"
    assert primero["numero_guia_tauro"] == 50300
    assert [r["numero_guia_tauro"] for r in numeros(conexion)] == [50300, 50301]


def test_descargas_simultaneas_de_la_misma_guia_no_cambian_numero(guias_db):
    conexion, _ = guias_db
    sid = crear(conexion)
    antes = numeros(conexion)[0]["tracking"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        archivos = list(pool.map(lambda _: sg.preparar_documentos_envio_portal(sid, "WAIMAO"), range(12)))
    assert {d["filename"] for d in archivos} == {"TAURO - WAIMAO - MARSANTEX - CN - 50300.pdf"}
    assert len({d["pdf"] for d in archivos}) == 1
    assert numeros(conexion)[0]["tracking"] == antes
    otro = crear(conexion)
    assert sg.preparar_documentos_envio_portal(otro, "WAIMAO")["numero_guia_tauro"] == 50301


def test_guias_distintas_concurrentes_reciben_numeros_distintos(guias_db):
    conexion, _ = guias_db
    ids = [crear(conexion) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        docs = list(pool.map(lambda sid: sg.preparar_documentos_envio_portal(sid, "WAIMAO"), ids))
    assert {d["numero_guia_tauro"] for d in docs} == set(range(50300, 50306))


@pytest.mark.parametrize("cambio", [{"estado": "CANCELADO"}, {"estado": "REEMPLAZADO"},
                                    {"test": True}, {"visible": False}, {"label": False}])
def test_no_asigna_numero_a_descargas_no_permitidas(guias_db, cambio):
    conexion, _ = guias_db
    sid = crear(conexion, **cambio)
    assert sg.preparar_documentos_envio_portal(sid, "WAIMAO") is None
    assert numeros(conexion)[0]["numero_guia_tauro"] is None
    valido = crear(conexion)
    assert sg.preparar_documentos_envio_portal(valido, "WAIMAO")["numero_guia_tauro"] == 50300


def test_otro_cliente_no_recibe_pdf_ni_consume_numero(guias_db):
    conexion, _ = guias_db
    sid = crear(conexion)
    assert sg.preparar_documentos_envio_portal(sid, "DEMO") is None
    assert numeros(conexion)[0]["numero_guia_tauro"] is None
    assert sg.preparar_documentos_envio_portal(sid, "WAIMAO")["numero_guia_tauro"] == 50300


def test_error_de_invoice_no_numera_una_descarga_fallida(guias_db):
    conexion, _ = guias_db
    sid = crear(conexion, invoice=b"no es PDF")
    with pytest.raises(ValueError):
        sg.preparar_documentos_envio_portal(sid, "WAIMAO")
    assert numeros(conexion)[0]["numero_guia_tauro"] is None
    valido = crear(conexion)
    assert sg.preparar_documentos_envio_portal(valido, "WAIMAO")["numero_guia_tauro"] == 50300


def test_numera_al_guardar_guia_confirmada_y_reintento_conserva_numero(guias_db):
    conexion, _ = guias_db
    sid = crear(conexion, estado="SOLICITADO", label=False)
    pdf = _pdf_con_paginas((100, 200))
    for _ in range(2):
        assert sg.guardar_guia_generada(sid, "DHL-TRACKING-REAL", pdf, courier="DHL")
        fila = numeros(conexion)[0]
        assert fila["numero_guia_tauro"] == 50300
        assert fila["tracking"] == "DHL-TRACKING-REAL"
    assert sg.preparar_documentos_envio_portal(sid, "WAIMAO")["filename"].endswith("CN - 50300.pdf")
    siguiente = crear(conexion)
    assert sg.preparar_documentos_envio_portal(siguiente, "WAIMAO")["numero_guia_tauro"] == 50301


def test_emision_de_prueba_no_consume_secuencia_comercial(guias_db):
    conexion, _ = guias_db
    sid = crear(conexion, test=True)
    sg.guardar_guia_generada(sid, "DHL-SANDBOX", _pdf_con_paginas((100, 200)), courier="DHL")
    assert numeros(conexion)[0]["numero_guia_tauro"] is None
    normal = crear(conexion)
    assert sg.preparar_documentos_envio_portal(normal, "WAIMAO")["numero_guia_tauro"] == 50300


def test_migracion_repetida_no_reinicia_numeracion(guias_db):
    conexion, schema_sql = guias_db
    uno = crear(conexion)
    assert sg.preparar_documentos_envio_portal(uno, "WAIMAO")["numero_guia_tauro"] == 50300
    with conexion() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
    dos = crear(conexion)
    assert sg.preparar_documentos_envio_portal(uno, "WAIMAO")["numero_guia_tauro"] == 50300
    assert sg.preparar_documentos_envio_portal(dos, "WAIMAO")["numero_guia_tauro"] == 50301


def test_base_impide_duplicar_numero_y_no_recicla_cancelados(guias_db):
    conexion, _ = guias_db
    uno, dos = crear(conexion), crear(conexion)
    sg.preparar_documentos_envio_portal(uno, "WAIMAO")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conexion() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE solicitudes_guia SET numero_guia_tauro=50300 WHERE id=%s", (dos,))
    with conexion() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE solicitudes_guia SET estado='CANCELADO' WHERE id=%s", (uno,))
    assert sg.preparar_documentos_envio_portal(dos, "WAIMAO")["numero_guia_tauro"] == 50301
