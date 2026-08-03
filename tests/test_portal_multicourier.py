"""
El portal muestra los 3 couriers, cada cliente con SU markup.

Decisión de Leandro (01/08/2026): "todos los clientes ven todas las
cotizaciones de los couriers. Cada cliente tiene su markup diferente."

Lo que se protege acá es lo que cuesta plata: que el courier que el cliente
eligió en pantalla sea el que se guarda y se cobra. El submit recotiza del
lado servidor (bien: al navegador no se le cree el precio), y si la opción
elegida no vuelve a aparecer, la tentación es agarrar la primera de la lista.
Eso ya pasa en el camino nacional. Acá NO: falla a la vista.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints.portal_cliente as pc  # noqa: E402


def _submit() -> str:
    return inspect.getsource(pc.envio_nuevo_post)


def test_el_submit_recibe_el_courier_elegido():
    assert "intl_courier" in _submit(), (
        "el courier elegido en pantalla no llega al servidor"
    )


def test_no_agarra_otro_courier_cuando_el_elegido_no_cotiza():
    """
    EL TEST QUE IMPORTA. Si el cliente eligió DHL y en la recotización DHL no
    aparece, no puede terminar con un FedEx debitado sin enterarse.
    """
    fuente = _submit()
    assert "no pudo cotizar este envío" in fuente, (
        "falta el error explícito: el submit estaría eligiendo por descarte"
    )


def test_el_courier_queda_guardado_en_la_solicitud():
    """
    Sin esto el despachador de emisión no sabe por dónde sale el envío y
    cae al default — que es justamente el bug que arreglamos hoy.
    """
    fuente = _submit()
    assert 'courier_extra = {"courier": op["id"].upper()}' in fuente


def test_el_endpoint_del_preview_devuelve_las_opciones():
    fuente = inspect.getsource(pc.api_precio_envio_multi)
    assert '"opciones"' in fuente
    assert "no_disponibles" in fuente, (
        "el cliente tiene que poder distinguir 'DHL no llega ahí' de "
        "'DHL no cotiza varias cajas'"
    )


def test_el_preview_no_devuelve_costo_ni_margen():
    """Mismo criterio que la API B2B: el cliente ve precio, nada más."""
    import re
    fuente = inspect.getsource(pc.api_precio_envio_multi)
    retorno = fuente[fuente.rindex("return JSONResponse({"):]
    sin_comentarios = "\n".join(
        l for l in retorno.splitlines() if not l.lstrip().startswith("#")
    )
    filtradas = re.findall(r"[\"']((?:costo|margen|markup)[a-z_]*)[\"']", sin_comentarios, re.I)
    assert not filtradas, f"el preview filtra el costo de TAURO: {filtradas}"
