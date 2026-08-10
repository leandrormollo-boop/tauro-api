"""Integracion del handler y el parseo localizado del cotizador del portal.

El cotizador real queda reemplazado por un fake: la prueba cubre FastAPI,
la firma del formulario y el borde numerico, pero nunca llama couriers ni DB.
"""

import inspect
from types import SimpleNamespace

from endpoints import portal_cliente as portal
from servicios.api_b2b import _piezas_del_catalogo


def test_cotizar_acepta_coma_y_punto_sin_422_y_envia_el_mismo_numero(monkeypatch):
    recibidos = []

    def cotizador_falso(**datos):
        recibidos.append(datos)
        return {
            "encontrado": True,
            "opciones": [],
            "no_disponibles": [],
            "resumen": {"ok": True},
        }

    def respuesta_falsa(*, request, name, context, status_code=200, **_kwargs):
        return SimpleNamespace(
            status_code=status_code,
            template=name,
            context=context,
        )

    monkeypatch.setattr(portal, "cotizar_referencia_couriers", cotizador_falso)
    monkeypatch.setattr(
        portal,
        "_paises_con_nacional",
        lambda: [("AR", "Argentina"), ("US", "Estados Unidos")],
    )
    monkeypatch.setattr(portal.templates, "TemplateResponse", respuesta_falsa)

    firma = inspect.signature(portal.cotizar_post)
    for campo in ("peso_kg", "largo_cm", "ancho_cm", "alto_cm"):
        assert firma.parameters[campo].annotation in (str, "str")

    for numero in ("5,5", "5.5"):
        respuesta = portal.cotizar_post(
            request=SimpleNamespace(),
            origen_pais="AR",
            destino_pais="US",
            peso_kg=numero,
            largo_cm=numero,
            ancho_cm=numero,
            alto_cm=numero,
            cliente="CLIENTE_TEST",
        )
        assert respuesta.status_code == 200
        assert respuesta.status_code != 422
        assert respuesta.context["error"] is None

    assert len(recibidos) == 2
    for datos in recibidos:
        assert datos["cliente"] == "CLIENTE_TEST"
        assert datos["peso_kg"] == 5.5
        assert datos["largo_cm"] == 5.5
        assert datos["ancho_cm"] == 5.5
        assert datos["alto_cm"] == 5.5


def test_cotizar_invalido_no_llama_al_courier(monkeypatch):
    llamadas = []
    monkeypatch.setattr(
        portal, "cotizar_referencia_couriers", lambda **datos: llamadas.append(datos)
    )
    monkeypatch.setattr(
        portal, "_paises_con_nacional", lambda: [("AR", "Argentina"), ("US", "Estados Unidos")]
    )
    monkeypatch.setattr(
        portal.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context
        ),
    )

    respuesta = portal.cotizar_post(
        request=SimpleNamespace(), origen_pais="AR", destino_pais="US",
        peso_kg="5,foo", largo_cm="30", ancho_cm="20", alto_cm="10",
        cliente="CLIENTE_TEST",
    )

    assert respuesta.status_code == 200
    assert "ingresá un número válido" in respuesta.context["error"]
    assert llamadas == []


def test_preview_multi_bulto_acepta_coma_y_rechaza_override_invalido():
    base = {
        "producto": "",
        "cantidad": "1",
        "unidades_aduana": "1",
        "peso_kg": "5,5",
        "largo_cm": "30,5",
        "ancho_cm": "20",
        "alto_cm": "10",
        "valor_unitario_usd": "100,50",
        "descripcion_en": "MERCHANDISE",
    }
    _piezas, detalle, error = _piezas_del_catalogo("CLIENTE_TEST", [base])
    assert error is None
    assert detalle[0]["peso_kg"] == 5.5
    assert detalle[0]["largo_cm"] == 30.5
    assert detalle[0]["valor_unitario_usd"] == 100.5

    manipulado = {**base, "peso_kg": "5,foo"}
    _piezas, _detalle, error = _piezas_del_catalogo("CLIENTE_TEST", [manipulado])
    assert "medidas válidas" in error
