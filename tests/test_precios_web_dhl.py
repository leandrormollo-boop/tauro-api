"""Regla DHL exclusiva del cotizador internacional público."""
from decimal import Decimal
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios import carriers
from servicios import precios_web_dhl as precios


def _filas(**valores):
    return [
        {"parametro": parametro, "valor": valor}
        for parametro, valor in valores.items()
    ]


RANGOS_DEMO = [
    {"desde": "0", "hasta": "150", "tipo": "FIJO_ARS", "valor": "20000"},
    {"desde": "150", "hasta": None, "tipo": "FIJO_USD", "valor": "100"},
]


def test_legacy_fijo_gana_sin_validar_porcentaje_inactivo_real():
    configuracion = precios._resolver(_filas(
        WEB_MARGEN_FIJO_DHL_ARS="135000",
        WEB_MARKUP_PCT_DHL="100000",
        WEB_MARKUP_PCT="20",
    ), entorno={})

    assert configuracion["modo"] == precios.MODO_FIJO_ARS
    assert configuracion["modo_explicito"] is False
    assert configuracion["margen_fijo_ars"] == Decimal("135000")
    assert configuracion["markup_texto"] == "100000"
    assert configuracion["markup_valido"] is False
    assert configuracion["publicable"] is True
    assert configuracion["error"] is None


def test_legacy_porcentaje_respeta_precedencia_historica():
    filas = _filas(WEB_MARKUP_PCT_DHL="25", WEB_MARKUP_PCT="30")
    configuracion = precios._resolver(
        filas,
        entorno={
            "WEB_MARKUP_PCT_DHL": "40",
            "WEB_MARGEN_FIJO_DHL_ARS": "0",
            "WEB_MARKUP_PCT": "50",
        },
    )
    assert configuracion["modo"] == precios.MODO_PCT
    assert configuracion["markup_pct"] == Decimal("25")
    assert configuracion["markup_fuente"] == "DB_DHL_LEGACY"


def test_modo_nuevo_pct_ignora_fijo_legacy_sin_borrarlo():
    configuracion = precios._resolver(_filas(
        WEB_DHL_PRICING_MODE="PCT",
        WEB_DHL_MARKUP_PCT="0",
        WEB_MARGEN_FIJO_DHL_ARS="135000",
    ), entorno={})
    regla = precios.pricing_publico_dhl({
        precios.FILAS_CONFIG_KEY: _filas(
            WEB_DHL_PRICING_MODE="PCT",
            WEB_DHL_MARKUP_PCT="0",
            WEB_MARGEN_FIJO_DHL_ARS="135000",
        )
    })

    assert configuracion["modo"] == precios.MODO_PCT
    assert configuracion["markup_pct"] == Decimal("0")
    assert configuracion["fijo_texto"] == "135000"
    assert regla["tipo"] == precios.MODO_PCT
    assert regla["valor"] == Decimal("0")


def test_modo_nuevo_fijo_cero_es_valido_y_no_activa_porcentaje():
    regla = precios.pricing_publico_dhl({
        precios.FILAS_CONFIG_KEY: _filas(
            WEB_DHL_PRICING_MODE="FIJO_ARS",
            WEB_DHL_MARGEN_FIJO_ARS="0",
            WEB_MARKUP_PCT_DHL="100000",
        )
    })
    assert regla["tipo"] == precios.MODO_FIJO_ARS
    assert regla["valor"] == Decimal("0")


def test_modo_rangos_lee_json_normalizado_y_no_valores_inactivos():
    import json
    regla = precios.pricing_publico_dhl({
        precios.FILAS_CONFIG_KEY: _filas(
            WEB_DHL_PRICING_MODE="RANGOS_USD",
            WEB_DHL_RANGOS_USD=json.dumps(RANGOS_DEMO),
            WEB_DHL_MARKUP_PCT="500",
            WEB_DHL_MARGEN_FIJO_ARS="-1",
        )
    })
    assert regla["tipo"] == "RANGOS_USD"
    assert regla["valor"] == RANGOS_DEMO
    assert regla["fuente"] == "DB_NUEVA"


def test_rangos_invalidos_inactivos_no_bloquean_pct():
    configuracion = precios._resolver(_filas(
        WEB_DHL_PRICING_MODE="PCT",
        WEB_DHL_MARKUP_PCT="20",
        WEB_DHL_RANGOS_USD="json roto",
    ), entorno={})
    assert configuracion["publicable"] is True
    assert configuracion["modo"] == "PCT"
    assert configuracion["rangos_valido"] is False
    assert configuracion["rangos_error"]


def test_modo_rangos_db_no_completa_desde_env_y_env_ignora_db_huerfano():
    import json
    entorno = {precios.PARAMETRO_RANGOS_USD: json.dumps(RANGOS_DEMO)}
    incompleta = precios._resolver(
        _filas(WEB_DHL_PRICING_MODE="RANGOS_USD"),
        entorno=entorno,
    )
    assert incompleta["publicable"] is False
    invalida = precios._resolver(
        _filas(
            WEB_DHL_PRICING_MODE="RANGOS_USD",
            WEB_DHL_RANGOS_USD="json DB roto",
        ),
        entorno=entorno,
    )
    assert invalida["publicable"] is False
    assert invalida["rangos_fuente"] == "DB_NUEVA"
    desde_entorno = precios._resolver(
        _filas(WEB_DHL_RANGOS_USD="json DB roto"),
        entorno={precios.PARAMETRO_MODO: "RANGOS_USD", **entorno},
    )
    assert desde_entorno["publicable"] is True
    assert desde_entorno["rangos_fuente"] == "ENV_NUEVO"
    assert desde_entorno["rangos_usd"] == RANGOS_DEMO


@pytest.mark.parametrize(
    ("modo", "clave_db", "clave_env", "valor_db_invalido", "valor_env"),
    [
        ("PCT", precios.PARAMETRO_MARKUP_PCT, precios.PARAMETRO_MARKUP_PCT, "500", "77"),
        ("FIJO_ARS", precios.PARAMETRO_FIJO_ARS, precios.PARAMETRO_FIJO_ARS, "-1", "77000"),
    ],
)
@pytest.mark.parametrize("con_valor_db_invalido", [False, True])
def test_modo_db_exige_valor_db_y_no_completa_desde_entorno(
    modo,
    clave_db,
    clave_env,
    valor_db_invalido,
    valor_env,
    con_valor_db_invalido,
):
    filas = [{"parametro": precios.PARAMETRO_MODO, "valor": modo}]
    if con_valor_db_invalido:
        filas.append({"parametro": clave_db, "valor": valor_db_invalido})

    configuracion = precios._resolver(
        filas,
        entorno={clave_env: valor_env},
    )

    assert configuracion["modo_fuente"] == "DB_NUEVA"
    assert configuracion["publicable"] is False
    assert configuracion["error"]


@pytest.mark.parametrize(
    ("modo", "clave", "valor_db_huerfano", "valor_env", "esperado"),
    [
        ("PCT", precios.PARAMETRO_MARKUP_PCT, "99", "12", Decimal("12")),
        ("FIJO_ARS", precios.PARAMETRO_FIJO_ARS, "99000", "12000", Decimal("12000")),
    ],
)
def test_modo_entorno_no_usa_valor_nuevo_huerfano_de_db(
    modo, clave, valor_db_huerfano, valor_env, esperado,
):
    configuracion = precios._resolver(
        [{"parametro": clave, "valor": valor_db_huerfano}],
        entorno={
            precios.PARAMETRO_MODO: modo,
            clave: valor_env,
        },
    )

    assert configuracion["modo_fuente"] == "ENV_NUEVO"
    assert configuracion["publicable"] is True
    if modo == "PCT":
        assert configuracion["markup_pct"] == esperado
        assert configuracion["markup_fuente"] == "ENV_NUEVO"
    else:
        assert configuracion["margen_fijo_ars"] == esperado
        assert configuracion["fijo_fuente"] == "ENV_NUEVO"


@pytest.mark.parametrize("valor", ["-1", "300.0001", "20.12345", "NaN"])
def test_porcentaje_activo_invalido_falla_cerrado(valor):
    with pytest.raises(precios.PrecioWebDHLNoDisponible):
        precios.pricing_publico_dhl({
            precios.FILAS_CONFIG_KEY: _filas(
                WEB_DHL_PRICING_MODE="PCT",
                WEB_DHL_MARKUP_PCT=valor,
            )
        })


def test_calculo_decimal_pct_fijo_y_piso_de_costo():
    costo = {"costo": "100", "moneda": "USD"}
    assert precios.calcular_precio_publico_dhl(
        costo, "1000", {"tipo": "PCT", "valor": Decimal("0")}
    ) == {"precio_ars": 100000, "precio_usd": 100.0}
    assert precios.calcular_precio_publico_dhl(
        costo, "1000", {"tipo": "FIJO_ARS", "valor": Decimal("0")}
    ) == {"precio_ars": 100000, "precio_usd": 100.0}
    assert precios.calcular_precio_publico_dhl(
        costo, "1000", {"tipo": "FIJO_ARS", "valor": Decimal("135000")}
    )["precio_ars"] == 235000
    with pytest.raises(precios.PrecioWebDHLNoDisponible, match="cubre el costo"):
        precios.calcular_precio_publico_dhl(
            {**costo, "costo_lista": "50"},
            "1000",
            {"tipo": "PCT", "valor": Decimal("20")},
        )


def test_pct_nuevo_es_sobre_costo_y_legacy_conserva_tarifa_lista():
    resultado = {"costo": "100", "costo_lista": "150", "moneda": "USD"}
    nuevo = precios.calcular_precio_publico_dhl(
        resultado,
        "1000",
        {"tipo": "PCT", "valor": Decimal("20"), "modo_explicito": True},
    )
    legacy = precios.calcular_precio_publico_dhl(
        resultado,
        "1000",
        {"tipo": "PCT", "valor": Decimal("20"), "modo_explicito": False},
    )
    assert nuevo["precio_ars"] == 120000
    assert legacy["precio_ars"] == 180000


class _Cursor:
    def __init__(self, filas=()):
        self.filas = list(filas)
        self.ejecutadas = []

    def execute(self, sql, params=None):
        self.ejecutadas.append((" ".join(sql.split()), params))

    def fetchall(self):
        return list(self.filas)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_guardado_valida_solo_regla_activa_y_audita_en_transaccion(monkeypatch):
    cursor = _Cursor(_filas(
        WEB_MARGEN_FIJO_DHL_ARS="135000",
        WEB_MARKUP_PCT_DHL="100000",
    ))
    monkeypatch.setattr(precios, "get_conn", lambda: _Conn(cursor))
    auditorias = []
    monkeypatch.setattr(
        "servicios.auditoria.registrar_desde_request_con_cursor",
        lambda cur, request, **kwargs: auditorias.append((cur, kwargs)),
    )

    resultado = precios.guardar_configuracion_dhl(
        request=SimpleNamespace(),
        modo="FIJO_ARS",
        markup_pct="100000",
        margen_fijo_ars="150.000",
    )

    assert resultado["modo"] == "FIJO_ARS"
    assert resultado["margen_fijo_ars"] == Decimal("150000")
    assert resultado["markup_texto"] == "100000"
    assert resultado["publicable"] is True
    assert auditorias[0][0] is cursor
    assert auditorias[0][1]["event"] == "admin.configurar_precio_web_dhl"
    assert any(
        params == (precios.PARAMETRO_FIJO_ARS, "150000")
        for _, params in cursor.ejecutadas
    )
    assert not any(
        params and params[0] == precios.PARAMETRO_MARKUP_PCT
        for _, params in cursor.ejecutadas
    )


def test_guardado_rangos_es_atomico_y_serializa_json_canonico(monkeypatch):
    cursor = _Cursor()
    monkeypatch.setattr(precios, "get_conn", lambda: _Conn(cursor))
    auditorias = []
    monkeypatch.setattr(
        "servicios.auditoria.registrar_desde_request_con_cursor",
        lambda cur, request, **kwargs: auditorias.append((cur, kwargs)),
    )

    resultado = precios.guardar_configuracion_dhl(
        request=SimpleNamespace(),
        modo="RANGOS_USD",
        markup_pct="inactivo inválido",
        margen_fijo_ars="inactivo inválido",
        rangos_usd=[
            {"desde": Decimal("0"), "hasta": Decimal("150.50"),
             "tipo": "PCT", "valor": Decimal("20.5")},
            {"desde": Decimal("150.50"), "hasta": None,
             "tipo": "FIJO_USD", "valor": Decimal("100")},
        ],
    )

    assert resultado["modo"] == "RANGOS_USD"
    assert resultado["rangos_usd"][0] == {
        "desde": "0", "hasta": "150.5", "tipo": "PCT", "valor": "20.5",
    }
    guardado = next(
        params[1]
        for _, params in cursor.ejecutadas
        if params and params[0] == precios.PARAMETRO_RANGOS_USD
    )
    assert guardado == (
        '[{"desde":"0","hasta":"150.5","tipo":"PCT","valor":"20.5"},'
        '{"desde":"150.5","hasta":null,"tipo":"FIJO_USD","valor":"100"}]'
    )
    assert auditorias[0][0] is cursor
    assert auditorias[0][1]["metadata"]["despues"]["rangos_usd"]


def _carrier_dhl_falso():
    class DHLFalso:
        def get_rates(self, _origen, _destino, _paquete):
            return {
                "encontrado": True,
                "costo": 100,
                "moneda": "USD",
                "servicio": "EXPRESS_WORLDWIDE",
                "dias_estimados": "2",
            }

    return [{
        "id": "dhl",
        "nombre": "DHL Express",
        "servicio": "Express Worldwide",
        "logo": "/dhl.svg",
        "requisitos": ("FAKE_DHL_KEY",),
        "cliente": DHLFalso,
    }]


def _cotizar_con_filas(filas, fn):
    config = {precios.FILAS_CONFIG_KEY: filas}
    with mock.patch.dict("os.environ", {"FAKE_DHL_KEY": "1"}, clear=True), \
         mock.patch.object(carriers, "CARRIERS", _carrier_dhl_falso()), \
         mock.patch.object(carriers, "_pricing_configurado", return_value=config):
        return fn(
            {"country": "AR"}, {"country": "US"}, {"peso_kg": 1},
            1000, 20,
        )[0]


def test_admin_nuevo_mueve_solo_web_y_allowlist_no_filtra_costos():
    filas_10 = _filas(
        WEB_DHL_PRICING_MODE="PCT", WEB_DHL_MARKUP_PCT="10"
    )
    filas_30 = _filas(
        WEB_DHL_PRICING_MODE="PCT", WEB_DHL_MARKUP_PCT="30"
    )
    web_10 = _cotizar_con_filas(filas_10, carriers.cotizar_carriers_web)
    web_30 = _cotizar_con_filas(filas_30, carriers.cotizar_carriers_web)
    legacy_10 = _cotizar_con_filas(filas_10, carriers.cotizar_carriers)
    legacy_30 = _cotizar_con_filas(filas_30, carriers.cotizar_carriers)

    assert web_10["precio_ars"] == 110000
    assert web_30["precio_ars"] == 130000
    assert legacy_10["precio_ars"] == legacy_30["precio_ars"] == 120000
    assert not {
        clave for clave in web_30
        if clave.startswith(("costo", "markup", "margen", "fuente"))
    }


def test_rangos_afectan_solo_wrapper_web_y_no_filtran_tramo():
    import json
    rangos_web_demo = [
        {"desde": "0", "hasta": "150", "tipo": "FIJO_ARS", "valor": "30000"},
        {"desde": "150", "hasta": None, "tipo": "FIJO_USD", "valor": "100"},
    ]
    filas = _filas(
        WEB_DHL_PRICING_MODE="RANGOS_USD",
        WEB_DHL_RANGOS_USD=json.dumps(rangos_web_demo),
    )
    web = _cotizar_con_filas(filas, carriers.cotizar_carriers_web)
    legacy = _cotizar_con_filas(filas, carriers.cotizar_carriers)
    assert web["precio_ars"] == 130000
    assert legacy["precio_ars"] == 120000
    assert not {"rango", "tipo", "valor", "fuente"}.intersection(web)


def test_web_dhl_falla_cerrado_si_no_puede_verificar_db():
    with mock.patch.dict("os.environ", {"FAKE_DHL_KEY": "1"}, clear=True), \
         mock.patch.object(carriers, "CARRIERS", _carrier_dhl_falso()), \
         mock.patch.object(
             carriers,
             "_pricing_configurado",
             return_value={precios.ERROR_DB_CONFIG_KEY: True},
         ):
        resultado = carriers.cotizar_carriers_web(
            {"country": "AR"}, {"country": "US"}, {"peso_kg": 1},
            1000, 20,
        )[0]

    assert resultado["estado"] == "sin_tarifa"
    assert "precio_ars" not in resultado
