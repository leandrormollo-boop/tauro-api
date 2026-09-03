from decimal import Decimal
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from servicios.facturacion_clientes import (
    FacturacionClienteError,
    _normalizar_seleccion,
    numero_factura_visible,
)


ROOT = Path(__file__).resolve().parents[1]


def test_schema_modela_factura_items_exclusividad_e_inmutabilidad():
    schema = (ROOT / "sql" / "schema.sql").read_text()
    bloque = schema[schema.index("CREATE TABLE IF NOT EXISTS facturas_cliente (") :]

    assert "subtotal          NUMERIC(14,2)" in bloque
    assert "iva               NUMERIC(14,2)" in bloque
    assert "total             NUMERIC(14,2)" in bloque
    assert "CREATE TABLE IF NOT EXISTS facturas_cliente_items" in bloque
    assert "NUM_NONNULLS(envio_id, ajuste_id) = 1" in bloque
    assert "tauro_validar_factura_cliente_item" in bloque
    assert "FOR UPDATE OF a, e" in bloque
    assert "El cargo ya integra otra factura emitida" in bloque
    assert "DEFERRABLE INITIALLY DEFERRED" in bloque
    assert "'facturas_cliente_items'" in bloque


def test_facturador_nuevo_no_escribe_columnas_legacy_de_envios():
    servicio = (ROOT / "servicios" / "facturacion_clientes.py").read_text()
    assert "UPDATE envios SET nro_fc" not in servicio
    assert "factura_pdf =" not in servicio
    assert "INSERT INTO facturas_cliente (" in servicio
    assert "INSERT INTO facturas_cliente_items" in servicio


def test_seleccion_documental_es_estricta_y_sin_duplicados():
    assert _normalizar_seleccion(["E:12", "A:9"]) == [("E", 12), ("A", 9)]
    with pytest.raises(FacturacionClienteError, match="repetida"):
        _normalizar_seleccion(["E:12", "e:12"])
    with pytest.raises(FacturacionClienteError, match="inválida"):
        _normalizar_seleccion(["ENVIO:12"])


def test_numero_visible_preserva_formato_arca():
    assert numero_factura_visible("FC", 5, 123) == "FC 0005-00000123"


def test_templates_nuevos_compilan_y_portal_no_muestra_costos_internos():
    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    env.get_template("admin/factura_cliente_nueva.html")
    env.get_template("admin/facturas_cliente.html")
    env.get_template("portal/cuenta.html")
    portal = (ROOT / "templates" / "portal" / "cuenta.html").read_text()
    assert "Pendientes de facturar" in portal
    assert "Descargar PDF" in portal
    assert "costo_courier" not in portal
    assert "margen_tauro" not in portal


def test_total_se_calcula_con_decimal_en_servicio():
    servicio = (ROOT / "servicios" / "facturacion_clientes.py").read_text()
    assert 'sum((p["monto"] for p in partidas), Decimal("0"))' in servicio
    assert "float(" not in servicio
    assert Decimal("2990000.00") + Decimal("10000.00") == Decimal("3000000.00")

