"""Rangos por costo USD del canal DHL web; todos los valores son demo."""
from decimal import Decimal

import pytest

from servicios.pricing_rangos_usd import (
    aplicar_rangos_usd,
    normalizar_rangos_usd,
    serializar_rangos_usd,
)


RANGOS_DEMO = [
    {"desde": "0", "hasta": "150", "tipo": "FIJO_ARS", "valor": "20000"},
    {"desde": "150", "hasta": None, "tipo": "FIJO_USD", "valor": "100"},
]


@pytest.mark.parametrize(
    ("costo_usd", "precio_ars"),
    [
        ("149.99", 169990),
        ("150", 250000),
        ("150.01", 250010),
    ],
)
def test_limites_desde_inclusivo_hasta_exclusivo(costo_usd, precio_ars):
    assert aplicar_rangos_usd(
        costo=costo_usd,
        moneda="USD",
        dolar="1000",
        rangos=RANGOS_DEMO,
    )["precio_ars"] == precio_ars


def test_costo_ars_se_convierte_a_usd_sin_redondear_antes_del_umbral():
    debajo = aplicar_rangos_usd(
        costo="149999",
        moneda="ARS",
        dolar="1000",
        rangos=RANGOS_DEMO,
    )
    en_limite = aplicar_rangos_usd(
        costo="150000",
        moneda="ARS",
        dolar="1000",
        rangos=RANGOS_DEMO,
    )
    assert debajo["precio_ars"] == 169999
    assert en_limite["precio_ars"] == 250000


def test_pct_se_aplica_sobre_costo_real_y_una_sola_vez():
    rangos = [{
        "desde": "0", "hasta": None, "tipo": "PCT", "valor": "20",
    }]
    assert aplicar_rangos_usd(
        costo="125.55", moneda="USD", dolar="1000", rangos=rangos,
    ) == {"precio_ars": 150660, "precio_usd": 150.66}


def test_ganancia_cero_redondea_hacia_arriba_para_no_quedar_bajo_costo():
    rango_cero = [{
        "desde": "0", "hasta": None, "tipo": "FIJO_ARS", "valor": "0",
    }]
    assert aplicar_rangos_usd(
        costo="100.4", moneda="ARS", dolar="1", rangos=rango_cero,
    ) == {"precio_ars": 101, "precio_usd": 101.0}
    assert aplicar_rangos_usd(
        costo="1.004", moneda="USD", dolar="100", rangos=rango_cero,
    ) == {"precio_ars": 101, "precio_usd": 1.01}


def test_piso_no_cambia_un_precio_normal_con_ganancia():
    assert aplicar_rangos_usd(
        costo="100",
        moneda="USD",
        dolar="1000",
        rangos=[{
            "desde": "0", "hasta": None,
            "tipo": "FIJO_ARS", "valor": "20000",
        }],
    ) == {"precio_ars": 120000, "precio_usd": 120.0}


def test_normaliza_y_serializa_decimales_sin_float():
    normalizados = normalizar_rangos_usd([
        {"desde": 0, "hasta": "150.0000", "tipo": "pct", "valor": "20.5000"},
        {"desde": "150", "hasta": None, "tipo": "FIJO_USD", "valor": "100.00"},
    ])
    assert normalizados == [
        {"desde": "0", "hasta": "150", "tipo": "PCT", "valor": "20.5"},
        {"desde": "150", "hasta": None, "tipo": "FIJO_USD", "valor": "100"},
    ]
    assert serializar_rangos_usd(normalizados) == (
        '[{"desde":"0","hasta":"150","tipo":"PCT","valor":"20.5"},'
        '{"desde":"150","hasta":null,"tipo":"FIJO_USD","valor":"100"}]'
    )


def test_canonicos_decimales_son_idempotentes():
    entrada = [
        {"desde": Decimal("0"), "hasta": Decimal("150.125"),
         "tipo": "PCT", "valor": Decimal("20.125")},
        {"desde": Decimal("150.125"), "hasta": None,
         "tipo": "FIJO_USD", "valor": Decimal("150.50")},
    ]
    una_vez = normalizar_rangos_usd(entrada)
    dos_veces = normalizar_rangos_usd(una_vez)
    assert una_vez == dos_veces == [
        {"desde": "0", "hasta": "150.125", "tipo": "PCT", "valor": "20.125"},
        {"desde": "150.125", "hasta": None, "tipo": "FIJO_USD", "valor": "150.5"},
    ]


@pytest.mark.parametrize(
    "rangos",
    [
        [],
        [{"desde": 1, "hasta": None, "tipo": "PCT", "valor": 20}],
        [
            {"desde": 0, "hasta": 100, "tipo": "PCT", "valor": 20},
            {"desde": 101, "hasta": None, "tipo": "PCT", "valor": 20},
        ],
        [{"desde": 0, "hasta": 100, "tipo": "PCT", "valor": 20}],
        [
            {"desde": 0, "hasta": None, "tipo": "PCT", "valor": 20},
            {"desde": 100, "hasta": None, "tipo": "PCT", "valor": 20},
        ],
        [{"desde": 0, "hasta": None, "tipo": "OTRO", "valor": 20}],
        [{"desde": 0, "hasta": None, "tipo": "PCT", "valor": 301}],
        [{"desde": 0, "hasta": None, "tipo": "FIJO_USD", "valor": "1.0001"}],
        [{"desde": 0, "hasta": None, "tipo": "FIJO_ARS", "valor": -1}],
        [{"desde": 0, "hasta": None, "tipo": "PCT", "valor": 20, "minimo": 1}],
        "no-json",
    ],
)
def test_rangos_invalidos_fallan_cerrado(rangos):
    with pytest.raises(ValueError):
        normalizar_rangos_usd(rangos)


def test_maximo_treinta_rangos():
    rangos = [
        {
            "desde": str(i),
            "hasta": str(i + 1) if i < 29 else None,
            "tipo": "PCT",
            "valor": "1",
        }
        for i in range(30)
    ]
    assert len(normalizar_rangos_usd(rangos)) == 30
    rangos.insert(-1, {
        "desde": "29", "hasta": "29.5", "tipo": "PCT", "valor": "1",
    })
    with pytest.raises(ValueError, match="1 y 30"):
        normalizar_rangos_usd(rangos)
