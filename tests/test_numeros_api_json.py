"""Las APIs JSON aceptan los mismos números humanos que los formularios."""

import pytest
from pydantic import ValidationError

from main import CotizarWebRequest, LeadCotizacionRequest, PedidoRequest
from modelos.cotizacion import CotizacionAvanzada, CotizacionInput
from modelos.producto import ItemPedido, ProductoNuevo


@pytest.mark.parametrize("peso", ["5,5", "5.5", 5.5])
def test_cotizador_web_acepta_decimal_localizado(peso):
    body = CotizarWebRequest(
        origen_pais="AR",
        destino_pais="US",
        peso_kg=peso,
        largo_cm="30,5",
        ancho_cm="20.5",
        alto_cm=10,
        valor_declarado_usd="100.000",
    )
    assert body.peso_kg == 5.5
    assert body.largo_cm == 30.5
    assert body.valor_declarado_usd == 100_000


def test_modelos_de_cotizacion_simple_y_avanzada_comparten_parser():
    simple = CotizacionInput(
        ruta_id="AR-US", peso_kg="5,5", largo_cm="30,5",
        ancho_cm="20,5", alto_cm="10,5", valor_declarado_usd="1.234,56",
    )
    avanzada = CotizacionAvanzada(
        origen_pais="AR", origen_ciudad="Buenos Aires", origen_zip="1000",
        destino_pais="US", destino_ciudad="Miami", destino_zip="33101",
        peso_kg="5.5", largo_cm="30.5", ancho_cm="20.5", alto_cm="10.5",
    )
    assert simple.peso_kg == avanzada.peso_kg == 5.5
    assert simple.valor_declarado_usd == 1234.56


def test_producto_y_pedido_aceptan_importes_es_en_sin_cambiar_el_valor():
    producto = ProductoNuevo(
        alias_interno="zapato", nombre_invoice="Leather shoes",
        hs_code="6403.99.00", largo_cm="12,5", ancho_cm="33.5",
        alto_cm="36", peso_kg="1,2", valor_usd_default="100,000",
    )
    item = ItemPedido(alias_interno="zapato", cantidad=1, valor_unitario_usd="1.234,56")
    pedido = PedidoRequest(
        producto_id="zapato", destino_pais="US", nombre_comprador="Cliente",
        direccion_exacta="Main Street 100", ciudad="Miami", estado="FL",
        zip_code="33101", pais="US", telefono="0000000000",
        email_comprador="cliente@example.com", precio_cliente_final_ars="100.000",
    )
    assert producto.peso_kg == 1.2
    assert producto.valor_usd_default == 100_000
    assert item.valor_unitario_usd == 1234.56
    assert pedido.precio_cliente_final_ars == 100_000


def test_lead_acepta_solo_referencia_del_snapshot_y_no_precios_del_browser():
    lead = LeadCotizacionRequest(
        email="cliente@example.com",
        quote_id="Q-abcdefghijklmnopqrstuvwxyz123456",
    )
    assert lead.quote_id.startswith("Q-")
    with pytest.raises(ValidationError):
        LeadCotizacionRequest(
            email="cliente@example.com",
            quote_id="Q-abcdefghijklmnopqrstuvwxyz123456",
            carriers=[{"precio_ars": 1}],
        )
    with pytest.raises(ValidationError):
        CotizarWebRequest(
            origen_pais="AR", destino_pais="US", peso_kg="5,foo",
            largo_cm=30, ancho_cm=20, alto_cm=10, valor_declarado_usd=100,
        )


def test_cotizador_web_no_inventa_valor_ni_acepta_medidas_excedidas():
    base = {
        "origen_pais": "AR", "destino_pais": "US", "peso_kg": 5,
        "largo_cm": 30, "ancho_cm": 20, "alto_cm": 10,
    }
    with pytest.raises(ValidationError):
        CotizarWebRequest(**base)
    with pytest.raises(ValidationError):
        CotizarWebRequest(**{**base, "largo_cm": 300, "ancho_cm": 20,
                             "alto_cm": 20, "valor_declarado_usd": 100})


def test_cotizacion_simple_no_inventa_valor_declarado():
    with pytest.raises(ValidationError):
        CotizacionInput(
            ruta_id="AR-US", peso_kg=5, largo_cm=30, ancho_cm=20, alto_cm=10,
        )
