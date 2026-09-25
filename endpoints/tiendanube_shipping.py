"""Callbacks públicos de la Shipping Carrier App de Tiendanube."""
from __future__ import annotations

import asyncio
import json
import re

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
from servicios.tiendanube_label_worker import load_label_document
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
_LABEL_CANCEL_ENDPOINT_TIMEOUT_SECONDS = 4.5
_MAX_LABEL_DOCUMENT_ID_LENGTH = 128
_LABEL_DOCUMENT_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_DOCUMENT_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "sandbox",
}


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


def _label_document_not_found() -> JSONResponse:
    return JSONResponse(
        {"error": "documento_no_encontrado"},
        status_code=404,
        headers=_LABEL_DOCUMENT_HEADERS,
    )


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
        # La cancelación es sincrónica porque debe confirmar el estado del
        # carrier. Se ejecuta fuera del event loop y con un techo menor al SLA
        # de 5 s; el servicio interno usa un presupuesto aún menor y deja un
        # claim durable antes de cualquier escritura externa.
        result = await asyncio.wait_for(
            asyncio.to_thread(recibir_cancel, payload, callback_token),
            timeout=_LABEL_CANCEL_ENDPOINT_TIMEOUT_SECONDS,
        )
        status = int(getattr(result, "http_status", 0) or 0)
        if status == 204:
            return Response(status_code=204)
        body = getattr(result, "response_body", None)
        if status == 207 and isinstance(body, dict):
            return JSONResponse(body, status_code=207)
        if 400 <= status < 500 and isinstance(body, dict):
            return JSONResponse(body, status_code=status)
        raise LabelsUnavailableError(
            "La cancelación no produjo una confirmación interpretable."
        )
    except asyncio.TimeoutError:
        return JSONResponse({"error": "operacion_no_disponible"}, status_code=503)
    except _PayloadTooLarge:
        return JSONResponse({"error": "payload_demasiado_grande"}, status_code=413)
    except Exception as exc:
        return _labels_error(exc)


@router.get("/labels/documents/{store_id}/{label_id}")
async def download_label_document(
    store_id: str,
    label_id: str,
    token: str = "",
):
    """Entrega un PDF durable sólo mientras su token siga activo."""
    if (
        not 0 < len(store_id) <= _MAX_LABEL_DOCUMENT_ID_LENGTH
        or not 0 < len(label_id) <= _MAX_LABEL_DOCUMENT_ID_LENGTH
        or not _LABEL_DOCUMENT_TOKEN_RE.fullmatch(token)
    ):
        return _label_document_not_found()

    try:
        document = load_label_document(store_id, label_id, token)
    except Exception as exc:
        print(f"[tiendanube-label-documents] error inesperado: {type(exc).__name__}")
        return JSONResponse(
            {"error": "servicio_no_disponible"},
            status_code=503,
            headers=_LABEL_DOCUMENT_HEADERS,
        )
    if document is None:
        return _label_document_not_found()

    return Response(
        document,
        media_type="application/pdf",
        headers={
            **_LABEL_DOCUMENT_HEADERS,
            "Content-Disposition": 'attachment; filename="tauro-label.pdf"',
        },
    )
