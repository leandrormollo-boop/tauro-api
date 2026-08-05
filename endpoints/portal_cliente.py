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
from datetime import datetime
from urllib.parse import quote

from core.database import get_conn
from typing import Optional
from fastapi import APIRouter, Request, Form, Cookie, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
from servicios.catalogo import (
    get_productos, get_producto, agregar_producto,
    actualizar_producto_cliente, eliminar_producto_cliente,
)
from servicios.cotizador import cotizar, cotizar_opciones
from servicios.cuenta_corriente import (
    saldo, total_pagado, get_pagos, get_facturado_real, get_facturas_recientes,
    movimientos,
)
from servicios.api_b2b import (
    obtener_precio_envio, obtener_precio_envio_multi, cotizar_couriers_cliente,
)
from servicios.nacional import cotizar_nacional_cliente, nacional_activo
from servicios.solicitudes_guia import (
    crear_solicitud_guia, listar_solicitudes_cliente, obtener_label_pdf,
    obtener_solicitud_de_cliente, contar_guias_listas,
)
from servicios.carriers import courier_default_cliente
from servicios.impuestos import normalizar as normalizar_tax, tax_paga_cliente
from servicios.pricing import parse_monto_ars
from servicios.panel_cliente import checklist_arranque, embudo_envios
from servicios.integraciones_tienda import (
    conectar_tienda, listar_tiendas, desconectar_tienda,
    listar_pedidos, contar_pendientes, obtener_pedido,
    marcar_convertido, descartar_pedido,
)
from servicios.politica_envio import (
    obtener_config, guardar_config, guardar_tax_producto, tax_de_productos,
)
from servicios.direcciones import (
    TIPO_DESTINATARIO,
    TIPO_REMITENTE,
    actualizar_direccion,
    contar_direcciones,
    crear_direccion,
    eliminar_direccion,
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

# Helpers de courier disponibles en TODOS los templates del portal: la URL
# de tracking y la división nacional/internacional salen de un solo lugar.
from servicios.couriers_urls import es_nacional, url_tracking
templates.env.globals["url_tracking"] = url_tracking
templates.env.globals["es_nacional"] = es_nacional

# Canal de ayuda (WhatsApp/mail) disponible en todos los templates. Se
# evalúa por render porque el número puede cargarse en /admin/config sin
# deploy — con cache interno de 60s para no viajar a la base por página.
from servicios.ayuda import ayuda_info
templates.env.globals["ayuda"] = ayuda_info


def _pendientes_menu(cliente_id: str) -> dict:
    """
    Globos rojos del menú lateral: sólo cuenta lo que espera una acción
    DEL CLIENTE. Un fallo contando nunca puede tumbar la página, así que
    ante cualquier error devuelve ceros (el menú sale sin globos).
    """
    if not cliente_id:
        return {"envios": 0, "tienda": 0}
    try:
        return {
            "envios": contar_guias_listas(cliente_id),
            "tienda": contar_pendientes(cliente_id),
        }
    except Exception as e:
        print(f"[portal] no pude contar pendientes de {cliente_id}: {e}")
        return {"envios": 0, "tienda": 0}


templates.env.globals["pendientes_menu"] = _pendientes_menu


def _saldo_menu(cliente_id: str, ya_calculado: Optional[dict] = None) -> Optional[dict]:
    """
    Saldo para la barra lateral: visible en TODAS las pantallas del portal,
    no sólo en el escritorio. Es el número que el cliente quiere chequear
    de reojo sin ir a buscarlo — hoy tenía que entrar a "Mi cuenta".

    `ya_calculado` es el dict de saldo que la vista pudo haber calculado por
    su cuenta (el escritorio y la cuenta corriente lo hacen para mostrar el
    desglose facturado/pagado). Reusarlo evita repetir dos consultas por
    render en las dos pantallas más visitadas del portal.

    Devuelve None ante cualquier problema y la barra sale sin el bloque:
    un saldo que no se puede calcular no se muestra en cero, porque un
    cero falso es peor que no mostrar nada cuando hablamos de plata.
    """
    if not cliente_id:
        return None
    try:
        data = ya_calculado
        if not data:
            facturado = get_facturado_real(cliente_id)
            data = saldo(cliente_id, total_facturado_ars=facturado)
        pendiente = float(data.get("saldo_pendiente_ars") or 0)
        return {
            "pendiente_ars": pendiente,
            "al_dia": pendiente <= 0,
            # A favor: pagó de más. Se muestra en positivo con otro cartel,
            # que si no queda un "-$5.000" que parece un error.
            "a_favor_ars": abs(pendiente) if pendiente < 0 else 0,
        }
    except Exception as e:
        print(f"[portal] no pude calcular el saldo de {cliente_id}: {e}")
        return None


templates.env.globals["saldo_menu"] = _saldo_menu

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
def login_form(request: Request, token: Optional[str] = Cookie(None)):
    # Si ya hay sesión activa, directo al portal — así el botón
    # "Iniciar sesión" de la web abre el escritorio sin fricción.
    if token and validar_token(token):
        return RedirectResponse(url="/portal/home", status_code=303)
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
    """
    Login con EMAIL o ID DE CLIENTE + contraseña. El campo se sigue llamando
    `email` para no romper autocompletes guardados, pero acepta las dos cosas
    (MELCIOR o melcior@…): la distinción la hace autenticar_cliente.
    """
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
    auth = autenticar_cliente(email, password)
    if auth and auth.get("sin_password"):
        # Cuenta recién creada sin contraseña: decirlo, no fingir un error
        # de tipeo. El camino es el link mágico (o que Tauro le asigne una).
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={
                "mensaje": "Tu cuenta todavía no tiene contraseña. Usá "
                           "«Entrar sin contraseña» acá abajo y te mandamos un "
                           "link de acceso, o pedile a Tauro que te asigne una.",
                "tipo_msg": "error",
                "email_prefill": email,
            },
            status_code=401,
        )
    if not auth:
        from servicios.auditoria import registrar_desde_request
        registrar_desde_request(request, event="portal.login", actor_type="cliente",
                                actor_ref=(email or "")[:120], success=False, status_code=401)
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={
                "mensaje": "Usuario o contraseña incorrectos.",
                "tipo_msg": "error",
                "email_prefill": email,
            },
            status_code=401,
        )

    reset_rate(f"portal_login:{ip}")
    from servicios.auditoria import registrar_desde_request
    registrar_desde_request(request, event="portal.login", actor_type="cliente",
                            actor_ref=auth["cliente_id"], success=True, status_code=303)
    # La sesión guarda el email real aunque hayan entrado con el ID.
    token = generar_token(auth["email"], auth["cliente_id"])
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

    # También acá vale el ID de cliente: si no trae arroba, se busca el
    # email registrado de esa cuenta y el link viaja a ESE buzón (el que
    # está en la ficha — nunca a una dirección tipeada por un tercero).
    email = (email or "").strip()
    if email and "@" not in email:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT email FROM clientes WHERE cliente_id = %s AND activo = TRUE",
                        (email.upper(),))
                    fila = cur.fetchone()
                    if fila and fila.get("email"):
                        email = str(fila["email"]).strip().lower()
        except Exception as e:
            print(f"[login] lookup por ID falló: {e}")

    cliente = buscar_cliente_por_email(email)
    if not cliente:
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={"mensaje": "Usuario no registrado. Contactá a Tauro.", "tipo_msg": "error"},
        )

    token = generar_token(email, cliente)
    # El token que autentica viaja en este link. NO derivarlo del Host del
    # request (envenenable): fallback fijo al dominio oficial, igual que el
    # admin. Así un Host falso nunca puede desviar el magic link a otro lado.
    base_url = BASE_URL or "https://taurosolutions.ar"
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
            # Bloques "¿qué falta?" y "¿qué pasó?" — ver servicios/panel_cliente.py
            "checklist": checklist_arranque(cliente),
            "embudo": embudo_envios(cliente),
        },
    )


# ── Rastreo: resuelve el courier antes de mandar afuera ─────
@router.get("/track")
def track_redirect(nro: str = "", cliente: str = Depends(cliente_actual)):
    """
    El buscador del home estaba clavado a FedEx: un tracking de envia.com
    pegado ahí abría FedEx y daba "no encontrado". Ahora se busca el número
    entre los envíos DEL cliente para saber de qué courier es; si no está,
    FedEx sigue siendo el default razonable (es el courier principal).
    """
    from servicios.couriers_urls import url_tracking

    nro = (nro or "").strip()
    if not nro:
        return RedirectResponse(url="/portal/home", status_code=303)

    courier = "FEDEX"
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT courier FROM solicitudes_guia
                    WHERE cliente_id = %s AND tracking = %s
                    LIMIT 1
                """, (cliente, nro))
                fila = cur.fetchone()
                if fila and fila.get("courier"):
                    courier = fila["courier"]
    except Exception as e:
        print(f"[portal] track lookup falló ({e}); default FedEx")

    return RedirectResponse(url=url_tracking(courier, nro), status_code=303)


# ── Backup en Excel del cliente ─────────────────────────────
@router.get("/backup.xlsx")
def backup_cliente(cliente: str = Depends(cliente_actual)):
    """
    SUS datos en un Excel para su PC: envíos, cuenta corriente y catálogo.
    El pedido textual del dueño: "Melcior podrá tocar descargar backup para
    extraer la info a un sheets y controlarlo en su pc".
    """
    from fastapi.responses import Response
    from servicios.export_cliente import generar_excel_cliente

    contenido = generar_excel_cliente(cliente)
    fecha = datetime.now().strftime("%Y-%m-%d")
    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="TAURO_{cliente}_{fecha}.xlsx"'},
    )


# ── Recolecciones ───────────────────────────────────────────
@router.get("/recolecciones", response_class=HTMLResponse)
def recolecciones_view(request: Request, cliente: str = Depends(cliente_actual)):
    """Que el chofer pase a buscar, en vez de llevar los paquetes."""
    from servicios.recolecciones import listar
    from datetime import date, timedelta

    try:
        recolecciones = listar(cliente)
    except Exception as e:
        print(f"[portal] no pude listar recolecciones de {cliente}: {e}")
        recolecciones = []

    remitente = obtener_remitente_para_envio(cliente, None)
    # Mañana por defecto (hoy suele estar pasado el corte); si cae finde,
    # el lunes — así el form abre con una fecha que el courier acepta.
    sugerida = date.today() + timedelta(days=1)
    while sugerida.weekday() >= 5:
        sugerida += timedelta(days=1)

    return templates.TemplateResponse(
        request=request, name="portal/recolecciones.html",
        context={"cliente": cliente, "recolecciones": recolecciones,
                 "remitente": remitente,
                 "fecha_sugerida": sugerida.strftime("%Y-%m-%d"),
                 "fecha_max": (date.today() + timedelta(days=14)).strftime("%Y-%m-%d")},
    )


@router.post("/recolecciones/nueva")
def recoleccion_nueva(
    fecha: str = Form(...),
    ready_time: str = Form("09:00"),
    close_time: str = Form("17:00"),
    bultos: int = Form(1),
    peso_kg: str = Form("1"),
    instrucciones: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    from servicios.recolecciones import crear

    try:
        r = crear(cliente, fecha, ready_time, close_time, bultos,
                  float((peso_kg or "1").replace(",", ".")), instrucciones)
    except Exception as e:
        print(f"[portal] error agendando recolección de {cliente}: {e}")
        r = {"ok": False, "error": "No pudimos agendarla. Probá de nuevo o escribinos."}

    if r.get("ok"):
        return RedirectResponse(url="/portal/recolecciones?ok=1", status_code=303)
    return RedirectResponse(
        url=f"/portal/recolecciones?error={quote(str(r.get('error') or 'Error'))}",
        status_code=303)


@router.post("/recolecciones/{rec_id}/cancelar")
def recoleccion_cancelar(rec_id: int, cliente: str = Depends(cliente_actual)):
    from servicios.recolecciones import cancelar

    r = cancelar(rec_id, cliente_id=cliente)
    if r.get("ok"):
        return RedirectResponse(url="/portal/recolecciones?ok=2", status_code=303)
    return RedirectResponse(
        url=f"/portal/recolecciones?error={quote(str(r.get('error') or 'Error'))}",
        status_code=303)


# ── Cuenta corriente ────────────────────────────────────────
@router.get("/cuenta", response_class=HTMLResponse)
def cuenta_corriente(request: Request, cliente: str = Depends(cliente_actual)):
    """
    Timeline completo de facturas y pagos. La spec lo pide explícito: el
    cliente administra su cuenta corriente con TAURO desde el portal, no
    preguntando el saldo por WhatsApp.
    """
    facturado = get_facturado_real(cliente)
    saldo_data = saldo(cliente, total_facturado_ars=facturado)
    # Todas las facturas, no las últimas 5 del home: esto ES el historial.
    facturas = get_facturas_recientes(cliente, limite=500)
    movs = movimientos(cliente, facturas)

    return templates.TemplateResponse(
        request=request, name="portal/cuenta.html",
        context={"cliente": cliente, "saldo": saldo_data, "movimientos": movs},
    )


# ── Informar un pago con comprobante ────────────────────────
@router.post("/pagos/informar")
async def informar_pago(
    request: Request,
    cliente: str = Depends(cliente_actual),
):
    """
    El cliente avisa que pagó y adjunta el comprobante (JPG/PNG/PDF).
    Queda PENDIENTE: no toca el saldo hasta que el admin lo verifica —
    decisión de Leandro: nadie se acredita plata con un comprobante sin
    revisar. El tamaño ya lo limitó el middleware (8 MB).
    """
    from servicios.cuenta_corriente import leer_comprobante_con_tope, registrar_pago

    form = await request.form()
    try:
        monto = parse_monto_ars(str(form.get("monto") or "")) or 0
        if monto <= 0:
            raise ValueError("El monto tiene que ser mayor a cero.")

        archivo = form.get("comprobante")
        contenido = await leer_comprobante_con_tope(archivo)
        if not contenido:
            raise ValueError("Falta el comprobante (foto o PDF).")

        registrar_pago(
            cliente_id=cliente,
            fecha=datetime.now().strftime("%Y-%m-%d"),
            monto_ars=monto,
            metodo=str(form.get("metodo") or "transferencia")[:60],
            referencia=str(form.get("referencia") or "")[:120],
            nota="Informado por el cliente desde el portal",
            estado="PENDIENTE",
            comprobante=contenido,
            comprobante_nombre=getattr(archivo, "filename", "") or "",
        )
    except ValueError as e:
        return RedirectResponse(url=f"/portal/cuenta?error={quote(str(e))}", status_code=303)
    except Exception as e:
        print(f"[portal] informar_pago falló para {cliente}: {e}")
        return RedirectResponse(
            url=f"/portal/cuenta?error={quote('No pudimos guardar el pago. Probá de nuevo.')}",
            status_code=303)
    return RedirectResponse(url="/portal/cuenta?ok=1", status_code=303)


@router.get("/facturas/{envio_id}/pdf")
def ver_factura_propia(envio_id: int, cliente: str = Depends(cliente_actual)):
    """El cliente descarga SUS facturas; las ajenas no existen para él."""
    from fastapi.responses import Response

    from servicios.cuenta_corriente import get_factura_pdf

    dato = get_factura_pdf(envio_id, cliente_id=cliente)
    if not dato:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    contenido, nombre = dato
    return Response(content=contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{nombre}"'})


@router.get("/pagos/{pago_id}/comprobante")
def ver_comprobante_propio(pago_id: int, cliente: str = Depends(cliente_actual)):
    """El cliente ve SUS comprobantes; los ajenos no existen para él (404)."""
    from fastapi.responses import Response

    from servicios.cuenta_corriente import get_comprobante

    dato = get_comprobante(pago_id, cliente_id=cliente)
    if not dato:
        raise HTTPException(status_code=404, detail="Comprobante no encontrado")
    contenido, tipo, nombre = dato
    return Response(content=contenido, media_type=tipo,
                    headers={"Content-Disposition": f'inline; filename="{nombre}"'})


# ── Cotizar ─────────────────────────────────────────────────
@router.get("/cotizar", response_class=HTMLResponse)
def cotizar_form(request: Request, cliente: str = Depends(cliente_actual)):
    return templates.TemplateResponse(
        request=request, name="portal/cotizar.html",
        context={
            "cliente": cliente,
            "paises_origen": _paises_con_nacional(),
            "paises_destino": _paises_con_nacional(),
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
    opciones = None

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
        # Todas las opciones de servicio (Priority, Economy...) en una llamada
        opciones = cotizar_opciones(cliente, markup_pct=markup, input_data=input_data)
        if not opciones:
            raise ValueError("FedEx no devolvió opciones para esa ruta.")
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        request=request, name="portal/cotizar.html",
        context={
            "cliente": cliente,
            "paises_origen": _paises_con_nacional(),
            "paises_destino": _paises_con_nacional(),
            "opciones": opciones,
            "resultado": opciones[0] if opciones else None,
            "error": error,
            # Para que cada tarjeta de opción linkee a "crear envío" con el
            # destino ya elegido.
            "destino_sel": destino_pais,
        },
    )


# ── API JSON: precio en vivo para el wizard ─────────────────
@router.get("/api/precio")
def api_precio_envio(
    producto: str,
    destino: str,
    cantidad: int = 1,
    cliente: str = Depends(cliente_actual),
):
    """
    Precio en vivo para el form de nuevo envío: producto del catálogo +
    destino + cantidad → precio final con el markup del cliente.
    Lo consume el JS del wizard; responde rápido y nunca lanza 500.
    """
    try:
        precio = obtener_precio_envio(cliente, producto, destino, cantidad=cantidad)
    except Exception as e:
        print(f"[portal] api_precio error: {e}")
        return JSONResponse({"ok": False, "motivo": "error_cotizando"}, status_code=200)

    if not precio.get("encontrado"):
        return JSONResponse(
            {"ok": False, "motivo": precio.get("motivo") or "sin_precio"},
            status_code=200,
        )

    return JSONResponse({
        "ok": True,
        "precio_ars": precio["precio_ars"],
        "precio_usd": precio["precio_usd"],
        "tarifa_lista_ars": precio.get("tarifa_lista_ars"),
        "peso_total_kg": precio.get("peso_total_kg"),
        "cantidad": precio.get("cantidad", cantidad),
        "dias_estimados": precio.get("dias_estimados"),
        "coti_id": precio.get("coti_id"),
    })


# ── API JSON: precio en vivo MULTI-BULTO ────────────────────
@router.post("/api/precio-multi")
async def api_precio_envio_multi(
    request: Request,
    cliente: str = Depends(cliente_actual),
):
    """
    Precio en vivo para el wizard multi-bulto.
    Body JSON: {"destino": "US", "bultos": [{"producto": alias, "cantidad": n}, ...]}
    Cada caja viaja como pieza propia; FedEx tarifa el conjunto.
    """
    try:
        body = await request.json()
        destino = str(body.get("destino") or "").strip()
        # CP/ciudad reales del destinatario, si el form ya los tiene cargados.
        # Sin esto el preview cotiza contra el CP de referencia de la ruta y
        # muestra un precio sin los recargos de zona remota.
        destino_real = {
            "cp": str(body.get("dest_zip") or "").strip(),
            "ciudad": str(body.get("dest_ciudad") or "").strip(),
            "estado": str(body.get("dest_estado") or "").strip(),
        }
        # El origen que el cliente eligió en el form (puede ser un proveedor
        # del exterior). Vacío = el remitente de la libreta, como siempre.
        origen_real = {
            "pais": str(body.get("origen_pais") or "").strip(),
            "ciudad": str(body.get("origen_ciudad") or "").strip(),
            "cp": str(body.get("origen_cp") or "").strip(),
        }
        if not origen_real["pais"]:
            rem = obtener_remitente_para_envio(cliente) or {}
            origen_real = {"pais": rem.get("pais") or "AR",
                           "ciudad": rem.get("ciudad") or "",
                           "cp": rem.get("cp") or ""}
        bultos = body.get("bultos") or []
        # Sin truncar en silencio: si hay de más, obtener_precio_envio_multi
        # lo rechaza con motivo y el preview muestra lo MISMO que diría el submit.
        bultos = [
            {
                "producto": str(b.get("producto") or ""),
                "cantidad": int(b.get("cantidad") or 1),
                # Carga libre: el preview cotiza con lo tipeado, igual que
                # el submit — peso, medidas y la invoice de esta caja.
                **{k: b.get(k) for k in (
                    "peso_kg", "largo_cm", "ancho_cm", "alto_cm",
                    "valor_unitario_usd", "descripcion_en", "hs_code",
                    "pais_origen",
                ) if b.get(k) not in (None, "")},
            }
            for b in bultos
            if str(b.get("producto") or "").strip()
               or b.get("peso_kg") or b.get("descripcion_en")
        ]
    except Exception:
        return JSONResponse({"ok": False, "motivo": "body_invalido"}, status_code=200)

    if not destino or not bultos:
        return JSONResponse({"ok": False, "motivo": "faltan_datos"}, status_code=200)

    try:
        # Los 3 couriers con el precio de ESTE cliente. Todos los clientes
        # ven todas las cotizaciones; lo que cambia es el markup de cada uno
        # (decisión de Leandro, 01/08/2026).
        precio = cotizar_couriers_cliente(
            cliente, destino, bultos,
            destino_real=destino_real, origen_real=origen_real,
        )
    except Exception as e:
        print(f"[portal] api_precio_multi error: {e}")
        return JSONResponse({"ok": False, "motivo": "error_cotizando"}, status_code=200)

    if not precio.get("encontrado"):
        return JSONResponse(
            {"ok": False, "motivo": precio.get("motivo") or "sin_precio"},
            status_code=200,
        )

    # Las claves se eligen a mano: nunca costo ni margen (test_no_fuga_costo).
    # `opciones` viene ordenada de más barata a más cara.
    opciones = precio.get("opciones") or []
    return JSONResponse({
        "ok": True,
        "opciones": [
            {
                "id": o["id"],
                "nombre": o["nombre"],
                "logo": o.get("logo"),
                "servicio": o.get("servicio"),
                "precio_ars": o["precio_ars"],
                "precio_usd": o["precio_usd"],
                "dias": o.get("dias_estimados"),
            }
            for o in opciones
        ],
        "no_disponibles": precio.get("no_disponibles") or [],
        # Retrocompat: el JS viejo lee precio_ars suelto. Va el más barato.
        "precio_ars": opciones[0]["precio_ars"] if opciones else None,
        "precio_usd": opciones[0]["precio_usd"] if opciones else None,
        "dias_estimados": opciones[0].get("dias_estimados") if opciones else None,
        "peso_total_kg": precio.get("peso_total_kg"),
        "piezas_total": precio.get("piezas_total"),
    })


# ── API JSON: precio en vivo NACIONAL (AR→AR vía envia.com) ─
@router.post("/api/precio-nacional")
async def api_precio_nacional(
    request: Request,
    cliente: str = Depends(cliente_actual),
):
    """
    Cotización nacional en vivo para el wizard: destino {cp, ciudad,
    provincia} + bultos del catálogo → opciones de todos los couriers
    nacionales con el markup del cliente aplicado.
    """
    if not nacional_activo():
        return JSONResponse({"ok": False, "motivo": "nacional_inactivo"}, status_code=200)
    try:
        body = await request.json()
        destino = {
            "cp": str(body.get("cp") or "").strip(),
            "ciudad": str(body.get("ciudad") or "").strip(),
            "provincia": str(body.get("provincia") or "").strip(),
        }
        bultos = [
            {"producto": str(b.get("producto") or ""), "cantidad": int(b.get("cantidad") or 1)}
            for b in (body.get("bultos") or [])
            if str(b.get("producto") or "").strip()
        ]
    except Exception:
        return JSONResponse({"ok": False, "motivo": "body_invalido"}, status_code=200)

    if not destino["cp"] or not destino["ciudad"] or not bultos:
        return JSONResponse({"ok": False, "motivo": "faltan_datos"}, status_code=200)

    remitente = obtener_remitente_para_envio(cliente)
    if not remitente:
        return JSONResponse({"ok": False, "motivo": "sin_remitente"}, status_code=200)
    origen = {
        "nombre": remitente.get("nombre"),
        "email": remitente.get("email"),
        "telefono": remitente.get("telefono"),
        "direccion": remitente.get("direccion"),
        "ciudad": remitente.get("ciudad"),
        "provincia": remitente.get("estado") or "CABA",
        "cp": remitente.get("cp"),
    }

    try:
        precio = cotizar_nacional_cliente(cliente, origen, destino, bultos)
    except Exception as e:
        print(f"[portal] api_precio_nacional error: {e}")
        return JSONResponse({"ok": False, "motivo": "error_cotizando"}, status_code=200)

    if not precio.get("encontrado"):
        return JSONResponse(
            {"ok": False, "motivo": precio.get("motivo") or "sin_cobertura"},
            status_code=200,
        )
    return JSONResponse({
        "ok": True,
        "opciones": [
            {
                "carrier": o["carrier"],
                "servicio": o["servicio"],
                "nombre": f"{o['carrier_nombre']} · {o['servicio_nombre']}",
                "precio_ars": o["precio_final_ars"],
                "dias": o["dias_estimados"],
            }
            for o in precio["opciones"]
        ],
        "piezas_total": precio["piezas_total"],
        "peso_total_kg": precio["peso_total_kg"],
        "coti_id": precio["coti_id"],
    })


# ── API JSON: parsear pedido pegado (mail del cliente) ──────
@router.post("/api/parsear-pedido")
async def api_parsear_pedido(
    request: Request,
    cliente: str = Depends(cliente_actual),
):
    """
    Recibe el texto de un pedido tal como llega por mail y devuelve los
    campos detectados para precargar el form de nuevo envío.
    """
    from servicios.parser_pedidos import parsear_pedido
    try:
        body = await request.json()
        texto = str(body.get("texto") or "")[:20000]
    except Exception:
        return JSONResponse({"ok": False, "motivo": "body_invalido"}, status_code=200)

    if not texto.strip():
        return JSONResponse({"ok": False, "motivo": "texto_vacio"}, status_code=200)

    resultado = parsear_pedido(texto)
    return JSONResponse({"ok": True, **resultado})


# ── Envíos / solicitudes de guía ───────────────────────────
@router.get("/envios", response_class=HTMLResponse)
def envios_view(
    request: Request,
    ok: Optional[str] = None,
    tipo: str = "",
    paso: str = "",
    cliente: str = Depends(cliente_actual),
):
    from servicios.couriers_urls import es_nacional as _es_nac
    from servicios.panel_cliente import PASOS_EMBUDO, paso_de_estado

    solicitudes = listar_solicitudes_cliente(cliente)
    # División nacional/internacional de la spec. El filtro es por courier
    # (la regla vive en couriers_urls, no acá): se filtra en memoria porque
    # el listado ya viene acotado por cliente.
    tipo = (tipo or "").lower()
    if tipo == "nacional":
        solicitudes = [s for s in solicitudes if _es_nac(s.get("courier"))]
    elif tipo == "internacional":
        solicitudes = [s for s in solicitudes if not _es_nac(s.get("courier"))]

    # Contadores del embudo, chicos, arriba de la tabla. Se cuentan DESPUÉS
    # del filtro nacional/internacional y ANTES del de paso: así el número
    # del chip siempre coincide con las filas que vas a ver al tocarlo.
    # Se saltea "por_armar" — esas son ventas de tienda, todavía no envíos.
    todos_n = len(solicitudes)
    conteos = {}
    for s in solicitudes:
        p = paso_de_estado(s.get("estado"))
        if p:
            conteos[p] = conteos.get(p, 0) + 1
    chips = [
        {**p, "cantidad": conteos.get(p["clave"], 0)}
        for p in PASOS_EMBUDO
        if p["clave"] != "por_armar"
    ]

    paso = (paso or "").lower()
    if paso in {c["clave"] for c in chips}:
        solicitudes = [s for s in solicitudes if paso_de_estado(s.get("estado")) == paso]
    else:
        paso = ""

    return templates.TemplateResponse(
        request=request, name="portal/envios.html",
        context={
            "cliente": cliente,
            "solicitudes": solicitudes,
            "tipo_filtro": tipo,
            "chips": chips,
            "paso_filtro": paso,
            # El chip "Todos" cuenta las filas de verdad (incluidos los
            # cancelados, que no viven en ningún paso del embudo).
            "total_sin_filtrar": todos_n,
            "flash_ok": "Solicitud creada. Tauro ya la ve en el admin." if ok == "solicitado" else None,
        },
    )


@router.get("/envios/{solicitud_id}/guia.pdf")
def descargar_guia(solicitud_id: int, cliente: str = Depends(cliente_actual)):
    """Descarga el label PDF de la guía. Solo si la solicitud es del cliente logueado."""
    pdf = obtener_label_pdf(solicitud_id, cliente_id=cliente)
    if not pdf:
        return RedirectResponse(url="/portal/envios", status_code=303)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="guia-tauro-{solicitud_id}.pdf"'},
    )


def _paises_con_nacional() -> list:
    """
    Países que el cliente puede elegir como DESTINO: todos.

    Antes salían de las rutas cargadas a mano, así que un cliente sólo podía
    despachar a donde el admin ya hubiera creado la fila. Regla de Leandro
    (05/08): el cliente elige desde dónde y hacia dónde, cualquier país. La
    cobertura la decide el courier, no una tabla nuestra: si ninguno cotiza
    esa ruta, el precio en vivo lo dice con todas las letras.

    Devuelve [(iso, nombre), ...] ordenado por nombre.
    """
    from servicios.paises import opciones
    return opciones()


@router.get("/envios/nuevo", response_class=HTMLResponse)
def envio_nuevo_form(
    request: Request,
    pedido_tienda: Optional[int] = None,
    destino: str = "",
    cliente: str = Depends(cliente_actual),
):
    # Si viene de un pedido de la tienda (Shopify/Tiendanube), prellenamos
    # el destinatario con lo que el comprador completó en el checkout.
    form: dict = {}
    pedido_info = None
    # Si viene del cotizador ("tocá la opción para crear el envío"), el
    # destino elegido ya llega puesto — una promesa menos que romper.
    if destino.strip() and not pedido_tienda:
        form["destino_pais"] = destino.strip().upper()[:3]
    if pedido_tienda:
        p = obtener_pedido(cliente, pedido_tienda)
        if p and p["estado"] == "PENDIENTE":
            d = p.get("destinatario") or {}
            items = p.get("items") or []
            resumen = " · ".join(
                f"{it.get('cantidad', 1)}x {it.get('titulo', '')}".strip() for it in items
            )[:400]
            # La dirección va completa (calle + piso/depto): si el piso se
            # pierde, el paquete llega al edificio pero no al departamento.
            direccion_full = " - ".join(
                x for x in [d.get("direccion", ""), d.get("direccion2", "")] if x
            )
            # En ventas B2B lo que figura en recepción es la razón social.
            nombre_full = d.get("nombre", "")
            if d.get("empresa"):
                nombre_full = f"{nombre_full} ({d['empresa']})" if nombre_full else d["empresa"]

            form = {
                "dest_nombre": nombre_full,
                "dest_email": d.get("email", ""),
                "dest_telefono": d.get("telefono", ""),
                "dest_direccion": direccion_full,
                "dest_ciudad": d.get("ciudad", ""),
                "dest_estado": d.get("estado", ""),
                "dest_zip": d.get("cp", ""),
                "destino_pais": d.get("pais", ""),
                "dest_pais": d.get("pais", ""),
                "observaciones": f"Pedido {p.get('numero') or p.get('pedido_externo_id')} de la tienda: {resumen}",
                "pedido_tienda_id": p["id"],
            }
            pedido_info = p
    return templates.TemplateResponse(
        request=request, name="portal/envio_nuevo.html",
        context={
            "cliente": cliente,
            "productos": get_productos(cliente),
            "paises_destino": _paises_con_nacional(),
            "remitente": obtener_remitente_para_envio(cliente),
            "remitentes": listar_direcciones(cliente, TIPO_REMITENTE),
            "destinatarios": listar_direcciones(cliente, TIPO_DESTINATARIO),
            "form": form,
            "pedido_tienda": pedido_info,
            # Arranca con lo que el cliente dejó configurado en su ficha; el
            # selector del wizard sólo sirve para pisarlo en este envío.
            "tax_paga_default": tax_paga_cliente(cliente),
            # El courier que el cliente dejó configurado (WAIMAO → DHL):
            # el selector del wizard arranca con este elegido.
            "courier_default": courier_default_cliente(cliente),
            "error": None,
        },
    )


@router.post("/envios/nuevo", response_class=HTMLResponse)
def envio_nuevo_post(
    request: Request,
    destino_pais: str = Form(...),
    # Multi-bulto: arrays paralelos, una entrada por fila de caja del form.
    # cantidad como str: un valor basura no debe tirar un 422 pelado, se
    # normaliza a 1 más abajo.
    bulto_producto: list[str] = Form([]),
    bulto_cantidad: list[str] = Form([]),
    # Declaración de invoice POR RENGLÓN (Leandro 05/08): el valor real de
    # venta cambia entre envíos; declarar el default del catálogo cuando se
    # vendió a otro precio es un problema en la aduana. Vacío = catálogo.
    # Carga libre (guía HAILU 05/08): peso y medidas por caja, sin catálogo.
    bulto_peso: list[str] = Form([]),
    bulto_largo: list[str] = Form([]),
    bulto_ancho: list[str] = Form([]),
    bulto_alto: list[str] = Form([]),
    bulto_desc_en: list[str] = Form([]),
    bulto_valor_usd: list[str] = Form([]),
    bulto_hs: list[str] = Form([]),
    bulto_pais_fab: list[str] = Form([]),
    # Legacy (por si queda un form viejo cacheado): un solo producto.
    producto_alias: str = Form(""),
    cantidad: int = Form(1),
    # Internacional: courier elegido en el comparador en vivo (fedex/dhl/ups).
    intl_courier: str = Form(""),
    # Quién paga los impuestos de destino EN ESTE envío. Viene con el default
    # del cliente ya seleccionado; acá se guarda lo que quedó elegido.
    tax_paga: str = Form(""),
    # Nacional: carrier/servicio elegido en el comparador en vivo.
    nac_carrier: str = Form(""),
    nac_servicio: str = Form(""),
    remitente_id: str = Form(""),
    # Remitente EDITABLE (05/08): lo que quedó en los campos manda sobre lo
    # que vino de la libreta. Para una importación el remitente es el
    # proveedor del exterior y puede cambiar envío a envío.
    rem_nombre: str = Form(""),
    rem_contacto: str = Form(""),
    rem_documento: str = Form(""),
    rem_email: str = Form(""),
    rem_telefono: str = Form(""),
    rem_direccion: str = Form(""),
    rem_ciudad: str = Form(""),
    rem_estado: str = Form(""),
    rem_zip: str = Form(""),
    rem_pais: str = Form(""),
    destinatario_id: str = Form(""),
    dest_nombre: str = Form(...),
    dest_contacto: str = Form(""),
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
    # Si el envío nació de un pedido de la tienda, al crearse la solicitud
    # el pedido pasa de PENDIENTE a CONVERTIDO.
    pedido_tienda_id: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    productos = get_productos(cliente)
    paises_destino = _paises_con_nacional()
    remitentes = listar_direcciones(cliente, TIPO_REMITENTE)
    destinatarios = listar_direcciones(cliente, TIPO_DESTINATARIO)

    # Normalizar filas de bultos: pares (producto, cantidad) sin filas vacías.
    # Fallback legacy: producto_alias + cantidad sueltos = una sola fila.
    filas = []
    n_filas = max(len(bulto_producto or []), len(bulto_peso or []),
                  len(bulto_desc_en or []))
    for i in range(n_filas):
        def _campo(lista, idx=i):
            v = lista[idx] if idx < len(lista or []) else ""
            return (v or "").strip()

        alias = _campo(bulto_producto)
        # Una fila vale con producto del catálogo O con carga manual: si no
        # tiene ninguna de las dos cosas, es un renglón vacío y se saltea.
        if not alias and not (_campo(bulto_peso) or _campo(bulto_desc_en)):
            continue
        try:
            cant = int(_campo(bulto_cantidad) or 1)
        except (TypeError, ValueError):
            cant = 1
        fila = {"producto": alias, "cantidad": max(cant, 1)}
        # Caja manual: peso y medidas de ESTE envío (con catálogo, lo pisan).
        for lista, clave in ((bulto_peso, "peso_kg"), (bulto_largo, "largo_cm"),
                             (bulto_ancho, "ancho_cm"), (bulto_alto, "alto_cm")):
            v = _campo(lista).replace(",", ".")
            if v:
                try:
                    fila[clave] = float(v)
                except ValueError:
                    pass
        # Overrides de invoice: sólo viajan los completados; el resto sale
        # del catálogo, como siempre.
        if _campo(bulto_desc_en):
            fila["descripcion_en"] = _campo(bulto_desc_en)[:75]
        if _campo(bulto_hs):
            fila["hs_code"] = _campo(bulto_hs)
        if _campo(bulto_pais_fab):
            fila["pais_origen"] = _campo(bulto_pais_fab).upper()[:2]
        v = _campo(bulto_valor_usd).replace(",", ".")
        if v:
            try:
                valor = float(v)
                if valor > 0:
                    fila["valor_unitario_usd"] = round(valor, 2)
            except ValueError:
                pass   # un valor ilegible cae al catálogo, no rompe el envío
        filas.append(fila)
    # Form viejo cacheado (pre multi-bulto): ahí "cantidad" significaba
    # unidades dentro de UNA caja — se respeta esa semántica para que el
    # precio cobrado sea el mismo que ese form mostró.
    legacy_single = False
    if not filas and (producto_alias or "").strip():
        legacy_single = True
        filas = [{"producto": producto_alias.strip(), "cantidad": max(int(cantidad or 1), 1)}]

    form = {
        "bultos": filas,
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
        # BUG corregido: sin esto, un error de validación re-renderizaba el
        # form con el hidden del pedido VACÍO — el vínculo con la venta de la
        # tienda se perdía, el pedido quedaba "pendiente" para siempre y se
        # podía terminar despachando dos veces.
        "pedido_tienda_id": pedido_tienda_id,
    }

    try:
        remitente = obtener_remitente_para_envio(cliente, _id_opt(remitente_id)) or {}
        # Lo que el cliente EDITÓ en el form manda sobre la libreta: campo
        # por campo, para que elegir de la libreta y corregir una sola cosa
        # (el CP, el teléfono) no pierda el resto.
        editado = {
            "nombre": rem_nombre, "documento": rem_documento,
            "email": rem_email, "telefono": rem_telefono,
            "direccion": rem_direccion, "ciudad": rem_ciudad,
            "estado": rem_estado, "cp": rem_zip, "pais": rem_pais,
        }
        for campo, valor in editado.items():
            if (valor or "").strip():
                remitente[campo] = valor.strip()
        if not (remitente.get("nombre") and remitente.get("direccion")
                and remitente.get("ciudad")):
            raise ValueError(
                "Faltan datos del remitente: nombre, dirección y ciudad como mínimo. "
                "Completalos en el paso 1 o elegí uno de la libreta."
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

        if not filas:
            raise ValueError("Agregá al menos una caja al envío.")

        for fila in filas:
            producto = get_producto(cliente, fila["producto"])
            if not producto or not producto.activo:
                raise ValueError(f"El producto \"{fila['producto']}\" no está activo en tu catálogo.")

        courier_extra = {}
        es_nacional = destino_pais.strip().upper() == "AR"
        if es_nacional:
            # ── Envío NACIONAL (AR→AR vía envia.com) ─────────
            if not nacional_activo():
                raise ValueError("Los envíos nacionales todavía no están habilitados.")
            origen_nac = {
                "nombre": remitente.get("nombre"),
                "email": remitente.get("email"),
                "telefono": remitente.get("telefono"),
                "direccion": remitente.get("direccion"),
                "ciudad": remitente.get("ciudad"),
                "provincia": remitente.get("estado") or "CABA",
                "cp": remitente.get("cp"),
            }
            destino_nac = {"cp": dest_zip, "ciudad": dest_ciudad, "provincia": dest_estado}
            precio_n = cotizar_nacional_cliente(cliente, origen_nac, destino_nac, filas)
            if not precio_n.get("encontrado"):
                raise ValueError(f"No se pudo cotizar ese envío nacional ({precio_n.get('motivo')}).")
            opciones_n = precio_n["opciones"]
            elegido = next(
                (o for o in opciones_n
                 if o["carrier"] == nac_carrier.strip() and o["servicio"] == nac_servicio.strip()),
                opciones_n[0],
            )
            bultos_detalle = precio_n["bultos"]
            precio = {
                "encontrado": True,
                "ruta_id": "NACIONAL",
                "coti_id": precio_n["coti_id"],
                "peso_total_kg": precio_n["peso_total_kg"],
                "valor_total_usd": round(
                    sum(b["valor_unitario_usd"] * b["cantidad"] for b in bultos_detalle), 2
                ),
                "precio_ars": elegido["precio_final_ars"],
                "precio_usd": elegido["precio_final_usd"],
            }
            courier_extra = {
                "courier": "ENVIA",
                "servicio_courier": f"{elegido['carrier']}/{elegido['servicio']}",
            }
        elif legacy_single:
            precio = obtener_precio_envio(
                cliente, filas[0]["producto"], destino_pais, cantidad=filas[0]["cantidad"]
            )
            bultos_detalle = None
        else:
            # La dirección real ya está en el scope (viene del form o de la
            # libreta). La rama nacional de acá arriba ya la usaba; la
            # internacional la tiraba y cotizaba contra el CP de referencia —
            # el recargo por zona remota lo comía TAURO.
            destino_real = {"cp": dest_zip, "ciudad": dest_ciudad, "estado": dest_estado}
            multi = cotizar_couriers_cliente(
                cliente, destino_pais, filas,
                destino_real=destino_real,
                # El remitente puede ser un proveedor del exterior: de ahí
                # sale el país de ORIGEN del envío, no de una constante.
                origen_real={
                    "pais": remitente.get("pais") or "AR",
                    "ciudad": remitente.get("ciudad") or "",
                    "cp": remitente.get("cp") or "",
                    "estado": remitente.get("estado") or "",
                },
            )
            if not multi.get("encontrado"):
                raise ValueError(
                    f"No se pudo cotizar ese envío ({multi.get('motivo') or 'sin_precio'})."
                )

            # El courier elegido en pantalla manda. Si en esta recotización
            # no volvió a aparecer —se cayó, o dejó de cubrir la ruta— NO se
            # agarra otro: el cliente eligió DHL y no puede terminar con un
            # FedEx debitado sin enterarse. Falla a la vista y elige de nuevo.
            opciones = multi["opciones"]
            elegido = (intl_courier or "").strip().lower()
            if elegido:
                op = next((o for o in opciones if o["id"] == elegido), None)
                if op is None:
                    nombres = ", ".join(o["nombre"] for o in opciones) or "ninguno"
                    raise ValueError(
                        f"{elegido.upper()} no pudo cotizar este envío en este momento. "
                        f"Disponibles ahora: {nombres}. Elegí otro proveedor y probá de nuevo."
                    )
            else:
                op = opciones[0]   # sin elección explícita: el más barato

            # El courier queda GUARDADO en la solicitud: sin esto el
            # despachador de emisión no sabe por dónde sale y cae al default.
            courier_extra = {"courier": op["id"].upper()}

            # ruta_id / coti_id salen de la cotización base (la de trazabilidad
            # que ya se loguea); el precio, del courier elegido.
            precio = {
                "encontrado": True,
                "precio_ars": op["precio_ars"],
                "precio_usd": op["precio_usd"],
                "dias_estimados": op.get("dias"),
                "ruta_id": multi.get("ruta_id"),
                "coti_id": multi.get("coti_id"),
                "bultos": multi.get("bultos"),
                "piezas_total": multi.get("piezas_total"),
                "peso_total_kg": multi.get("peso_total_kg"),
            }
            bultos_detalle = precio.get("bultos")
        if not precio.get("encontrado"):
            motivo = precio.get("motivo") or "sin_precio"
            raise ValueError(f"No se pudo cotizar ese envío ({motivo}).")

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

        # Campos legacy: primer bulto + totales (los listados y el admin los usan).
        if legacy_single:
            prod0 = get_producto(cliente, filas[0]["producto"])
            alias_display = prod0.alias_interno if prod0 else filas[0].get("producto") or "CARGA"
            total_cajas = filas[0]["cantidad"]
            dims0 = (prod0.largo_cm, prod0.ancho_cm, prod0.alto_cm)
            valor_declarado = round(prod0.valor_usd_default * total_cajas, 2)
        else:
            primero = bultos_detalle[0]
            alias_display = primero["producto_alias"]
            total_cajas = sum(b["cantidad"] for b in bultos_detalle)
            dims0 = (primero["largo_cm"], primero["ancho_cm"], primero["alto_cm"])
            # El valor declarado sale de la INVOICE de este envío (suma de
            # valor unitario × cantidad por renglón), no de un default 100:
            # declarar 100 USD cuando la carga vale 960 es falsear la aduana.
            valor_declarado = round(sum(
                float(b.get("valor_unitario_usd") or 0) * int(b.get("cantidad") or 1)
                for b in bultos_detalle
            ), 2) or (precio.get("valor_total_usd") or 100)
        solicitud_creada = crear_solicitud_guia(
            cliente_id=cliente,
            producto_alias=alias_display,
            cantidad=total_cajas,
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
            remitente_contacto=rem_contacto or remitente.get("contacto") or "",
            dest_nombre=dest_nombre,
            dest_contacto=dest_contacto,
            dest_documento=dest_documento,
            dest_email=dest_email,
            dest_telefono=dest_telefono,
            dest_direccion=dest_direccion,
            dest_ciudad=dest_ciudad,
            dest_estado=dest_estado,
            dest_zip=dest_zip,
            observaciones=observaciones,
            # Totales del envío completo (lo que se cotizó y se declara).
            peso_kg=precio.get("peso_total_kg") or 0.5,
            largo_cm=dims0[0],
            ancho_cm=dims0[1],
            alto_cm=dims0[2],
            valor_declarado_usd=valor_declarado,
            ruta_id=precio["ruta_id"],
            coti_id=precio["coti_id"],
            precio_tauro_ars=precio["precio_ars"],
            precio_tauro_usd=precio["precio_usd"],
            precio_cliente_final_ars=precio_final,
            bultos=bultos_detalle,
            # Se guarda POR ENVÍO: si el cliente cambia su default mañana,
            # los envíos ya despachados no cambian de manos.
            tax_paga=normalizar_tax(tax_paga, tax_paga_cliente(cliente)),
            **courier_extra,
        )
    except Exception as e:
        # ValueError = mensaje escrito para humanos (validaciones propias).
        # Cualquier otra excepción es interna: al log completa, a la pantalla
        # una frase — un traceback de Python asusta y no ayuda a corregir.
        if isinstance(e, ValueError):
            mensaje_error = str(e)
        else:
            print(f"[portal] error creando envío de {cliente}: {type(e).__name__}: {e}")
            mensaje_error = ("No pudimos crear el envío por un problema nuestro. "
                            "Probá de nuevo en un minuto o escribinos.")
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
                "error": mensaje_error,
                # Sin estos dos, el re-render tras un error pierde el courier
                # y el default de impuestos configurados del cliente.
                "tax_paga_default": tax_paga_cliente(cliente),
                "courier_default": courier_default_cliente(cliente),
            },
        )

    if pedido_tienda_id.strip().isdigit():
        try:
            marcar_convertido(cliente, int(pedido_tienda_id),
                              solicitud_id=solicitud_creada.get("id"))
        except Exception as e:
            print(f"[integraciones] no pude marcar convertido el pedido {pedido_tienda_id}: {e}")

    return RedirectResponse(url="/portal/envios?ok=solicitado", status_code=303)


# ── Detalle de envío ────────────────────────────────────────
# OJO: declarado DESPUÉS de /envios/nuevo para que "nuevo" no matchee
# como {solicitud_id}.
@router.get("/envios/{solicitud_id}", response_class=HTMLResponse)
def envio_detalle(
    request: Request,
    solicitud_id: int,
    cliente: str = Depends(cliente_actual),
):
    s = obtener_solicitud_de_cliente(solicitud_id, cliente)
    if not s:
        return RedirectResponse(url="/portal/envios", status_code=303)

    # ¿Este cliente puede emitir solo? Define si se muestra el botón.
    puede_emitir = False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT puede_emitir FROM clientes WHERE cliente_id = %s",
                            (cliente,))
                fila = cur.fetchone()
                puede_emitir = bool(fila and fila["puede_emitir"])
    except Exception as e:
        print(f"[portal] no pude leer puede_emitir de {cliente}: {e}")

    return templates.TemplateResponse(
        request=request, name="portal/envio_detalle.html",
        context={"cliente": cliente, "s": s, "puede_emitir": puede_emitir},
    )


@router.post("/envios/{solicitud_id}/emitir")
def emitir_guia_portal(
    request: Request,
    solicitud_id: int,
    cliente: str = Depends(cliente_actual),
):
    """
    El cliente emite su propia guía (spec: "el cliente podrá generar las
    guías ahí mismo o reenviar al admin"). Las tres llaves —es suya, tiene
    el permiso, no supera su tope de deuda— viven en el servicio; acá sólo
    se traduce el resultado a la pantalla.
    """
    from servicios.solicitudes_guia import emitir_guia_como_cliente

    resultado = emitir_guia_como_cliente(solicitud_id, cliente)
    if resultado.get("ok"):
        from servicios.auditoria import registrar_desde_request
        registrar_desde_request(request, event="portal.emitir_guia", actor_type="cliente",
                                actor_ref=cliente, metadata={"solicitud_id": solicitud_id})
        return RedirectResponse(url=f"/portal/envios/{solicitud_id}?ok=guia",
                                status_code=303)
    return RedirectResponse(
        url=f"/portal/envios/{solicitud_id}?error={quote(str(resultado.get('error') or 'No se pudo emitir'))}",
        status_code=303)


# ── Mi tienda (Shopify / Tiendanube) ────────────────────────

@router.get("/tienda", response_class=HTMLResponse)
def tienda_view(
    request: Request,
    ok: Optional[str] = None,
    error: Optional[str] = None,
    cliente: str = Depends(cliente_actual),
):
    # Detrás del proxy de Railway, request.base_url viene en http:// —
    # y una URL de webhook en http no sirve: Shopify exige https.
    base_url = (BASE_URL or str(request.base_url)).rstrip("/")
    if base_url.startswith("http://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
        base_url = "https://" + base_url[len("http://"):]
    tiendas = [t for t in listar_tiendas(cliente) if t["activa"]]
    # Tiendas sin dueño: SÓLO las que este cliente puede probar que son
    # suyas. Antes se listaban TODAS a TODOS — un cliente veía el dominio
    # myshopify de otro comercio y podía reclamarlo. Ahora se filtra contra
    # la verificación real (mail de la cuenta Shopify = mail del cliente),
    # así que la lista queda vacía salvo para su verdadero dueño.
    huerfanas = []
    try:
        from servicios.shopify_app import es_dueno_de_la_tienda, instalaciones_sin_dueno
        for h in instalaciones_sin_dueno():
            try:
                if es_dueno_de_la_tienda(h["dominio"], cliente):
                    huerfanas.append(h)
            except Exception as e:
                print(f"[portal] no pude verificar {h.get('dominio')}: {e}")
    except Exception as e:
        print(f"[portal] no pude listar instalaciones sin dueño: {e}")
    # La política de flete se configura por tienda; hoy mostramos la de la
    # primera (el caso normal es una tienda por cuenta).
    dominio_cfg = tiendas[0]["dominio"] if tiendas else ""
    try:
        from servicios.tiendanube_app import app_configurada as _tn_ok
        tiendanube_activa = _tn_ok()
    except Exception:
        tiendanube_activa = False
    return templates.TemplateResponse(
        request=request, name="portal/tienda.html",
        context={
            "cliente": cliente,
            "tiendas": tiendas,
            "pedidos": listar_pedidos(cliente, "PENDIENTE"),
            "convertidos": listar_pedidos(cliente, "CONVERTIDO", limite=10),
            "webhook_url": f"{base_url}/integraciones/shopify/webhook",
            "dominio_cfg": dominio_cfg,
            "cfg": obtener_config(dominio_cfg),
            "huerfanas": huerfanas,
            "tiendanube_activa": tiendanube_activa,
            "flash_ok": ok,
            "flash_error": error,
        },
    )


@router.post("/tienda/reclamar")
def tienda_reclamar(
    request: Request,
    dominio: str = Form(...),
    cliente: str = Depends(cliente_actual),
):
    """
    El comerciante dice "esta tienda es mía" — y ahora hay que probarlo.

    AGUJERO CERRADO (03/08): esto sólo verificaba que el dominio estuviera
    en la lista de tiendas sin vincular, lista que además se le mostraba a
    TODOS los clientes. Cualquiera podía reclamar la tienda de otro y
    quedarse con sus ventas y con los datos personales de sus compradores.
    Ahora se le pregunta a la propia tienda Shopify quién es su dueño.
    """
    from servicios.shopify_app import (
        es_dueno_de_la_tienda, instalaciones_sin_dueno, vincular_cliente,
    )

    dominio = dominio.strip().lower()
    if dominio not in {h["dominio"] for h in instalaciones_sin_dueno()}:
        return RedirectResponse(
            url="/portal/tienda?error=Esa+tienda+ya+esta+vinculada+a+una+cuenta.",
            status_code=303,
        )

    from servicios.auditoria import registrar_desde_request
    if not es_dueno_de_la_tienda(dominio, cliente):
        print(f"[portal] {cliente} intentó reclamar {dominio} sin ser el dueño")
        # Un intento fallido de reclamar una tienda ajena es exactamente la
        # señal que este registro existe para capturar.
        registrar_desde_request(request, event="portal.reclamar_tienda", actor_type="cliente",
                                actor_ref=cliente, success=False,
                                metadata={"dominio": dominio})
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "No pudimos verificar que esa tienda sea tuya. El mail de tu "
                "cuenta Shopify tiene que ser el mismo que el de tu cuenta "
                "TAURO. Si no coincide, escribinos y la vinculamos nosotros."),
            status_code=303,
        )

    vincular_cliente(dominio, cliente)
    registrar_desde_request(request, event="portal.reclamar_tienda", actor_type="cliente",
                            actor_ref=cliente, success=True, metadata={"dominio": dominio})
    return RedirectResponse(url="/portal/tienda?ok=conectada", status_code=303)


@router.post("/tienda/politica")
def tienda_politica(
    dominio: str = Form(...),
    politica: str = Form("real"),
    markup_pct: str = Form("0"),
    precio_fijo_ars: str = Form("0"),
    mostrar_tax: Optional[str] = Form(None),
    tax_pct_default: str = Form("0"),
    etiqueta: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    """Guarda qué precio de envío ve el comprador en el checkout."""
    # La tienda tiene que ser de este cliente: nadie configura la ajena.
    if dominio.strip().lower() not in {t["dominio"] for t in listar_tiendas(cliente)}:
        return RedirectResponse(url="/portal/tienda?error=Esa+tienda+no+es+tuya.", status_code=303)

    def _num(v: str) -> float:
        try:
            return float(str(v).replace(",", ".").strip() or 0)
        except ValueError:
            return 0.0

    r = guardar_config(
        dominio=dominio, cliente_id=cliente, politica=politica,
        markup_pct=_num(markup_pct), precio_fijo_ars=_num(precio_fijo_ars),
        mostrar_tax=bool(mostrar_tax), tax_pct_default=_num(tax_pct_default),
        etiqueta=etiqueta,
    )
    if not r.get("ok"):
        return RedirectResponse(url=f"/portal/tienda?error={quote(r.get('error', 'No se pudo guardar.'))}", status_code=303)
    return RedirectResponse(url="/portal/tienda?ok=politica", status_code=303)


@router.post("/tienda/conectar")
def tienda_conectar(
    plataforma: str = Form(...),
    dominio: str = Form(...),
    secreto: str = Form(...),
    cliente: str = Depends(cliente_actual),
):
    r = conectar_tienda(cliente, plataforma, dominio, secreto)
    if not r.get("ok"):
        return RedirectResponse(url=f"/portal/tienda?error={quote(r.get('error', 'No se pudo conectar.'))}", status_code=303)
    return RedirectResponse(url="/portal/tienda?ok=conectada", status_code=303)


@router.get("/tienda/tiendanube/instalar")
def tienda_tiendanube_instalar(cliente: str = Depends(cliente_actual)):
    """
    Arranca el OAuth de Tiendanube. Genera un `state` que viaja en cookie: a la
    vuelta, el callback sólo ata la tienda a esta cuenta si el navegador trae
    ese state (prueba de que el flujo empezó ACÁ, desde el botón del portal, y
    no de un callback disparado por un tercero). Sin app configurada, vuelve
    al portal con aviso.
    """
    from servicios.tiendanube_app import app_configurada, url_instalacion
    if not app_configurada():
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "La app de Tiendanube todavía no está habilitada. Escribinos y la activamos."),
            status_code=303)
    import secrets as _secrets
    state = _secrets.token_urlsafe(24)
    resp = RedirectResponse(url=url_instalacion(state), status_code=303)
    # El state se ata al cliente: cookie httponly/secure, 10 min, y guarda a
    # quién vincular cuando Tiendanube devuelva el code.
    resp.set_cookie(
        key="tn_oauth", value=f"{state}:{cliente}",
        httponly=True, max_age=600, samesite="lax", secure=COOKIE_SECURE,
    )
    return resp


@router.post("/tienda/desconectar")
def tienda_desconectar(
    tienda_id: int = Form(...),
    cliente: str = Depends(cliente_actual),
):
    desconectar_tienda(cliente, tienda_id)
    return RedirectResponse(url="/portal/tienda?ok=desconectada", status_code=303)


@router.post("/tienda/pedidos/descartar")
def tienda_pedido_descartar(
    pedido_id: int = Form(...),
    cliente: str = Depends(cliente_actual),
):
    descartar_pedido(cliente, pedido_id)
    return RedirectResponse(url="/portal/tienda?ok=descartado", status_code=303)


# ── Direcciones ─────────────────────────────────────────────
@router.get("/direcciones", response_class=HTMLResponse)
def direcciones_view(
    request: Request,
    ok: Optional[str] = None,
    error: Optional[str] = None,
    cliente: str = Depends(cliente_actual),
):
    flash_ok = None
    if ok == "1":
        flash_ok = "Dirección guardada."
    elif ok == "2":
        flash_ok = "Dirección eliminada."
    return templates.TemplateResponse(
        request=request, name="portal/direcciones.html",
        context={
            "cliente": cliente,
            "remitente": obtener_remitente_para_envio(cliente),
            "remitentes": listar_direcciones(cliente, TIPO_REMITENTE),
            "destinatarios": listar_direcciones(cliente, TIPO_DESTINATARIO),
            "flash_ok": flash_ok,
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
    direccion_id: str = Form(""),  # presente = editar en vez de crear
    cliente: str = Depends(cliente_actual),
):
    campos = dict(
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
    try:
        dir_id = _id_opt(direccion_id)
        if dir_id:
            actualizado = actualizar_direccion(dir_id, **campos)
            if not actualizado:
                raise ValueError("Esa dirección no existe o no es tuya.")
        else:
            crear_direccion(**campos)
    except Exception as e:
        return RedirectResponse(url=f"/portal/direcciones?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url="/portal/direcciones?ok=1", status_code=303)


@router.post("/direcciones/{direccion_id}/eliminar")
def direcciones_delete(
    direccion_id: int,
    cliente: str = Depends(cliente_actual),
):
    try:
        if not eliminar_direccion(cliente, direccion_id):
            raise ValueError("Esa dirección no existe o no es tuya.")
    except Exception as e:
        return RedirectResponse(url=f"/portal/direcciones?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url="/portal/direcciones?ok=2", status_code=303)


# ── Catálogo ────────────────────────────────────────────────
@router.get("/catalogo", response_class=HTMLResponse)
def catalogo_view(request: Request, cliente: str = Depends(cliente_actual)):
    productos = get_productos(cliente, solo_activos=False)
    try:
        taxes = tax_de_productos(cliente)
    except Exception as e:
        print(f"[catalogo] no pude leer los tax: {e}")
        taxes = {}
    return templates.TemplateResponse(
        request=request, name="portal/catalogo.html",
        context={"cliente": cliente, "productos": productos, "taxes": taxes,
                 # Para la columna "costo de envío por unidad": el JS cotiza
                 # cada producto contra el destino elegido, con el pricing
                 # del cliente ya aplicado (/portal/api/precio).
                 "paises_destino": _paises_con_nacional()},
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
    tax_estimado_usd: str = Form("0"),
    alias_original: str = Form(""),  # presente = editar en vez de crear
    cliente: str = Depends(cliente_actual),
):
    try:
        nuevo = ProductoNuevo(
            alias_interno=alias_interno, nombre_invoice=nombre_invoice,
            hs_code=hs_code, largo_cm=largo_cm, ancho_cm=ancho_cm,
            alto_cm=alto_cm, peso_kg=peso_kg, valor_usd_default=valor_usd_default,
        )
        if alias_original.strip():
            if not actualizar_producto_cliente(cliente, alias_original, nuevo):
                raise ValueError("Ese producto no existe en tu catálogo.")
        else:
            agregar_producto(cliente, nuevo)
        # Campo opcional, guardado aparte para no tocar el alta del catálogo.
        try:
            guardar_tax_producto(cliente, alias_interno,
                                 float(str(tax_estimado_usd).replace(",", ".") or 0))
        except Exception as e:
            print(f"[catalogo] no pude guardar el tax de {alias_interno}: {e}")
    except Exception as e:
        return RedirectResponse(url=f"/portal/catalogo?error={e}", status_code=303)
    return RedirectResponse(url="/portal/catalogo?ok=1", status_code=303)


@router.post("/catalogo/eliminar")
def catalogo_delete(
    alias_interno: str = Form(...),
    cliente: str = Depends(cliente_actual),
):
    try:
        if not eliminar_producto_cliente(cliente, alias_interno):
            raise ValueError("Ese producto no existe en tu catálogo.")
    except Exception as e:
        return RedirectResponse(url=f"/portal/catalogo?error={e}", status_code=303)
    return RedirectResponse(url="/portal/catalogo?ok=2", status_code=303)
