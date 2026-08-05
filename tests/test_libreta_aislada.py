"""
La libreta de direcciones de cada cliente es SUYA.

Leandro preguntó alarmado: "¡Todos los clientes podrían editar esa hoja!".
La respuesta es no, y estos tests lo fijan — porque es la clase de cosa que
se rompe sin que nadie se entere: alcanza con que alguien "simplifique" un
WHERE o tome el cliente del formulario en vez de la sesión.

Y WAIMAO necesita cargar remitentes del EXTERIOR: cuando importa, el
remitente es su proveedor de China, India o Bangladesh, no una dirección
argentina.
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints.portal_cliente as pc  # noqa: E402
import servicios.direcciones as dd  # noqa: E402


def test_toda_consulta_filtra_por_cliente():
    """
    Sin el cliente_id en el WHERE, un id ajeno alcanza para leer, editar o
    borrar la libreta de otro.
    """
    for fn in (dd.obtener_direccion, dd.actualizar_direccion, dd.eliminar_direccion):
        fuente = inspect.getsource(fn)
        assert "cliente_id = %s" in fuente or "cliente_id=%s" in fuente, (
            f"{fn.__name__} no filtra por cliente: un id ajeno tocaría datos de otro"
        )


def test_el_cliente_sale_de_la_sesion_no_del_formulario():
    """
    EL AGUJERO CLÁSICO: si el cliente_id viniera de un campo del form,
    cualquiera lo edita y escribe en la libreta de otro.
    """
    for fn in (pc.direcciones_add, pc.direcciones_delete):
        fuente = inspect.getsource(fn)
        assert "Depends(cliente_actual)" in fuente, (
            f"{fn.__name__} no toma el cliente de la sesión"
        )
        assert not re.search(r"cliente_id\s*:\s*str\s*=\s*Form", fuente), (
            f"{fn.__name__} acepta el cliente por formulario — se puede falsificar"
        )


def test_borrar_una_direccion_ajena_avisa_y_no_borra():
    fuente = inspect.getsource(pc.direcciones_delete)
    assert "no es tuya" in fuente


def test_la_libreta_acepta_remitentes_del_exterior():
    """
    WAIMAO importa: su remitente es el proveedor de China, India o
    Bangladesh. Si la lista de países no los tiene, no puede cargarlo.
    """
    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "portal", "direcciones.html")
    html = open(ruta, encoding="utf-8").read()
    lista = re.search(r"paises_nombres = \{(.*?)\} %\}", html, re.S).group(1)
    for iso in ("CN", "IN", "BD", "US"):
        assert f"'{iso}'" in lista, f"falta {iso} en la libreta: WAIMAO no puede cargar ese origen"


def test_el_pais_no_esta_forzado_a_argentina():
    """El default es AR, pero tiene que aceptar cualquier país."""
    fuente = inspect.getsource(dd.crear_direccion)
    assert 'pais = (pais or "AR").strip().upper()' in fuente, (
        "el país se estaría pisando en vez de tomar el elegido"
    )
