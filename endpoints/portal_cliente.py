# ============================================================
# Endpoints del Portal del Cliente
# ============================================================
# /portal/login           - Form de email
# /portal/login/send      - POST: manda link mágico
# /portal/auth?token=X    - Valida token, setea cookie
# /portal/logout          - Cierra sesión
# /portal/home            - Saldo + últimos envíos (requiere auth)
# /portal/cotizar         - Form de cotización (GET) + ejecuta (POST)
# /portal/catalogo        - Productos del cliente (GET) + agregar (POST)
# ============================================================

import os
from typing import Optional
from fastapi import APIRouter, Request, Form, Cookie, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from servicios.auth import (
    buscar_cliente_por_email, generar_token, validar_token,
    revocar_token, link_magico_url, get_markup_pct,
)
from servicios.rutas import (
    get_rutas_activas, get_ruta,
    get_paises_origen, get_paises_destino, find_ruta_por_paises,
)
from servicios.catalogo import get_productos, agregar_producto
from servicios.cotizador import cotizar
from servicios.cuenta_corriente import saldo, total_pagado, get_pagos, get_facturado_real, get_facturas_recientes
from modelos.cotizacion import CotizacionInput
from modelos.producto import ProductoNuevo

# Email sender — para link mágico de login
from core.email_sender import enviar_link_magico


router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="templates")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"


# ── Dependency: cliente actual ──────────────────────────────
def cliente_actual(token: Optional[str] = Cookie(None)) -> str:
    """Devuelve el cliente del token o redirige a login."""
    if not token:
        raise HTTPException(status_code=303, headers={"Location": "/portal/login"})
    cliente = validar_token(token)
    if not cliente:
        raise HTTPException(status_code=303, headers={"Location": "/portal/login"})
    return cliente


# ── Login ───────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        request=request, name="portal/login.html",
        context={"mensaje": None},
    )


@router.post("/login/send", response_class=HTMLResponse)
def login_send(request: Request, email: str = Form(...)):
    cliente = buscar_cliente_por_email(email)
    if not cliente:
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={"mensaje": "Email no registrado. Contactá a Tauro.", "tipo_msg": "error"},
        )

    token = generar_token(email, cliente)
    link = link_magico_url(BASE_URL, token)

    # Enviar email real — siempre logueamos el link en consola para debug
    print(f"\n[LOGIN MÁGICO] {email} → {cliente}")
    print(f"  Link: {link}\n")
    enviado = False
    try:
        enviado = enviar_link_magico(email, link, cliente)
    except Exception as e:
        print(f"[login] Error enviando email: {e}")

    # En DEV mostramos el link en pantalla (no hay SMTP configurado todavía)
    es_dev = os.getenv("ENV", "DEV").upper() != "PROD"

    return templates.TemplateResponse(
        request=request, name="portal/login.html",
        context={
            "mensaje": f"Link generado para {email}. " + (
                "Hacé click en el botón de abajo para entrar." if es_dev else "Revisá tu inbox."
            ),
            "tipo_msg": "ok",
            "dev_link": link if es_dev else None,
        },
    )


@router.get("/auth")
def auth_callback(token: str):
    cliente = validar_token(token)
    if not cliente:
        return RedirectResponse(url="/portal/login?error=token_invalido", status_code=303)

    response = RedirectResponse(url="/portal/home", status_code=303)
    response.set_cookie(
        key="token", value=token,
        httponly=True, max_age=60 * 60 * 24 * 7,
        samesite="lax",
        secure=COOKIE_SECURE,  # True en PROD (HTTPS), False en DEV
    )
    return response


@router.get("/logout")
def logout(token: Optional[str] = Cookie(None)):
    if token:
        try:
            revocar_token(token)
        except Exception:
            pass
    response = RedirectResponse(url="/portal/login", status_code=303)
    response.delete_cookie("token")
    return response


# ── Home ────────────────────────────────────────────────────
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, cliente: str = Depends(cliente_actual)):
    facturado = get_facturado_real(cliente)
    saldo_data = saldo(cliente, total_facturado_ars=facturado)
    pagos_recientes = get_pagos(cliente)[-5:][::-1]
    facturas_recientes = get_facturas_recientes(cliente, limite=5)

    return templates.TemplateResponse(
        request=request, name="portal/home.html",
        context={
            "cliente": cliente,
            "saldo": saldo_data,
            "pagos_recientes": pagos_recientes,
            "facturas_recientes": facturas_recientes,
        },
    )


# ── Cotizar ─────────────────────────────────────────────────
@router.get("/cotizar", response_class=HTMLResponse)
def cotizar_form(request: Request, cliente: str = Depends(cliente_actual)):
    return templates.TemplateResponse(
        request=request, name="portal/cotizar.html",
        context={
            "cliente": cliente,
            "paises_origen": get_paises_origen(),
            "paises_destino": get_paises_destino(),
            "resultado": None,
        },
    )


@router.post("/cotizar", response_class=HTMLResponse)
def cotizar_post(
    request: Request,
    origen_pais: str = Form(...),
    destino_pais: str = Form(...),
    peso_kg: float = Form(...),
    largo_cm: float = Form(...),
    ancho_cm: float = Form(...),
    alto_cm: float = Form(...),
    cliente: str = Depends(cliente_actual),
):
    error = None
    resultado = None

    try:
        ruta = find_ruta_por_paises(origen_pais, destino_pais)
        if not ruta:
            raise ValueError(
                f"No hay ruta activa para {origen_pais} → {destino_pais}. "
                "Contactá a Tauro para habilitarla."
            )

        input_data = CotizacionInput(
            ruta_id=ruta.ruta_id, peso_kg=peso_kg,
            largo_cm=largo_cm, ancho_cm=ancho_cm, alto_cm=alto_cm,
        )
        markup = get_markup_pct(cliente)
        resultado = cotizar(cliente, markup_pct=markup, input_data=input_data)
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        request=request, name="portal/cotizar.html",
        context={
            "cliente": cliente,
            "paises_origen": get_paises_origen(),
            "paises_destino": get_paises_destino(),
            "resultado": resultado,
            "error": error,
        },
    )


# ── Catálogo ────────────────────────────────────────────────
@router.get("/catalogo", response_class=HTMLResponse)
def catalogo_view(request: Request, cliente: str = Depends(cliente_actual)):
    productos = get_productos(cliente, solo_activos=False)
    return templates.TemplateResponse(
        request=request, name="portal/catalogo.html",
        context={"cliente": cliente, "productos": productos},
    )


@router.post("/catalogo/add")
def catalogo_add(
    alias_interno: str = Form(...),
    nombre_invoice: str = Form(...),
    hs_code: str = Form(...),
    largo_cm: float = Form(...),
    ancho_cm: float = Form(...),
    alto_cm: float = Form(...),
    peso_kg: float = Form(...),
    valor_usd_default: float = Form(...),
    cliente: str = Depends(cliente_actual),
):
    try:
        nuevo = ProductoNuevo(
            alias_interno=alias_interno, nombre_invoice=nombre_invoice,
            hs_code=hs_code, largo_cm=largo_cm, ancho_cm=ancho_cm,
            alto_cm=alto_cm, peso_kg=peso_kg, valor_usd_default=valor_usd_default,
        )
        agregar_producto(cliente, nuevo)
    except Exception as e:
        return RedirectResponse(url=f"/portal/catalogo?error={e}", status_code=303)
    return RedirectResponse(url="/portal/catalogo?ok=1", status_code=303)
