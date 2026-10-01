"""La raíz /portal redirige al home del portal (antes respondía 404)."""

from endpoints import portal_cliente


def test_raiz_del_portal_redirige_al_home():
    respuesta = portal_cliente.portal_raiz()
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/portal/home"


def test_raiz_del_portal_esta_registrada_con_y_sin_barra():
    rutas = {ruta.path for ruta in portal_cliente.router.routes}
    assert {"/portal", "/portal/"} <= rutas
