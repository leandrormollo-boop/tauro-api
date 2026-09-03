from __future__ import annotations

import io
import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from pypdf import PdfReader

from servicios import cotizaciones_reseller as reseller
from servicios import configuracion_couriers_cliente as config


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()


def _cotizacion():
    return {
        "quote_id": "RQ-prueba-segura",
        "cliente_id": "WAIMAO",
        "ruta": "Argentina → Estados Unidos",
        "bultos": [{
            "cantidad": 1, "peso_kg": "10", "largo_cm": "50",
            "ancho_cm": "36", "alto_cm": "36",
        }],
        "peso_facturable_kg": Decimal("12.960"),
        "tiempo_estimado": "3 días",
        "precio_base_ars": Decimal("299000.00"),
        "courier": "DHL",
        "servicio": "Express Worldwide",
    }


def test_pdf_admite_subir_precio_y_no_muestra_base_interna(monkeypatch):
    monkeypatch.setattr(reseller, "_obtener", lambda *_: _cotizacion())

    contenido, nombre = reseller.generar_pdf(
        "WAIMAO", "RQ-prueba-segura", "350.000",
    )
    texto = "\n".join(
        pagina.extract_text() or ""
        for pagina in PdfReader(io.BytesIO(contenido)).pages
    )

    assert contenido.startswith(b"%PDF")
    assert nombre.endswith(".pdf")
    assert "Argentina" in texto and "Estados Unidos" in texto
    assert "12.960 kg" in texto
    assert "$ 350.000,00 ARS" in texto
    assert "costo" not in texto.lower()
    assert "margen" not in texto.lower()


def test_pdf_no_permite_bajar_del_precio_del_cliente(monkeypatch):
    monkeypatch.setattr(reseller, "_obtener", lambda *_: _cotizacion())
    with pytest.raises(ValueError, match="no bajar"):
        reseller.generar_pdf("WAIMAO", "RQ-prueba-segura", "298999")


def test_flag_esta_en_admin_y_el_boton_solo_en_bloque_reseller():
    admin_html = (ROOT / "templates/admin/cliente_acceso_precios.html").read_text()
    portal_html = (ROOT / "templates/portal/cotizar.html").read_text()
    endpoint = (ROOT / "endpoints/portal_cliente.py").read_text()

    assert 'name="es_reseller"' in admin_html
    assert "if es_reseller and op.reseller_quote_id" in portal_html
    assert "Descargar cotización PDF" in portal_html
    assert "/cotizaciones/reseller.pdf" in endpoint


def test_matriz_expone_flag_reseller():
    cliente = {
        "cliente_id": "WAIMAO", "nombre": "WAIMAO", "activo": True,
        "markup_pct": 25, "markup_tipo": "PCT", "markup_valor": 25,
        "courier_default": "", "tope_deuda_ars": None, "es_reseller": True,
    }
    assert config._armar_matriz(cliente, [])["es_reseller"] is True


@pytest.mark.skipif(not DATABASE_URL, reason="requiere TAURO_TEST_DATABASE_URL aislada")
def test_schema_persiste_snapshot_sin_columnas_de_costo(monkeypatch):
    schema = f"test_reseller_{uuid.uuid4().hex}"
    schema_sql = (ROOT / "sql/schema.sql").read_text()
    admin = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def conexion():
            conn = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
            )
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

        monkeypatch.setattr(reseller, "get_conn", conexion)
        with conexion() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clientes (cliente_id,email,es_reseller) "
                    "VALUES ('WAIMAO','reseller@example.invalid',TRUE)"
                )
        opciones = reseller.guardar_opciones(
            "WAIMAO", ruta="AR → US", bultos=_cotizacion()["bultos"],
            peso_facturable_kg="12.96", opciones=[{
                "precio_final_ars": "299000", "dias_estimados": "3 días",
                "carrier_nombre": "DHL", "servicio": "Express",
            }],
        )
        assert opciones[0]["reseller_quote_id"].startswith("RQ-")
        with conexion() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='cotizaciones_reseller'",
                    (schema,),
                )
                columnas = {fila["column_name"] for fila in cur.fetchall()}
        assert "precio_base_ars" in columnas
        assert not {"costo_courier", "margen_tauro", "tipo_cambio"} & columnas
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
