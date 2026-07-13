# ============================================================
# Endpoints del Portal del Cliente
# ============================================================
# /portal/login           - Form de email
# /portal/login/send      - POST: manda link mágico
# /portal/auth?token=X    - Valida token, setea cookie
# /portal/logout          - Cierra sesión
# /portal/home            - Saldo + últimos envíos (requiere auth)
# /portal/cotizar         - Form de cotización (GET) + ejecuta (POST)
# /portal/envios          - Solicitudes de guía del cliente
# /portal/catalogo        - Productos del cliente (GET) + agregar (POST)
# ============================================================

import os
from urllib.parse import quote
from typing import Optional
from fastapi import APIRouter, Request, Form, Cookie, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from servicios.auth import (
    buscar_cliente_por_email, generar_token, validar_token,
    revocar_token, link_magico_url, get_markup_pct,
    autenticar_cliente, consumir_magic_token,
)
from servicios.rate_limit import check_rate, reset_rate, client_ip
from servicios.rutas import (
    get_rutas_activas, get_ruta,
    get_paises_origen, get_paises_destino, find_ruta_por_paises,
)
from servicios.catalogo import get_productos, get_producto, agregar_producto
from servicios.cotizador import cotizar
from servicios.cuenta_corriente import saldo, total_pagado, get_pagos, get_facturado_real, get_facturas_recientes
from servicios.api_b2b import obtener_precio_envio
from servicios.solicitudes_guia import crear_solicitud_guia, listar_solicitudes_cliente
from servicios.pricing import parse_monto_ars
from servicios.direcciones import (
    TIPO_DESTINATARIO,
    TIPO_REMITENTE,
    contar_direcciones,
    crear_direccion,
    listar_direcciones,
    obtener_direccion,
    obtener_remitente_para_envio,
)
from modelos.cotizacion import CotizacionInput
from modelos.producto import ProductoNuevo

# Email sender — para link mágico de login
from core.email_sender import enviar_link_magico


router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="templates")

BASE_URL = os.getenv("BASE_URL")
# Cookies con Secure por defecto (Railway sirve por HTTPS). Apagar solo para
# desarrollo local por HTTP con SESSION_COOKIE_SECURE=0.
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"
SESSION_DAYS_INT = 7  # idéntico a SESSION_DAYS en servicios.auth


def _id_opt(value: str) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    """Login clásico con email + password. Setea cookie y redirige a /portal/home."""
    ip = client_ip(request)
    if not check_rate(f"portal_login:{ip}", max_attempts=8, window_seconds=300):
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={
                "mensaje": "Demasiados intentos. Esperá unos minutos e intentá de nuevo.",
                "tipo_msg": "error",
                "email_prefill": email,
            },
            status_code=429,
        )
    cliente = autenticar_cliente(email, password)
    if not cliente:
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={
                "mensaje": "Email o contraseña incorrectos.",
                "tipo_msg": "error",
                "email_prefill": email,
            },
            status_code=401,
        )

    reset_rate(f"portal_login:{ip}")
    token = generar_token(email, cliente)
    response = RedirectResponse(url="/portal/home", status_code=303)
    response.set_cookie(
        key="token", value=token,
        httponly=True, max_age=60 * 60 * 24 * SESSION_DAYS_INT,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


@router.post("/login/send", response_class=HTMLResponse)
def login_send(request: Request, email: str = Form(...)):
    """Magic link — para recuperación de contraseña / clientes sin password seteado."""
    ip = client_ip(request)
    if not check_rate(f"magic:{ip}", max_attempts=5, window_seconds=900):
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={"mensaje": "Demasiados pedidos de link. Esperá unos minutos.", "tipo_msg": "error"},
            status_code=429,
        )

    cliente = buscar_cliente_por_email(email)
    if not cliente:
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={"mensaje": "Email no registrado. Contactá a Tauro.", "tipo_msg": "error"},
        )

    token = generar_token(email, cliente)
    base_url = BASE_URL or str(request.base_url).rstrip("/")
    link = link_magico_url(base_url, token)

    # DEV explícito: mostrar el link en pantalla para poder entrar sin SMTP.
    # El default (ENV no seteada) trata como producción: nunca expone el token.
    es_dev = os.getenv("ENV", "").strip().upper() == "DEV"
    if es_dev:
        print(f"[login] magic link DEV para {email} → {cliente}")
    else:
        # Nunca imprimir el token de sesión en logs de producción.
        print(f"[login] magic link solicitado para {cliente}")

    try:
        enviar_link_magico(email, link, cliente)
    except Exception as e:
        print(f"[login] Error enviando email: {e}")

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
    # El token del email es de un solo uso: se valida y se marca usado de forma
    # atómica, y se canjea por una sesión nueva (el link nunca se convierte en
    # la cookie de larga duración).
    datos = consumir_magic_token(token)
    if not datos:
        return RedirectResponse(url="/portal/login?error=token_invalido", status_code=303)

    nueva_sesion = generar_token(datos["email"], datos["cliente_id"])
    response = RedirectResponse(url="/portal/home", status_code=303)
    response.set_cookie(
        key="token", value=nueva_sesion,
        httponly=True, max_age=60 * 60 * 24 * SESSION_DAYS_INT,
        samesite="lax",
        secure=COOKIE_SECURE,
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
    solicitudes = listar_solicitudes_cliente(cliente, limite=5)
    direcciones_count = contar_direcciones(cliente)

    return templates.TemplateResponse(
        request=request, name="portal/home.html",
        context={
            "cliente": cliente,
            "saldo": saldo_data,
            "pagos_recientes": pagos_recientes,
            "facturas_recientes": facturas_recientes,
            "solicitudes": solicitudes,
            "direcciones_count": direcciones_count,
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


# ── Envíos / solicitudes de guía ───────────────────────────
@router.get("/envios", response_class=HTMLResponse)
def envios_view(
    request: Request,
    ok: Optional[str] = None,
    cliente: str = Depends(cliente_actual),
):
    solicitudes = listar_solicitudes_cliente(cliente)
    return templates.TemplateResponse(
        request=request, name="portal/envios.html",
        context={
            "cliente": cliente,
            "solicitudes": solicitudes,
            "flash_ok": "Solicitud creada. Tauro ya la ve en el admin." if ok == "solicitado" else None,
        },
    )


@router.get("/envios/nuevo", response_class=HTMLResponse)
def envio_nuevo_form(request: Request, cliente: str = Depends(cliente_actual)):
    return templates.TemplateResponse(
        request=request, name="portal/envio_nuevo.html",
        context={
            "cliente": cliente,
            "productos": get_productos(cliente),
            "paises_destino": get_paises_destino(),
            "remitente": obtener_remitente_para_envio(cliente),
            "remitentes": listar_direcciones(cliente, TIPO_REMITENTE),
            "destinatarios": listar_direcciones(cliente, TIPO_DESTINATARIO),
            "form": {},
            "error": None,
        },
    )


@router.post("/envios/nuevo", response_class=HTMLResponse)
def envio_nuevo_post(
    request: Request,
    producto_alias: str = Form(...),
    destino_pais: str = Form(...),
    cantidad: int = Form(1),
    remitente_id: str = Form(""),
    destinatario_id: str = Form(""),
    dest_nombre: str = Form(...),
    dest_documento: str = Form(""),
    dest_email: str = Form(""),
    dest_telefono: str = Form(""),
    dest_direccion: str = Form(...),
    dest_ciudad: str = Form(...),
    dest_estado: str = Form(""),
    dest_zip: str = Form(...),
    dest_pais: str = Form(""),
    dest_alias: str = Form(""),
    guardar_destinatario: Optional[str] = Form(None),
    precio_cliente_final_ars: str = Form(""),
    observaciones: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    productos = get_productos(cliente)
    paises_destino = get_paises_destino()
    remitentes = listar_direcciones(cliente, TIPO_REMITENTE)
    destinatarios = listar_direcciones(cliente, TIPO_DESTINATARIO)
    form = {
        "producto_alias": producto_alias,
        "destino_pais": destino_pais,
        "cantidad": cantidad,
        "remitente_id": remitente_id,
        "destinatario_id": destinatario_id,
        "dest_nombre": dest_nombre,
        "dest_documento": dest_documento,
        "dest_email": dest_email,
        "dest_telefono": dest_telefono,
        "dest_direccion": dest_direccion,
        "dest_ciudad": dest_ciudad,
        "dest_estado": dest_estado,
        "dest_zip": dest_zip,
        "dest_pais": dest_pais,
        "dest_alias": dest_alias,
        "guardar_destinatario": guardar_destinatario,
        "precio_cliente_final_ars": precio_cliente_final_ars,
        "observaciones": observaciones,
    }

    try:
        remitente = obtener_remitente_para_envio(cliente, _id_opt(remitente_id))
        if not remitente:
            raise ValueError(
                "No hay remitente cargado. Agregá uno en Direcciones o completá los datos del cliente en el admin."
            )

        if destinatario_id:
            destinatario = obtener_direccion(cliente, _id_opt(destinatario_id) or 0)
            if destinatario and destinatario["tipo"] == TIPO_DESTINATARIO:
                dest_nombre = destinatario["nombre"]
                dest_documento = destinatario.get("documento") or ""
                dest_email = destinatario.get("email") or ""
                dest_telefono = destinatario.get("telefono") or ""
                dest_direccion = destinatario["direccion"]
                dest_ciudad = destinatario["ciudad"]
                dest_estado = destinatario.get("estado") or ""
                dest_zip = destinatario["cp"]
                dest_pais = destinatario["pais"]

        producto = get_producto(cliente, producto_alias)
        if not producto or not producto.activo:
            raise ValueError("Ese producto no está activo en tu catálogo.")

        precio = obtener_precio_envio(cliente, producto_alias, destino_pais)
        if not precio.get("encontrado"):
            motivo = precio.get("motivo") or "sin_precio"
            raise ValueError(f"No se pudo cotizar ese producto/destino ({motivo}).")

        precio_final = parse_monto_ars(precio_cliente_final_ars)

        if guardar_destinatario:
            crear_direccion(
                cliente_id=cliente,
                tipo=TIPO_DESTINATARIO,
                alias=dest_alias or dest_nombre,
                nombre=dest_nombre,
                documento=dest_documento,
                email=dest_email,
                telefono=dest_telefono,
                direccion=dest_direccion,
                ciudad=dest_ciudad,
                estado=dest_estado,
                cp=dest_zip,
                pais=dest_pais or destino_pais,
                predeterminada=False,
                notas="Guardado desde creación de envío.",
            )

        crear_solicitud_guia(
            cliente_id=cliente,
            producto_alias=producto.alias_interno,
            cantidad=cantidad,
            destino_pais=destino_pais,
            remitente_alias=remitente.get("alias") or remitente.get("label") or "",
            remitente_nombre=remitente.get("nombre") or "",
            remitente_documento=remitente.get("documento") or "",
            remitente_email=remitente.get("email") or "",
            remitente_telefono=remitente.get("telefono") or "",
            remitente_direccion=remitente.get("direccion") or "",
            remitente_ciudad=remitente.get("ciudad") or "",
            remitente_estado=remitente.get("estado") or "",
            remitente_zip=remitente.get("cp") or "",
            remitente_pais=remitente.get("pais") or "AR",
            dest_nombre=dest_nombre,
            dest_documento=dest_documento,
            dest_email=dest_email,
            dest_telefono=dest_telefono,
            dest_direccion=dest_direccion,
            dest_ciudad=dest_ciudad,
            dest_estado=dest_estado,
            dest_zip=dest_zip,
            observaciones=observaciones,
            peso_kg=producto.peso_kg,
            largo_cm=producto.largo_cm,
            ancho_cm=producto.ancho_cm,
            alto_cm=producto.alto_cm,
            valor_declarado_usd=producto.valor_usd_default,
            ruta_id=precio["ruta_id"],
            coti_id=precio["coti_id"],
            precio_tauro_ars=precio["precio_ars"],
            precio_tauro_usd=precio["precio_usd"],
            precio_cliente_final_ars=precio_final,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request, name="portal/envio_nuevo.html",
            context={
                "cliente": cliente,
                "productos": productos,
                "paises_destino": paises_destino,
                "remitente": obtener_remitente_para_envio(cliente, _id_opt(remitente_id)),
                "remitentes": remitentes,
                "destinatarios": destinatarios,
                "form": form,
                "error": str(e),
            },
        )

    return RedirectResponse(url="/portal/envios?ok=solicitado", status_code=303)


# ── Direcciones ─────────────────────────────────────────────
@router.get("/direcciones", response_class=HTMLResponse)
def direcciones_view(
    request: Request,
    ok: Optional[str] = None,
    error: Optional[str] = None,
    cliente: str = Depends(cliente_actual),
):
    return templates.TemplateResponse(
        request=request, name="portal/direcciones.html",
        context={
            "cliente": cliente,
            "remitente": obtener_remitente_para_envio(cliente),
            "remitentes": listar_direcciones(cliente, TIPO_REMITENTE),
            "destinatarios": listar_direcciones(cliente, TIPO_DESTINATARIO),
            "flash_ok": "Dirección guardada." if ok == "1" else None,
            "error": error,
        },
    )


@router.post("/direcciones")
def direcciones_add(
    tipo: str = Form(...),
    alias: str = Form(""),
    nombre: str = Form(...),
    documento: str = Form(""),
    email: str = Form(""),
    telefono: str = Form(""),
    direccion: str = Form(...),
    ciudad: str = Form(...),
    estado: str = Form(""),
    cp: str = Form(...),
    pais: str = Form("AR"),
    predeterminada: Optional[str] = Form(None),
    notas: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    try:
        crear_direccion(
            cliente_id=cliente,
            tipo=tipo,
            alias=alias,
            nombre=nombre,
            documento=documento,
            email=email,
            telefono=telefono,
            direccion=direccion,
            ciudad=ciudad,
            estado=estado,
            cp=cp,
            pais=pais,
            predeterminada=bool(predeterminada),
            notas=notas,
        )
    except Exception as e:
        return RedirectResponse(url=f"/portal/direcciones?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url="/portal/direcciones?ok=1", status_code=303)


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
