"""Contrato canónico de las ventanas visibles de la cuenta."""

from datetime import date, datetime, timezone

import pytest

from servicios import periodo_visual_cuenta as periodo


HOY = date(2026, 10, 6)


def test_default_es_anio_calendario_completo_y_no_oculta_futuros():
    assert periodo.normalizar_ventana_cuenta("CLIENTE", hoy=HOY) == {
        "ventana": "anio",
        "desde": "2026-01-01",
        "hasta": "2026-12-31",
        "periodo": "",
        "label": "Este año · 2026",
    }


def test_ventana_explicita_manda_sobre_parametros_inactivos():
    assert periodo.normalizar_ventana_cuenta(
        "CLIENTE", ventana="anio", periodo="2025-02",
        desde="fecha-invalida", hasta="tambien-invalida", hoy=HOY,
    )["ventana"] == "anio"
    assert periodo.normalizar_ventana_cuenta(
        "CLIENTE", ventana="mes", periodo="2025-02",
        desde="fecha-invalida", hasta="tambien-invalida", hoy=HOY,
    ) == {
        "ventana": "mes", "desde": "2025-02-01", "hasta": "2025-02-28",
        "periodo": "2025-02", "label": "Febrero 2025",
    }


@pytest.mark.parametrize(
    "kwargs,esperado",
    [
        ({"periodo": "2024-02"}, ("mes", "2024-02-01", "2024-02-29")),
        ({"desde": "2025-12-01", "hasta": "2026-01-02"},
         ("rango", "2025-12-01", "2026-01-02")),
    ],
)
def test_url_antigua_infiere_mes_o_rango(kwargs, esperado):
    resultado = periodo.normalizar_ventana_cuenta("CLIENTE", hoy=HOY, **kwargs)
    assert (resultado["ventana"], resultado["desde"], resultado["hasta"]) == esperado


def test_septiembre_usa_corte_configurado_o_septiembre_actual():
    waimao = periodo.normalizar_ventana_cuenta(
        " waimao ", ventana="septiembre", hoy=HOY,
    )
    otro = periodo.normalizar_ventana_cuenta(
        "CLIENTE", ventana="septiembre", hoy=HOY,
    )
    assert waimao == otro == {
        "ventana": "septiembre",
        "desde": "2026-09-01",
        "hasta": "2026-12-31",
        "periodo": "",
        "label": "Desde septiembre de 2026",
    }


def test_septiembre_sin_politica_no_se_ofrece_antes_de_septiembre():
    with pytest.raises(ValueError, match="todavía no está disponible"):
        periodo.normalizar_ventana_cuenta(
            "CLIENTE", ventana="septiembre", hoy=date(2026, 8, 31),
        )


@pytest.mark.parametrize(
    "kwargs,mensaje",
    [
        ({"ventana": "desconocida"}, "período seleccionado"),
        ({"ventana": "mes"}, "Seleccioná un mes"),
        ({"ventana": "mes", "periodo": "02/2026"}, "mes seleccionado"),
        ({"ventana": "rango", "desde": "2026-01-01"}, "Desde y Hasta"),
        ({"ventana": "rango", "desde": "2026-02-01", "hasta": "2026-01-01"},
         "anterior o igual"),
        ({"ventana": "rango", "desde": "ayer", "hasta": "2026-01-01"},
         "fecha válida"),
    ],
)
def test_rechaza_ventanas_incompletas_o_invalidas(kwargs, mensaje):
    with pytest.raises(ValueError, match=mensaje):
        periodo.normalizar_ventana_cuenta("CLIENTE", hoy=HOY, **kwargs)


def test_hoy_debe_ser_fecha_sin_datetime():
    with pytest.raises(ValueError, match="fecha actual"):
        periodo.normalizar_ventana_cuenta(
            "CLIENTE", hoy=datetime(2026, 10, 6, tzinfo=timezone.utc),
        )
