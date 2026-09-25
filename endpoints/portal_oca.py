"""Pantallas OCA autenticadas; CSRF y sesiones provistos por el portal."""

from urllib.parse import quote
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Request, HTTPException
from servicios.rate_limit import check_rate
from servicios.direcciones import listar_direcciones
from servicios.agenda_nacional import proyectar
from fastapi.responses import RedirectResponse
from endpoints.portal_cliente import cliente_actual, templates
from servicios import oca_portal as oca
from servicios.provincias import opciones, normalizar_provincia
from servicios.solicitudes_guia import obtener_solicitud_de_cliente

router = APIRouter(prefix="/portal/oca", tags=["portal"])


def pantalla(request, cliente, **context):
    agenda = []
    if context.get("disponible") and not context.get("tarifa"):
        agenda = [item for row in listar_direcciones(cliente) if (item := proyectar(row))]
    return templates.TemplateResponse(
        request=request,
        name="portal/oca_nuevo.html",
        context={
            "cliente": cliente,
            "provincias": opciones(),
            "agenda_nacional": agenda,
            "form": {},
            "tarifa": None,
            **context,
        },
    )


@router.get("/nuevo")
def nuevo(request: Request, cliente: str = Depends(cliente_actual)):
    try:
        config, _ = oca.adapter_cliente(cliente)
        form = {key: request.query_params[key][:100] for key in (
            "origen_provincia", "origen_localidad", "origen_cp", "destino_provincia",
            "destino_localidad", "destino_cp", "cantidad_bultos", "peso_kg",
            "largo_cm", "ancho_cm", "alto_cm", "valor_declarado_ars"
        ) if key in request.query_params}
        error = None
        from servicios.direcciones import obtener_direccion
        for param, prefix, tipo in [("remitente_id", "origen", "REMITENTE"),
                                    ("destinatario_id", "destino", "DESTINATARIO")]:
            ident = request.query_params.get(param)
            if not ident:
                continue
            row = obtener_direccion(cliente, int(ident), tipo) if ident.isdigit() else None
            contact = proyectar(row) if row else None
            if not contact:
                error = "Ese contacto nacional no está disponible en tu cuenta."
                continue
            form.update({prefix + "_" + key: value for key, value in contact["fields"].items()})
            form[prefix + "_agenda_id"] = contact["id"]
        return pantalla(request, cliente, disponible=True, asegurada=config.insured_operation,
                        form=form, error=error)
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


@router.get("/cotizacion/{ident}/editar")
def editar(request: Request, ident: str, cliente: str = Depends(cliente_actual)):
    """Recover this client's exact quote inputs; editing always requires a new rate."""
    try:
        row = oca.obtener(ident, cliente)
        config, _ = oca.adapter_cliente(cliente)
    except oca.OCAPortalError:
        return RedirectResponse("/portal/oca/nuevo", 303)
    payload = row["payload"]
    form = {}
    for side, prefix in [("origin", "origen"), ("destination", "destino")]:
        address = payload[side]
        for key, source in [("nombre", "contacto"), ("calle", "calle"), ("numero", "nro"),
                            ("piso", "piso"), ("depto", "depto"), ("localidad", "localidad"),
                            ("cp", "cp"), ("email", "email"), ("telefono", "telefono")]:
            form[prefix + "_" + key] = address.get(source, "")
        form[prefix + "_provincia"] = normalizar_provincia(address["provincia"])
    form["destino_nombre"] = payload["recipient"]["first_name"]
    form["destino_apellido"] = payload["recipient"]["last_name"]
    box = payload["packages"][0]
    for field, source in [("cantidad_bultos", "quantity"), ("peso_kg", "weight_kg"),
                          ("largo_cm", "length_cm"), ("ancho_cm", "width_cm"), ("alto_cm", "height_cm")]:
        form[field] = box[source]
    form["valor_declarado_ars"] = payload["declared_value"]
    return pantalla(request, cliente, disponible=True, asegurada=config.insured_operation, form=form)


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
