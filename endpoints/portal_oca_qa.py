"""Rutas de piloto QA: sólo se montan desde el launcher local dedicado."""

import json, secrets
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from servicios.oca_portal_qa import QAPortalError

ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT / "templates"))
router = APIRouter(prefix="/portal/nacional/oca", tags=["OCA QA local"])
OWNER = "TAURO-QA-LOCAL"


def pilot(request):
    if request.client.host not in {
        "127.0.0.1",
        "::1",
        "testclient",
    } or request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
        raise HTTPException(403, "El piloto OCA sólo está disponible localmente.")
    return request.app.state.oca_qa


async def post(request):
    service = pilot(request)
    if request.headers.get("sec-fetch-site", "") == "cross-site":
        raise HTTPException(403, "Origen inválido.")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Origen inválido.")
    form = await request.form()
    if not secrets.compare_digest(
        str(form.get("csrf", "")), request.app.state.oca_csrf
    ):
        raise HTTPException(403, "Token inválido.")
    return service, form


@router.get("")
def index(request: Request, id: str = "", error: str = ""):
    service = pilot(request)
    try:
        row = service.get(id, OWNER) if id else None
    except QAPortalError:
        raise HTTPException(404, "Prueba no encontrada.")
    return templates.TemplateResponse(
        request=request,
        name="portal/oca_qa.html",
        context={
            "cliente": None,
            "row": row,
            "events": json.loads(row["events"]) if row else [],
            "rows": service.list(OWNER),
            "csrf": request.app.state.oca_csrf,
            "error": error,
        },
    )


@router.post("/cotizar")
async def quote(request: Request):
    service, form = await post(request)
    try:
        ident = service.quote(OWNER, form)
    except (QAPortalError, ValueError):
        return RedirectResponse(
            "/portal/nacional/oca?error=Revisá+los+datos+de+peso+y+medidas", 303
        )
    except Exception:
        return RedirectResponse(
            "/portal/nacional/oca?error=OCA+no+pudo+cotizar.+Intentá+nuevamente", 303
        )
    return RedirectResponse("/portal/nacional/oca?id=" + ident, 303)


@router.post("/{ident}/{action}")
async def action(request: Request, ident: str, action: str):
    service, _ = await post(request)
    operations = {
        "emitir": service.emit,
        "etiqueta": service.label,
        "anular": service.cancel,
        "tracking": service.track,
    }
    if action not in operations:
        raise HTTPException(404)
    try:
        operations[action](ident, OWNER)
    except QAPortalError:
        raise HTTPException(409, "La acción no está disponible en el estado actual.")
    except Exception:
        return RedirectResponse(
            "/portal/nacional/oca?id="
            + ident
            + "&error=OCA+no+respondió.+El+estado+quedó+guardado",
            303,
        )
    return RedirectResponse("/portal/nacional/oca?id=" + ident, 303)


@router.get("/{ident}/etiqueta.pdf")
def label(request: Request, ident: str):
    try:
        row = pilot(request).get(ident, OWNER)
    except QAPortalError:
        raise HTTPException(404)
    if not row["pdf"]:
        raise HTTPException(404, "PDF pendiente.")
    return Response(
        bytes(row["pdf"]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="OCA-QA-'
            + row["tracking"]
            + '.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
