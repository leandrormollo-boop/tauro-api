from pathlib import Path

import pytest

from servicios.cuenta_corriente import _normalizar_destinos_documentales


ROOT = Path(__file__).resolve().parents[1]


def test_destinos_documentales_son_opacos_ordenados_y_unicos():
    assert _normalizar_destinos_documentales(["F:12", "e:9"]) == [
        ("F", 12), ("E", 9),
    ]
    assert _normalizar_destinos_documentales([]) == []
    assert _normalizar_destinos_documentales(None) is None
    with pytest.raises(ValueError, match="repetido"):
        _normalizar_destinos_documentales(["E:9", "e:9"])
    with pytest.raises(ValueError, match="inválido"):
        _normalizar_destinos_documentales(["CLIENTE:9"])


def test_schema_agrega_objetivos_deriva_ambito_y_preserva_legado():
    schema = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    bloque = schema[schema.index("-- ── Imputación documental de pagos") :]
    assert "ADD COLUMN IF NOT EXISTS factura_id BIGINT" in bloque
    assert "ADD COLUMN IF NOT EXISTS envio_id INTEGER" in bloque
    assert "NUM_NONNULLS(factura_id, envio_id) <= 1" in bloque
    assert "NEW.ambito := ambito_documento" in bloque
    assert "uq_pago_aplicacion_factura" in bloque
    assert "uq_pago_aplicacion_envio" in bloque
    assert "Las aplicaciones superan el monto del pago" in bloque
    assert "La aplicación supera el saldo del documento" in bloque


def test_formularios_ofrecen_facturas_envios_parcial_y_saldo_a_favor():
    portal = (ROOT / "templates" / "portal" / "cuenta.html").read_text()
    admin = (ROOT / "templates" / "admin" / "pago_form.html").read_text()
    pendientes = (ROOT / "templates" / "admin" / "pagos_pendientes.html").read_text()
    for html in (portal, admin):
        assert 'name="destinos"' in html
        assert 'data-saldo="{{ d.disponible }}"' in html
        assert "a favor" in html
        assert "Queda pendiente" in html or "queda pendiente" in html
    assert 'name="fecha"' in portal
    assert 'name="preservar_solicitud" value="1"' in pendientes


def test_factura_suma_pago_directo_y_arrastrado_sin_float():
    servicio = (ROOT / "servicios" / "facturacion_clientes.py").read_text()
    assert "pa.factura_id=f.id" in servicio
    assert "pa.envio_id IN" in servicio
    assert "pa.estado='APLICADA'" in servicio
    assert "Decimal" in servicio

