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


def test_presentacion_explica_valor_inicial_diferencia_y_final():
    detalle = presentar_diferencia({
        "motivo": "RECARGO",
        "valor_inicial_ars": "97699.999",
        "diferencia_ars": "24571.341",
        "valor_final_ars": "122271.34",
    })
    assert detalle["montos_completos"] is True
    assert detalle["valor_inicial_ars"] == Decimal("97700.00")
    assert detalle["diferencia_ars"] == Decimal("24571.34")
    assert detalle["valor_final_ars"] == Decimal("122271.34")
    assert detalle["es_credito"] is False


def test_presentacion_distingue_credito_y_oculta_flujo_inconsistente():
    credito = presentar_diferencia({
        "valor_inicial_ars": "1000",
        "diferencia_ars": "-200",
        "valor_final_ars": "800",
    })
    inconsistente = presentar_diferencia({
        "valor_inicial_ars": "1000",
        "diferencia_ars": "200",
        "valor_final_ars": "999",
    })
    assert credito["montos_completos"] is True
    assert credito["es_credito"] is True
    assert inconsistente["montos_completos"] is False


def test_portal_muestra_explicacion_en_cuenta_y_detalle():
    cuenta = (ROOT / "templates" / "portal" / "cuenta.html").read_text()
    envio = (ROOT / "templates" / "portal" / "envio_detalle.html").read_text()
    servicio = (ROOT / "servicios" / "cuenta_corriente.py").read_text()
    for html in (cuenta, envio):
        assert "Peso inicial" in html
        assert "diferencia_peso_kg" in html
        assert "leyenda" in html
        assert "costo_courier" not in html
        assert "margen_tauro" not in html
    for texto in ("Valor cotizado", "Diferencia", "Costo final"):
        assert texto in cuenta
    assert "Peso facturado por el courier" in envio
    assert "'valor_inicial_ars', a.precio_anterior_ars" in servicio
    assert "'valor_final_ars', a.precio_nuevo_ars" in servicio
    assert "concepto_courier" in servicio
    assert "i.concepto_tipo <> 'FLETE'" in servicio
