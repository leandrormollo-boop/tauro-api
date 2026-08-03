"""
Se cotiza contra el CP REAL del destinatario, no contra el de referencia.

El agujero que cierra: la tabla `rutas` guarda una ciudad y un CP de ejemplo
por par de países (US → MIAMI 33101). El camino internacional del portal
cotizaba contra ESE CP y después emitía la guía contra la dirección real.

Los couriers cobran recargos que dependen del CP exacto — zona remota (DHL:
USD 38 o USD 0,70/kg, el mayor), dirección residencial (USD 9,50) — y esos
recargos vienen adentro del precio SI les pasás la dirección real. Cotizando
contra el CP céntrico, el courier contesta barato, le cobramos eso al cliente,
despachamos al CP real y el courier nos factura el recargo. La diferencia la
comía TAURO, y como nadie concilia lo cotizado contra lo facturado, no
aparecía en ningún lado.

Delataba el bug que la rama NACIONAL del mismo formulario ya lo hacía bien
(portal_cliente.py, `destino_nac`): sólo la internacional tiraba la dirección.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios.cotizador import _destino_para_cotizar  # noqa: E402


class _Ruta:
    """La ruta trae SIEMPRE el CP de referencia: US → MIAMI 33101."""
    destino_ciudad = "MIAMI"
    destino_zip = "33101"
    destino_pais = "US"
    origen_ciudad = "BUENOS AIRES"
    origen_zip = "1043"
    origen_pais = "AR"


RUTA = _Ruta()


def test_con_direccion_real_cotiza_contra_esa():
    """
    Jackson, Wyoming 83001 es zona remota. Si cotizamos contra 33101 (Miami),
    el recargo aparece recién en la factura del courier y lo paga TAURO.
    """
    d = _destino_para_cotizar(RUTA, {"cp": "83001", "ciudad": "JACKSON", "estado": "WY"})
    assert d["postal_code"] == "83001", "cotizó contra el CP de referencia"
    assert d["city"] == "JACKSON"
    assert d["state"] == "WY"
    assert d["country"] == "US"


def test_sin_direccion_real_cae_a_la_referencia():
    """Una estimación sin destinatario cargado sigue funcionando."""
    d = _destino_para_cotizar(RUTA, None)
    assert d["postal_code"] == "33101"
    assert d["city"] == "MIAMI"


def test_un_cp_vacio_no_pisa_la_referencia():
    """
    El form manda strings vacíos antes de que el cliente complete. Un CP
    vacío al courier es un error garantizado: mejor la referencia.
    """
    d = _destino_para_cotizar(RUTA, {"cp": "", "ciudad": "", "estado": ""})
    assert d["postal_code"] == "33101"


def test_acepta_las_dos_formas_de_nombrar_los_campos():
    """
    El portal manda {cp, ciudad, estado} y los clientes de courier hablan
    {postal_code, city, state}. Que un rename silencioso no vuelva a tirar
    la dirección.
    """
    d = _destino_para_cotizar(RUTA, {"postal_code": "83001", "city": "JACKSON", "state": "WY"})
    assert d["postal_code"] == "83001"
    assert d["city"] == "JACKSON"


def test_el_pais_manda_la_ruta_no_el_destinatario():
    """
    El país sale de la ruta a propósito: es lo que define la tarifa base y
    ya está validado. El destinatario sólo aporta el detalle de la dirección.
    """
    d = _destino_para_cotizar(RUTA, {"cp": "83001", "ciudad": "JACKSON", "estado": "WY"})
    assert d["country"] == "US"


def test_el_portal_pasa_el_destino_real_al_cotizar():
    """
    Test de cableado: que la firma exista no sirve si el portal no la usa.
    Antes el submit llamaba obtener_precio_envio_multi(cliente, pais, filas)
    con la dirección real a 30 líneas de distancia, en el scope, sin usarla.
    """
    import inspect
    import endpoints.portal_cliente as pc

    fuente = inspect.getsource(pc.envio_nuevo_post)
    assert "destino_real=" in fuente, (
        "el submit internacional volvió a cotizar sólo por país: el precio que "
        "se debita no incluye los recargos del CP real"
    )
