from decimal import Decimal

import pytest

from servicios.numeros_humanos import (
    NumeroHumanoAmbiguo,
    NumeroHumanoInvalido,
    decimal_a_texto,
    parse_configuracion_numerica,
    parse_float_formulario,
    parse_importe_humano,
    parse_numero_humano,
)


@pytest.mark.parametrize(
    ("espanol", "ingles", "esperado"),
    [
        ("5,5", "5.5", Decimal("5.5")),
        ("0,25", "0.25", Decimal("0.25")),
        ("1234,56", "1234.56", Decimal("1234.56")),
        ("-42,75", "-42.75", Decimal("-42.75")),
        ("+7,1", "+7.1", Decimal("7.1")),
    ],
)
def test_decimal_simple_es_y_en_son_equivalentes(espanol, ingles, esperado):
    assert parse_numero_humano(espanol) == esperado
    assert parse_numero_humano(ingles) == esperado


@pytest.mark.parametrize(
    ("espanol", "ingles", "esperado"),
    [
        ("1.234,56", "1,234.56", Decimal("1234.56")),
        ("12.345.678,901", "12,345,678.901", Decimal("12345678.901")),
        ("-1.000,50", "-1,000.50", Decimal("-1000.50")),
    ],
)
def test_miles_y_decimales_es_y_en_son_equivalentes(espanol, ingles, esperado):
    assert parse_numero_humano(espanol) == esperado
    assert parse_numero_humano(ingles) == esperado


@pytest.mark.parametrize(
    ("espanol", "ingles", "esperado"),
    [
        ("1.234.567", "1,234,567", Decimal("1234567")),
        ("12.345.678", "12,345,678", Decimal("12345678")),
    ],
)
def test_agrupaciones_repetidas_validas_son_enteros(espanol, ingles, esperado):
    assert parse_numero_humano(espanol) == esperado
    assert parse_numero_humano(ingles) == esperado


@pytest.mark.parametrize("texto", ["10.000", "10,000", "100.000", "100,000"])
def test_numero_generico_rechaza_una_separacion_ambigua(texto):
    with pytest.raises(NumeroHumanoAmbiguo):
        parse_numero_humano(texto)


def test_importe_tambien_acepta_coma_o_punto_decimal():
    assert parse_importe_humano("5,5") == Decimal("5.5")
    assert parse_importe_humano("5.5") == Decimal("5.5")


@pytest.mark.parametrize(
    ("espanol", "ingles", "esperado"),
    [
        ("1.000", "1,000", Decimal("1000")),
        ("10.000", "10,000", Decimal("10000")),
        ("100.000", "100,000", Decimal("100000")),
        ("999.999", "999,999", Decimal("999999")),
    ],
)
def test_importe_resuelve_tres_cifras_como_miles(espanol, ingles, esperado):
    assert parse_importe_humano(espanol) == esperado
    assert parse_importe_humano(ingles) == esperado


def test_importe_cero_con_tres_decimales_no_se_convierte_en_quinientos():
    assert parse_importe_humano("0.500") == Decimal("0.500")
    assert parse_importe_humano("0,500") == Decimal("0.500")


@pytest.mark.parametrize("texto", ["1234.567", "1234,567"])
def test_importe_rechaza_primer_grupo_invalido_en_vez_de_adivinar(texto):
    with pytest.raises(NumeroHumanoAmbiguo):
        parse_importe_humano(texto)


def test_parseo_no_redondea_decimales():
    original = Decimal("0.12345678901234567890123456789")
    assert parse_numero_humano("0,12345678901234567890123456789") == original
    assert parse_numero_humano("0.12345678901234567890123456789") == original


@pytest.mark.parametrize(
    "texto",
    [
        "1.23.456",
        "1,23,456",
        "1,234,56",
        "1.234.56",
        "1,23.456",
        "1.23,456",
        ".5",
        "5,",
        "1 000",
        "$ 1.000",
        "1e3",
        "--5",
        "NaN",
        "Infinity",
    ],
)
def test_formatos_invalidos_se_rechazan(texto):
    with pytest.raises(NumeroHumanoInvalido):
        parse_numero_humano(texto)


def test_vacios_y_tipos_numericos_directos():
    assert parse_numero_humano(None) is None
    assert parse_numero_humano("") is None
    assert parse_numero_humano("   ") is None
    assert parse_numero_humano(5) == Decimal("5")
    assert parse_numero_humano(5.5) == Decimal("5.5")
    exacto = Decimal("10000.01")
    assert parse_numero_humano(exacto) is exacto


@pytest.mark.parametrize(
    "valor",
    [True, False, float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity")],
)
def test_tipos_no_numericos_o_no_finitos_se_rechazan(valor):
    with pytest.raises(NumeroHumanoInvalido):
        parse_numero_humano(valor)


def test_errores_especializados_siguen_siendo_value_error():
    assert issubclass(NumeroHumanoInvalido, ValueError)
    assert issubclass(NumeroHumanoAmbiguo, NumeroHumanoInvalido)


def test_borde_de_formulario_comparte_regla_y_valida_rangos():
    assert parse_float_formulario("5,5", "Peso", minimo=0.1, maximo=70) == 5.5
    assert parse_float_formulario("5.5", "Peso", minimo=0.1, maximo=70) == 5.5
    assert parse_float_formulario("100.000", "Monto", importe=True) == 100000
    assert parse_float_formulario("100,000", "Monto", importe=True) == 100000

    with pytest.raises(ValueError, match="máximo es 70"):
        parse_float_formulario("70,1", "Peso", minimo=0.1, maximo=70)
    with pytest.raises(ValueError, match="mínimo es 0.1"):
        parse_float_formulario("0", "Peso", minimo=0.1, maximo=70)


def test_borde_de_formulario_distingue_vacio_opcional_y_requerido():
    assert parse_float_formulario("", "Impuesto", requerido=False) is None
    with pytest.raises(ValueError, match="completá este valor"):
        parse_float_formulario("", "Peso")


def test_config_financiera_comparte_la_misma_politica_segun_la_clave():
    assert parse_configuracion_numerica(
        "WEB_MARGEN_FIJO_DHL_ARS", "100.000"
    ) == Decimal("100000")
    assert parse_configuracion_numerica(
        "WEB_MARGEN_FIJO_DHL_ARS", "100,000"
    ) == Decimal("100000")
    assert parse_configuracion_numerica("WEB_MARKUP_PCT_DHL", "5,5") == Decimal("5.5")
    assert parse_configuracion_numerica("WEB_MARKUP_PCT_DHL", "5.5") == Decimal("5.5")
    assert decimal_a_texto(Decimal("100000.00")) == "100000"


def test_config_financiera_rechaza_descuento_y_valores_peligrosos():
    with pytest.raises(ValueError, match="máximo es 100"):
        parse_configuracion_numerica("WEB_DESC_FEDEX_PCT", "100,5")
    with pytest.raises(ValueError, match="no puede ser negativo"):
        parse_configuracion_numerica("WEB_MARGEN_FIJO_DHL_ARS", "-1")
    with pytest.raises(ValueError, match="política numérica conocida"):
        parse_configuracion_numerica("ADMIN_PASSWORD", "123")
