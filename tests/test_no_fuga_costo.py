"""
El cliente NUNCA puede ver ni deducir lo que TAURO le paga al courier.

Regla de Leandro (01/08/2026): "en su portal él realiza el envío y ve todo el
tiempo el precio final que tiene este cliente. NO puede ver el costo nuestro."

No es una preferencia de diseño: de esa diferencia sale el margen. Si WAIMAO
—costo + $95.000 fijos— ve el markup, sabe nuestro costo con una resta, y con
eso puede negociar contra nuestro propio número o irse a comprarle directo al
courier.

Este test vigila la superficie más peligrosa: el endpoint /cotizar de la API
B2B, que el cliente llama con SU API key. Hasta el 01/08/2026 devolvía
markup_tipo, markup_valor y markup_pct_equivalente — el margen servido en
bandeja. El diccionario que le llega desde api_b2b trae además costo_fedex_ars
y margen_ars, así que un `return {**resultado}` distraído filtraría el costo
crudo. Por eso las claves se listan una por una en main.py y este test verifica
que ninguna prohibida se cuele.
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

# Claves que jamás pueden viajar en una respuesta que ve el cliente.
# Sin \b: un word-boundary antes de una comilla no existe (comilla y espacio
# son los dos "no palabra"), así que con \b este regex no matcheaba NADA y el
# test pasaba siempre — incluso contra el código que sí filtraba.
PROHIBIDOS = re.compile(r"[\"']((?:costo|margen|markup)[a-z_]*)[\"']", re.I)


def _cuerpo(func) -> str:
    return inspect.getsource(func)


def _solo_el_return(func) -> str:
    """
    El bloque del return, sin los comentarios de arriba. Si no, un comentario
    que menciona la clave prohibida hace fallar el test por hablar del tema.
    """
    fuente = _cuerpo(func)
    bloque = fuente[fuente.rindex("return {"):]
    return "\n".join(
        linea for linea in bloque.splitlines()
        if not linea.lstrip().startswith("#")
    )


def test_cotizar_no_devuelve_costo_ni_margen():
    """
    Se lee el código del endpoint en vez de llamarlo: llamarlo necesita base,
    API key y FedEx. Lo que importa es que ninguna clave prohibida esté escrita
    en el dict de respuesta, y eso se ve en la fuente.
    """
    retorno = _solo_el_return(main.cotizar)
    filtrados = PROHIBIDOS.findall(retorno)
    assert not filtrados, (
        "POST /cotizar devuelve claves que revelan el costo o el margen de "
        f"TAURO: {sorted(set(filtrados))}"
    )


def test_el_dict_interno_si_tiene_el_costo():
    """
    Contra-test: confirma que el riesgo es real y el test de arriba no pasa
    por casualidad. api_b2b SÍ arma el costo y el margen — por eso main.py
    tiene que elegir claves a mano y nunca hacer {**resultado}.
    """
    import servicios.api_b2b as b2b

    fuente = inspect.getsource(b2b.obtener_precio_envio)
    assert "costo_fedex_ars" in fuente and "margen_ars" in fuente, (
        "cambió api_b2b: revisá si main.py sigue necesitando filtrar a mano"
    )


def test_cotizar_no_hace_spread_del_resultado():
    """
    La forma en que esto se rompería en el futuro: alguien 'simplifica' el
    return a {**resultado} y filtra todo de una.
    """
    retorno = _solo_el_return(main.cotizar)
    assert "**resultado" not in retorno, (
        "un spread del dict interno filtra costo_fedex_ars y margen_ars al cliente"
    )
