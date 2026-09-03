from decimal import Decimal
from pathlib import Path

from servicios.cotizador_nacional import preparar_cotizacion_nacional
from servicios.pesos_envio import calcular_pesos_bultos


ROOT = Path(__file__).resolve().parents[1]


def test_internacional_muestra_que_una_caja_de_10kg_factura_12_96kg():
    resultado = calcular_pesos_bultos([{
        "cantidad": 1,
        "peso_kg": 10,
        "largo_cm": 50,
        "ancho_cm": 36,
        "alto_cm": 36,
    }], "internacional")

    assert resultado["real_total_kg"] == Decimal("10.00")
    assert resultado["volumetrico_total_kg"] == Decimal("12.96")
    assert resultado["facturable_total_kg"] == Decimal("12.96")
    assert resultado["cobra_por_volumen"] is True


def test_multibulto_factura_maximo_por_cada_caja_no_maximo_global():
    resultado = calcular_pesos_bultos([
        {"cantidad": 2, "peso_kg": 1, "largo_cm": 50, "ancho_cm": 20, "alto_cm": 20},
        {"cantidad": 1, "peso_kg": 10, "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10},
    ], "internacional")

    assert resultado["real_total_kg"] == Decimal("12.00")
    assert resultado["volumetrico_total_kg"] == Decimal("8.20")
    assert resultado["facturable_total_kg"] == Decimal("18.00")
    assert resultado["cobra_por_volumen"] is True


def test_nacional_usa_divisor_4000_en_backend():
    resultado = preparar_cotizacion_nacional(
        origen_provincia="C", origen_localidad="CABA", origen_cp="1000",
        modalidad_origen="domicilio", destino_provincia="B",
        destino_localidad="La Plata", destino_cp="1900",
        modalidad_destino="domicilio", cantidad_bultos="1", peso_kg="10",
        largo_cm="50", ancho_cm="36", alto_cm="36",
        valor_declarado_ars="100000",
    )

    assert resultado["totales"]["peso_volumetrico_kg"] == "16.2"
    assert resultado["totales"]["peso_facturable_kg"] == "16.2"
    assert resultado["totales"]["cobra_por_volumen"] is True


def test_cotizadores_y_detalle_explican_los_tres_pesos():
    internacional = (ROOT / "templates/portal/cotizar.html").read_text()
    nacional = (ROOT / "templates/portal/_cotizador_nacional.html").read_text()
    detalle = (ROOT / "templates/portal/envio_detalle.html").read_text()

    assert "/ 5000" in internacional
    assert "/ 4000" in nacional
    for contenido in (internacional, nacional, detalle):
        assert "Peso real" in contenido
        assert "Peso volumétrico" in contenido
        assert "Peso facturable" in contenido
        assert "Se cobra por volumen" in contenido
