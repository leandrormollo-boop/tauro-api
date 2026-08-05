"""
En el PORTAL, el cliente elige desde dónde y hacia dónde. Cualquier país.

Leandro (05/08): "el desplegable tiene que permitir que el cliente elija
desde donde hacia donde va el envio. Puede elegir cualquier pais."

Lo que había: el desplegable de destino salía de las rutas cargadas a mano
en el admin, así que WAIMAO sólo podía despachar a donde alguien ya hubiera
creado la fila. Y el origen era Argentina fija, cuando en una importación el
remitente es el proveedor de China.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints.portal_cliente as pc  # noqa: E402
import servicios.api_b2b as b2b  # noqa: E402
from servicios.paises import PAISES  # noqa: E402


def test_el_desplegable_ofrece_todos_los_paises():
    """
    Antes: get_paises_destino(), o sea sólo los que tenían ruta cargada.
    Ahora: el catálogo completo.
    """
    paises = pc._paises_con_nacional()
    assert len(paises) == len(PAISES), (
        f"el portal ofrece {len(paises)} países de {len(PAISES)} — "
        "sigue atado a las rutas cargadas"
    )
    isos = {iso for iso, _ in paises}
    for esperado in ("CN", "IN", "BD", "US", "AR"):
        assert esperado in isos, f"falta {esperado} en el desplegable del portal"


def test_cotizar_no_exige_una_ruta_cargada():
    """
    Exigir una fila en `rutas` era pedirle al admin cientos de pares a mano,
    y cada país nuevo bloqueaba al cliente hasta que alguien la creara.
    """
    fuente = inspect.getsource(b2b.cotizar_couriers_cliente)
    assert "buscar_ruta_para_destino" not in fuente, (
        "la cotización del portal sigue dependiendo de una ruta cargada"
    )
    assert "ruta_no_encontrada" not in fuente


def test_el_origen_sale_del_remitente_no_de_una_constante():
    """
    En una importación el remitente es el proveedor del exterior: de ahí
    sale el país de origen. Con "AR" fijo, una importación de China se
    cotizaba —y se declaraba— como si saliera de Argentina.
    """
    fuente = inspect.getsource(pc.envio_nuevo_post)
    assert "origen_real=" in fuente, "el submit no manda el origen del remitente"

    firma = inspect.signature(b2b.cotizar_couriers_cliente)
    assert "origen_real" in firma.parameters


def test_un_pais_inventado_se_rechaza_con_su_motivo():
    r = b2b.cotizar_couriers_cliente("TEST", "XX", [{"producto": "x", "cantidad": 1}])
    assert not r["encontrado"]
    assert "pais_no_soportado" in r["motivo"]


def test_NINGUNA_pantalla_del_portal_queda_con_la_lista_vieja():
    """
    Se me escapó /portal/cotizar la primera vez: cambié el wizard y dejé el
    cotizador suelto con get_paises_destino(), que sale de las rutas. Leandro
    lo vio enseguida ("me sigue figurando igual el portal").

    Este test recorre el archivo entero en vez de confiar en que me acordé de
    todas las pantallas.
    """
    import re

    ruta = inspect.getsourcefile(pc)
    fuente = open(ruta, encoding="utf-8").read()
    # Se ignoran los comentarios: hablar de la función vieja está permitido.
    codigo = "\n".join(l for l in fuente.splitlines() if not l.lstrip().startswith("#"))
    culpables = re.findall(r'"paises_(?:destino|origen)":\s*get_paises_\w+\(\)', codigo)
    assert not culpables, (
        f"quedaron {len(culpables)} pantallas atadas a las rutas cargadas: {culpables}"
    )
