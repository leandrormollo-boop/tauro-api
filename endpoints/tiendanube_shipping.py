"""Callbacks públicos de la Shipping Carrier App de Tiendanube."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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
