"""
Cada courier tiene su propio margen (Leandro, 01/08/2026: "tenemos que
entender que cada COURIER FEDEX UPS o DHL tienen su markup diferente").

Lo que se protege acá es el ORDEN DE PRIORIDAD, que es donde esto se rompe
sin dar error: si el admin edita un margen en /admin/config y el código
sigue leyendo la variable de entorno, la pantalla muestra una perilla que no
mueve nada. Eso ya pasaba con WEB_MARKUP_PCT: la fila existía en la tabla
desde el día uno y nadie la leía.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios.carriers import _markup_de, _desc_fedex  # noqa: E402


def test_el_admin_le_gana_a_la_variable_de_entorno():
    """Lo que Leandro edita en /admin/config manda."""
    with mock.patch.dict(os.environ, {"WEB_MARKUP_PCT_DHL": "99"}, clear=True):
        assert _markup_de("dhl", 20.0, {"WEB_MARKUP_PCT_DHL": 25.0}) == 25.0


def test_sin_fila_en_config_vale_la_variable_de_entorno():
    with mock.patch.dict(os.environ, {"WEB_MARKUP_PCT_DHL": "30"}, clear=True):
        assert _markup_de("dhl", 20.0, {}) == 30.0


def test_sin_nada_propio_cae_al_general_de_config():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _markup_de("ups", 20.0, {"WEB_MARKUP_PCT": 18.0}) == 18.0


def test_cada_courier_puede_tener_el_suyo():
    """El punto de todo esto: tres couriers, tres márgenes distintos."""
    config = {
        "WEB_MARKUP_PCT": 20.0,
        "WEB_MARKUP_PCT_FEDEX": 35.0,
        "WEB_MARKUP_PCT_DHL": 20.0,
        "WEB_MARKUP_PCT_UPS": 28.0,
    }
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _markup_de("fedex", 20.0, config) == 35.0
        assert _markup_de("dhl", 20.0, config) == 20.0
        assert _markup_de("ups", 20.0, config) == 28.0


def test_un_valor_roto_no_tumba_el_cotizador():
    """Un margen mal tipeado tiene que caer al general, no explotar."""
    with mock.patch.dict(os.environ, {"WEB_MARKUP_PCT_UPS": "veinte"}, clear=True):
        assert _markup_de("ups", 20.0, {"WEB_MARKUP_PCT": 22.0}) == 22.0


def test_el_descuento_de_fedex_tambien_se_edita_desde_el_admin():
    """
    Si el margen de FedEx viviera en el admin y su descuento en Railway, la
    pantalla mostraría una perilla que no hace nada.
    """
    with mock.patch.dict(os.environ, {"WEB_DESC_FEDEX_PCT": "90"}, clear=True):
        assert _desc_fedex({"WEB_DESC_FEDEX_PCT": 88.0}) == 88.0
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _desc_fedex({}) == 88.0
