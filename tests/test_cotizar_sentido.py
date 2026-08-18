"""
El cotizador público tiene que cotizar los DOS sentidos (Leandro, 01/08/2026:
"el cotizador debe permitir cotizar importaciones y exportaciones").

ACTUALIZADO 05/08: el campo `sentido` de la RESPUESTA se reemplazó por
`origen_pais` / `destino_pais`, porque el cotizador pasó a aceptar cualquier
par de países y "sentido" ya no alcanza para describir un China → India. El
parámetro de ENTRADA sigue existiendo por retrocompatibilidad y estos tests
lo siguen cubriendo: lo que cambió es cómo se lee el resultado.

Lo que se verifica acá es que el sentido no sea cosmético: en una importación
la caja sale del exterior y entra a Argentina, y de eso depende qué cuenta de
DHL se factura. Si origen y destino no se dan vuelta de verdad, DHL cotiza con
la cuenta de exportación una importación — y eso NO da error, devuelve el
precio del otro sentido.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from main import CotizarWebRequest  # noqa: E402

CUERPO = {
    "destino_pais": "US",
    "peso_kg": 1.4,
    "largo_cm": 33,
    "ancho_cm": 33,
    "alto_cm": 22,
    "valor_declarado_usd": 300,
}


def _cotizar(sentido=None):
    """Llama al endpoint y devuelve (origen, destino) que recibió cotizar_carriers."""
    visto = {}

    def fake_cotizar(origen, destino, paquete, dolar, markup_pct):
        visto["origen"] = origen
        visto["destino"] = destino
        return [{
            "id": "dhl", "nombre": "DHL Express", "logo": "",
            "estado": "cotizado", "servicio": "Express Worldwide",
            "dias_estimados": "2", "precio_ars": 100, "precio_usd": 1.0,
        }]

    cuerpo = dict(CUERPO)
    if sentido:
        cuerpo["sentido"] = sentido

    # Se llama la función del endpoint directo: prueba la misma lógica sin
    # levantar el servidor ni sumar httpx como dependencia sólo para un test.
    # `request` sólo se usa para el rate limit (IP del visitante): alcanza con
    # un doble mínimo, y así el test no depende del middleware ni de httpx.
    class _Req:
        headers = {}
        client = type("C", (), {"host": "127.0.0.1"})()

    with mock.patch.object(main, "cotizar_carriers", fake_cotizar), \
         mock.patch("servicios.cotizador.dolar_ars", return_value=1450.0), \
         mock.patch("servicios.rate_limit.check_rate", return_value=True), \
         mock.patch("servicios.leads.guardar_cotizacion", return_value={
             "quote_id": "Q-abcdefghijklmnopqrstuvwxyz123456",
             "referencia": "TW-20260818-ABC123",
             "emitida_en": "2026-08-18T12:00:00-03:00",
             "vigente_hasta": "2026-08-19T12:00:00-03:00",
         }):
        data = main.cotizar_web(CotizarWebRequest(**cuerpo), _Req())

    return visto, data


def test_exportacion_sale_de_argentina():
    visto, data = _cotizar("exportacion")
    assert visto["origen"]["country"] == "AR"
    assert visto["destino"]["country"] == "US"
    assert data["origen_pais"] == "AR"


def test_importacion_da_vuelta_origen_y_destino():
    visto, data = _cotizar("importacion")
    assert visto["origen"]["country"] == "US", "la caja tiene que salir del exterior"
    assert visto["destino"]["country"] == "AR", "y entrar a Argentina"
    assert data["origen_pais"] == "US"
    assert data["destino_pais"] == "AR"


def test_sin_sentido_sigue_siendo_exportacion():
    """Retrocompatible: quien ya llamaba este endpoint no se rompe."""
    visto, data = _cotizar(None)
    assert visto["origen"]["country"] == "AR"
    assert data["origen_pais"] == "AR"


def test_la_importacion_le_pega_a_dhl_con_la_cuenta_de_impo():
    """
    El test que cierra el círculo: que el sentido llegue hasta la cuenta.
    Sin esto, dar vuelta origen/destino sería decorativo.
    """
    from core.dhl_client import DHLClient

    visto, _ = _cotizar("importacion")
    with mock.patch.dict(os.environ, {
        "DHL_API_KEY": "k", "DHL_API_SECRET": "s",
        "DHL_ACCOUNT_NUMBER_EXPO": "741622792",
        "DHL_ACCOUNT_NUMBER_IMPO": "730089966",
    }, clear=True):
        cuenta, error = DHLClient()._cuenta_para(visto["origen"], visto["destino"])

    assert error is None
    assert cuenta == "730089966", "una importación tiene que facturar contra la cuenta de impo"
