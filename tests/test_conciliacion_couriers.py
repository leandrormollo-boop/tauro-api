from decimal import Decimal

import pytest

from servicios import conciliacion_couriers as conciliacion


def test_formula_preserva_margen_y_propone_solo_la_diferencia():
    resultado = conciliacion.calcular_precio_con_margen_protegido(
        costo_courier_real_ars="15000",
        margen_tauro_protegido_ars="5000",
        precio_cliente_inicial_ars="10000",
    )

    assert resultado == {
        "costo_courier_real_ars": Decimal("15000.0000"),
        "margen_tauro_protegido_ars": Decimal("5000.0000"),
        "precio_cliente_inicial_ars": Decimal("10000.0000"),
        "precio_cliente_final_ars": Decimal("20000.0000"),
        "ajuste_cliente_ars": Decimal("10000.0000"),
    }


def test_nc_tiene_signo_negativo_y_no_se_convierte_en_fc():
    assert conciliacion.signo_documento("FC") == 1
    assert conciliacion.signo_documento("ND") == 1
    assert conciliacion.signo_documento("NC") == -1


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        (" 1234-5678  ", "12345678"),
        ("JD01 4600 0039 85", "JD014600003985"),
        ("abc/000-9", "ABC0009"),
    ],
)
def test_normalizacion_de_tracking_es_deterministica(entrada, esperado):
    assert conciliacion.normalizar_tracking(entrada) == esperado


@pytest.mark.parametrize("valor", [True, "NaN", "Infinity"])
def test_formula_rechaza_importes_invalidos(valor):
    with pytest.raises(conciliacion.ConciliacionCourierError):
        conciliacion.calcular_precio_con_margen_protegido(
            costo_courier_real_ars=valor,
            margen_tauro_protegido_ars="5000",
            precio_cliente_inicial_ars="10000",
        )


def test_costo_neto_negativo_por_nc_es_valido_si_el_precio_final_no_lo_es():
    resultado = conciliacion.calcular_precio_con_margen_protegido(
        costo_courier_real_ars="-1000",
        margen_tauro_protegido_ars="5000",
        precio_cliente_inicial_ars="10000",
    )

    assert resultado["precio_cliente_final_ars"] == Decimal("4000.0000")
    assert resultado["ajuste_cliente_ars"] == Decimal("-6000.0000")


def test_item_en_usd_exige_conversion_ars_coherente():
    item = conciliacion._preparar_item(
        {
            "linea_numero": 1,
            "tracking": "DHL-123",
            "concepto_tipo": "FLETE",
            "importe": "10",
            "moneda": "USD",
            "tipo_cambio_ars": "1450",
            "importe_ars": "14500",
        },
        moneda_documento="USD",
    )

    assert item["importe"] == Decimal("10.0000")
    assert item["tipo_cambio_ars"] == Decimal("1450.000000")
    assert item["importe_ars"] == Decimal("14500.0000")

    with pytest.raises(
        conciliacion.ConciliacionCourierError,
        match="conversión ARS",
    ):
        conciliacion._preparar_item(
            {
                "linea_numero": 1,
                "importe": "10",
                "moneda": "USD",
                "tipo_cambio_ars": "1450",
                "importe_ars": "15000",
            },
            moneda_documento="USD",
        )


def test_parsea_tabla_excel_con_tracking_repetido_y_pesos():
    items = conciliacion.parsear_lineas_factura_texto(
        "tracking;importe;concepto;peso_facturado;peso_base;descripcion\n"
        "DHL-001;10.000;FLETE;3,2;VOLUMETRICO;Flete\n"
        "DHL-001;500;COMBUSTIBLE;;;Recargo",
        moneda="ARS",
        tipo_cambio_ars="1",
    )

    assert [item["tracking"] for item in items] == ["DHL-001", "DHL-001"]
    assert [item["importe"] for item in items] == [
        Decimal("10000"), Decimal("500")
    ]
    assert items[0]["peso_facturado_kg"] == "3.2"
    assert items[0]["peso_base"] == "VOLUMETRICO"


def test_parseo_de_factura_rechaza_linea_sin_tracking():
    with pytest.raises(conciliacion.ConciliacionCourierError, match="tracking"):
        conciliacion.parsear_lineas_factura_texto(
            ";15000;FLETE", moneda="ARS", tipo_cambio_ars="1"
        )
