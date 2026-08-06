"""
El catálogo es OPCIONAL para armar un envío desde el portal.

Regla de Leandro (06/08/2026): "El tema del producto en catálogo debe ser
opcional, para que los clientes luego integren la api a su tienda. Para el
portal no pasa nada, el cliente puede cargar manualmente o decidir si usar el
producto ya guardado."

Antes, TODA fila exigía un producto aprobado (`portal_cliente.py`: "El producto
X no está activo en tu catálogo") y el submit venía `disabled` cuando el cliente
no tenía ninguno. O sea: un cliente nuevo no podía crear NI UN envío hasta que
Tauro le validara el catálogo a mano.

Lo que sí sigue siendo obligatorio con carga manual es lo que el courier
necesita para cotizar y para declarar en aduana — sin catálogo de dónde
sacarlo, hay que pedirlo.
"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servicios.nacional as nac  # noqa: E402


class _ProductoFalso:
    activo = True
    alias_interno = "CAJA-A"
    nombre_invoice = "Cotton shirts"
    hs_code = "6205.30"
    peso_kg = 4.0
    largo_cm = 40.0
    ancho_cm = 30.0
    alto_cm = 20.0
    valor_usd_default = 100.0


@pytest.fixture
def _sin_red(monkeypatch):
    """Aísla la cotización: sólo se mide el armado de piezas, no la API."""
    monkeypatch.setattr(nac, "_get_dolar_ars", lambda: 1000.0)


def _piezas(bultos, producto=None):
    """Corre el armado de bultos y devuelve las piezas que salieron."""
    capturado = {}

    class _ClienteFalso:
        configurado = True

        def cotizar_nacional(self, origen, destino, piezas):
            capturado["piezas"] = piezas
            return {"encontrado": False, "error": "corte a propósito"}

    with mock.patch.object(nac, "get_producto", lambda c, a: producto), \
         mock.patch.object(nac, "EnviaClient", _ClienteFalso), \
         mock.patch.object(nac, "get_pricing_config", lambda *a, **k: None), \
         mock.patch.object(nac, "get_markup_pct", lambda *a, **k: 20.0):
        r = nac.cotizar_nacional_cliente("ACME", {"cp": "1414"}, {"cp": "5000"}, bultos)
    return r, capturado.get("piezas")


def test_caja_manual_sin_producto_no_se_rechaza(_sin_red):
    """El corte tiene que venir de la API, NO de 'producto_no_encontrado'."""
    bultos = [{"producto": "", "cantidad": 2, "peso_kg": 3.5,
               "largo_cm": 40, "ancho_cm": 30, "alto_cm": 20,
               "valor_unitario_usd": 50, "descripcion_en": "Zapatillas"}]
    r, piezas = _piezas(bultos)

    assert "producto_no_encontrado" not in str(r.get("motivo", ""))
    assert piezas, "la caja manual nunca llegó al carrier"
    assert piezas[0]["peso_kg"] == 3.5
    assert piezas[0]["largo_cm"] == 40
    assert piezas[0]["descripcion"] == "Zapatillas"
    assert piezas[0]["cantidad"] == 2


def test_sin_descripcion_la_pieza_igual_sale_con_algo(_sin_red):
    """Mandar descripción vacía al carrier es un rechazo garantizado."""
    bultos = [{"producto": "", "cantidad": 1, "peso_kg": 1.0,
               "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10}]
    _, piezas = _piezas(bultos)
    assert piezas[0]["descripcion"].strip()


def test_un_alias_que_no_existe_sigue_fallando(_sin_red):
    """Opcional no es 'se ignora': si eligió un producto, tiene que existir."""
    bultos = [{"producto": "NO-EXISTE", "cantidad": 1, "peso_kg": 1.0}]
    r, _ = _piezas(bultos, producto=None)
    assert r["encontrado"] is False
    assert "producto_no_encontrado" in r["motivo"]


def test_lo_cargado_a_mano_pisa_al_catalogo(_sin_red):
    """
    El catálogo trae el default; lo tipeado en ESTE envío manda. Si no, el
    cliente corrige el peso en pantalla, ve un precio y se le cobra otro.
    """
    bultos = [{"producto": "CAJA-A", "cantidad": 1, "peso_kg": 9.9, "largo_cm": 55}]
    _, piezas = _piezas(bultos, producto=_ProductoFalso())

    assert piezas[0]["peso_kg"] == 9.9      # el del form, no los 4.0 del catálogo
    assert piezas[0]["largo_cm"] == 55.0
    assert piezas[0]["ancho_cm"] == 30.0    # este no se tocó: sale del catálogo


def test_caja_sin_peso_se_rechaza_con_motivo_claro(_sin_red):
    """Peso 0 cotizaría gratis: tiene que cortar y decir por qué."""
    bultos = [{"producto": "", "cantidad": 1, "largo_cm": 10,
               "ancho_cm": 10, "alto_cm": 10, "descripcion_en": "Algo"}]
    r, _ = _piezas(bultos)
    assert r["encontrado"] is False
    assert "sin_peso" in r["motivo"]


def test_el_tope_de_peso_nacional_se_aplica_igual_sin_catalogo(_sin_red):
    bultos = [{"producto": "", "cantidad": 1,
               "peso_kg": nac.MAX_KG_POR_CAJA_NAC + 1,
               "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
               "descripcion_en": "Bulto pesado"}]
    r, _ = _piezas(bultos)
    assert r["encontrado"] is False
    assert "máximo nacional" in r["motivo"]
