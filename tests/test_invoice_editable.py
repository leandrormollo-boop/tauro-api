"""
La declaración de la invoice se completa POR ENVÍO, no sólo del catálogo.

Leandro (05/08): "en donde se carga el producto no permite completar la
declaración de la invoice. Para el portal tenés que entender perfectamente
cómo se realiza una guía en estos couriers."

Cómo se realiza de verdad: FedEx/DHL/UPS exigen por ítem descripción en
inglés, valor unitario, HS code y país de fabricación — y el VALOR REAL DE
VENTA cambia entre envíos. Declarar el default del catálogo cuando se vendió
a otro precio es un problema en la aduana. Igual que en MyDHL+ o Ship
Manager: el producto precarga, el cliente corrige para ese envío.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints.portal_cliente as pc  # noqa: E402
import servicios.api_b2b as b2b  # noqa: E402

RUTA_TPL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "portal", "envio_nuevo.html")


def _html():
    return open(RUTA_TPL, encoding="utf-8").read()


def test_cada_renglon_tiene_los_campos_de_la_invoice():
    html = _html()
    for campo in ("bulto_desc_en", "bulto_valor_usd", "bulto_hs", "bulto_pais_fab"):
        assert f'name="{campo}"' in html, f"falta {campo} en el renglón del producto"


def test_elegir_producto_precarga_su_invoice():
    """data-attrs del catálogo + JS que llena la fila. Editable después."""
    html = _html()
    assert 'data-valor=' in html and 'data-hs=' in html and 'data-desc=' in html
    assert "bulto-valor" in html and "op.dataset.valor" in html


def test_el_submit_recibe_los_overrides():
    firma = inspect.signature(pc.envio_nuevo_post)
    for campo in ("bulto_desc_en", "bulto_valor_usd", "bulto_hs", "bulto_pais_fab"):
        assert campo in firma.parameters, f"el submit no recibe {campo}"


def test_lo_declarado_manda_sobre_el_catalogo():
    """
    El corazón: en las piezas que van al courier, el valor/HS/descripción
    declarados en el envío pisan el default del producto.
    """
    # Ya no se inspecciona la fuente: se ejecuta con datos reales, que es
    # más fuerte — la refactorización de carga libre cambió el código pero
    # la regla es la misma.
    _, det, err = b2b._piezas_del_catalogo("X", [{
        "peso_kg": 3.9, "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20,
        "cantidad": 1, "unidades_aduana": 8,
        "descripcion_en": "SHIRTS 60% POLYESTER 40% COTTON",
        "valor_unitario_usd": 120, "hs_code": "6205.30", "pais_origen": "CN",
    }])
    assert not err
    assert det[0]["valor_unitario_usd"] == 120
    assert det[0]["hs_code"] == "6205.30"
    assert det[0]["pais_origen"] == "CN"


def test_un_valor_ilegible_cae_al_catalogo_sin_romper():
    fuente = inspect.getsource(pc.envio_nuevo_post)
    assert "except ValueError" in fuente


def test_la_emision_fedex_ya_no_declara_AR_fijo():
    """
    Regla de Leandro (01/08): el país de fabricación respeta el ORIGEN del
    envío, o lo declarado por ítem. "AR" fijo declaraba como argentina una
    importación china.
    """
    import servicios.solicitudes_guia as sg
    fuente = inspect.getsource(sg)
    # Puede quedar como último fallback, pero nunca como valor directo.
    assert '"pais_origen": "AR",' not in fuente, (
        "la emisión sigue declarando AR fijo como país de fabricación"
    )
