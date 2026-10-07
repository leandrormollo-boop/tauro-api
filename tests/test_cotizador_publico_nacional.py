import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from servicios.carrier_adapter import OperationState, QuoteResult
from servicios.oca_adapter import OCAUnavailableError
from servicios import cotizador_publico_nacional as publico


def datos(**cambios):
    base = {
        "origen_cp": "C1425ABC",
        "destino_cp": "2000",
        "cantidad_bultos": "2",
        "peso_kg": "1,5",
        "largo_cm": "30",
        "ancho_cm": "20",
        "alto_cm": "10",
        "valor_declarado_ars": "100.000",
    }
    base.update(cambios)
    return base


class AdapterCotizado:
    request = None
    pricing = None

    def __init__(self, config, *, session, pricing_loader):
        self.config = config
        self.session = session
        type(self).pricing = pricing_loader("web-publica", 999)

    def quote(self, request):
        type(self).request = request
        return (
            QuoteResult(
                state=OperationState.COTIZADO,
                carrier_id="oca",
                quote_id="oca-publica-1",
                service_code="1",
                service_name="Puerta a Puerta",
                carrier_cost=Decimal("12100.00"),
                carrier_currency="ARS",
                customer_price=Decimal("14520.00"),
                currency="ARS",
                estimated_days=3,
                origin_mode="domicilio",
                destination_mode="domicilio",
            ),
        )


@pytest.fixture
def preparado(monkeypatch):
    monkeypatch.setattr(publico, "_configuracion_productiva", lambda: object())
    monkeypatch.setattr(
        publico,
        "_pricing_publico",
        lambda: {
            "tipo": "PCT",
            "valor": Decimal("20"),
            "oca_tarifa_neta_iva_21": True,
        },
    )
    monkeypatch.setattr(publico, "OCAAdapter", AdapterCotizado)


def test_cotiza_por_cp_con_decimales_exactos_y_pricing_publico(preparado):
    resultado = publico.cotizar_publico_nacional(**datos())

    request = AdapterCotizado.request
    package = request.packages[0]
    assert request.customer_id == "web-publica"
    assert request.origin == {"pais": "AR", "codigo_postal": "1425"}
    assert request.destination == {"pais": "AR", "codigo_postal": "2000"}
    assert request.origin_mode == request.destination_mode == "domicilio"
    assert package.quantity == 2
    assert package.weight_kg == Decimal("1.5")
    assert package.length_cm * package.width_cm * package.height_cm == Decimal("6000")
    assert request.declared_value == Decimal("100000")
    assert AdapterCotizado.pricing == {
        "tipo": "PCT",
        "valor": Decimal("20"),
        "oca_tarifa_neta_iva_21": True,
    }

    assert resultado == {
        "status": "success",
        "ambito": "NACIONAL",
        "moneda": "ARS",
        "origen_cp": "1425",
        "destino_cp": "2000",
        "cantidad_bultos": 2,
        "peso_kg": "1.5",
        "carriers": [{
            "id": "oca",
            "nombre": "OCA",
            "logo": "",
            "estado": "cotizado",
            "servicio": "Puerta a Puerta",
            "precio_ars": "14520.00",
            "moneda": "ARS",
            "dias_estimados": 3,
            "iva_incluido": True,
            "estado_publicacion": "tarifa_publica",
        }],
        "recomendado": "oca",
    }


def test_cierra_la_sesion_http_despues_de_cotizar(preparado, monkeypatch):
    class Session:
        cerrada = False

        def close(self):
            self.cerrada = True

    session = Session()
    monkeypatch.setattr(publico.requests, "Session", lambda: session)
    publico.cotizar_publico_nacional(**datos())
    assert session.cerrada is True
    assert AdapterCotizado.request is not None


def test_respuesta_publica_no_expone_costo_markup_contrato_ni_datos_cliente(preparado):
    resultado = publico.cotizar_publico_nacional(**datos())
    serializado = json.dumps(resultado).lower()
    for prohibido in (
        "carrier_cost", "costo", "markup", "operativa", "cuit",
        "cuenta", "password", "customer_id", "email", "telefono",
    ):
        assert prohibido not in serializado


def test_plazo_ausente_permanece_ausente_sin_inventar(preparado, monkeypatch):
    original = AdapterCotizado.quote

    def sin_plazo(self, request):
        result = original(self, request)[0]
        return (QuoteResult(**{**result.__dict__, "estimated_days": None}),)

    monkeypatch.setattr(AdapterCotizado, "quote", sin_plazo)
    resultado = publico.cotizar_publico_nacional(**datos())
    assert resultado["carriers"][0]["dias_estimados"] is None


@pytest.mark.parametrize(
    "cambios",
    [
        {"origen_cp": "C1425"},
        {"cantidad_bultos": "1,5"},
        {"peso_kg": "0"},
        {"largo_cm": "301"},
        {"valor_declarado_ars": "100,50"},
    ],
)
def test_entrada_invalida_falla_antes_de_llamar_oca(preparado, monkeypatch, cambios):
    llamado = False

    class NoDebeCrearse:
        def __init__(self, *args, **kwargs):
            nonlocal llamado
            llamado = True

    monkeypatch.setattr(publico, "OCAAdapter", NoDebeCrearse)
    with pytest.raises(ValueError):
        publico.cotizar_publico_nacional(**datos(**cambios))
    assert llamado is False


def test_sin_tarifa_y_falla_de_oca_son_errores_publicos_seguros(preparado, monkeypatch):
    class SinTarifa(AdapterCotizado):
        def quote(self, request):
            return (QuoteResult(
                state=OperationState.SIN_TARIFA,
                carrier_id="oca",
                safe_message="detalle interno",
            ),)

    monkeypatch.setattr(publico, "OCAAdapter", SinTarifa)
    with pytest.raises(publico.CotizacionPublicaNoDisponible) as error:
        publico.cotizar_publico_nacional(**datos())
    assert "detalle interno" not in str(error.value)

    class Caida(AdapterCotizado):
        def quote(self, request):
            raise OCAUnavailableError("host y contrato privados")

    monkeypatch.setattr(publico, "OCAAdapter", Caida)
    with pytest.raises(publico.CotizacionPublicaNoDisponible) as error:
        publico.cotizar_publico_nacional(**datos())
    assert "host" not in str(error.value)
    assert "contrato" not in str(error.value)


def test_no_publica_precio_final_menor_al_costo(preparado, monkeypatch):
    class MargenNegativo(AdapterCotizado):
        def quote(self, request):
            return (QuoteResult(
                state=OperationState.COTIZADO,
                carrier_id="oca",
                quote_id="oca-publica-invalida",
                service_code="1",
                service_name="Puerta a Puerta",
                carrier_cost=Decimal("12100.00"),
                carrier_currency="ARS",
                customer_price=Decimal("12099.99"),
                currency="ARS",
            ),)

    monkeypatch.setattr(publico, "OCAAdapter", MargenNegativo)
    with pytest.raises(publico.CotizacionPublicaNoDisponible, match="revisión"):
        publico.cotizar_publico_nacional(**datos())


def test_configuracion_incompleta_falla_antes_de_oca(monkeypatch):
    monkeypatch.setattr(
        publico,
        "_configuracion_productiva",
        lambda: (_ for _ in ()).throw(
            publico.CotizacionPublicaNoConfigurada("no disponible")
        ),
    )
    monkeypatch.setattr(
        publico,
        "OCAAdapter",
        lambda *args, **kwargs: pytest.fail("no debe crear el adapter"),
    )
    with pytest.raises(publico.CotizacionPublicaNoConfigurada) as error:
        publico.cotizar_publico_nacional(**datos())
    assert type(error.value) is publico.CotizacionPublicaNoConfigurada
    assert str(error.value) == "no disponible"


@pytest.mark.parametrize(
    "motivo",
    [
        "CANAL_DESACTIVADO",
        "PRECIO_NO_CONFIGURADO",
        "CONFIGURACION_INVALIDA",
    ],
)
def test_pricing_no_publicable_informa_canal_sin_llamar_adapter(monkeypatch, motivo):
    from servicios import precios_web_nacional as precios

    monkeypatch.setattr(publico, "_configuracion_productiva", lambda: object())

    def pricing_no_disponible():
        raise precios.PrecioWebNoDisponible(
            "detalle de administración",
            motivo=getattr(precios.MotivoPrecioWebNoDisponible, motivo),
        )

    monkeypatch.setattr(precios, "pricing_publico_oca", pricing_no_disponible)
    monkeypatch.setattr(
        publico,
        "OCAAdapter",
        lambda *args, **kwargs: pytest.fail("no debe crear el adapter"),
    )

    with pytest.raises(publico.CotizacionPublicaDesactivada) as error:
        publico.cotizar_publico_nacional(**datos())

    assert str(error.value) == "La cotización nacional todavía no está habilitada."
    assert "administración" not in str(error.value)


def test_falla_lectura_pricing_es_tecnica_y_no_llama_adapter(monkeypatch):
    from servicios import precios_web_nacional as precios

    monkeypatch.setattr(publico, "_configuracion_productiva", lambda: object())

    def pricing_no_disponible():
        raise precios.PrecioWebNoDisponible(
            "detalle privado",
            motivo=precios.MotivoPrecioWebNoDisponible.LECTURA_CONFIG,
        )

    monkeypatch.setattr(precios, "pricing_publico_oca", pricing_no_disponible)
    monkeypatch.setattr(
        publico,
        "OCAAdapter",
        lambda *args, **kwargs: pytest.fail("no debe crear el adapter"),
    )

    with pytest.raises(publico.CotizacionPublicaNoConfigurada) as error:
        publico.cotizar_publico_nacional(**datos())

    assert type(error.value) is publico.CotizacionPublicaNoConfigurada
    assert str(error.value) == "El cotizador nacional no está disponible en este momento."


@pytest.mark.parametrize(
    "cambios",
    [
        {"environment": "qa"},
        {"account": "111757/001"},
        {"username": "test@oca.com.ar"},
        {"origin_mode": "sucursal"},
        {"destination_mode": "sucursal"},
        {"reverse_logistics": True},
    ],
)
def test_configuracion_publica_rechaza_qa_muestras_y_modalidad_incorrecta(
    monkeypatch, cambios
):
    config = {
        "environment": "production",
        "account": "123456/001",
        "username": "produccion@example.invalid",
        "origin_mode": "domicilio",
        "destination_mode": "domicilio",
        "reverse_logistics": False,
        "assert_ready": lambda: None,
    }
    config.update(cambios)
    monkeypatch.setattr(
        publico.OCAConfig,
        "from_env",
        lambda: SimpleNamespace(**config),
    )
    with pytest.raises(publico.CotizacionPublicaNoConfigurada) as error:
        publico._configuracion_productiva()
    assert type(error.value) is publico.CotizacionPublicaNoConfigurada
    assert str(error.value) == "El cotizador nacional no está disponible en este momento."


def test_modelo_publico_rechaza_campos_personales_y_no_trunca_cantidad():
    import main
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        main.CotizarWebNacionalRequest(**{
            **datos(),
            "email": "persona@example.invalid",
        })
    body = main.CotizarWebNacionalRequest(**datos(cantidad_bultos="1,5"))
    assert body.cantidad_bultos == Decimal("1.5")


def test_endpoint_mapea_configuracion_y_disponibilidad_sin_filtrar(monkeypatch):
    import main
    from servicios import rate_limit

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    body = main.CotizarWebNacionalRequest(**datos())
    monkeypatch.setattr(rate_limit, "check_auth_rate", lambda *args, **kwargs: True)

    monkeypatch.setattr(
        publico,
        "cotizar_publico_nacional",
        lambda **kwargs: (_ for _ in ()).throw(
            publico.CotizacionPublicaDesactivada(
                "La cotización nacional todavía no está habilitada."
            )
        ),
    )
    respuesta = main.cotizar_web_nacional(body, request)
    assert respuesta.status_code == 503
    assert json.loads(respuesta.body) == {
        "status": "error",
        "code": "oca_public_quote_disabled",
        "detail": "La cotización nacional todavía no está habilitada.",
    }
    assert respuesta.headers["cache-control"] == "no-store"

    monkeypatch.setattr(
        publico,
        "cotizar_publico_nacional",
        lambda **kwargs: (_ for _ in ()).throw(
            publico.CotizacionPublicaNoConfigurada("temporal")
        ),
    )
    respuesta = main.cotizar_web_nacional(body, request)
    assert respuesta.status_code == 503
    assert json.loads(respuesta.body) == {
        "status": "error",
        "code": "oca_public_quote_unavailable",
        "detail": "temporal",
    }

    monkeypatch.setattr(
        publico,
        "cotizar_publico_nacional",
        lambda **kwargs: (_ for _ in ()).throw(
            publico.CotizacionPublicaNoDisponible("OCA no respondió")
        ),
    )
    respuesta = main.cotizar_web_nacional(body, request)
    assert respuesta.status_code == 502
    assert json.loads(respuesta.body) == {
        "status": "error",
        "code": "oca_quote_unavailable",
        "detail": "OCA no respondió",
    }


def test_endpoint_aplica_limite_durable_por_ip_y_global_antes_de_oca(monkeypatch):
    import main
    from servicios import rate_limit

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.4"))
    body = main.CotizarWebNacionalRequest(**datos())
    llamadas = []
    oca_llamada = False

    def limite(clave, *, max_attempts, window_seconds):
        llamadas.append((clave, max_attempts, window_seconds))
        return True

    def cotizar(**kwargs):
        nonlocal oca_llamada
        oca_llamada = True
        return {"status": "success"}

    monkeypatch.setattr(rate_limit, "check_auth_rate", limite)
    monkeypatch.setattr(publico, "cotizar_publico_nacional", cotizar)
    respuesta = main.cotizar_web_nacional(body, request)

    assert respuesta.status_code == 200
    assert llamadas == [
        ("cotweb-nacional:ip:203.0.113.4", 30, 300),
        ("cotweb-nacional:global", 120, 300),
    ]
    assert oca_llamada is True


def test_limite_ip_no_consume_global_y_db_caida_falla_antes_de_oca(monkeypatch):
    import main
    from servicios import rate_limit

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.5"))
    body = main.CotizarWebNacionalRequest(**datos())
    llamadas = []

    def bloqueado(clave, **kwargs):
        llamadas.append(clave)
        return False

    monkeypatch.setattr(rate_limit, "check_auth_rate", bloqueado)
    monkeypatch.setattr(
        publico,
        "cotizar_publico_nacional",
        lambda **kwargs: pytest.fail("no debe consultar OCA"),
    )
    respuesta = main.cotizar_web_nacional(body, request)
    assert respuesta.status_code == 429
    assert respuesta.headers["retry-after"] == "300"
    assert llamadas == ["cotweb-nacional:ip:203.0.113.5"]

    monkeypatch.setattr(
        rate_limit,
        "check_auth_rate",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("DB privada")),
    )
    respuesta = main.cotizar_web_nacional(body, request)
    assert respuesta.status_code == 503
    assert b"DB privada" not in respuesta.body


def test_limite_global_bloquea_aunque_la_ip_todavia_tenga_cupo(monkeypatch):
    import main
    from servicios import rate_limit

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="198.51.100.9"))
    body = main.CotizarWebNacionalRequest(**datos())
    respuestas = iter((True, False))
    monkeypatch.setattr(
        rate_limit,
        "check_auth_rate",
        lambda *args, **kwargs: next(respuestas),
    )
    monkeypatch.setattr(
        publico,
        "cotizar_publico_nacional",
        lambda **kwargs: pytest.fail("no debe consultar OCA"),
    )
    respuesta = main.cotizar_web_nacional(body, request)
    assert respuesta.status_code == 429


def test_lookup_publico_fija_argentina_y_tiene_rate_limit(monkeypatch):
    import main
    from servicios import rate_limit, ubicaciones_cotizador

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    llamadas = []
    monkeypatch.setattr(rate_limit, "check_rate", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        ubicaciones_cotizador,
        "buscar_ubicaciones",
        lambda *args: llamadas.append(args) or {"suggestions": [], "automatic": None},
    )

    assert main.ubicaciones_web_nacional(request, "Rosario", "city", "S") == {
        "suggestions": [],
        "automatic": None,
    }
    assert llamadas == [("AR", "Rosario", "city", "S")]
