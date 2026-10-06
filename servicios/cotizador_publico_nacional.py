"""Cotización nacional pública contra OCA, sin crear envíos.

Este borde acepta sólo los datos que requiere ``Tarifar_Envio_Corporativo``.
El pricing de la web se inyecta de forma explícita y la respuesta pública se
construye con una lista cerrada de campos para no exponer costo ni contrato.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import uuid

import requests

from servicios.carrier_adapter import (
    OperationState,
    Package,
    QuoteRequest,
    validate_quote_result,
)
from servicios.carrier_contract import Ambito
from servicios.numeros_humanos import parse_importe_humano, parse_numero_humano
from servicios.oca_adapter import (
    OCAAdapter,
    OCAConfig,
    OCAConfigurationError,
    OCAError,
)
from servicios.provincias import descomponer_codigo_postal


class CotizacionPublicaNoConfigurada(RuntimeError):
    """La web o la cuenta OCA no tienen una configuración publicable."""


class CotizacionPublicaNoDisponible(RuntimeError):
    """OCA no pudo devolver una tarifa pública utilizable."""


_MAX_PESO_KG = Decimal("1000")
_MAX_MEDIDA_CM = Decimal("300")
_MAX_VALOR_ARS = Decimal("999999999")
_DOS_DECIMALES = Decimal("0.01")


def _decimal_positivo(valor, campo: str, maximo: Decimal, *, importe=False) -> Decimal:
    try:
        numero = parse_importe_humano(valor) if importe else parse_numero_humano(valor)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{campo} debe ser un número válido.") from None
    if numero is None or not numero.is_finite() or numero <= 0:
        raise ValueError(f"{campo} debe ser mayor a cero.")
    if numero > maximo:
        raise ValueError(f"{campo} supera el máximo admitido.")
    return numero


def _cantidad(valor) -> int:
    if isinstance(valor, bool):
        raise ValueError("La cantidad de bultos debe ser un número entero.")
    numero = _decimal_positivo(valor, "La cantidad de bultos", Decimal("20"))
    if numero != numero.to_integral_value():
        raise ValueError("La cantidad de bultos debe ser un número entero.")
    return int(numero)


def _cp4(valor, campo: str) -> str:
    postal = descomponer_codigo_postal(valor)
    if not postal:
        raise ValueError(
            f"El código postal de {campo} debe tener 4 dígitos o ser un CPA completo."
        )
    return postal["cp4"]


def _configuracion_productiva() -> OCAConfig:
    try:
        config = OCAConfig.from_env()
        config.assert_ready()
    except OCAConfigurationError:
        raise CotizacionPublicaNoConfigurada(
            "El cotizador nacional no está disponible en este momento."
        ) from None
    if (
        config.environment != "production"
        or config.account == "111757/001"
        or config.username.lower() == "test@oca.com.ar"
        or config.origin_mode != "domicilio"
        or config.destination_mode != "domicilio"
        or config.reverse_logistics
    ):
        raise CotizacionPublicaNoConfigurada(
            "El cotizador nacional no está disponible en este momento."
        )
    return config


def _pricing_publico() -> dict:
    # Import tardío: este módulo no debe volver obligatoria la DB al importar
    # ``main`` ni convertir un fallo de pricing en un error de arranque.
    from servicios.precios_web_nacional import (
        PrecioWebNoDisponible,
        pricing_publico_oca,
    )

    try:
        return dict(pricing_publico_oca())
    except PrecioWebNoDisponible:
        raise CotizacionPublicaNoConfigurada(
            "El cotizador nacional no está disponible en este momento."
        ) from None


def cotizar_publico_nacional(
    *,
    origen_cp,
    destino_cp,
    cantidad_bultos,
    peso_kg,
    largo_cm,
    ancho_cm,
    alto_cm,
    valor_declarado_ars,
) -> dict:
    """Devuelve una tarifa final OCA apta para la web y ninguna acción más."""

    origen = _cp4(origen_cp, "origen")
    destino = _cp4(destino_cp, "destino")
    cantidad = _cantidad(cantidad_bultos)
    peso = _decimal_positivo(peso_kg, "El peso por bulto", _MAX_PESO_KG)
    largo = _decimal_positivo(largo_cm, "El largo", _MAX_MEDIDA_CM)
    ancho = _decimal_positivo(ancho_cm, "El ancho", _MAX_MEDIDA_CM)
    alto = _decimal_positivo(alto_cm, "El alto", _MAX_MEDIDA_CM)
    valor = _decimal_positivo(
        valor_declarado_ars,
        "El valor declarado",
        _MAX_VALOR_ARS,
        importe=True,
    )
    if valor != valor.to_integral_value():
        raise ValueError("El valor declarado debe ingresarse en pesos enteros.")

    config = _configuracion_productiva()
    pricing = _pricing_publico()
    session = requests.Session()
    try:
        adapter = OCAAdapter(
            config,
            session=session,
            pricing_loader=lambda *_: pricing,
        )
        resultados = adapter.quote(
            QuoteRequest(
                request_id=uuid.uuid4().hex,
                # Identifica el canal de pricing. No corresponde a un cliente
                # registrado y el loader público nunca consulta su matriz.
                customer_id="web-publica",
                scope=Ambito.NACIONAL,
                origin={"pais": "AR", "codigo_postal": origen},
                destination={"pais": "AR", "codigo_postal": destino},
                packages=(
                    Package(
                        quantity=cantidad,
                        weight_kg=peso,
                        length_cm=largo,
                        width_cm=ancho,
                        height_cm=alto,
                    ),
                ),
                declared_value=valor,
                declared_currency="ARS",
                origin_mode="domicilio",
                destination_mode="domicilio",
            )
        )
    except OCAConfigurationError:
        raise CotizacionPublicaNoConfigurada(
            "El cotizador nacional no está disponible en este momento."
        ) from None
    except OCAError:
        raise CotizacionPublicaNoDisponible(
            "No pudimos obtener la tarifa de OCA. Probá de nuevo en un momento."
        ) from None
    finally:
        try:
            session.close()
        except Exception:
            # Cerrar el transporte no puede convertir una tarifa ya obtenida
            # en un error ni filtrar detalles del cliente HTTP.
            pass

    result = next(
        (item for item in resultados if item.state == OperationState.COTIZADO),
        None,
    )
    if result is None:
        raise CotizacionPublicaNoDisponible(
            "OCA no devolvió una tarifa para esta ruta."
        )
    try:
        validate_quote_result(result, "oca")
    except ValueError:
        raise CotizacionPublicaNoDisponible(
            "OCA no devolvió una tarifa válida."
        ) from None
    if result.currency != "ARS" or result.carrier_currency != "ARS":
        raise CotizacionPublicaNoDisponible(
            "OCA no devolvió una tarifa válida."
        )
    if result.customer_price < result.carrier_cost:
        raise CotizacionPublicaNoDisponible(
            "La tarifa requiere revisión de TAURO."
        )

    precio = result.customer_price.quantize(_DOS_DECIMALES, rounding=ROUND_HALF_UP)
    carrier = {
        "id": "oca",
        "nombre": "OCA",
        # La web no usa logos de operadores nacionales.
        "logo": "",
        "estado": "cotizado",
        "servicio": result.service_name,
        "precio_ars": format(precio, "f"),
        "moneda": "ARS",
        # ``None`` significa que la API no informó plazo; no se completa con
        # una promesa comercial inventada.
        "dias_estimados": result.estimated_days,
        "iva_incluido": True,
        "estado_publicacion": "tarifa_publica",
    }
    return {
        "status": "success",
        "ambito": "NACIONAL",
        "moneda": "ARS",
        "origen_cp": origen,
        "destino_cp": destino,
        "cantidad_bultos": cantidad,
        "peso_kg": format(peso, "f"),
        "carriers": [carrier],
        "recomendado": "oca",
    }
