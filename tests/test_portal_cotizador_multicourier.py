"""Contrato del cotizador rápido multi-courier del portal."""
import inspect
import os
import re
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints.portal_cliente as portal  # noqa: E402
from servicios import cotizador  # noqa: E402


def _tarjetas_dos_couriers():
    return [
        {
            "id": "fedex", "nombre": "FedEx", "logo": "/fedex.svg",
            "servicio": "International Priority", "estado": "cotizado",
            "dias_estimados": "3", "precio_ars": 210000, "precio_usd": 140,
        },
        {
            "id": "ups", "nombre": "UPS", "logo": "/ups.svg",
            "servicio": "Worldwide Express", "estado": "proximamente",
            "error": "UPS_CLIENT_SECRET no configurado",
        },
        {
            "id": "dhl", "nombre": "DHL Express", "logo": "/dhl.svg",
            "servicio": "Express Worldwide", "estado": "cotizado",
            "dias_estimados": "2", "precio_ars": 195000, "precio_usd": 130,
        },
    ]


def _cotizar_con(tarjetas):
    capturado = {}

    def falso(**kwargs):
        capturado.update(kwargs)
        return tarjetas

    with mock.patch(
        "servicios.carriers.cotizar_carriers_cliente", side_effect=falso,
    ), mock.patch.object(cotizador, "_get_dolar_ars", return_value=1500), \
         mock.patch(
             "servicios.configuracion_couriers_cliente.configuracion_cotizacion",
             return_value={
                 "pricing_general": {"tipo": "FIJO_ARS", "valor": 14000},
                 "pricing_por_courier": {
                     "fedex": {"tipo": "FIJO_ARS", "valor": 14000},
                     "dhl": {"tipo": "FIJO_ARS", "valor": 14000},
                     "ups": {"tipo": "FIJO_ARS", "valor": 14000},
                 },
                 "couriers_habilitados": {"fedex", "dhl", "ups"},
             },
         ):
        resultado = cotizador.cotizar_referencia_couriers(
            cliente="MELCIOR", origen_pais="AR", destino_pais="US",
            peso_kg=1.2, largo_cm=40, ancho_cm=30, alto_cm=20,
            valor_declarado_usd=250,
        )
    return resultado, capturado


def test_muestra_fedex_y_dhl_ordenados_por_precio():
    resultado, _ = _cotizar_con(_tarjetas_dos_couriers())

    assert resultado["encontrado"] is True
    assert [o["carrier_id"] for o in resultado["opciones"]] == ["dhl", "fedex"]
    assert resultado["opciones"][0]["carrier_nombre"] == "DHL Express"
    assert resultado["opciones"][0]["carrier_logo"] == "/dhl.svg"
    assert resultado["resumen"]["couriers_consultados"] == 3
    assert resultado["resumen"]["valor_declarado_usd"] == 250


def test_envia_al_courier_las_medidas_reales_con_claves_canonicas():
    resultado, capturado = _cotizar_con(_tarjetas_dos_couriers())

    assert capturado["origen"]["country"] == "AR"
    assert capturado["destino"]["country"] == "US"
    assert capturado["paquete"] == {
        "peso_kg": 1.2, "largo": 40.0, "ancho": 30.0, "alto": 20.0,
        "valor_declarado_usd": 250.0, "descripcion_en": "Merchandise",
        "unidades": 1, "valor_unitario_usd": 250.0,
    }
    assert resultado["resumen"]["peso_volumetrico_kg"] == 4.8
    assert resultado["resumen"]["peso_usado_kg"] == 4.8


def test_multibulto_expande_cajas_y_suma_peso_facturable():
    capturado = {}
    with mock.patch(
        "servicios.carriers.cotizar_carriers_cliente",
        side_effect=lambda **kwargs: capturado.update(kwargs) or _tarjetas_dos_couriers(),
    ), mock.patch.object(cotizador, "_get_dolar_ars", return_value=1500), \
         mock.patch(
             "servicios.configuracion_couriers_cliente.configuracion_cotizacion",
             return_value={
                 "pricing_general": {"tipo": "FIJO_ARS", "valor": 95_000},
                 "pricing_por_courier": {
                     "dhl": {"tipo": "FIJO_ARS", "valor": 95_000},
                 },
                 "couriers_habilitados": {"dhl"},
             },
         ):
        resultado = cotizador.cotizar_referencia_couriers(
            cliente="WAIMAO", origen_pais="AR", destino_pais="US",
            peso_kg=2, largo_cm=40, ancho_cm=30, alto_cm=20,
            valor_declarado_usd=300,
            paquetes=[
                {"cantidad": 2, "peso_kg": 2, "largo_cm": 40,
                 "ancho_cm": 30, "alto_cm": 20},
                {"cantidad": 1, "peso_kg": 5, "largo_cm": 50,
                 "ancho_cm": 40, "alto_cm": 30},
            ],
        )

    assert len(capturado["paquetes"]) == 3
    assert [p["peso_kg"] for p in capturado["paquetes"]] == [2, 2, 5]
    assert all(p["valor_unitario_usd"] == 100 for p in capturado["paquetes"])
    assert resultado["resumen"]["cantidad_bultos"] == 3
    assert resultado["resumen"]["peso_real_kg"] == 9
    assert resultado["resumen"]["peso_volumetrico_kg"] == 21.6
    assert resultado["resumen"]["peso_usado_kg"] == 21.6


def test_multibulto_rechaza_mas_de_20_cajas_antes_del_courier():
    with mock.patch("servicios.carriers.cotizar_carriers_cliente") as consultar:
        with pytest.raises(ValueError, match="máximo es 20"):
            cotizador.cotizar_referencia_couriers(
                cliente="WAIMAO", origen_pais="AR", destino_pais="US",
                peso_kg=1, largo_cm=10, ancho_cm=10, alto_cm=10,
                valor_declarado_usd=100,
                paquetes=[{"cantidad": 21, "peso_kg": 1, "largo_cm": 10,
                           "ancho_cm": 10, "alto_cm": 10}],
            )
    consultar.assert_not_called()


def test_un_courier_sin_tarifa_no_borra_al_otro_ni_filtra_el_error():
    tarjetas = _tarjetas_dos_couriers()
    tarjetas[-1] = {
        "id": "dhl", "nombre": "DHL Express", "logo": "/dhl.svg",
        "servicio": "Express Worldwide", "estado": "sin_tarifa",
        "error": "DHL_ACCOUNT_NUMBER_IMPO no configurada",
    }
    resultado, _ = _cotizar_con(tarjetas)

    assert [o["carrier_id"] for o in resultado["opciones"]] == ["fedex"]
    dhl = next(c for c in resultado["no_disponibles"] if c["id"] == "dhl")
    assert dhl == {
        "id": "dhl", "nombre": "DHL Express", "estado": "sin_tarifa",
        "motivo": "No devolvió tarifa para esta referencia.",
    }
    assert "error" not in repr(resultado["no_disponibles"]).lower()
    assert "account" not in repr(resultado["no_disponibles"]).lower()


def test_dhl_puede_cotizar_aunque_fedex_no_devuelva_tarifa():
    tarjetas = _tarjetas_dos_couriers()
    tarjetas[0] = {
        "id": "fedex", "nombre": "FedEx", "logo": "/fedex.svg",
        "servicio": "International Priority", "estado": "sin_tarifa",
        "error": "rechazo interno del proveedor",
    }
    resultado, _ = _cotizar_con(tarjetas)

    assert resultado["encontrado"] is True
    assert [o["carrier_id"] for o in resultado["opciones"]] == ["dhl"]


def test_error_401_se_convierte_en_aviso_seguro_y_accionable():
    tarjetas = _tarjetas_dos_couriers()
    tarjetas[-1] = {
        "id": "dhl", "nombre": "DHL Express", "logo": "/dhl.svg",
        "servicio": "Express Worldwide", "estado": "sin_tarifa",
        "error": "DHL rechazó las credenciales productivas (HTTP 401).",
    }
    resultado, _ = _cotizar_con(tarjetas)

    dhl = next(c for c in resultado["no_disponibles"] if c["id"] == "dhl")
    assert dhl["motivo"] == "La conexión productiva necesita revisión de TAURO."
    assert "credencial" not in repr(dhl).lower()


def test_si_ninguno_cotiza_el_resultado_es_neutral_y_sin_secretos():
    tarjetas = [
        {
            "id": t["id"], "nombre": t["nombre"], "logo": t["logo"],
            "servicio": t["servicio"], "estado": "sin_tarifa",
            "error": "SECRET_ACCOUNT=123",
        }
        for t in _tarjetas_dos_couriers()
    ]
    resultado, _ = _cotizar_con(tarjetas)

    assert resultado["encontrado"] is False
    assert resultado["opciones"] == []
    assert len(resultado["no_disponibles"]) == 3
    assert "secret" not in repr(resultado).lower()


def test_nunca_devuelve_costo_margen_o_markup():
    resultado, _ = _cotizar_con(_tarjetas_dos_couriers())
    prohibidas = re.findall(
        r"['\"]((?:costo|margen|markup)[a-z_]*)['\"]", repr(resultado), re.I,
    )
    assert not prohibidas


@pytest.mark.parametrize(
    "campo,valor,mensaje",
    [
        ("peso_kg", 71, "70 kg"),
        ("peso_kg", 0, "mayores a cero"),
        ("largo_cm", 301, "330 cm"),
    ],
)
def test_conserva_los_limites_del_formulario(campo, valor, mensaje):
    datos = dict(
        cliente="MELCIOR", origen_pais="AR", destino_pais="US",
        peso_kg=1.2, largo_cm=40, ancho_cm=30, alto_cm=20,
        valor_declarado_usd=250,
    )
    datos[campo] = valor
    with pytest.raises(ValueError, match=mensaje):
        cotizador.cotizar_referencia_couriers(**datos)


@pytest.mark.parametrize("valor", [None, "", 0, -1, float("nan")])
def test_valor_declarado_invalido_no_consulta_carriers(valor):
    with mock.patch("servicios.carriers.cotizar_carriers_cliente") as consultar:
        with pytest.raises(ValueError, match="valor declarado"):
            cotizador.cotizar_referencia_couriers(
                cliente="MELCIOR", origen_pais="AR", destino_pais="US",
                peso_kg=1.2, largo_cm=40, ancho_cm=30, alto_cm=20,
                valor_declarado_usd=valor,
            )
    consultar.assert_not_called()


def test_el_post_rapido_ya_no_esta_atado_a_fedex_ni_a_rutas_manual():
    fuente = inspect.getsource(portal.cotizar_post)
    assert "cotizar_referencia_couriers" in fuente
    assert "find_ruta_por_paises" not in fuente
    assert "cotizar_opciones" not in fuente
    assert "FedEx no devolvió" not in fuente
    assert "Ningún courier" in fuente


def test_la_vista_no_esconde_dhl_despues_de_dos_opciones():
    ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "templates", "portal", "cotizar.html")
    html = open(ruta, encoding="utf-8").read()
    assert "opciones[:2]" not in html
    assert "opciones[2:]" not in html
    assert "op.carrier_logo" in html
    assert 'class="quote-carrier-logo"' in html
    assert "no_disponibles" in html
    assert "        {% endif %}\n\n        {% if no_disponibles %}" in html


def test_la_vista_permite_agregar_y_quitar_cajas():
    ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "templates", "portal", "cotizar.html")
    html = open(ruta, encoding="utf-8").read()
    for campo in (
        "bulto_cantidad", "bulto_peso", "bulto_largo",
        "bulto_ancho", "bulto_alto",
    ):
        assert f'name="{campo}"' in html
    assert 'id="quote-add-package"' in html
    assert "data-remove-package" in html
