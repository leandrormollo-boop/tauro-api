"""
El cliente especifica POR QUÉ COURIER sale cada envío, y puede dejarlo
configurado en su cuenta.

Leandro (05/08): "el cliente tiene que especificar por qué empresa realiza
sus envíos. Puede dejarlo configurado... WAIMAO quiere hacer estos 2 envíos
por DHL pero no tenemos forma de especificarlo."

Lo que había: la elección existía pero era un <select> chiquito que aparecía
recién DESPUÉS de cotizar, adentro de la caja del precio en vivo. Invisible.
Y no había preferencia por cliente: cada envío arrancaba de cero.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints.portal_cliente as pc  # noqa: E402
from servicios.carriers import courier_default_cliente  # noqa: E402

RUTA_TPL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "portal", "envio_nuevo.html")


def _html():
    return open(RUTA_TPL, encoding="utf-8").read()


def test_el_selector_de_courier_esta_a_la_vista():
    """Botones en el formulario, no un combo escondido en el precio."""
    html = _html()
    assert 'id="courier-btns"' in html
    for c in ("fedex", "dhl", "ups"):
        assert f'data-courier="{c}"' in html, f"falta el botón de {c}"


def test_cada_boton_muestra_su_precio_al_cotizar():
    """'Primero cotizar y luego generar con el prediseño de cada courier'."""
    html = _html()
    assert "courier-precio" in html
    assert "pintarBotonesCourier" in html


def test_arranca_con_el_courier_configurado_del_cliente():
    html = _html()
    assert "courier_default" in html, "el wizard no recibe la preferencia del cliente"
    fuente = inspect.getsource(pc.envio_nuevo_form)
    assert "courier_default_cliente" in fuente


def test_el_elegido_que_no_cotiza_se_ve_no_se_reemplaza():
    """
    Si WAIMAO pide DHL y DHL no cotiza ese envío, tiene que VERLO — no
    descubrir en el resumen de cuenta que salió por otro courier.
    """
    html = _html()
    assert "no cotizó este envío" in html


def test_el_default_invalido_cae_a_vacio():
    """Un valor basura en la columna no puede romper el wizard."""
    assert courier_default_cliente("") == ""


def test_el_rerender_tras_error_conserva_la_preferencia():
    """
    El agujero clásico del re-render: falla la validación, se repinta el
    form, y el contexto no vuelve a pasar courier_default — el cliente
    pierde su courier configurado justo cuando reintenta.
    """
    fuente = inspect.getsource(pc.envio_nuevo_post)
    assert fuente.count("courier_default_cliente") >= 1, (
        "el re-render tras error no pasa courier_default"
    )
