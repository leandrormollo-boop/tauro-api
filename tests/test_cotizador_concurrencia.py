"""Concurrencia y gates del cotizador internacional."""

import os
import threading
import time
import inspect
from unittest import mock

import pytest

from servicios import carriers, cotizador


ENVIO = {
    "cliente": "CLIENTE",
    "origen_pais": "AR",
    "destino_pais": "US",
    "peso_kg": 1,
    "largo_cm": 20,
    "ancho_cm": 20,
    "alto_cm": 20,
    "valor_declarado_usd": 100,
}


def _registro(carrier_id, cliente, requisito="FAKE_QUOTES"):
    return {
        "id": carrier_id,
        "nombre": carrier_id.upper(),
        "servicio": "Express",
        "logo": f"/{carrier_id}.svg",
        "requisitos": (requisito,),
        "cliente": cliente,
    }


def _acceso(ids):
    return {
        "pricing_general": {"tipo": "FIJO_ARS", "valor": 10_000},
        "pricing_por_courier": {},
        "couriers_habilitados": set(ids),
    }


def test_fedex_y_ups_pendientes_no_se_instancian_aunque_haya_credenciales():
    creados = []

    class Prohibido:
        def __init__(self):
            creados.append(True)
            raise AssertionError("una integración pendiente no debe instanciarse")

    registro = [
        _registro("fedex", Prohibido, "FEDEX_API_KEY"),
        _registro("ups", Prohibido, "UPS_CLIENT_ID"),
    ]
    with mock.patch.object(carriers, "CARRIERS", registro), mock.patch.dict(
        os.environ,
        {"FEDEX_API_KEY": "presente", "UPS_CLIENT_ID": "presente"},
        clear=True,
    ):
        crudos = carriers.costos_carriers(
            origen={"country": "AR"},
            destino={"country": "US"},
            paquete={"peso_kg": 1},
        )
        web = carriers.cotizar_carriers_web(
            origen={"country": "AR"},
            destino={"country": "US"},
            paquete={"peso_kg": 1},
            dolar=1_000,
            markup_pct=20,
        )

    assert creados == []
    assert [fila["estado"] for fila in crudos] == ["proximamente", "proximamente"]
    assert [fila["estado"] for fila in web] == ["proximamente", "proximamente"]
    assert not any(
        clave.startswith(("costo", "precio", "margen", "markup"))
        for fila in web
        for clave in fila
    )


def test_rutas_legacy_fedex_bloquean_antes_de_permisos_y_api():
    with mock.patch(
        "servicios.configuracion_couriers_cliente.configuracion_cotizacion",
    ) as configuracion:
        with pytest.raises(ValueError, match="todavía no está disponible"):
            cotizador._pricing_courier_cliente("CLIENTE", "fedex")
    configuracion.assert_not_called()
    for funcion in (
        cotizador.cotizar_opciones,
        cotizador.cotizar_bultos,
        cotizador.cotizar,
    ):
        fuente = inspect.getsource(funcion)
        assert fuente.index("_pricing_courier_cliente") < fuente.index("FedExClient")


def test_snapshots_salen_al_completar_y_el_final_conserva_orden_publico():
    barrera = threading.Barrier(2)

    class Lento:
        def get_rates(self, *_args, **_kwargs):
            barrera.wait(timeout=1)
            time.sleep(0.05)
            return {"encontrado": True, "costo": 100, "moneda": "USD"}

    class Rapido:
        def get_rates(self, *_args, **_kwargs):
            barrera.wait(timeout=1)
            return {"encontrado": True, "costo": 200, "moneda": "USD"}

    registro = [_registro("alpha", Lento), _registro("bravo", Rapido)]
    with mock.patch.object(carriers, "CARRIERS", registro), mock.patch.dict(
        os.environ, {"FAKE_QUOTES": "1"}, clear=True,
    ), mock.patch.object(cotizador, "_get_dolar_ars", return_value=1_000), mock.patch(
        "servicios.configuracion_couriers_cliente.configuracion_cotizacion",
        return_value=_acceso({"alpha", "bravo"}),
    ):
        snapshots = list(cotizador.iterar_cotizar_referencia_couriers(**ENVIO))

    assert [snapshot["completo"] for snapshot in snapshots] == [False, True]
    assert [op["carrier_id"] for op in snapshots[0]["opciones"]] == ["bravo"]
    assert [op["carrier_id"] for op in snapshots[-1]["opciones"]] == [
        "alpha",
        "bravo",
    ]
    assert snapshots[-1]["resumen"]["couriers_consultados"] == 2
    assert not any(
        palabra in repr(snapshots).lower()
        for palabra in ("costo", "margen", "markup", "_base_interna")
    )


def test_cero_habilitados_devuelve_un_unico_final_sin_tocar_apis():
    class Prohibido:
        def __init__(self):
            raise AssertionError("sin permiso no se construye el cliente")

    registro = [_registro("alpha", Prohibido)]
    with mock.patch.object(carriers, "CARRIERS", registro), mock.patch.dict(
        os.environ, {"FAKE_QUOTES": "1"}, clear=True,
    ), mock.patch.object(cotizador, "_get_dolar_ars", return_value=1_000) as dolar, mock.patch(
        "servicios.configuracion_couriers_cliente.configuracion_cotizacion",
        return_value=_acceso(set()),
    ) as configuracion:
        snapshots = list(cotizador.iterar_cotizar_referencia_couriers(**ENVIO))

    assert snapshots == [{
        "encontrado": False,
        "opciones": [],
        "no_disponibles": [],
        "resumen": {
            "ruta": "AR → US",
            "peso_usado_kg": 1.6,
            "peso_real_kg": 1.0,
            "peso_volumetrico_kg": 1.6,
            "cobra_por_volumen": True,
            "valor_declarado_usd": 100.0,
            "cantidad_bultos": 1,
            "couriers_consultados": 0,
        },
        "completo": True,
    }]
    configuracion.assert_called_once_with("CLIENTE")
    dolar.assert_called_once_with()


def test_cerrar_consumidor_no_espera_al_worker_que_sigue_en_red():
    liberar = threading.Event()
    ambos_iniciados = threading.Barrier(2)

    class Rapido:
        def get_rates(self, *_args, **_kwargs):
            ambos_iniciados.wait(timeout=1)
            return {"encontrado": True, "costo": 100, "moneda": "USD"}

    class Bloqueado:
        def get_rates(self, *_args, **_kwargs):
            ambos_iniciados.wait(timeout=1)
            liberar.wait(timeout=2)
            return {"encontrado": True, "costo": 200, "moneda": "USD"}

    registro = [_registro("alpha", Rapido), _registro("bravo", Bloqueado)]
    with mock.patch.object(carriers, "CARRIERS", registro), mock.patch.dict(
        os.environ, {"FAKE_QUOTES": "1"}, clear=True,
    ), mock.patch.object(cotizador, "_get_dolar_ars", return_value=1_000), mock.patch(
        "servicios.configuracion_couriers_cliente.configuracion_cotizacion",
        return_value=_acceso({"alpha", "bravo"}),
    ):
        generador = cotizador.iterar_cotizar_referencia_couriers(**ENVIO)
        primero = next(generador)
        inicio = time.monotonic()
        generador.close()
        demora = time.monotonic() - inicio
        liberar.set()

    assert primero["completo"] is False
    assert demora < 0.1


def test_evento_cancela_un_next_bloqueado_sin_esperar_timeout_http():
    liberar = threading.Event()
    iniciado = threading.Event()
    cancelar = threading.Event()

    class Bloqueado:
        def get_rates(self, *_args, **_kwargs):
            iniciado.set()
            liberar.wait(timeout=2)
            return {"encontrado": True, "costo": 200, "moneda": "USD"}

    registro = [_registro("alpha", Bloqueado)]
    salida = []
    with mock.patch.object(carriers, "CARRIERS", registro), mock.patch.dict(
        os.environ, {"FAKE_QUOTES": "1"}, clear=True,
    ), mock.patch.object(cotizador, "_get_dolar_ars", return_value=1_000), mock.patch(
        "servicios.configuracion_couriers_cliente.configuracion_cotizacion",
        return_value=_acceso({"alpha"}),
    ):
        generador = cotizador.iterar_cotizar_referencia_couriers(
            **ENVIO, _cancel_event=cancelar,
        )

        def consumir():
            salida.extend(generador)

        hilo = threading.Thread(target=consumir)
        hilo.start()
        assert iniciado.wait(timeout=1)
        inicio = time.monotonic()
        cancelar.set()
        hilo.join(timeout=0.4)
        demora = time.monotonic() - inicio
        liberar.set()

    assert not hilo.is_alive()
    assert demora < 0.3
    assert salida == []


def test_evento_ya_cancelado_no_instancia_ni_envia_trabajos():
    creados = []

    class Prohibido:
        def __init__(self):
            creados.append(True)

    cancelar = threading.Event()
    cancelar.set()
    registro = [_registro("alpha", Prohibido)]
    with mock.patch.object(carriers, "CARRIERS", registro), mock.patch.dict(
        os.environ, {"FAKE_QUOTES": "1"}, clear=True,
    ), mock.patch.object(carriers._QUOTE_EXECUTOR, "submit") as submit:
        resultados = list(carriers.iterar_cotizar_carriers_cliente(
            origen={"country": "AR"},
            destino={"country": "US"},
            paquete={"peso_kg": 1},
            dolar=1_000,
            pricing_cliente={"tipo": "PCT", "valor": 10},
            couriers_habilitados={"alpha"},
            _cancel_event=cancelar,
        ))

    assert resultados == []
    assert creados == []
    submit.assert_not_called()


def test_logs_de_carrier_no_copian_excepcion_payload_ni_importes():
    class Falla:
        def get_rates(self, *_args, **_kwargs):
            raise RuntimeError("SECRET_ACCOUNT costo=999999 payload=privado")

    registro = [_registro("alpha", Falla)]
    with mock.patch.object(carriers, "CARRIERS", registro), mock.patch.dict(
        os.environ, {"FAKE_QUOTES": "1"}, clear=True,
    ), mock.patch.object(carriers._LOGGER, "info") as log:
        resultado = carriers.costos_carriers(
            origen={"country": "AR", "postal_code": "1000"},
            destino={"country": "US", "postal_code": "90210"},
            paquete={"peso_kg": 1},
        )

    assert resultado[0]["estado"] == "sin_tarifa"
    texto_log = repr(log.call_args_list).lower()
    assert "secret" not in texto_log
    assert "payload" not in texto_log
    assert "999999" not in texto_log
    assert "90210" not in texto_log
    assert "carrier_quote" in texto_log
