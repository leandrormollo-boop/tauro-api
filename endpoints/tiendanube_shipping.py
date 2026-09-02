"""Callbacks públicos de la Shipping Carrier App de Tiendanube."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from servicios.tiendanube_labels import (
    LabelsAuthenticationError,
    LabelsBlockedError,
    LabelsConflictError,
    LabelsContractError,
    LabelsUnavailableError,
    recibir_cancel,
    recibir_generate,
)
from servicios.tiendanube_shipping import (
    ShippingAuthenticationError,
    ShippingContractError,
    ShippingUnavailableError,
    cotizar_callback,
)


router = APIRouter(
    prefix="/integraciones/tiendanube/shipping",
    tags=["tiendanube-shipping"],
)

_MAX_LABEL_CALLBACK_BYTES = 2 * 1024 * 1024


class _PayloadTooLarge(ValueError):
    pass


async def _bounded_json(request: Request):
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError:
            raise LabelsContractError("Content-Length inválido.") from None
        if content_length < 0:
            raise LabelsContractError("Content-Length inválido.")
        if content_length > _MAX_LABEL_CALLBACK_BYTES:
            raise _PayloadTooLarge
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_LABEL_CALLBACK_BYTES:
            raise _PayloadTooLarge
        body.extend(chunk)
    try:
        return json.loads(bytes(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LabelsContractError("Payload JSON inválido.") from None


def _labels_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, LabelsAuthenticationError):
        return JSONResponse({"error": "no_autorizado"}, status_code=401)
    if isinstance(exc, LabelsConflictError):
        return JSONResponse({"error": "label_en_conflicto"}, status_code=409)
    if isinstance(exc, LabelsContractError):
        return JSONResponse({"error": "payload_invalido"}, status_code=422)
    if isinstance(exc, (LabelsBlockedError, LabelsUnavailableError)):
        return JSONResponse({"error": "operacion_no_disponible"}, status_code=503)
    print(f"[tiendanube-labels] error inesperado: {type(exc).__name__}")
    return JSONResponse({"error": "servicio_no_disponible"}, status_code=503)


@router.post("/rates/{callback_token}")
async def rates(callback_token: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "payload_invalido"}, status_code=422)
    try:
        return cotizar_callback(payload, callback_token)
    except ShippingAuthenticationError:
        return JSONResponse({"error": "no_autorizado"}, status_code=401)
    except ShippingContractError as exc:
        return JSONResponse({"error": "carrito_no_cotizable", "detail": str(exc)}, status_code=422)
    except ShippingUnavailableError:
        return JSONResponse({"error": "tarifa_no_disponible"}, status_code=503)
    except Exception as exc:
        print(f"[tiendanube-shipping] error inesperado: {type(exc).__name__}")
        return JSONResponse({"error": "servicio_no_disponible"}, status_code=503)


@router.post("/labels/{callback_token}/generate")
async def generate_labels(callback_token: str, request: Request):
    try:
        payload = await _bounded_json(request)
        recibir_generate(payload, callback_token)
        return Response(status_code=202)
    except _PayloadTooLarge:
        return JSONResponse({"error": "payload_demasiado_grande"}, status_code=413)
    except Exception as exc:
        return _labels_error(exc)


@router.post("/labels/{callback_token}/cancel")
async def cancel_labels(callback_token: str, request: Request):
    try:
        payload = await _bounded_json(request)
        recibir_cancel(payload, callback_token)
        # Aun si una futura implementación omitiera el error del servicio,
        # este borde nunca aprueba cancelaciones sin confirmación explícita.
        return JSONResponse({"error": "operacion_no_disponible"}, status_code=503)
    except _PayloadTooLarge:
        return JSONResponse({"error": "payload_demasiado_grande"}, status_code=413)
    except Exception as exc:
        return _labels_error(exc)
