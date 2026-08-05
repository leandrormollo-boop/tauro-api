"""
El cotizador acepta CUALQUIER par de países.

Leandro (05/08/2026): "nuestro servicio permite país x a país b, país b a
país x, país x a país x. Ejemplo: Arg-China, Chn-Arg, Arg-Arg, Chn-India".

Ese último caso es el que rompe el modelo viejo: China → India, donde
Argentina ni aparece. La tabla `rutas` guardaba pares cargados a mano, así
que un país nuevo era un pedido al admin antes de poder cotizar. Ahora el
catálogo es una constante y la cobertura la decide el courier.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from main import CotizarWebRequest  # noqa: E402
from servicios.paises import existe, opciones, referencia  # noqa: E402


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


def test_los_cuatro_casos_que_nombro_leandro():
    for origen, destino in (("AR", "CN"), ("CN", "AR"), ("AR", "AR"), ("CN", "IN")):
        visto, _ = _cotizar(origen_pais=origen, destino_pais=destino)
        assert visto["origen"]["country"] == origen
        assert visto["destino"]["country"] == destino


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
