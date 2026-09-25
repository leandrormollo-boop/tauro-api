"""Pantallas OCA autenticadas; CSRF y sesiones provistos por el portal."""

from urllib.parse import quote
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Request, HTTPException
from servicios.rate_limit import check_rate
from fastapi.responses import RedirectResponse
from endpoints.portal_cliente import cliente_actual, templates
from servicios import oca_portal as oca
from servicios.provincias import opciones
from servicios.solicitudes_guia import obtener_solicitud_de_cliente

router = APIRouter(prefix="/portal/oca", tags=["portal"])


def pantalla(request, cliente, **context):
    return templates.TemplateResponse(
        request=request,
        name="portal/oca_nuevo.html",
        context={
            "cliente": cliente,
            "provincias": opciones(),
            "form": {},
            "tarifa": None,
            **context,
        },
    )


@router.get("/nuevo")
def nuevo(request: Request, cliente: str = Depends(cliente_actual)):
    try:
        config, _ = oca.adapter_cliente(cliente)
        return pantalla(
            request, cliente, disponible=True, asegurada=config.insured_operation
        )
    except Exception:
        return pantalla(request, cliente, disponible=False)


@router.post("/cotizar")
async def cotizar(request: Request, cliente: str = Depends(cliente_actual)):
    if not check_rate("oca_quote:" + cliente, max_attempts=30, window_seconds=60):
        raise HTTPException(429, "Esperá un minuto antes de volver a cotizar.")
    form = dict(await request.form())
    try:
        ident = oca.cotizar(cliente, form)
        return RedirectResponse("/portal/oca/cotizacion/" + ident, 303)
    except ValueError as exc:
        return pantalla(request, cliente, disponible=True, form=form, error=str(exc))
    except Exception:
        return pantalla(
            request,
            cliente,
            disponible=True,
            form=form,
            error="No pudimos consultar OCA. Intentá nuevamente en unos minutos.",
        )


@router.get("/cotizacion/{ident}")
def cotizacion(request: Request, ident: str, cliente: str = Depends(cliente_actual)):
    try:
        row = oca.obtener(ident, cliente)
        row["expires_at"] = row["expires_at"].astimezone(
            ZoneInfo("America/Argentina/Buenos_Aires")
        )
        # Sólo precio al cliente y direcciones: costo/margen nunca llegan al HTML.
        return pantalla(
            request,
            cliente,
            disponible=True,
            tarifa={k: row[k] for k in ("id", "precio_ars", "payload", "expires_at")},
        )
    except oca.OCAPortalError:
        return RedirectResponse("/portal/oca/nuevo", 303)


@router.post("/cotizacion/{ident}/confirmar")
def confirmar(request: Request, ident: str, cliente: str = Depends(cliente_actual)):
    try:
        solicitud_id = oca.confirmar(ident, cliente)
        from servicios.auditoria import registrar_desde_request

        registrar_desde_request(
            request,
            event="portal.oca_solicitud",
            actor_type="cliente",
            actor_ref=cliente,
            metadata={"solicitud_id": solicitud_id},
        )
        return RedirectResponse(f"/portal/envios/{solicitud_id}", 303)
    except Exception:
        return RedirectResponse(
            "/portal/oca/nuevo?error="
            + quote(
                "No pudimos guardar la solicitud. Revisá la habilitación y volvé a cotizar."
            ),
            303,
        )


@router.post("/envios/{ident}/etiqueta")
def etiqueta(ident: int, cliente: str = Depends(cliente_actual)):
    sol = obtener_solicitud_de_cliente(ident, cliente)
    if not sol or sol.get("courier") != "OCA":
        return RedirectResponse("/portal/envios", 303)
    try:
        result = oca.recuperar_etiqueta(sol)
        if not result.get("ok"):
            raise ValueError()
        return RedirectResponse(f"/portal/envios/{ident}", 303)
    except Exception:
        return RedirectResponse(
            f"/portal/envios/{ident}?error="
            + quote("No pudimos recuperar el PDF. La guía ya existe; no emitas otra."),
            303,
        )


@router.get("/envios/{ident}/seguimiento")
def seguimiento(request: Request, ident: int, cliente: str = Depends(cliente_actual)):
    sol = obtener_solicitud_de_cliente(ident, cliente)
    if not sol or sol.get("courier") != "OCA":
        return RedirectResponse("/portal/envios", 303)
    try:
        events = oca.seguimiento(sol)
        error = None
    except Exception:
        events = []
        error = "OCA no pudo responder el seguimiento. Intentá nuevamente más tarde."
    return templates.TemplateResponse(
        request=request,
        name="portal/oca_seguimiento.html",
        context={"cliente": cliente, "s": sol, "events": events, "error": error},
    )
