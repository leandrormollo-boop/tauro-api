"""Ficha de control por envío: precio, cuenta corriente y recolección."""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import RedirectResponse, Response

from endpoints import admin as panel
from servicios import control_envios_admin as control
from servicios import recolecciones_admin as retiros
from servicios import ajustes_precio_admin as precios
from servicios.solicitudes_guia import obtener_solicitud

router = APIRouter()
log = logging.getLogger(__name__)


def _ruta(*, solicitud_id=None, envio_id=None, cliente_id=None):
    if envio_id is not None:
        return f"/admin/clientes/{quote(cliente_id.strip().upper(), safe='')}/envios/{envio_id}/control"
    return f"/admin/pedidos/{solicitud_id}/control"


def _alcance(ruta, accion, revision, esperado, clave):
    return f"control-envio:{ruta}:{accion}:{revision}:{esperado}:{clave}"


def _contexto(**identidad):
    dato = control.obtener_control_envio_admin(**identidad)
    sol = dato.get("solicitud")
    if sol:
        detalle = obtener_solicitud(sol["id"])
        if not detalle or detalle["cliente_id"] != sol["cliente_id"]:
            raise control.ControlEnvioAdminError("El envío no existe.", codigo="NO_ENCONTRADO")
        detalle["tiene_label"] = bool(detalle.pop("label_pdf", None))
        dato["detalle"] = detalle
        dato["retiro"] = retiros.contexto_recoleccion_admin(sol["id"])
    else:
        dato["detalle"] = {}
        dato["retiro"] = {
            "habilitada": False,
            "motivo": "Este cargo histórico no tiene los datos de origen y paquetes para pedir una recolección.",
        }
    dato["cliente_id"] = (sol or dato["cargo"])["cliente_id"]
    dato["cancelado"] = (
        (dato.get("cargo") or {}).get("estado") == "CANCELADO"
        or (sol or {}).get("estado") in {"CANCELADO", "REEMPLAZADO"}
    )
    return dato


def _render(request, dato, ruta, *, error=None, status=200, form=None):
    valores = dict(form or {})
    esperado = str(dato["precio"]["vigente_ars"] if dato["precio"]["vigente_ars"] is not None else "")
    operaciones = {}
    for accion in ("precio", "cancelar", "recoleccion"):
        clave = panel._nueva_idempotency_key()
        # Un resultado incierto de retiro conserva el token de ese intento.
        if accion == valores.get("accion"):
            clave = panel._idempotency_key_para_reintento(valores.get("idempotency_key"))
        operaciones[accion] = {
            "clave": clave,
            "csrf": panel._csrf_dhl(_alcance(ruta, accion, dato["revision"], esperado, clave)),
        }
    return panel.templates.TemplateResponse(
        request=request, name="admin/envio_control.html",
        context={
            "seccion": "control_envios", "c": dato, "ruta_control": ruta,
            "operaciones": operaciones, "precio_esperado": esperado,
            "hoy": date.today().isoformat(), "valores": valores,
            "control_error": error,
            "control_ok": {
                "precio": "Precio actualizado. El cambio ya figura en la cuenta del cliente.",
                "sin_cambios": "El precio ya tenía ese importe.",
                "cancelado": "Cancelado en TAURO. El cargo ya no suma en la cuenta del cliente.",
                "cancelado_con_retiro": "Cancelado en TAURO: se anuló el retiro ante el courier y el cargo ya no suma en la cuenta del cliente.",
                "recoleccion": "Recolección confirmada por el operador.",
            }.get(request.query_params.get("ok", "")),
        }, status_code=status, headers={"Cache-Control": "private, no-store"},
    )


def _get(request, admin_token, **identidad):
    if not panel._is_auth(admin_token):
        return panel._redirect_login()
    try:
        return _render(request, _contexto(**identidad), _ruta(**identidad))
    except control.ControlEnvioAdminError as exc:
        return Response(str(exc), status_code=404, media_type="text/plain")
    except Exception as exc:
        log.error("No se pudo abrir control de envío: %s", type(exc).__name__)
        return Response("No pudimos consultar el envío. Recargá la página.", status_code=503)


async def _post(request, admin_token, **identidad):
    if not panel._is_auth(admin_token):
        return panel._redirect_login()
    ruta = _ruta(**identidad)
    form = await request.form()
    accion = str(form.get("accion") or "")
    revision = str(form.get("revision") or "")
    esperado = str(form.get("precio_esperado") or "")
    clave = str(form.get("idempotency_key") or "")
    if accion not in {"precio", "cancelar", "recoleccion"} or not panel._csrf_dhl_valido(
        form.get("csrf"), _alcance(ruta, accion, revision, esperado, clave)
    ):
        return Response("El formulario venció. Recargá el envío y volvé a intentar.", status_code=403)
    if form.get("confirmar") != "1":
        return Response("Confirmá la acción antes de continuar.", status_code=400)
    try:
        panel._idempotency_key_form(clave)
        dato = _contexto(**identidad)
    except control.ControlEnvioAdminError as exc:
        return Response(str(exc), status_code=404, media_type="text/plain")
    except ValueError as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")
    except Exception as exc:
        log.error("No se pudo consultar control de envío: %s", type(exc).__name__)
        return Response("No pudimos consultar el envío. No confirmes otra operación hasta recargar.", status_code=503)
    actor = "admin:" + hashlib.sha256(str(admin_token).encode()).hexdigest()[:12]
    try:
        if accion == "cancelar":
            resultado = control.cancelar_envio_admin(
                **identidad, motivo=str(form.get("motivo") or ""), actor=actor, revision=revision,
            )
            ok = "cancelado_con_retiro" if (resultado or {}).get("retiros_cancelados") else "cancelado"
        elif accion == "precio":
            if not dato["precio"]["habilitada"]:
                raise ValueError(dato["precio"]["motivo"])
            resultado = precios.aplicar_nuevo_precio(
                cliente_id=dato["cliente_id"], envio_id=dato["cargo"]["id"],
                nuevo_precio_ars=panel._importe_contable_form(form.get("nuevo_precio_ars"), "Nuevo precio", permitir_cero=True),
                precio_esperado_ars=esperado, motivo=str(form.get("motivo") or ""),
                actor=actor, idempotency_key=clave,
            )
            ok = "sin_cambios" if resultado.get("sin_cambios") else "precio"
        else:
            if not dato.get("solicitud"):
                raise ValueError(dato["retiro"]["motivo"])
            resultado = retiros.programar_recoleccion_admin(
                solicitud_id=dato["solicitud"]["id"], fecha=str(form.get("fecha") or ""),
                ready_time=str(form.get("ready_time") or ""), close_time=str(form.get("close_time") or ""),
                instrucciones=str(form.get("instrucciones") or ""), actor=actor, idempotency_key=clave,
            )
            if not resultado.get("ok"):
                raise ValueError(resultado.get("error") or "El operador no confirmó la recolección. Revisá su estado antes de repetir.")
            ok = "recoleccion"
    except ValueError as exc:
        # Consultar de nuevo muestra el estado real si cambió durante la acción.
        try:
            actualizado = _contexto(**identidad)
        except Exception as refresh_exc:
            log.error("No se pudo refrescar control de envío: %s", type(refresh_exc).__name__)
            return Response("No pudimos consultar el estado actual. Recargá antes de repetir la acción.", status_code=503)
        return _render(request, actualizado, ruta, error=str(exc), status=409, form=form)
    except Exception as exc:
        log.error("Fallo en control de envío (%s): %s", accion, type(exc).__name__)
        return Response(
            "No pudimos confirmar el resultado. Abrí nuevamente el envío para comprobar su estado antes de repetir la acción.",
            status_code=503, headers={"Cache-Control": "private, no-store"},
        )
    return RedirectResponse(f"{ruta}?ok={ok}", status_code=303)


@router.get("/pedidos/{solicitud_id}/control")
def pedido_control(request: Request, solicitud_id: int, admin_token: str | None = Cookie(None)):
    return _get(request, admin_token, solicitud_id=solicitud_id)


@router.post("/pedidos/{solicitud_id}/control")
async def pedido_control_post(request: Request, solicitud_id: int, admin_token: str | None = Cookie(None)):
    return await _post(request, admin_token, solicitud_id=solicitud_id)


@router.get("/clientes/{cliente_id}/envios/{envio_id}/control")
def cargo_control(request: Request, cliente_id: str, envio_id: int, admin_token: str | None = Cookie(None)):
    return _get(request, admin_token, cliente_id=cliente_id, envio_id=envio_id)


@router.post("/clientes/{cliente_id}/envios/{envio_id}/control")
async def cargo_control_post(request: Request, cliente_id: str, envio_id: int, admin_token: str | None = Cookie(None)):
    return await _post(request, admin_token, cliente_id=cliente_id, envio_id=envio_id)
