"""
El remitente del wizard es EDITABLE, y de él sale el país de origen.

Leandro (05/08): "el portal del cliente no permite modificar los datos del
remitente al momento de generar un nuevo envío" y "no permite cotizar desde
cualquier país". Son el mismo bug: el remitente era un desplegable de sólo
lectura, y como el origen del envío sale del remitente, el origen quedaba
clavado en lo que hubiera en la libreta.

Para WAIMAO el remitente es un proveedor del exterior que puede cambiar
envío a envío: China hoy, India la semana que viene.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints.portal_cliente as pc  # noqa: E402

RUTA_TPL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "portal", "envio_nuevo.html")


def test_el_wizard_tiene_los_campos_del_remitente():
    html = open(RUTA_TPL, encoding="utf-8").read()
    for campo in ("rem_nombre", "rem_direccion", "rem_ciudad", "rem_zip",
                  "rem_pais", "rem_documento"):
        assert f'name="{campo}"' in html, f"falta el campo editable {campo}"


def test_el_pais_de_origen_es_un_desplegable_completo():
    """No un texto fijo: el combo con el catálogo entero."""
    html = open(RUTA_TPL, encoding="utf-8").read()
    assert 'id="rem_pais"' in html
    ini = html.index('id="rem_pais"')
    bloque = html[ini:ini + 600]
    assert "paises_destino" in bloque, "el país de origen no usa el catálogo"


def test_cambiar_el_origen_recotiza():
    """El precio depende de dónde sale la caja: China ≠ Argentina."""
    html = open(RUTA_TPL, encoding="utf-8").read()
    ini = html.index('const remPais = document.getElementById("rem_pais")')
    assert "refreshLivePrice" in html[ini:ini + 400], (
        "cambiar el país de origen no refresca el precio en vivo"
    )


def test_el_preview_manda_el_origen():
    html = open(RUTA_TPL, encoding="utf-8").read()
    assert "origen_pais:" in html, "el fetch del precio en vivo no manda el origen"


def test_el_submit_acepta_el_remitente_editado():
    firma = inspect.signature(pc.envio_nuevo_post)
    for campo in ("rem_nombre", "rem_pais", "rem_zip", "rem_documento"):
        assert campo in firma.parameters, f"el submit no recibe {campo}"


def test_lo_editado_manda_sobre_la_libreta():
    """
    Elegir de la libreta y corregir UN campo (el CP) no puede pisar el
    resto ni perderse: campo por campo, lo editado gana.
    """
    fuente = inspect.getsource(pc.envio_nuevo_post)
    assert "editado" in fuente
    assert 'remitente[campo] = valor.strip()' in fuente
