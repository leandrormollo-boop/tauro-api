"""
El cotizador internacional acepta cualquier par de países salvo AR → AR.

Leandro (05/08/2026): "nuestro servicio permite país x a país b, país b a
país x, país x a país x. Ejemplo: Arg-China, Chn-Arg, Arg-Arg, Chn-India".

China → India es el caso que rompe el modelo viejo, porque Argentina ni
aparece. La tabla `rutas` guardaba pares cargados a mano, así que un país
nuevo era un pedido al admin antes de poder cotizar. Ahora el catálogo es
una constante y la cobertura internacional la decide el courier; AR → AR se
reserva para las conexiones nacionales directas de OCA y Andreani.
"""
import os
import sys
import unicodedata
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from main import CotizarWebRequest  # noqa: E402
from servicios.paises import (  # noqa: E402
    PAISES, existe, nombre, normalizar_iso2, opciones, referencia,
)
from servicios.rutas import pais_a_iso2  # noqa: E402


def _cotizar(**kw):
    """Devuelve (origen, destino) que recibió el comparador de couriers."""
    visto = {}

    def fake(origen, destino, paquete, dolar, markup_pct, paquetes=None):
        visto["origen"], visto["destino"] = origen, destino
        return [{"id": "dhl", "nombre": "DHL", "logo": "", "estado": "cotizado",
                 "servicio": "WW", "dias_estimados": "3",
                 "precio_ars": 1, "precio_usd": 1.0}]

    cuerpo = {"peso_kg": 1.4, "largo_cm": 33, "ancho_cm": 33, "alto_cm": 22,
              "valor_declarado_usd": 300, **kw}
    req = mock.Mock()
    req.headers, req.client = {}, mock.Mock(host="1.2.3.4")

    with mock.patch.object(main, "cotizar_carriers", fake), \
         mock.patch("servicios.cotizador.dolar_ars", return_value=1450.0), \
         mock.patch("servicios.rate_limit.check_rate", return_value=True), \
         mock.patch("servicios.rate_limit.client_ip", return_value="1.2.3.4"):
        datos = main.cotizar_web(CotizarWebRequest(**cuerpo), req)
    return visto, datos


def test_los_tres_casos_internacionales_que_nombro_leandro():
    for origen, destino in (("AR", "CN"), ("CN", "AR"), ("CN", "IN")):
        visto, _ = _cotizar(origen_pais=origen, destino_pais=destino)
        assert visto["origen"]["country"] == origen
        assert visto["destino"]["country"] == destino


def test_endpoint_normaliza_codigos_y_nombres_antes_de_llamar_carriers():
    visto, datos = _cotizar(origen_pais=" china ", destino_pais=" us ")

    assert visto["origen"]["country"] == "CN"
    assert visto["destino"]["country"] == "US"
    assert datos["origen_pais"] == "CN"
    assert datos["destino_pais"] == "US"


def test_argentina_a_argentina_va_al_futuro_circuito_nacional():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        _cotizar(origen_pais=" argentina ", destino_pais=" ar ")

    assert error.value.status_code == 409
    assert "OCA" in error.value.detail
    assert "Andreani" in error.value.detail


def test_tercer_pais_a_tercer_pais():
    """
    El caso que rompía el modelo viejo: ni el origen ni el destino son
    Argentina. Con la tabla de rutas esto era imposible sin cargar la fila.
    """
    visto, _ = _cotizar(origen_pais="BD", destino_pais="US")
    assert visto["origen"]["country"] == "BD"
    assert visto["destino"]["country"] == "US"


def test_un_pais_no_soportado_avisa_cual():
    from fastapi import HTTPException
    try:
        _cotizar(origen_pais="XX", destino_pais="US")
        assert False, "tendría que haber rechazado el país inventado"
    except HTTPException as e:
        assert "origen" in e.detail.lower()


def test_el_widget_viejo_sigue_andando():
    """Retrocompat: sin origen_pais se deduce del sentido, como antes."""
    visto, _ = _cotizar(destino_pais="US")
    assert visto["origen"]["country"] == "AR"
    visto, _ = _cotizar(destino_pais="US", sentido="importacion")
    assert visto["origen"]["country"] == "US"
    assert visto["destino"]["country"] == "AR"


def test_estan_los_origenes_de_importacion_que_nombro():
    for iso in ("CN", "IN", "BD", "US"):
        assert existe(iso), f"falta {iso} en el catálogo"
        assert referencia(iso)["city"], f"{iso} sin ciudad de referencia: no se puede cotizar"


def test_el_combo_viene_ordenado_por_nombre():
    nombres = [n for _, n in opciones()]
    assert nombres == sorted(nombres), "el desplegable sale desordenado"


def _sin_acentos(valor: str) -> str:
    return "".join(
        caracter for caracter in unicodedata.normalize("NFD", valor)
        if not unicodedata.combining(caracter)
    )


@pytest.mark.parametrize("iso,datos", PAISES.items())
def test_normaliza_iso_y_todos_los_nombres_del_catalogo(iso, datos):
    nombre_catalogo = datos[0]

    assert normalizar_iso2(iso) == iso
    assert normalizar_iso2(iso.lower()) == iso
    assert normalizar_iso2(nombre_catalogo) == iso
    assert normalizar_iso2(_sin_acentos(nombre_catalogo).lower()) == iso
    assert pais_a_iso2(nombre_catalogo) == iso


@pytest.mark.parametrize(
    "valor",
    [
        "Estados Unidos", "USA", "EEUU", "EE.UU.", "United States",
        "United States of America", "U.S.A.",
    ],
)
def test_aliases_de_estados_unidos_siempre_son_us(valor):
    assert normalizar_iso2(valor) == "US"
    assert normalizar_iso2(valor) != "ES"


@pytest.mark.parametrize(
    "valor",
    [None, "", "ZZ", "Estados Unicornio", "Estados Unidos del Sur"],
)
def test_pais_desconocido_falla_cerrado(valor):
    assert normalizar_iso2(valor) == ""
    assert existe(valor) is False
    assert referencia(valor) == {}
    assert nombre(valor) == ""


def test_helpers_publicos_aceptan_nombre_o_alias():
    assert existe("méxico") is True
    assert nombre("USA") == "Estados Unidos"
    assert referencia("United States")["country"] == "US"
