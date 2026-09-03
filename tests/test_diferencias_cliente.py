from decimal import Decimal
from pathlib import Path

from servicios.diferencias_cliente import presentar_diferencia


ROOT = Path(__file__).resolve().parents[1]


def test_presentacion_por_peso_calcula_diferencia_y_no_filtra_costos():
    detalle = presentar_diferencia({
        "motivo_diferencia": "PESO_VOLUMETRICO",
        "peso_cotizado_kg": "10",
        "peso_final_facturado_kg": "13",
        "peso_base_facturado": "VOLUMETRICO",
        "costo_courier_real_ars": "999999",
        "margen_tauro_protegido_ars": "888888",
    })
    assert detalle["es_peso"] is True
    assert detalle["peso_inicial_kg"] == Decimal("10.000")
    assert detalle["peso_facturado_kg"] == Decimal("13.000")
    assert detalle["diferencia_peso_kg"] == Decimal("3.000")
    assert detalle["leyenda"] == (
        "TAURO traslada la diferencia del courier sin agregar margen."
    )
    assert "costo_courier_real_ars" not in detalle
    assert "margen_tauro_protegido_ars" not in detalle


def test_presentacion_de_recargo_usa_descripcion_documentada():
    detalle = presentar_diferencia({
        "motivo": "RECARGO",
        "concepto_courier": "Cargo por zona extendida",
    })
    assert detalle["es_peso"] is False
    assert detalle["concepto_courier"] == "Cargo por zona extendida"
    assert detalle["motivo_legible"] == "Recargo del courier"


def test_portal_muestra_explicacion_en_cuenta_y_detalle():
    cuenta = (ROOT / "templates" / "portal" / "cuenta.html").read_text()
    envio = (ROOT / "templates" / "portal" / "envio_detalle.html").read_text()
    servicio = (ROOT / "servicios" / "cuenta_corriente.py").read_text()
    for html in (cuenta, envio):
        assert "Peso inicial" in html
        assert "Peso facturado por el courier" in html
        assert "diferencia_peso_kg" in html
        assert "leyenda" in html
        assert "costo_courier" not in html
        assert "margen_tauro" not in html
    assert "concepto_courier" in servicio
    assert "i.concepto_tipo <> 'FLETE'" in servicio
