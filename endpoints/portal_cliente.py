# ============================================================
# Endpoints del Portal del Cliente
# ============================================================
# /portal/login           - Login por email/ID + contraseña
# /portal/password/forgot - Solicita restablecimiento por email
# /portal/password/reset  - Valida link y guarda contraseña nueva
# /portal/auth?token=X    - Canjea magic links antiguos aún vigentes
# /portal/logout          - Cierra sesión
# /portal/home            - Saldo + últimos envíos (requiere auth)
# /portal/cotizar         - Form de cotización (GET) + ejecuta (POST)
# /portal/envios          - Solicitudes de guía del cliente
# /portal/catalogo        - Productos del cliente (GET) + agregar (POST)
# ============================================================

import hashlib
import os
import re
import secrets
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote, urlencode, urlparse

from core.database import get_conn
from typing import Optional
from fastapi import APIRouter, Request, Form, Cookie, HTTPException, Depends
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.templating import Jinja2Templates

from servicios.auth import (
    generar_token, validar_token, revocar_token,
    autenticar_cliente, consumir_magic_token,
    buscar_cliente_para_password_reset, consumir_password_reset_token,
    password_reset_token_valido,
    validar_nueva_password,
)
from servicios.password_reset_queue import (
    encolar_password_reset,
    uniformar_password_reset_inexistente,
)
from servicios.rate_limit import check_rate, reset_rate, client_ip
from servicios.catalogo import (
    get_productos, get_producto, listar_stock_cliente, agregar_producto,
    actualizar_producto_cliente, eliminar_producto_cliente,
)
from servicios.cotizador import cotizar_referencia_couriers
from servicios.cotizador_nacional import preparar_cotizacion_nacional
from servicios.cuenta_corriente import (
    saldo, total_pagado, get_facturado_real, get_facturas_recientes,
    movimientos, resumir_facturacion, resumen_cuenta_por_ambito,
    movimientos_cuenta_paginados, listar_destinos_pago,
)
from servicios.facturacion_clientes import (
    get_factura_cliente_pdf,
    listar_facturas_cliente,
    listar_partidas_facturables,
)
from servicios.api_b2b import (
    obtener_precio_envio, obtener_precio_envio_multi, cotizar_couriers_cliente,
)
from servicios.solicitudes_guia import (
    crear_solicitud_guia, listar_solicitudes_cliente,
    preparar_documentos_envio_portal,
    obtener_factura_comercial_pdf,
    obtener_solicitud_de_cliente, contar_guias_listas,
    idempotency_hash_origen_tienda,
    validar_reemision_cliente, validar_cancelacion_cliente,
    cancelar_solicitud_cliente, periodos_solicitudes_cliente,
)
from servicios.periodos_envios import normalizar_periodo
from servicios.carriers import courier_default_cliente
from servicios.carrier_contract import Ambito, public_catalog
from servicios.impuestos import normalizar as normalizar_tax, tax_paga_cliente
from servicios.numeros_humanos import (
    parse_entero_formulario as _entero_form,
    parse_float_formulario as _numero_form,
    parse_importe_humano,
)
from servicios.paises import normalizar as normalizar_pais
from servicios.provincias import opciones as opciones_provincias
from servicios.panel_cliente import embudo_envios, preparar_historial_envios
from servicios.integraciones_tienda import (
    conectar_tienda, listar_tiendas, desconectar_tienda,
    reiniciar_integracion_shopify_cliente,
    limpiar_espejo_shopify_huerfano_cliente,
    listar_pedidos, listar_pedidos_tiendanube_seleccionados,
    contar_pendientes, obtener_pedido,
    marcar_convertido, descartar_pedido,
)
from servicios.politica_envio import (
    obtener_config, guardar_config, guardar_tax_producto, tax_de_productos,
)
from servicios.direcciones import (
    TIPO_DESTINATARIO,
    TIPO_REMITENTE,
    actualizar_direccion,
    crear_direccion,
    eliminar_direccion,
    listar_direcciones,
    obtener_direccion,
    obtener_remitente_para_envio,
)
from modelos.producto import ProductoNuevo

router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="templates")

# Helpers de courier disponibles en TODOS los templates del portal: la URL
# de tracking y la división nacional/internacional salen de un solo lugar.
from servicios.couriers_urls import ambito_envio, es_nacional, nombre_courier, url_tracking
from servicios.presentacion import dinero_ars, numero_ars
templates.env.globals["url_tracking"] = url_tracking
templates.env.globals["es_nacional"] = es_nacional
templates.env.globals["ambito_envio"] = ambito_envio
templates.env.globals["nombre_courier"] = nombre_courier
templates.env.globals["dinero_ars"] = dinero_ars
templates.env.globals["numero_ars"] = numero_ars


AMBITOS_PORTAL = {"nacional", "internacional"}
AMBITOS_CUENTA = {"consolidado", "nacional", "internacional"}
TIPOS_MOVIMIENTO_CUENTA = {
    "todos", "cargos", "pagos", "diferencias", "revision",
}
# Mantiene el resumen y una página completa de movimientos dentro del viewport
# de escritorio; el resto queda accesible con paginación explícita.
MOVIMIENTOS_CUENTA_POR_PAGINA = 10
_IDEMPOTENCY_KEY_MIN_LEN = 32
_IDEMPOTENCY_KEY_MAX_LEN = 128
_IDEMPOTENCY_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _operadores_cliente(cliente: str, ambito: Ambito) -> tuple[dict, ...]:
    """Catálogo efectivo; ante error nunca promete que un courier funciona."""
    try:
        from servicios.configuracion_couriers_cliente import catalogo_cliente

        return catalogo_cliente(cliente, ambito)
    except Exception as exc:
        print(
            f"[portal-operadores] catálogo no disponible: {type(exc).__name__}"
        )
        return public_catalog(ambito)


def _nueva_idempotency_key() -> str:
    """Token opaco por render; sólo deduplica, nunca identifica al cliente."""
    return secrets.token_urlsafe(32)


def _idempotency_key_form(valor) -> str:
    """Valida el token del formulario sin usarlo para autorización u ownership."""
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("Falta la clave de operación del formulario. Recargá la página.")
    clave = valor.strip()
    if (
        not (_IDEMPOTENCY_KEY_MIN_LEN <= len(clave) <= _IDEMPOTENCY_KEY_MAX_LEN)
        or any(caracter not in _IDEMPOTENCY_KEY_CHARS for caracter in clave)
    ):
        raise ValueError("La clave de operación del formulario no es válida. Recargá la página.")
    return clave


def _ambito_portal(valor) -> str:
    """Normaliza el alcance elegido sin aceptar valores inventados."""
    if not isinstance(valor, str):
        return ""
    normalizado = valor.strip().lower()
    return normalizado if normalizado in AMBITOS_PORTAL else ""


def _ambito_cuenta(valor) -> str:
    """Ámbito contable visible; un valor manipulado vuelve al total seguro."""
    if not isinstance(valor, str):
        return "consolidado"
    normalizado = valor.strip().lower()
    return normalizado if normalizado in AMBITOS_CUENTA else "consolidado"


def _tipo_movimiento_cuenta(valor) -> str:
    """Filtro cerrado para no pasar valores arbitrarios a la consulta."""
    if not isinstance(valor, str):
        return "todos"
    normalizado = valor.strip().lower()
    return normalizado if normalizado in TIPOS_MOVIMIENTO_CUENTA else "todos"


def _pagina_cuenta(valor) -> int:
    """Una query mal formada no debe convertir la cuenta en un error 422."""
    try:
        return max(1, int(valor))
    except (TypeError, ValueError):
        return 1


def _importe_cuenta_form(valor, campo: str, *, minimo: Decimal) -> Decimal:
    """Dinero localizado para la cuenta, sin pasar por punto flotante."""
    try:
        monto = parse_importe_humano(valor)
    except ValueError:
        raise ValueError(
            f"{campo}: ingresá un número válido, por ejemplo 100.000 o 100,000."
        ) from None
    if monto is None:
        raise ValueError(f"{campo}: completá este valor.")
    if monto < minimo:
        raise ValueError(f"{campo}: el mínimo es {minimo}.")
    if monto.as_tuple().exponent < -2:
        raise ValueError(f"{campo}: usá como máximo dos decimales.")
    return monto.quantize(Decimal("0.01"))


def _ambito_post(valor) -> str:
    """Compatibilidad con invocaciones directas fuera de FastAPI.

    En una llamada Python el default ``Form`` llega como metadata, mientras
    que por HTTP FastAPI entrega el string. Los forms viejos sin el hidden
    pertenecen al único flujo que existía entonces: internacional.
    """
    if not isinstance(valor, str):
        return "internacional"
    return _ambito_portal(valor)


def _url_envio_por_ambito(ambito: str, **valores) -> str:
    """Conserva el prellenado al pasar por el selector Nacional/Internacional."""
    params = {"ambito": ambito}
    for clave, valor in valores.items():
        if valor not in (None, ""):
            params[clave] = valor
    return "/portal/envios/nuevo?" + urlencode(params)

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
        print(f"[portal] no pude contar pendientes: {type(e).__name__}")
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
        print(f"[portal] no pude calcular el saldo: {type(e).__name__}")
        return None


templates.env.globals["saldo_menu"] = _saldo_menu

BASE_URL = os.getenv("BASE_URL")
# Cookies con Secure por defecto (Railway sirve por HTTPS). Apagar solo para
# desarrollo local por HTTP con SESSION_COOKIE_SECURE=0.
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"
SESSION_DAYS_INT = 7  # idéntico a SESSION_DAYS en servicios.auth
PASSWORD_RESET_MENSAJE = (
    "Si la cuenta existe y el correo pudo enviarse, vas a recibir un link "
    "para crear una nueva contraseña. Revisá también Spam o Promociones."
)


def _password_reset_base_url() -> str:
    """El link sensible sólo puede apuntar a un dominio oficial por HTTPS."""
    raw = (BASE_URL or "https://taurosolutions.ar").strip().rstrip("/")
    parsed = urlparse(raw)
    try:
        port = parsed.port
    except ValueError:
        return "https://taurosolutions.ar"
    if (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in {
            "taurosolutions.ar", "www.taurosolutions.ar",
        }
        and not parsed.username
        and not parsed.password
        and port in (None, 443)
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    ):
        return raw
    return "https://taurosolutions.ar"


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
_QUOTE_ID_PORTAL_RE = re.compile(r"^Q-[A-Za-z0-9_-]{20,64}$")


def _quote_id_portal(valor: str) -> str:
    if not isinstance(valor, str):
        return ""
    valor = (valor or "").strip()
    return valor if _QUOTE_ID_PORTAL_RE.fullmatch(valor) else ""


def _destino_post_login(quote_id: str) -> str:
    """Único retorno especial permitido: un snapshot vigente de TAURO."""
    quote_id = _quote_id_portal(quote_id)
    if not quote_id:
        return "/portal/home"
    try:
        from servicios.leads import obtener_cotizacion
        existe = obtener_cotizacion(quote_id, exigir_vigente=True)
    except Exception:
        existe = None
    if not existe:
        return "/portal/home"
    return (
        "/portal/envios/nuevo?ambito=internacional&quote_id="
        + quote(quote_id, safe="")
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    token: Optional[str] = Cookie(None),
    password_reset: Optional[str] = None,
    quote_id: str = "",
):
    # Si ya hay sesión activa, directo al portal — así el botón
    # "Iniciar sesión" de la web abre el escritorio sin fricción.
    if token and validar_token(token):
        return RedirectResponse(url=_destino_post_login(quote_id), status_code=303)
    quote_id = _quote_id_portal(quote_id)
    return templates.TemplateResponse(
        request=request, name="portal/login.html",
        context={
            "mensaje": (
                "Contraseña actualizada. Ya podés ingresar con la nueva."
                if password_reset == "ok" else None
            ),
            "tipo_msg": "ok" if password_reset == "ok" else None,
            "quote_id": quote_id,
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    quote_id: str = Form(""),
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
                "quote_id": _quote_id_portal(quote_id),
            },
            status_code=429,
        )
    auth = autenticar_cliente(email, password)
    if auth and auth.get("sin_password"):
        # Cuenta recién creada sin contraseña: el mismo flujo de recupero
        # permite crearla sin que Tauro tenga que conocerla.
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={
                "mensaje": "Tu cuenta todavía no tiene contraseña. Usá "
                           "«Restablecer contraseña» para crearla desde tu email.",
                "tipo_msg": "error",
                "email_prefill": email,
                "quote_id": _quote_id_portal(quote_id),
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
                "quote_id": _quote_id_portal(quote_id),
            },
            status_code=401,
        )

    reset_rate(f"portal_login:{ip}")
    from servicios.auditoria import registrar_desde_request
    registrar_desde_request(request, event="portal.login", actor_type="cliente",
                            actor_ref=auth["cliente_id"], success=True, status_code=303)
    # La sesión guarda el email real aunque hayan entrado con el ID.
    token = generar_token(auth["email"], auth["cliente_id"])
    response = RedirectResponse(url=_destino_post_login(quote_id), status_code=303)
    response.set_cookie(
        key="token", value=token,
        httponly=True, max_age=60 * 60 * 24 * SESSION_DAYS_INT,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


def _password_reset_rate_ref(identificador: str) -> str:
    """Clave irreversible sólo para rate-limit; nunca se persiste ni loguea."""
    normalizado = (identificador or "").strip().casefold()
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()


def _auditar_password_reset(request: Request, evento: str, *, success: bool,
                            status_code: int, estado: str) -> None:
    """Audita sólo estado operativo; jamás email, ID, token ni contraseña."""
    from servicios.auditoria import registrar_desde_request
    registrar_desde_request(
        request,
        event=evento,
        actor_type="anonimo",
        actor_ref=None,
        success=success,
        status_code=status_code,
        metadata={"estado": estado},
    )


@router.post("/password/forgot", response_class=HTMLResponse)
def password_forgot(
    request: Request,
    identificador: str = Form(...),
    quote_id: str = Form(""),
):
    """Solicita un restablecimiento sin revelar si la cuenta existe.

    La respuesta visible es la misma exista o no la cuenta. El request nunca
    crea tokens ni espera SMTP: sólo deja un trabajo durable para el worker.
    """
    quote_id = _quote_id_portal(quote_id)
    ip = client_ip(request)
    if not check_rate(f"password_forgot_ip:{ip}", max_attempts=5, window_seconds=3600):
        _auditar_password_reset(
            request, "portal.password_reset.request", success=False,
            status_code=429, estado="rate_ip",
        )
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={
                "mensaje": "Recibimos varios pedidos. Esperá unos minutos antes de intentar otra vez.",
                "tipo_msg": "error",
                "email_prefill": identificador,
                "quote_id": quote_id,
            },
            status_code=429,
        )

    rate_ref = _password_reset_rate_ref(identificador)
    if not check_rate(
        f"password_forgot_account:{rate_ref}", max_attempts=3, window_seconds=3600
    ):
        _auditar_password_reset(
            request, "portal.password_reset.request", success=False,
            status_code=200, estado="rate_account",
        )
        return templates.TemplateResponse(
            request=request, name="portal/login.html",
            context={
                "mensaje": PASSWORD_RESET_MENSAJE,
                "tipo_msg": "ok",
                "quote_id": quote_id,
            },
        )

    estado = "cuenta_no_encontrada"
    encolado = False
    try:
        cuenta = buscar_cliente_para_password_reset(identificador)
        if cuenta:
            resultado = encolar_password_reset(cuenta["cliente_id"], quote_id)
            encolado = resultado.accepted
            estado = resultado.code.lower()
        else:
            # Mismo número de operaciones DB que el camino real, sin guardar
            # el identificador ni crear un trabajo. Reduce el canal lateral de
            # tiempo sin introducir PII en la cola.
            uniformar_password_reset_inexistente(rate_ref)
    except Exception as exc:
        # Sólo el tipo ayuda a operar; no imprimir mensaje ni identificador.
        print(f"[password-reset] solicitud falló: {type(exc).__name__}")
        encolado = False
        estado = "error_interno"

    _auditar_password_reset(
        request, "portal.password_reset.request", success=encolado,
        status_code=200, estado=estado,
    )
    return templates.TemplateResponse(
        request=request, name="portal/login.html",
        context={
            "mensaje": PASSWORD_RESET_MENSAJE,
            "tipo_msg": "ok",
            "quote_id": quote_id,
        },
    )


def _respuesta_password_reset(
    request: Request,
    *,
    token: str = "",
    token_valido: bool,
    fragment_mode: bool = False,
    quote_id: str = "",
    mensaje: Optional[str] = None,
    status_code: int = 200,
):
    respuesta = templates.TemplateResponse(
        request=request,
        name="portal/password_reset.html",
        context={
            "token": token if token_valido else "",
            "token_valido": token_valido,
            "fragment_mode": fragment_mode,
            "quote_id": _quote_id_portal(quote_id),
            "mensaje": mensaje,
        },
        status_code=status_code,
    )
    # El secreto llega en el fragmento (que HTTP nunca recibe) y el JS lo
    # elimina del historial antes de mostrar el formulario.
    respuesta.headers["Referrer-Policy"] = "no-referrer"
    respuesta.headers["Cache-Control"] = "no-store, max-age=0"
    respuesta.headers["Pragma"] = "no-cache"
    return respuesta


@router.get("/password/reset", response_class=HTMLResponse)
def password_reset_form(request: Request):
    """Bootstrap sin secreto: el fragmento nunca llega al servidor/logs."""
    return _respuesta_password_reset(
        request, token="", token_valido=False, fragment_mode=True,
        mensaje="El link no es válido o ya venció.", status_code=200,
    )


@router.post("/password/reset", response_class=HTMLResponse)
def password_reset_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirmacion: str = Form(...),
    quote_id: str = Form(""),
):
    quote_id = _quote_id_portal(quote_id)
    ip = client_ip(request)
    if not check_rate(f"password_reset_submit:{ip}", max_attempts=8, window_seconds=900):
        _auditar_password_reset(
            request, "portal.password_reset.consume", success=False,
            status_code=429, estado="rate_ip",
        )
        return _respuesta_password_reset(
            request, token="", token_valido=False,
            mensaje="Demasiados intentos. Solicitá un link nuevo más tarde.",
            quote_id=quote_id,
            status_code=429,
        )

    try:
        token_valido = password_reset_token_valido(token)
    except Exception as exc:
        print(f"[password-reset] lectura falló: {type(exc).__name__}")
        token_valido = False
    if not token_valido:
        _auditar_password_reset(
            request, "portal.password_reset.consume", success=False,
            status_code=400, estado="token_invalido",
        )
        return _respuesta_password_reset(
            request, token="", token_valido=False,
            mensaje="El link no es válido o ya venció.", status_code=400,
            quote_id=quote_id,
        )

    error_password = validar_nueva_password(password, password_confirmacion)
    if error_password:
        _auditar_password_reset(
            request, "portal.password_reset.consume", success=False,
            status_code=400, estado="password_invalida",
        )
        return _respuesta_password_reset(
            request, token=token, token_valido=True,
            mensaje=error_password, status_code=400, quote_id=quote_id,
        )

    try:
        consumido = consumir_password_reset_token(token, password)
    except Exception as exc:
        print(f"[password-reset] canje falló: {type(exc).__name__}")
        consumido = False
        estado = "error_interno"
    else:
        estado = "completado" if consumido else "token_invalido"

    if not consumido:
        _auditar_password_reset(
            request, "portal.password_reset.consume", success=False,
            status_code=400, estado=estado,
        )
        return _respuesta_password_reset(
            request, token="", token_valido=False,
            mensaje="El link no es válido o ya venció.", status_code=400,
            quote_id=quote_id,
        )

    reset_rate(f"password_reset_submit:{ip}")
    _auditar_password_reset(
        request, "portal.password_reset.consume", success=True,
        status_code=303, estado="completado",
    )
    query = {"password_reset": "ok"}
    if quote_id:
        query["quote_id"] = quote_id
    response = RedirectResponse(
        url=f"/portal/login?{urlencode(query)}", status_code=303,
    )
    # Una contraseña nueva revoca todas las sesiones en DB; también limpiar
    # la cookie local evita que el navegador siga presentando el token viejo.
    response.delete_cookie("token")
    return response


@router.post("/login/send", response_class=HTMLResponse)
def login_send(request: Request, email: str = Form(...)):
    """Compatibilidad del formulario viejo, sin emitir nuevos magic-login.

    Cualquier cliente o marcador que conserve esta ruta recibe el mismo flujo
    real y anti-enumeración de restablecimiento.
    """
    return password_forgot(request, email, "")


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


# ── PWA del portal ──────────────────────────────────────────
# El portal instalable: manifest + service worker + pantalla offline.
# Las TRES rutas son públicas y no tocan datos de nadie. El service worker
# sólo puede precachear la pantalla offline neutra; todo lo autenticado es
# red-only y hereda el Cache-Control no-store del middleware de seguridad
# (main.headers_de_seguridad), que también cubre estas rutas nuevas.

_RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SW_PATH = os.path.join(_RAIZ_PROYECTO, "static", "js", "portal-sw.js")

# Cache-bust de los íconos: subir si se regeneran los PNG de static/img/pwa.
_PWA_ICONOS_V = "1"

# `id`, `scope` y `start_url` son la IDENTIDAD de la app instalada: si
# cambian, los teléfonos quedan con dos "TAURO" distintas. No moverlos.
MANIFEST_PORTAL = {
    "id": "/portal/",
    "name": "TAURO",
    "short_name": "TAURO",
    "description": (
        "Portal de clientes de Tauro Solutions: cotizá, emití y seguí "
        "tus envíos internacionales; organizá por separado los nacionales."
    ),
    "lang": "es-AR",
    "dir": "ltr",
    "start_url": "/portal/home",
    "scope": "/portal/",
    "display": "standalone",
    "background_color": "#0c0a14",
    "theme_color": "#0c0a14",
    "icons": [
        {
            "src": f"/static/img/pwa/icon-192.png?v={_PWA_ICONOS_V}",
            "sizes": "192x192", "type": "image/png", "purpose": "any",
        },
        {
            "src": f"/static/img/pwa/icon-512.png?v={_PWA_ICONOS_V}",
            "sizes": "512x512", "type": "image/png", "purpose": "any",
        },
        {
            "src": f"/static/img/pwa/icon-maskable-192.png?v={_PWA_ICONOS_V}",
            "sizes": "192x192", "type": "image/png", "purpose": "maskable",
        },
        {
            "src": f"/static/img/pwa/icon-maskable-512.png?v={_PWA_ICONOS_V}",
            "sizes": "512x512", "type": "image/png", "purpose": "maskable",
        },
    ],
}


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest_pwa():
    """Manifest de la app. Media type propio para que Chrome lo tome."""
    return JSONResponse(MANIFEST_PORTAL, media_type="application/manifest+json")


@router.get("/sw.js", include_in_schema=False)
def service_worker_portal():
    """
    El service worker se sirve DESDE /portal/sw.js para que su alcance
    máximo sea /portal/: nunca puede controlar el admin ni la web pública.
    El no-store del middleware hace que el navegador revise actualizaciones
    del worker en cada navegación, sin quedarse pegado a una versión vieja.
    """
    return FileResponse(_SW_PATH, media_type="application/javascript")


@router.get("/offline", response_class=HTMLResponse, include_in_schema=False)
def offline_pwa(request: Request):
    """
    Pantalla offline: pública y neutra A PROPÓSITO. Es lo único que el
    service worker precachea, así que no puede requerir sesión ni contener
    datos de clientes — instalarla no expone nada después de un logout.
    """
    return templates.TemplateResponse(
        request=request, name="portal/offline.html", context={},
    )


# ── Home ────────────────────────────────────────────────────
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, cliente: str = Depends(cliente_actual)):
    facturado = get_facturado_real(cliente)
    saldo_data = saldo(cliente, total_facturado_ars=facturado)
    # Se separa antes de limitar: tomar sólo las últimas 12 globales puede
    # ocultar nacionales más viejos y dibujar un falso "sin envíos".
    historial = listar_solicitudes_cliente(cliente, limite=None)
    solicitudes_nacionales = [
        solicitud for solicitud in historial
        if ambito_envio(solicitud) == "nacional"
    ][:3]
    solicitudes_internacionales = [
        solicitud for solicitud in historial
        if ambito_envio(solicitud) == "internacional"
    ][:3]

    return templates.TemplateResponse(
        request=request, name="portal/home.html",
        context={
            "cliente": cliente,
            "saldo": saldo_data,
            "solicitudes_nacionales": solicitudes_nacionales,
            "solicitudes_internacionales": solicitudes_internacionales,
            # Sólo alimenta recordatorios compactos de acciones reales.
            "embudo": embudo_envios(cliente),
        },
    )


# ── Rastreo: resuelve el courier antes de mandar afuera ─────
@router.get("/track")
def track_redirect(nro: str = "", cliente: str = Depends(cliente_actual)):
    """
    Resuelve el courier desde los envíos del cliente antes de abrir el
    seguimiento. Esto mantiene operables también los trackings nacionales
    históricos sin mezclar proveedores.
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
                      AND test=FALSE AND visible_cliente=TRUE
                    LIMIT 1
                """, (cliente, nro))
                fila = cur.fetchone()
                if fila and fila.get("courier"):
                    courier = fila["courier"]
    except Exception as e:
        print(f"[portal] track lookup falló: {type(e).__name__}; default FedEx")

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
        headers={
            "Content-Disposition": f'attachment; filename="TAURO_{cliente}_{fecha}.xlsx"',
            "Cache-Control": "private, no-store",
        },
    )


# ── Recolecciones ───────────────────────────────────────────
@router.get("/recolecciones", response_class=HTMLResponse)
def recolecciones_view(
    request: Request,
    envio: Optional[int] = None,
    cliente: str = Depends(cliente_actual),
):
    """
    Que el chofer pase a buscar, en vez de llevar los paquetes.

    Con ?envio=N el form llega desde "Mis envíos" (pedido de Leandro, 06/08:
    "la recolección tiene que programarse desde el perfil de todos los
    envíos") y se precargan courier, bultos y peso de ESA guía — el cliente
    coordina el retiro del envío que está mirando, no arranca de cero.
    """
    from servicios.recolecciones import (
        listar, datos_retiro_desde_solicitud, cliente_puede_recolectar,
    )
    from datetime import date, timedelta

    try:
        recolecciones = listar(cliente)
    except Exception as e:
        print(f"[portal] no pude listar recolecciones: {type(e).__name__}")
        recolecciones = []
    from servicios.configuracion_couriers_cliente import mapa_permisos
    permisos_pickup = mapa_permisos(cliente, "recolectar")
    couriers_recoleccion = [
        {"id": "FEDEX", "nombre": "FedEx"},
        {"id": "DHL", "nombre": "DHL Express"},
    ]
    couriers_recoleccion = [
        c for c in couriers_recoleccion
        if permisos_pickup.get(c["id"].lower(), False)
    ]

    envio_pre = None
    envio_pre_error = None
    if envio:
        s = obtener_solicitud_de_cliente(envio, cliente)
        # Sólo precarga si la guía existe: sin guía todavía no hay nada que
        # el chofer pueda llevarse.
        if s and s.get("tracking") and ambito_envio(s) == "internacional":
            try:
                envio_pre = datos_retiro_desde_solicitud(s)
            except ValueError as exc:
                envio_pre_error = (
                    f"No pudimos preparar el retiro de ese envío: {exc} "
                    "Corregí los datos de la guía o escribinos antes de agendar."
                )

    dhl_requiere_envio = bool(
        not envio_pre and permisos_pickup.get("dhl", False)
    )
    if not envio_pre:
        couriers_recoleccion = [
            c for c in couriers_recoleccion if c["id"] != "DHL"
        ]

    puede_recolectar = bool(couriers_recoleccion)
    if envio_pre:
        puede_recolectar = bool(
            permisos_pickup.get((envio_pre.get("courier") or "").lower(), False)
        )

    if envio_pre:
        origen = envio_pre["origen"]
        remitente = {
            "nombre": origen["nombre"], "direccion": origen["calle"],
            "ciudad": origen["ciudad"], "estado": origen["estado"],
            "cp": origen["zip"], "pais": origen["pais"],
        }
    else:
        remitente = obtener_remitente_para_envio(cliente, None)
    # Mañana por defecto (hoy suele estar pasado el corte); si cae finde,
    # el lunes — así el form abre con una fecha que el courier acepta.
    sugerida = date.today() + timedelta(days=1)
    while sugerida.weekday() >= 5:
        sugerida += timedelta(days=1)

    return templates.TemplateResponse(
        request=request, name="portal/recolecciones.html",
        context={"cliente": cliente, "recolecciones": recolecciones,
                 "remitente": remitente, "envio_pre": envio_pre,
                 "envio_pre_error": envio_pre_error,
                 "puede_recolectar": puede_recolectar,
                 "dhl_requiere_envio": dhl_requiere_envio,
                 "couriers_recoleccion": couriers_recoleccion,
                 "courier_default": (courier_default_cliente(cliente) or "fedex").upper(),
                 "fecha_sugerida": sugerida.strftime("%Y-%m-%d"),
                 "fecha_max": (date.today() + timedelta(days=14)).strftime("%Y-%m-%d")},
    )


@router.post("/recolecciones/nueva")
def recoleccion_nueva(
    fecha: str = Form(...),
    ready_time: str = Form("09:00"),
    close_time: str = Form("17:00"),
    bultos: str = Form("1"),
    peso_kg: str = Form("1"),
    instrucciones: str = Form(""),
    courier: str = Form("FEDEX"),
    solicitud_id: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    from servicios.recolecciones import crear

    try:
        bultos_num = _entero_form(bultos, "Cantidad de bultos", minimo=1, maximo=20)
        solicitud_id_num = _entero_form(
            solicitud_id, "Envío seleccionado", requerido=False, minimo=1
        )
        peso = _numero_form(peso_kg, "Peso total", minimo=0.001, maximo=1400)
        r = crear(cliente, fecha, ready_time, close_time, bultos_num,
                  peso, instrucciones,
                  courier=courier, solicitud_id=solicitud_id_num)
    except ValueError as e:
        print("[portal] datos inválidos al agendar recolección")
        r = {"ok": False, "error": str(e)}
    except Exception as e:
        print(f"[portal] error agendando recolección: {type(e).__name__}")
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
def cuenta_corriente(
    request: Request,
    ambito: str = "consolidado",
    tipo: str = "todos",
    pagina: str = "1",
    vista: str = "movimientos",
    cliente: str = Depends(cliente_actual),
):
    """
    Timeline completo de facturas y pagos. La spec lo pide explícito: el
    cliente administra su cuenta corriente con TAURO desde el portal, no
    preguntando el saldo por WhatsApp.
    """
    ambito = _ambito_cuenta(ambito)
    tipo = _tipo_movimiento_cuenta(tipo)
    pagina_numero = _pagina_cuenta(pagina)
    vista = str(vista or "movimientos").strip().lower()
    if vista not in {"movimientos", "facturas", "pendientes"}:
        vista = "movimientos"

    # Las dos consultas reciben exclusivamente el cliente autenticado. Ningún
    # query param o campo del form puede elegir la cuenta de otra persona.
    resumen = resumen_cuenta_por_ambito(cliente)
    movs = movimientos_cuenta_paginados(
        cliente, ambito, tipo, pagina_numero, MOVIMIENTOS_CUENTA_POR_PAGINA
    )
    consolidado = resumen["consolidado"]
    # Compatibilidad con el saldo del menú lateral: reutiliza el total ya
    # calculado y evita otra consulta a la base.
    saldo_data = {
        "facturado_ars": consolidado["debe_ars"],
        "pagado_ars": consolidado["haber_ars"],
        "saldo_pendiente_ars": consolidado["saldo_ars"],
    }
    facturas_cliente = (
        listar_facturas_cliente(cliente) if vista == "facturas" else []
    )
    pendientes_facturar = []
    if vista == "pendientes":
        for ambito_partida in ("NACIONAL", "INTERNACIONAL"):
            pendientes_facturar.extend(listar_partidas_facturables(
                cliente, tipo="FC", ambito=ambito_partida,
            ))
    try:
        destinos_pago = listar_destinos_pago(cliente)
    except RuntimeError:
        # Mantiene renderizable la página durante readiness/migración; el
        # saldo principal ya tiene su propio circuito de diagnóstico.
        destinos_pago = []

    return templates.TemplateResponse(
        request=request, name="portal/cuenta.html",
        context={
            "cliente": cliente,
            "saldo": saldo_data,
            "movimientos": movs,
            "resumen_cuenta": resumen,
            "ambito_filtro": ambito,
            "tipo_filtro": tipo,
            "vista_cuenta": vista,
            "facturas_cliente": facturas_cliente,
            "pendientes_facturar": pendientes_facturar,
            "destinos_pago": destinos_pago,
            "today": datetime.now().strftime("%Y-%m-%d"),
            "idempotency_key": _nueva_idempotency_key(),
        },
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
    volver_ambito = _ambito_cuenta(form.get("volver_ambito"))
    try:
        idempotency_key = _idempotency_key_form(form.get("idempotency_key"))
        monto = _importe_cuenta_form(
            form.get("monto"), "Monto", minimo=Decimal("0.01")
        )

        destinos = None
        aplicaciones = {}
        if str(form.get("seleccion_documental") or "") == "1":
            destinos = list(form.getlist("destinos"))
        else:
            # Compatibilidad con formularios abiertos antes del cambio.
            destino = str(
                form.get("destino_pago") or "SIN_IMPUTAR"
            ).strip().upper()
            if destino not in {
                "SIN_IMPUTAR", "NACIONAL", "INTERNACIONAL", "DIVIDIR"
            }:
                raise ValueError("Elegí un destino válido para el pago.")
            if destino in {"NACIONAL", "INTERNACIONAL"}:
                aplicaciones[destino] = monto
            elif destino == "DIVIDIR":
                monto_nacional = _importe_cuenta_form(
                    form.get("monto_nacional") or "0", "Monto para Nacional",
                    minimo=Decimal("0"),
                )
                monto_internacional = _importe_cuenta_form(
                    form.get("monto_internacional") or "0",
                    "Monto para Internacional", minimo=Decimal("0"),
                )
                if monto_nacional + monto_internacional <= 0:
                    raise ValueError("Indicá cuánto querés imputar.")
                if monto_nacional + monto_internacional > monto:
                    raise ValueError(
                        "La suma a imputar no puede superar el monto total del pago."
                    )
                if monto_nacional:
                    aplicaciones["NACIONAL"] = monto_nacional
                if monto_internacional:
                    aplicaciones["INTERNACIONAL"] = monto_internacional

        archivo = form.get("comprobante")
        contenido = await leer_comprobante_con_tope(archivo)
        if not contenido:
            raise ValueError("Falta el comprobante (foto o PDF).")

        registrar_pago(
            cliente_id=cliente,
            fecha=str(form.get("fecha") or datetime.now().strftime("%Y-%m-%d")),
            monto_ars=monto,
            metodo=str(form.get("metodo") or "transferencia")[:60],
            referencia=str(form.get("referencia") or "")[:120],
            nota="Informado por el cliente desde el portal",
            estado="PENDIENTE",
            comprobante=contenido,
            comprobante_nombre=getattr(archivo, "filename", "") or "",
            aplicaciones=aplicaciones,
            destinos=destinos,
            idempotency_key=idempotency_key,
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/portal/cuenta?ambito={volver_ambito}&error={quote(str(e))}",
            status_code=303,
        )
    except Exception as e:
        print(f"[portal] informar_pago falló: {type(e).__name__}")
        return RedirectResponse(
            url=(f"/portal/cuenta?ambito={volver_ambito}&error="
                 f"{quote('No pudimos guardar el pago. Probá de nuevo.') }"),
            status_code=303)
    return RedirectResponse(
        url=f"/portal/cuenta?ambito={volver_ambito}&ok=1", status_code=303
    )


@router.get("/facturas/{factura_id}/pdf")
def ver_factura_propia(factura_id: int, cliente: str = Depends(cliente_actual)):
    """Descarga una cabecera TAURO propia; ownership se valida en SQL."""
    from fastapi.responses import Response
    dato = get_factura_cliente_pdf(factura_id, cliente_id=cliente)
    if not dato:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    contenido, nombre = dato
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{nombre}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/facturas-legacy/{envio_id}/pdf")
def ver_factura_legacy_propia(
    envio_id: int,
    cliente: str = Depends(cliente_actual),
):
    """Fallback de sólo lectura para instalaciones con factura en ``envios``."""
    from servicios.cuenta_corriente import get_factura_pdf

    dato = get_factura_pdf(envio_id, cliente_id=cliente)
    if not dato:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    contenido, nombre = dato
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{nombre}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/pagos/{pago_id}/comprobante")
def ver_comprobante_propio(pago_id: int, cliente: str = Depends(cliente_actual)):
    """El cliente ve SUS comprobantes; los ajenos no existen para él (404)."""
    from fastapi.responses import Response

    from servicios.cuenta_corriente import get_comprobante

    dato = get_comprobante(pago_id, cliente_id=cliente)
    if not dato:
        raise HTTPException(status_code=404, detail="Comprobante no encontrado")
    contenido, tipo, nombre = dato
    return Response(
        content=contenido,
        media_type=tipo,
        headers={
            "Content-Disposition": f'inline; filename="{nombre}"',
            "Cache-Control": "private, no-store",
        },
    )


# ── Cotizar ─────────────────────────────────────────────────
def _bultos_cotizador_internacional(
    *,
    peso_kg: str,
    largo_cm: str,
    ancho_cm: str,
    alto_cm: str,
    cantidades,
    pesos,
    largos,
    anchos,
    altos,
) -> tuple[list[dict], list[dict]]:
    """Valida filas repetibles y conserva el texto para re-renderizar."""
    listas = [
        list(valor) if isinstance(valor, (list, tuple)) else []
        for valor in (cantidades, pesos, largos, anchos, altos)
    ]
    usa_filas = any(listas)
    if not usa_filas:
        listas = [["1"], [peso_kg], [largo_cm], [ancho_cm], [alto_cm]]
    longitudes = {len(lista) for lista in listas}
    if len(longitudes) != 1 or not next(iter(longitudes), 0):
        raise ValueError("Las cajas enviadas están incompletas. Volvé a cargarlas.")

    paquetes = []
    filas_form = []
    total_cajas = 0
    for indice, (cantidad, peso, largo, ancho, alto) in enumerate(zip(*listas), start=1):
        fila_form = {
            "cantidad": str(cantidad or ""),
            "peso_kg": str(peso or ""),
            "largo_cm": str(largo or ""),
            "ancho_cm": str(ancho or ""),
            "alto_cm": str(alto or ""),
        }
        filas_form.append(fila_form)
        cantidad_num = _entero_form(
            cantidad, f"Caja {indice}: cantidad", minimo=1, maximo=20,
        )
        paquete = {
            "cantidad": cantidad_num,
            "peso_kg": _numero_form(
                peso, f"Caja {indice}: peso", minimo=0.1, maximo=70,
            ),
            "largo_cm": _numero_form(largo, f"Caja {indice}: largo", minimo=1),
            "ancho_cm": _numero_form(ancho, f"Caja {indice}: ancho", minimo=1),
            "alto_cm": _numero_form(alto, f"Caja {indice}: alto", minimo=1),
        }
        if paquete["largo_cm"] + paquete["ancho_cm"] + paquete["alto_cm"] > 330:
            raise ValueError(
                f"Caja {indice}: la suma de las tres medidas no puede superar 330 cm."
            )
        total_cajas += cantidad_num
        if total_cajas > 20:
            raise ValueError("DHL admite como máximo 20 cajas por envío.")
        paquetes.append(paquete)
    return paquetes, filas_form


@router.get("/cotizar", response_class=HTMLResponse)
def cotizar_form(
    request: Request,
    ambito: str = "",
    cliente: str = Depends(cliente_actual),
):
    ambito = _ambito_portal(ambito)
    from servicios.cotizaciones_reseller import cliente_es_reseller
    es_reseller = cliente_es_reseller(cliente)
    return templates.TemplateResponse(
        request=request, name="portal/cotizar.html",
        context={
            "cliente": cliente,
            "ambito": ambito,
            "provincias": opciones_provincias(),
            "paises_origen": _paises_con_nacional(),
            "paises_destino": _paises_con_nacional(),
            "resultado": None,
            "opciones": None,
            "no_disponibles": [],
            "operadores_internacionales": _operadores_cliente(
                cliente, Ambito.INTERNACIONAL
            ),
            "resultado_nacional": None,
            "error": None,
            "form": {"bultos": [{}]},
            "es_reseller": es_reseller,
        },
    )


@router.post("/cotizar", response_class=HTMLResponse)
def cotizar_post(
    request: Request,
    ambito: str = Form("internacional"),
    # Son condicionalmente obligatorios: el formulario nacional deriva AR
    # de sus provincias y el internacional exige ambos países. Dejarlos con
    # default evita que FastAPI responda 422 antes de poder validar el ámbito.
    origen_pais: str = Form(""),
    destino_pais: str = Form(""),
    peso_kg: str = Form(""),
    largo_cm: str = Form(""),
    ancho_cm: str = Form(""),
    alto_cm: str = Form(""),
    valor_declarado_usd: str = Form(""),
    origen_provincia: str = Form(""),
    origen_localidad: str = Form(""),
    origen_cp: str = Form(""),
    modalidad_origen: str = Form("domicilio"),
    destino_provincia: str = Form(""),
    destino_localidad: str = Form(""),
    destino_cp: str = Form(""),
    modalidad_destino: str = Form("domicilio"),
    cantidad_bultos: str = Form("1"),
    valor_declarado_ars: str = Form(""),
    bulto_cantidad: list[str] = Form([]),
    bulto_peso: list[str] = Form([]),
    bulto_largo: list[str] = Form([]),
    bulto_ancho: list[str] = Form([]),
    bulto_alto: list[str] = Form([]),
    cliente: str = Depends(cliente_actual),
):
    ambito_normalizado = _ambito_post(ambito)
    if ambito_normalizado == "nacional":
        form_nacional = {
            "origen_provincia": origen_provincia,
            "origen_localidad": origen_localidad,
            "origen_cp": origen_cp,
            "modalidad_origen": modalidad_origen,
            "destino_provincia": destino_provincia,
            "destino_localidad": destino_localidad,
            "destino_cp": destino_cp,
            "modalidad_destino": modalidad_destino,
            "cantidad_bultos": cantidad_bultos,
            "valor_declarado_ars": valor_declarado_ars,
            "peso_kg": peso_kg,
            "largo_cm": largo_cm,
            "ancho_cm": ancho_cm,
            "alto_cm": alto_cm,
        }
        resultado_nacional = None
        error_nacional = None
        try:
            # Los hidden actuales mandan AR, pero un POST viejo puede no
            # traerlos. En ambos casos la ruta nacional se deriva como AR→AR;
            # si alguien fuerza otro país, se rechaza en vez de reclasificarlo.
            origen_iso = normalizar_pais(origen_pais) if origen_pais else "AR"
            destino_iso = normalizar_pais(destino_pais) if destino_pais else "AR"
            if origen_iso != "AR" or destino_iso != "AR":
                raise ValueError(
                    "El cotizador nacional sólo admite origen y destino dentro de Argentina."
                )
            resultado_nacional = preparar_cotizacion_nacional(
                origen_provincia=origen_provincia,
                origen_localidad=origen_localidad,
                origen_cp=origen_cp,
                modalidad_origen=modalidad_origen,
                destino_provincia=destino_provincia,
                destino_localidad=destino_localidad,
                destino_cp=destino_cp,
                modalidad_destino=modalidad_destino,
                cantidad_bultos=cantidad_bultos,
                peso_kg=peso_kg,
                largo_cm=largo_cm,
                ancho_cm=ancho_cm,
                alto_cm=alto_cm,
                valor_declarado_ars=valor_declarado_ars,
            )
            print(
                f"[portal-cotizar-nacional] cliente={cliente} "
                f"ruta={resultado_nacional['origen']['provincia_codigo']}->"
                f"{resultado_nacional['destino']['provincia_codigo']} "
                "lista_para_adapters"
            )
        except ValueError as exc:
            error_nacional = str(exc)

        return templates.TemplateResponse(
            request=request,
            name="portal/cotizar.html",
            context={
                "cliente": cliente,
                "ambito": "nacional",
                "provincias": opciones_provincias(),
                "resultado_nacional": resultado_nacional,
                "error": error_nacional,
                "form": form_nacional,
            },
        )

    # Un ámbito manipulado no puede caer accidentalmente en los carriers
    # internacionales: vuelve al selector sin consultar ninguna API.
    if ambito_normalizado != "internacional":
        return RedirectResponse(url="/portal/cotizar", status_code=303)

    error = None
    opciones = None
    no_disponibles = []
    resumen = None
    paquetes = []
    from servicios.cotizaciones_reseller import cliente_es_reseller, guardar_opciones
    es_reseller = cliente_es_reseller(cliente)
    filas_bultos_form = [{
        "cantidad": "1", "peso_kg": peso_kg, "largo_cm": largo_cm,
        "ancho_cm": ancho_cm, "alto_cm": alto_cm,
    }]

    try:
        paquetes, filas_bultos_form = _bultos_cotizador_internacional(
            peso_kg=peso_kg,
            largo_cm=largo_cm,
            ancho_cm=ancho_cm,
            alto_cm=alto_cm,
            cantidades=bulto_cantidad,
            pesos=bulto_peso,
            largos=bulto_largo,
            anchos=bulto_ancho,
            altos=bulto_alto,
        )
        primero = paquetes[0]
        valor_usd_num = _numero_form(
            valor_declarado_usd, "Valor declarado", importe=True, minimo=0.01,
        )
        comparacion = cotizar_referencia_couriers(
            cliente=cliente,
            origen_pais=origen_pais,
            destino_pais=destino_pais,
            peso_kg=primero["peso_kg"],
            largo_cm=primero["largo_cm"],
            ancho_cm=primero["ancho_cm"],
            alto_cm=primero["alto_cm"],
            valor_declarado_usd=valor_usd_num,
            paquetes=paquetes,
        )
        opciones = comparacion["opciones"]
        no_disponibles = comparacion["no_disponibles"]
        resumen = comparacion["resumen"]
        if es_reseller and opciones:
            opciones = guardar_opciones(
                cliente,
                ruta=resumen["ruta"],
                bultos=paquetes,
                peso_facturable_kg=resumen["peso_usado_kg"],
                opciones=opciones,
            )
        if not comparacion["encontrado"]:
            estados = ",".join(
                f"{item.get('id')}:{item.get('estado')}"
                for item in no_disponibles
            )
            print(
                f"[portal-cotizar] cliente={cliente} "
                f"ruta={origen_pais.upper()}->{destino_pais.upper()} "
                f"sin opciones ({estados or 'sin resultados'})"
            )
            raise ValueError("Ningún courier devolvió una tarifa para esa referencia.")
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        request=request, name="portal/cotizar.html",
        context={
            "cliente": cliente,
            "ambito": "internacional",
            "paises_origen": _paises_con_nacional(),
            "paises_destino": _paises_con_nacional(),
            "opciones": opciones,
            "resultado": resumen,
            "no_disponibles": no_disponibles,
            "operadores_internacionales": _operadores_cliente(
                cliente, Ambito.INTERNACIONAL
            ),
            "error": error,
            "form": {
                "origen_pais": origen_pais,
                "destino_pais": destino_pais,
                "peso_kg": filas_bultos_form[0].get("peso_kg", ""),
                "largo_cm": filas_bultos_form[0].get("largo_cm", ""),
                "ancho_cm": filas_bultos_form[0].get("ancho_cm", ""),
                "alto_cm": filas_bultos_form[0].get("alto_cm", ""),
                "bultos": filas_bultos_form,
                "valor_declarado_usd": valor_declarado_usd,
            },
            # Para que cada tarjeta de opción linkee a "crear envío" con el
            # destino ya elegido.
            "destino_sel": destino_pais,
            "es_reseller": es_reseller,
        },
    )


@router.post("/cotizaciones/reseller.pdf")
def descargar_cotizacion_reseller(
    quote_id: str = Form(...),
    precio: str = Form(...),
    cliente: str = Depends(cliente_actual),
):
    """Genera una copia comercial sin costo courier ni margen TAURO."""
    from servicios.cotizaciones_reseller import generar_pdf

    try:
        contenido, nombre = generar_pdf(cliente, quote_id, precio)
    except ValueError as exc:
        return Response(content=str(exc), status_code=422, media_type="text/plain")
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "Cache-Control": "private, no-store",
            "X-Robots-Tag": "noindex, nofollow",
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
        print(f"[portal] api_precio error: {type(e).__name__}")
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
            "estado": str(body.get("origen_estado") or "").strip(),
        }
        if not origen_real["pais"]:
            rem = obtener_remitente_para_envio(cliente) or {}
            origen_real = {"pais": rem.get("pais") or "AR",
                           "ciudad": rem.get("ciudad") or "",
                           "cp": rem.get("cp") or "",
                           "estado": rem.get("estado") or ""}
        if (normalizar_pais(origen_real["pais"]) == "AR"
                and normalizar_pais(destino) == "AR"):
            return JSONResponse({
                "ok": False,
                "motivo": "nacional_no_disponible",
                "mensaje": (
                    "Estamos conectando Andreani y OCA directamente. "
                    "Todavía no se puede cotizar ni emitir un envío nacional."
                ),
            }, status_code=200)
        bultos_entrada = body.get("bultos") or []
        # Sin truncar en silencio: si hay de más, obtener_precio_envio_multi
        # lo rechaza con motivo y el preview muestra lo MISMO que diría el submit.
        if not isinstance(bultos_entrada, list):
            raise ValueError("bultos inválidos")
        bultos = []
        for b in bultos_entrada:
            if not isinstance(b, dict):
                raise ValueError("bulto inválido")
            if not (
                str(b.get("producto") or "").strip()
                or b.get("peso_kg") or b.get("descripcion_en")
            ):
                continue
            bultos.append({
                "producto": str(b.get("producto") or ""),
                "cantidad": b.get("cantidad"),
                "unidades_aduana": b.get("unidades_aduana"),
                # Carga libre: el preview cotiza con lo tipeado, igual que
                # el submit — peso, medidas y la invoice de esta caja.
                **{k: b.get(k) for k in (
                    "peso_kg", "largo_cm", "ancho_cm", "alto_cm",
                    "valor_declarado_caja_usd", "valor_unitario_usd",
                    "descripcion_en", "hs_code",
                    "pais_origen",
                ) if b.get(k) not in (None, "")},
            })
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
            asegurar_carga=(
                body.get("asegurar_carga") is True
                or str(body.get("asegurar_carga") or "").strip().upper()
                in {"SI", "SÍ", "1", "TRUE"}
            ),
        )
    except Exception as e:
        print(f"[portal] api_precio_multi error: {type(e).__name__}")
        return JSONResponse({"ok": False, "motivo": "error_cotizando"}, status_code=200)

    if not precio.get("encontrado"):
        motivo = precio.get("motivo") or "sin_precio"
        seguro_pedido = (
            body.get("asegurar_carga") is True
            or str(body.get("asegurar_carga") or "").strip().upper()
            in {"SI", "SÍ", "1", "TRUE"}
        )
        if seguro_pedido and motivo == "sin_cobertura":
            motivo = "seguro_no_disponible"
        return JSONResponse(
            {"ok": False, "motivo": motivo},
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
def _cliente_puede_emitir(cliente: str) -> bool:
    """
    ¿Este cliente emite sus guías solo? El flag lo prende Tauro por ficha.

    Vive acá y no inline porque lo necesitan DOS pantallas: el detalle y el
    listado. Estaba sólo en el detalle, así que el botón quedaba escondido un
    click adentro y desde "Mis envíos" parecía que el cliente no podía emitir.

    Ante cualquier problema de base devuelve False: no poder leer un permiso
    nunca puede terminar en mostrar un botón que emite plata real.
    """
    from servicios.configuracion_couriers_cliente import mapa_permisos
    return any(mapa_permisos(cliente, "emitir").values())


def _cliente_puede_emitir_courier(cliente: str, courier: str) -> bool:
    """Permiso visual del courier concreto; el servicio lo revalida con lock."""
    from servicios.configuracion_couriers_cliente import permiso_courier
    return permiso_courier(cliente, courier, "emitir")


@router.get("/envios", response_class=HTMLResponse)
def envios_view(
    request: Request,
    ok: Optional[str] = None,
    tipo: str = "",
    paso: str = "",
    buscar: str = "",
    pagina: str = "1",
    anio: str = "",
    mes: str = "",
    semana: str = "",
    cliente: str = Depends(cliente_actual),
):
    # Esta es la vista de historial, no un preview: no ocultar silenciosamente
    # los envíos anteriores al límite por defecto del servicio.
    # Sin query se muestra el historial completo. El buscador es global: no
    # hereda ámbito, estado ni período de la pantalla desde la que se ejecutó.
    busqueda_global = str(buscar or "").strip()[:80]
    if busqueda_global:
        tipo, paso, anio, mes, semana = "", "", "", "", ""
    else:
        tipo = _ambito_portal(tipo)
    periodos_disponibles = periodos_solicitudes_cliente(cliente)
    periodo = normalizar_periodo(
        anio, mes, semana, periodos_disponibles,
    )
    historial = listar_solicitudes_cliente(
        cliente,
        limite=None,
        desde=periodo["desde"],
        hasta=periodo["hasta"],
    )
    vista = preparar_historial_envios(
        historial,
        tipo=tipo,
        paso=paso,
        pagina=pagina,
        buscar=busqueda_global,
    )
    # El período puede no tener filas aunque el cliente sí tenga historia. La
    # pantalla debe decir "sin envíos en agosto", no "nunca hiciste envíos".
    vista["tiene_historial"] = periodo["tiene_actividad_historica"]

    from servicios.configuracion_couriers_cliente import mapa_permisos
    permisos_emision = mapa_permisos(cliente, "emitir")
    puede_emitir = any(permisos_emision.values())
    for solicitud in vista["solicitudes"]:
        solicitud["puede_emitir_cliente"] = bool(
            ambito_envio(solicitud) == "internacional"
            and permisos_emision.get(
                (solicitud.get("courier") or "").lower(), False
            )
        )

    periodo_parametros = {}
    if periodo["anio"]:
        periodo_parametros["anio"] = periodo["anio"]
    if periodo["mes"]:
        periodo_parametros["mes"] = periodo["mes"]
    if periodo["semana"]:
        periodo_parametros["semana"] = periodo["semana"]

    return templates.TemplateResponse(
        request=request, name="portal/envios.html",
        context={
            "cliente": cliente,
            **vista,
            "periodo": periodo,
            "periodo_query": urlencode(periodo_parametros),
            "puede_emitir": puede_emitir,
            "flash_ok": (
                ("Solicitud creada. Podés emitir la guía vos mismo desde el botón "
                 "de la fila, o dejarla y la emitimos nosotros.")
                if (ok == "solicitado" and puede_emitir)
                else ("Solicitud creada. Tauro ya la ve en el admin." if ok == "solicitado" else None)
            ),
        },
    )


@router.get("/envios/{solicitud_id}/guia.pdf")
def descargar_guia(solicitud_id: int, cliente: str = Depends(cliente_actual)):
    """Descarga guía + invoice en un PDF, sólo para el cliente propietario."""
    try:
        documentos = preparar_documentos_envio_portal(solicitud_id, cliente)
    except ValueError:
        raise HTTPException(
            status_code=500,
            detail=("No pudimos preparar la guía junto con la invoice. "
                    "Contactá a TAURO para revisar el documento."),
        )
    if not documentos:
        return RedirectResponse(url="/portal/envios", status_code=303)
    return Response(
        content=documentos["pdf"],
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="{documentos["filename"]}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/envios/{solicitud_id}/factura-comercial.pdf")
def descargar_factura_comercial(
    solicitud_id: int, cliente: str = Depends(cliente_actual),
):
    """Descarga la invoice sin permitir documentos de otra cuenta."""
    pdf = obtener_factura_comercial_pdf(solicitud_id, cliente_id=cliente)
    if not pdf:
        return RedirectResponse(
            url=f"/portal/envios/{solicitud_id}", status_code=303,
        )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'inline; filename="factura-comercial-{solicitud_id}.pdf"',
            "Cache-Control": "private, no-store",
        },
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


def _origen_pedido_tienda_verificado(cliente_id: str, pedido_id_raw: str) -> dict:
    """Resuelve el origen de una venta sin confiar en datos ocultos del form.

    El navegador sólo devuelve el id interno del pedido. Plataforma, dominio
    y pedido externo se vuelven a leer de PostgreSQL y se filtran por la
    cuenta autenticada; esos son los únicos valores que pueden persistirse
    como linaje en la solicitud y, si corresponde, en la libreta.
    """
    pedido_id_raw = (pedido_id_raw or "").strip()
    if not pedido_id_raw.isdigit():
        raise ValueError("El pedido de la tienda no es válido. Volvé a abrirlo desde Pedidos.")

    pedido_id = int(pedido_id_raw)
    cliente_id = (cliente_id or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.estado, p.solicitud_id,
                       LOWER(p.plataforma) AS origen_plataforma,
                       LOWER(t.dominio) AS origen_dominio,
                       p.pedido_externo_id AS origen_pedido_externo_id
                FROM pedidos_tienda p
                JOIN tiendas_conectadas t ON t.id = p.tienda_id
                LEFT JOIN shopify_instalaciones i ON i.dominio = t.dominio
                WHERE p.id = %s AND p.cliente_id = %s
                  AND t.cliente_id = p.cliente_id
                  AND t.activa = TRUE
                  AND (
                      (
                          LOWER(p.plataforma) <> 'shopify'
                          AND i.id IS NULL
                      )
                      OR (
                          LOWER(p.plataforma) = 'shopify'
                          AND t.plataforma = 'shopify'
                          AND t.secreto = 'oauth:shopify-app'
                          AND i.id IS NOT NULL
                          AND UPPER(COALESCE(i.cliente_id, '')) = p.cliente_id
                          AND NULLIF(BTRIM(i.access_token), '') IS NOT NULL
                      )
                  )
                LIMIT 1
                """,
                (pedido_id, cliente_id),
            )
            row = cur.fetchone()

    if not row:
        raise ValueError("Ese pedido no pertenece a tu cuenta o ya no está disponible.")
    if row.get("estado") != "PENDIENTE" or row.get("solicitud_id"):
        raise ValueError("Ese pedido ya fue convertido o no está pendiente.")

    plataforma = (row.get("origen_plataforma") or "").strip().lower()
    dominio = (row.get("origen_dominio") or "").strip().lower()
    externo = str(row.get("origen_pedido_externo_id") or "").strip()
    if not (plataforma and dominio and externo):
        raise ValueError(
            "No se pudo verificar el origen del pedido. No se creó ningún envío."
        )
    return {
        "pedido_id": int(row["id"]),
        "origen_plataforma": plataforma,
        "origen_dominio": dominio,
        "origen_pedido_externo_id": externo,
    }


def _precargar_envio_existente(origen: dict, *, corregir_id: int | None = None):
    """Convierte una solicitud histórica en los campos del wizard actual."""
    bultos = origen.get("bultos") or []
    if isinstance(bultos, str):
        try:
            import json
            bultos = json.loads(bultos)
        except (TypeError, ValueError):
            bultos = []
    if not bultos:
        cantidad_cajas = int(origen.get("cantidad") or 1)
        bultos = [{
            "producto_alias": origen.get("producto_alias") or "",
            "cantidad": cantidad_cajas,
            "unidades_aduana": cantidad_cajas,
            "peso_kg": (
                float(origen.get("peso_kg") or 0) / max(cantidad_cajas, 1)
            ),
            "largo_cm": origen.get("largo_cm"),
            "ancho_cm": origen.get("ancho_cm"),
            "alto_cm": origen.get("alto_cm"),
            "valor_unitario_usd": (
                float(origen.get("valor_declarado_usd") or 0)
                / max(cantidad_cajas, 1)
            ),
            "valor_declarado_caja_usd": (
                float(origen.get("valor_declarado_usd") or 0)
                / max(cantidad_cajas, 1)
            ),
            "descripcion_en": origen.get("producto_alias") or "",
            "hs_code": "",
            "pais_origen": origen.get("remitente_pais") or "AR",
        }]
    filas = []
    for bulto in bultos:
        fila = dict(bulto)
        fila["producto"] = (
            fila.get("producto") or fila.get("producto_alias") or ""
        )
        fila.setdefault("unidades_aduana", fila.get("cantidad") or 1)
        if fila.get("valor_declarado_caja_usd") in (None, ""):
            cantidad_cajas = max(int(fila.get("cantidad") or 1), 1)
            unidades = max(int(fila.get("unidades_aduana") or cantidad_cajas), 1)
            fila["valor_declarado_caja_usd"] = round(
                float(fila.get("valor_unitario_usd") or 0) * unidades
                / cantidad_cajas,
                2,
            )
        filas.append(fila)

    courier = str(origen.get("courier") or "dhl").strip().lower()
    if courier not in {"dhl", "fedex", "ups"}:
        courier = "dhl"
    form = {
        "reemplaza_solicitud_id": str(corregir_id or ""),
        "reemision_motivo": (
            "Corrección solicitada por el cliente" if corregir_id else ""
        ),
        "rem_nombre": origen.get("remitente_nombre") or "",
        "rem_contacto": origen.get("remitente_contacto") or "",
        "rem_documento": origen.get("remitente_documento") or "",
        "rem_email": origen.get("remitente_email") or "",
        "rem_telefono": origen.get("remitente_telefono") or "",
        "rem_direccion": origen.get("remitente_direccion") or "",
        "rem_ciudad": origen.get("remitente_ciudad") or "",
        "rem_estado": origen.get("remitente_estado") or "",
        "rem_zip": origen.get("remitente_zip") or "",
        "rem_pais": origen.get("remitente_pais") or "AR",
        "destino_pais": origen.get("destino_pais") or "",
        "dest_nombre": origen.get("dest_nombre") or "",
        "dest_contacto": origen.get("dest_contacto") or "",
        "dest_documento": origen.get("dest_documento") or "",
        "dest_email": origen.get("dest_email") or "",
        "dest_telefono": origen.get("dest_telefono") or "",
        "dest_direccion": origen.get("dest_direccion") or "",
        "dest_ciudad": origen.get("dest_ciudad") or "",
        "dest_estado": origen.get("dest_estado") or "",
        "dest_zip": origen.get("dest_zip") or "",
        "observaciones": origen.get("observaciones") or "",
        "precio_cliente_final_ars": origen.get("precio_cliente_final_ars") or "",
        "tax_paga": origen.get("tax_paga") or "",
        "asegurar_carga": "SI" if origen.get("asegurar_carga") else "NO",
        "intl_courier": courier,
        # Siempre se vuelve a cotizar: repetir datos no conserva una tarifa
        # histórica que quizá ya cambió.
        "precio_cotizado_ars": "",
        "bultos": filas,
    }
    remitente = {
        "nombre": form["rem_nombre"], "contacto": form["rem_contacto"],
        "documento": form["rem_documento"], "email": form["rem_email"],
        "telefono": form["rem_telefono"], "direccion": form["rem_direccion"],
        "ciudad": form["rem_ciudad"], "estado": form["rem_estado"],
        "cp": form["rem_zip"], "pais": form["rem_pais"],
    }
    return form, remitente


@router.get("/envios/nuevo", response_class=HTMLResponse)
def envio_nuevo_form(
    request: Request,
    pedido_tienda: Optional[int] = None,
    destinatario_id: Optional[int] = None,
    origen: str = "",
    destino: str = "",
    ambito: str = "",
    courier: str = "",
    quote_id: str = "",
    corregir: Optional[int] = None,
    repetir: Optional[int] = None,
    cliente: str = Depends(cliente_actual),
):
    if corregir:
        repetir = None
    if corregir or repetir:
        ambito = "internacional"
    quote_id = _quote_id_portal(quote_id)
    if quote_id and not (ambito or "").strip():
        ambito = "internacional"
    ambito = _ambito_portal(ambito)
    selector_valores = {
        "pedido_tienda": pedido_tienda,
        "destinatario_id": destinatario_id,
        "origen": origen,
        "destino": destino,
        "courier": courier,
        "quote_id": quote_id,
    }
    if not ambito or ambito == "nacional":
        return templates.TemplateResponse(
            request=request, name="portal/envio_nuevo.html",
            context={
                "cliente": cliente,
                "ambito": ambito,
                "nacional_url": _url_envio_por_ambito("nacional", **selector_valores),
                "internacional_url": _url_envio_por_ambito("internacional", **selector_valores),
                "volver_url": "/portal/envios/nuevo",
            },
        )

    # Si viene de un pedido de la tienda (Shopify/Tiendanube), prellenamos
    # el destinatario con lo que el comprador completó en el checkout.
    form: dict = {}
    pedido_info = None
    cotizacion_web = None
    error = None
    reemision_origen = None
    repeticion_origen = None
    origen_existente_id = corregir or repetir
    if origen_existente_id:
        origen_existente = obtener_solicitud_de_cliente(
            origen_existente_id, cliente
        )
        if not origen_existente:
            return RedirectResponse(
                url="/portal/envios?error=El+env%C3%ADo+no+est%C3%A1+disponible",
                status_code=303,
            )
        if ambito_envio(origen_existente) != "internacional":
            return RedirectResponse(
                url=(f"/portal/envios/{origen_existente_id}?error="
                     "Repetir+env%C3%ADos+nacionales+se+habilitar%C3%A1+con+OCA+y+Andreani"),
                status_code=303,
            )
        if corregir:
            reemision_origen = origen_existente
            elegible = validar_reemision_cliente(corregir, cliente)
            if not elegible.get("ok"):
                destino_error = int(
                    elegible.get("reemision_existente_id") or corregir
                )
                return RedirectResponse(
                    url=(f"/portal/envios/{destino_error}?error="
                         f"{quote(str(elegible.get('error') or 'No se puede corregir'))}"),
                    status_code=303,
                )
        else:
            repeticion_origen = origen_existente

        form, remitente_precargado = _precargar_envio_existente(
            origen_existente, corregir_id=corregir
        )
        return templates.TemplateResponse(
            request=request, name="portal/envio_nuevo.html",
            context={
                "cliente": cliente, "ambito": "internacional",
                "productos": get_productos(cliente),
                "paises_destino": _paises_con_nacional(),
                "remitente": remitente_precargado,
                "remitentes": listar_direcciones(cliente, TIPO_REMITENTE),
                "destinatarios": listar_direcciones(cliente, TIPO_DESTINATARIO),
                "form": form, "pedido_tienda": None, "cotizacion_web": None,
                "reemision_origen": reemision_origen,
                "repeticion_origen": repeticion_origen,
                "tax_paga_default": tax_paga_cliente(cliente),
                "courier_default": form["intl_courier"], "error": None,
            },
        )
    if quote_id and not pedido_tienda:
        try:
            from servicios.leads import obtener_cotizacion
            cotizacion_web = obtener_cotizacion(quote_id, exigir_vigente=True)
        except Exception:
            cotizacion_web = None
        if cotizacion_web:
            recomendada = next(
                (o for o in cotizacion_web["opciones"] if o.get("recomendada")),
                cotizacion_web["opciones"][0],
            )
            form.update({
                "rem_pais": cotizacion_web["origen"],
                "destino_pais": cotizacion_web["destino"],
                "intl_courier": recomendada["id"],
                "precio_cotizado_ars": recomendada["precio_ars"],
                "observaciones": f"Cotización web {cotizacion_web['referencia']}",
                "bultos": [{
                    "producto": "",
                    "cantidad": 1,
                    "unidades_aduana": 1,
                    "peso_kg": cotizacion_web["peso_kg"],
                    "largo_cm": cotizacion_web["largo_cm"],
                    "ancho_cm": cotizacion_web["ancho_cm"],
                    "alto_cm": cotizacion_web["alto_cm"],
                    "valor_unitario_usd": cotizacion_web["valor_declarado_usd"],
                    "valor_declarado_caja_usd": cotizacion_web["valor_declarado_usd"],
                    "descripcion_en": "",
                    "hs_code": "",
                    "pais_origen": cotizacion_web["origen"],
                }],
            })
            courier = courier or recomendada["id"]
        else:
            error = "La cotización venció o ya no está disponible. Cotizá nuevamente."
    # Si viene del cotizador ("tocá la opción para crear el envío"), el
    # destino elegido ya llega puesto — una promesa menos que romper.
    if destino.strip() and not pedido_tienda:
        form["destino_pais"] = normalizar_pais(destino)
    if origen.strip() and not pedido_tienda:
        form["rem_pais"] = normalizar_pais(origen)
    courier = (courier or "").strip().lower()

    # Inicio directo desde "Mis clientes". El id nunca alcanza por sí solo:
    # se resuelve junto con el cliente autenticado, así una cuenta no puede
    # precargar un destinatario perteneciente a otra.
    if destinatario_id and not pedido_tienda:
        destinatario = obtener_direccion(cliente, destinatario_id, TIPO_DESTINATARIO)
        if destinatario:
            form.update({
                "destinatario_id": str(destinatario["id"]),
                "dest_nombre": destinatario.get("nombre") or "",
                "dest_documento": destinatario.get("documento") or "",
                "dest_email": destinatario.get("email") or "",
                "dest_telefono": destinatario.get("telefono") or "",
                "dest_direccion": destinatario.get("direccion") or "",
                "dest_ciudad": destinatario.get("ciudad") or "",
                "dest_estado": destinatario.get("estado") or "",
                "dest_zip": destinatario.get("cp") or "",
                "destino_pais": destinatario.get("pais") or "",
            })
        else:
            # Mismo mensaje para un id inexistente o ajeno: no revela si otra
            # cuenta tiene ese registro.
            error = "Ese cliente guardado no está disponible en tu cuenta."
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
                "observaciones": f"Pedido {p.get('numero') or p.get('pedido_externo_id')} de la tienda: {resumen}",
                "pedido_tienda_id": p["id"],
            }
            pedido_info = p
    if courier in {"dhl", "fedex", "ups"}:
        form["intl_courier"] = courier
    return templates.TemplateResponse(
        request=request, name="portal/envio_nuevo.html",
        context={
            "cliente": cliente,
            "ambito": "internacional",
            "productos": get_productos(cliente),
            "paises_destino": _paises_con_nacional(),
            "remitente": obtener_remitente_para_envio(cliente),
            "remitentes": listar_direcciones(cliente, TIPO_REMITENTE),
            "destinatarios": listar_direcciones(cliente, TIPO_DESTINATARIO),
            "form": form,
            "pedido_tienda": pedido_info,
            "cotizacion_web": cotizacion_web,
            # Arranca con lo que el cliente dejó configurado en su ficha; el
            # selector del wizard sólo sirve para pisarlo en este envío.
            "tax_paga_default": tax_paga_cliente(cliente),
            # El courier que el cliente dejó configurado (WAIMAO → DHL):
            # el selector del wizard arranca con este elegido.
            "courier_default": courier_default_cliente(cliente),
            "error": error,
            "reemision_origen": reemision_origen,
            "repeticion_origen": repeticion_origen,
        },
    )


@router.post("/envios/nuevo", response_class=HTMLResponse)
def envio_nuevo_post(
    request: Request,
    ambito: str = Form("internacional"),
    destino_pais: str = Form(...),
    # Multi-bulto: arrays paralelos, una entrada por fila de caja del form.
    # cantidad como str: un valor basura no debe tirar un 422 pelado; se
    # valida dentro del wizard y se vuelve a mostrar con un mensaje claro.
    bulto_producto: list[str] = Form([]),
    bulto_cantidad: list[str] = Form([]),
    # Unidades COMERCIALES declaradas en aduana, independientes de las
    # cajas fisicas. Ejemplo: una caja puede contener ocho camisas.
    bulto_unidades_aduana: list[str] = Form([]),
    # Declaración de invoice POR ÍTEM (Leandro 02/09): este importe es el
    # valor de UNA unidad comercial, nunca el total de la caja. DHL calcula
    # unidades_aduana × valor_unitario_usd. Vacío = catálogo.
    # Carga libre (guía HAILU 05/08): peso y medidas por caja, sin catálogo.
    bulto_peso: list[str] = Form([]),
    bulto_largo: list[str] = Form([]),
    bulto_ancho: list[str] = Form([]),
    bulto_alto: list[str] = Form([]),
    # Total de mercadería contenido EN CADA CAJA física. Se controla contra
    # unidades de invoice × valor unitario para no asegurar/declarar montos
    # distintos por accidente.
    bulto_valor_caja_usd: list[str] = Form([]),
    bulto_desc_en: list[str] = Form([]),
    bulto_valor_usd: list[str] = Form([]),
    bulto_hs: list[str] = Form([]),
    bulto_pais_fab: list[str] = Form([]),
    # Legacy (por si queda un form viejo cacheado): un solo producto.
    producto_alias: str = Form(""),
    cantidad: str = Form("1"),
    # Internacional: courier elegido en el comparador en vivo (fedex/dhl/ups).
    intl_courier: str = Form(""),
    # Precio que estaba visible al confirmar. No se usa para cobrar (el
    # servidor recotiza); sólo prueba consentimiento al importe vigente.
    precio_cotizado_ars: str = Form(""),
    # Quién paga los impuestos de destino EN ESTE envío. Viene con el default
    # del cliente ya seleccionado; acá se guarda lo que quedó elegido.
    tax_paga: str = Form(""),
    asegurar_carga: str = Form("NO"),
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
    dest_alias: str = Form(""),
    guardar_destinatario: Optional[str] = Form(None),
    precio_cliente_final_ars: str = Form(""),
    observaciones: str = Form(""),
    # Si el envío nació de un pedido de la tienda, al crearse la solicitud
    # el pedido pasa de PENDIENTE a CONVERTIDO.
    pedido_tienda_id: str = Form(""),
    reemplaza_solicitud_id: str = Form(""),
    reemision_motivo: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    if _ambito_post(ambito) != "internacional":
        return RedirectResponse(url="/portal/envios/nuevo?ambito=nacional", status_code=303)

    productos = get_productos(cliente)
    paises_destino = _paises_con_nacional()
    remitentes = listar_direcciones(cliente, TIPO_REMITENTE)
    destinatarios = listar_direcciones(cliente, TIPO_DESTINATARIO)
    # Algunos tests/consumidores internos invocan la función directamente,
    # fuera del inyector de FastAPI. En ese caso el default sigue siendo un
    # FormInfo, no una lista enviada por el navegador.
    if not isinstance(bulto_unidades_aduana, (list, tuple)):
        bulto_unidades_aduana = []
    if not isinstance(bulto_valor_caja_usd, (list, tuple)):
        bulto_valor_caja_usd = []
    if not isinstance(asegurar_carga, str):
        asegurar_carga = "NO"
    asegurar_carga = asegurar_carga.strip().upper()
    if asegurar_carga not in {"SI", "NO"}:
        asegurar_carga = "NO"
    # Invocaciones directas de tests/consumidores internos conservan el
    # FormInfo de FastAPI en parámetros nuevos que no enviaron.
    if not isinstance(reemplaza_solicitud_id, str):
        reemplaza_solicitud_id = ""
    if not isinstance(reemision_motivo, str):
        reemision_motivo = ""

    # Normalizar filas de bultos: pares (producto, cantidad) sin filas vacías.
    # Fallback legacy: producto_alias + cantidad sueltos = una sola fila.
    filas = []
    filas_form = []
    # Mantener separados los errores físicos (paso 3) y aduaneros (paso 4)
    # para reabrir exactamente la hoja que el cliente debe corregir.
    errores_paquete = []
    errores_invoice = []
    n_filas = max(len(bulto_producto or []), len(bulto_cantidad or []),
                  len(bulto_peso or []), len(bulto_desc_en or []),
                  len(bulto_unidades_aduana or []),
                  len(bulto_valor_caja_usd or []))
    for i in range(n_filas):
        def _campo(lista, idx=i):
            v = lista[idx] if idx < len(lista or []) else ""
            return (v or "").strip()

        alias = _campo(bulto_producto)
        # Una fila vale con producto del catálogo O con carga manual: si no
        # tiene ninguna de las dos cosas, es un renglón vacío y se saltea.
        if not alias and not (_campo(bulto_peso) or _campo(bulto_desc_en)):
            continue
        fila_form = {
            "producto": alias,
            "cantidad": _campo(bulto_cantidad),
            "unidades_aduana": _campo(bulto_unidades_aduana),
            "peso_kg": _campo(bulto_peso),
            "largo_cm": _campo(bulto_largo),
            "ancho_cm": _campo(bulto_ancho),
            "alto_cm": _campo(bulto_alto),
            "valor_declarado_caja_usd": _campo(bulto_valor_caja_usd),
            "descripcion_en": _campo(bulto_desc_en),
            "valor_unitario_usd": _campo(bulto_valor_usd),
            "hs_code": _campo(bulto_hs),
            "pais_origen": _campo(bulto_pais_fab),
        }
        cantidad_valida = True
        try:
            cant = _entero_form(
                _campo(bulto_cantidad), f"Caja {i + 1}: la cantidad de cajas",
                minimo=1, maximo=20,
            )
        except (TypeError, ValueError) as exc:
            cantidad_valida = False
            errores_paquete.append(
                str(exc)
            )
            cant = 1
        unidades_validas = True
        try:
            unidades_aduana = _entero_form(
                _campo(bulto_unidades_aduana),
                f"Caja {i + 1}: las unidades de aduana", minimo=1, maximo=9999,
            )
        except (TypeError, ValueError) as exc:
            unidades_validas = False
            errores_invoice.append(
                str(exc)
            )
            unidades_aduana = cant
        # Los enteros válidos vuelven al contrato histórico como `int`; los
        # valores inválidos quedan crudos para que el formulario pueda
        # mostrar exactamente qué debe corregir el cliente.
        if cantidad_valida:
            fila_form["cantidad"] = cant
        if unidades_validas:
            fila_form["unidades_aduana"] = unidades_aduana
        filas_form.append(fila_form)
        fila = {
            "producto": alias,
            "cantidad": cant,
            "unidades_aduana": max(unidades_aduana, 1),
        }
        # Caja manual: peso y medidas de ESTE envío (con catálogo, lo pisan).
        for lista, clave in ((bulto_peso, "peso_kg"), (bulto_largo, "largo_cm"),
                             (bulto_ancho, "ancho_cm"), (bulto_alto, "alto_cm"),
                             (bulto_valor_caja_usd, "valor_declarado_caja_usd")):
            v = _campo(lista)
            if v:
                try:
                    etiquetas = {
                        "peso_kg": "el peso", "largo_cm": "el largo",
                        "ancho_cm": "el ancho", "alto_cm": "el alto",
                        "valor_declarado_caja_usd": "el valor declarado por caja",
                    }
                    fila[clave] = _numero_form(
                        v, f"Caja {i + 1}: {etiquetas[clave]}",
                        importe=clave == "valor_declarado_caja_usd",
                        minimo=(0.01 if clave in {"peso_kg", "valor_declarado_caja_usd"}
                                else 1),
                        maximo=70 if clave == "peso_kg" else None,
                    )
                    fila_form[clave] = fila[clave]
                except ValueError as exc:
                    errores_paquete.append(str(exc))
        # Overrides de invoice: sólo viajan los completados; el resto sale
        # del catálogo, como siempre.
        if _campo(bulto_desc_en):
            fila["descripcion_en"] = _campo(bulto_desc_en)[:75]
        if _campo(bulto_hs):
            fila["hs_code"] = _campo(bulto_hs)
        if _campo(bulto_pais_fab):
            pais_fabricacion = normalizar_pais(_campo(bulto_pais_fab))
            if pais_fabricacion:
                fila["pais_origen"] = pais_fabricacion
            else:
                errores_invoice.append(
                    f"Caja {i + 1}: elegí un país de fabricación válido."
                )
        v = _campo(bulto_valor_usd)
        if v:
            try:
                valor = _numero_form(
                    v, f"Ítem {i + 1}: el valor unitario", importe=True,
                    minimo=0.01,
                )
                if valor > 0:
                    fila["valor_unitario_usd"] = round(valor, 2)
                    fila_form["valor_unitario_usd"] = fila["valor_unitario_usd"]
            except ValueError as exc:
                errores_invoice.append(str(exc))
        filas.append(fila)
    # Form viejo cacheado (pre multi-bulto): ahí "cantidad" significaba
    # unidades dentro de UNA caja — se respeta esa semántica para que el
    # precio cobrado sea el mismo que ese form mostró.
    legacy_single = False
    if not filas and (producto_alias or "").strip():
        legacy_single = True
        filas = [{"producto": producto_alias.strip(), "cantidad": _entero_form(
            cantidad, "Cantidad de cajas", minimo=1, maximo=20
        )}]

    form = {
        "ambito": "internacional",
        "bultos": filas_form,
        "producto_alias": producto_alias,
        "destino_pais": destino_pais,
        "cantidad": cantidad,
        "remitente_id": remitente_id,
        "rem_nombre": rem_nombre,
        "rem_contacto": rem_contacto,
        "rem_documento": rem_documento,
        "rem_email": rem_email,
        "rem_telefono": rem_telefono,
        "rem_direccion": rem_direccion,
        "rem_ciudad": rem_ciudad,
        "rem_estado": rem_estado,
        "rem_zip": rem_zip,
        "rem_pais": rem_pais,
        "destinatario_id": destinatario_id,
        "dest_nombre": dest_nombre,
        "dest_contacto": dest_contacto,
        "dest_documento": dest_documento,
        "dest_email": dest_email,
        "dest_telefono": dest_telefono,
        "dest_direccion": dest_direccion,
        "dest_ciudad": dest_ciudad,
        "dest_estado": dest_estado,
        "dest_zip": dest_zip,
        "dest_alias": dest_alias,
        "guardar_destinatario": guardar_destinatario,
        "precio_cliente_final_ars": precio_cliente_final_ars,
        "observaciones": observaciones,
        "intl_courier": intl_courier,
        "precio_cotizado_ars": precio_cotizado_ars,
        "tax_paga": tax_paga,
        "asegurar_carga": asegurar_carga,
        # BUG corregido: sin esto, un error de validación re-renderizaba el
        # form con el hidden del pedido VACÍO — el vínculo con la venta de la
        # tienda se perdía, el pedido quedaba "pendiente" para siempre y se
        # podía terminar despachando dos veces.
        "pedido_tienda_id": pedido_tienda_id,
        "reemplaza_solicitud_id": reemplaza_solicitud_id,
        "reemision_motivo": reemision_motivo,
    }

    error_step = 1
    pedido_origen = None
    reemision_origen = None
    origen_tienda: dict = {}
    idempotency_tienda = ""
    try:
        reemplaza_id = _id_opt(reemplaza_solicitud_id)
        if reemplaza_id:
            reemision_origen = obtener_solicitud_de_cliente(reemplaza_id, cliente)
            elegible = validar_reemision_cliente(
                reemplaza_id, cliente, consultar_courier=True,
            )
            if not elegible.get("ok"):
                raise ValueError(
                    str(elegible.get("error") or "La guía ya no se puede corregir.")
                )
            if (intl_courier or "").strip().lower() != "dhl":
                raise ValueError("La corrección debe volver a emitirse con DHL.")
        if (pedido_tienda_id or "").strip():
            pedido_origen = _origen_pedido_tienda_verificado(
                cliente, pedido_tienda_id,
            )
            origen_tienda = {
                clave: pedido_origen[clave]
                for clave in (
                    "origen_plataforma",
                    "origen_dominio",
                    "origen_pedido_externo_id",
                )
            }
            idempotency_tienda = idempotency_hash_origen_tienda(
                cliente_id=cliente,
                **origen_tienda,
            )

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
        paises_validos = {iso for iso, _nombre in paises_destino}
        origen_pais = normalizar_pais(remitente.get("pais") or "")
        if origen_pais not in paises_validos:
            raise ValueError("Elegí un país de origen válido en el paso 1.")

        error_step = 2
        if destinatario_id:
            destinatario = obtener_direccion(
                cliente, _id_opt(destinatario_id) or 0, TIPO_DESTINATARIO
            )
            if not destinatario:
                raise ValueError("Ese cliente guardado no está disponible en tu cuenta.")
            # La ficha sólo precarga el GET. En el POST mandan exactamente los
            # campos visibles: también permite borrar un email/teléfono viejo
            # para este envío sin modificar la ficha guardada.

        dest_nombre = dest_nombre.strip()
        dest_contacto = dest_contacto.strip()
        dest_documento = dest_documento.strip()
        dest_email = dest_email.strip()
        dest_telefono = dest_telefono.strip()
        dest_direccion = dest_direccion.strip()
        dest_ciudad = dest_ciudad.strip()
        dest_estado = dest_estado.strip()
        dest_zip = dest_zip.strip()
        if not all((dest_nombre, dest_direccion, dest_ciudad, dest_zip)):
            raise ValueError(
                "Completá nombre, dirección, ciudad y código postal del destinatario."
            )

        # Un único país canónico: el que el cliente ve y elige en pantalla.
        # Antes existía además un hidden `dest_pais`; si divergían se podía
        # cotizar una ruta y guardar/emitir otra.
        destino_pais = normalizar_pais(destino_pais)
        if destino_pais not in paises_validos:
            raise ValueError("Elegí un país de destino válido.")

        error_step = 3
        if errores_paquete:
            raise ValueError(errores_paquete[0])
        if not filas:
            raise ValueError("Agregá al menos una caja al envío.")
        if origen_pais == "AR" and destino_pais == "AR":
            raise ValueError(
                "Los envíos nacionales se habilitarán con las conexiones "
                "directas de Andreani y OCA. Todavía no están disponibles."
            )

        # El catálogo es OPCIONAL (Leandro, 06/08): sirve para que la tienda
        # integre por API sin volver a declarar el producto en cada venta, pero
        # desde el portal el cliente puede cargar la caja a mano. Antes toda
        # fila exigía un producto aprobado, así que un cliente nuevo no podía
        # crear NI UN envío hasta que Tauro le validara el catálogo.
        for i, fila in enumerate(filas, start=1):
            alias = (fila.get("producto") or "").strip()
            if alias:
                producto = get_producto(cliente, alias)
                if not producto or not producto.activo:
                    raise ValueError(f"El producto \"{alias}\" no está activo en tu catálogo.")
                continue

            # Carga manual: sin catálogo de dónde sacarlos, estos datos son
            # obligatorios. Este endpoint queda exclusivamente internacional
            # hasta que Andreani/OCA directos estén integrados.
            obligatorios = [("peso_kg", "el peso"), ("largo_cm", "el largo"),
                            ("ancho_cm", "el ancho"), ("alto_cm", "el alto")]
            faltan = [nombre for clave, nombre in obligatorios if not fila.get(clave)]
            if faltan:
                donde = f"la caja {i}" if len(filas) > 1 else "la caja"
                raise ValueError(
                    f"En {donde} te falta {', '.join(faltan)}. "
                    "Completalos, o elegí un producto de tu catálogo y se cargan solos."
                )
        error_step = 4
        if errores_invoice:
            raise ValueError(errores_invoice[0])
        for i, fila in enumerate(filas, start=1):
            if not fila.get("descripcion_en"):
                donde = f"el ítem {i}" if len(filas) > 1 else "el ítem"
                raise ValueError(
                    f"En {donde} te falta el nombre del producto en inglés."
                )
            if not fila.get("valor_unitario_usd"):
                donde = f"el ítem {i}" if len(filas) > 1 else "el ítem"
                raise ValueError(
                    f"En {donde} te falta el valor unitario en USD. No cargues el total de la caja."
                )

        courier_extra = {}
        if legacy_single:
            precio = obtener_precio_envio(
                cliente, filas[0]["producto"], destino_pais,
                cantidad=filas[0]["cantidad"], origen_pais=origen_pais,
            )
            bultos_detalle = None
        else:
            # La dirección real ya está en el scope (viene del form o de la
            # libreta). Cotizar contra el CP real incluye recargos por zona
            # remota en el importe que confirma el cliente.
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
                asegurar_carga=asegurar_carga == "SI",
            )
            if not multi.get("encontrado"):
                if asegurar_carga == "SI" and multi.get("motivo") == "sin_cobertura":
                    raise ValueError(
                        "DHL no pudo cotizar este envío con protección de carga. "
                        "Revisá el valor declarado o elegí sin seguro."
                    )
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

            try:
                precio_visto = float((precio_cotizado_ars or "").strip())
            except (TypeError, ValueError):
                precio_visto = None
            if precio_visto is None or precio_visto <= 0:
                raise ValueError(
                    "Esperá a que aparezca la tarifa de DHL y volvé a confirmar el envío."
                )
            if abs(float(precio_visto) - float(op["precio_ars"])) > 0.5:
                anterior = f"$ {float(precio_visto):,.0f}".replace(",", ".")
                nuevo = f"$ {float(op['precio_ars']):,.0f}".replace(",", ".")
                # Conservar la tarifa nueva al re-render. El JS la vuelve a
                # validar en vivo; el próximo click es la aceptación expresa.
                form["precio_cotizado_ars"] = str(op["precio_ars"])
                raise ValueError(
                    f"La tarifa de {op['nombre']} cambió de {anterior} a {nuevo}. "
                    "Revisá el nuevo importe y volvé a confirmar; no creamos ni cobramos nada."
                )

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

        if courier_extra.get("courier") == "DHL":
            # MyDHL exige contactos reales. Una guía aduanera no puede salir
            # con el antiguo teléfono ficticio 0000000000 ni sin clasificación
            # de mercadería: se corrige en el wizard, antes de crear/cobrar.
            faltan_contacto = []
            if not str(remitente.get("telefono") or "").strip():
                faltan_contacto.append("el teléfono del remitente")
            if not str(dest_telefono or "").strip():
                faltan_contacto.append("el teléfono del destinatario")
            if faltan_contacto:
                raise ValueError(
                    "Para emitir con DHL completá " + " y ".join(faltan_contacto) + "."
                )

        precio_final = _numero_form(
            precio_cliente_final_ars,
            "Precio final a tu cliente",
            importe=True,
            requerido=False,
            minimo=0,
        )

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
                pais=destino_pais,
                predeterminada=False,
                notas="Guardado desde creación de envío.",
                **origen_tienda,
            )

        # Campos legacy: primer bulto + totales (los listados y el admin los usan).
        if legacy_single:
            prod0 = get_producto(cliente, filas[0]["producto"])
            alias_display = prod0.alias_interno if prod0 else filas[0].get("producto") or "CARGA"
            total_cajas = filas[0]["cantidad"]
            # `prod0` puede venir None (form viejo cacheado apuntando a un
            # producto que ya no existe): la línea de abajo lo daba por hecho y
            # tiraba AttributeError sobre None en vez de un error entendible.
            if not prod0:
                raise ValueError(
                    f"El producto \"{filas[0].get('producto')}\" ya no está en tu catálogo. "
                    "Recargá la página y volvé a armar el envío."
                )
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
                (
                    float(b.get("valor_declarado_caja_usd"))
                    * int(b.get("cantidad") or 1)
                    if b.get("valor_declarado_caja_usd") not in (None, "")
                    else float(b.get("valor_unitario_usd") or 0)
                    * int(b.get("unidades_aduana") or b.get("cantidad") or 1)
                )
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
            asegurar_carga=asegurar_carga == "SI",
            idempotency_key_hash=idempotency_tienda,
            reemplaza_solicitud_id=reemplaza_id,
            reemision_motivo=reemision_motivo,
            **origen_tienda,
            **courier_extra,
        )
    except Exception as e:
        # ValueError = mensaje escrito para humanos (validaciones propias).
        # Cualquier otra excepción es interna: al log completa, a la pantalla
        # una frase — un traceback de Python asusta y no ayuda a corregir.
        if isinstance(e, ValueError):
            mensaje_error = str(e)
        else:
            print(f"[portal] error creando envío de {cliente}: {type(e).__name__}")
            mensaje_error = ("No pudimos crear el envío por un problema nuestro. "
                            "Probá de nuevo en un minuto o escribinos.")
        form["initial_step"] = error_step
        # Para re-renderizar se usan exactamente los campos visibles que el
        # cliente envió, incluidos vacíos intencionales. No volver a mezclar
        # silenciosamente una ficha vieja después de un error del paso 3.
        remitente_render = obtener_remitente_para_envio(
            cliente, _id_opt(remitente_id)
        ) or {}
        remitente_render.update({
            "nombre": rem_nombre,
            "contacto": rem_contacto,
            "documento": rem_documento,
            "email": rem_email,
            "telefono": rem_telefono,
            "direccion": rem_direccion,
            "ciudad": rem_ciudad,
            "estado": rem_estado,
            "cp": rem_zip,
            "pais": rem_pais,
        })
        return templates.TemplateResponse(
            request=request, name="portal/envio_nuevo.html",
            context={
                "cliente": cliente,
                "ambito": "internacional",
                "productos": productos,
                "paises_destino": paises_destino,
                "remitente": remitente_render,
                "remitentes": remitentes,
                "destinatarios": destinatarios,
                "form": form,
                "error": mensaje_error,
                # Sin estos dos, el re-render tras un error pierde el courier
                # y el default de impuestos configurados del cliente.
                "tax_paga_default": tax_paga_cliente(cliente),
                "courier_default": courier_default_cliente(cliente),
                "reemision_origen": reemision_origen,
            },
        )

    if pedido_origen:
        try:
            marcar_convertido(cliente, pedido_origen["pedido_id"],
                              solicitud_id=solicitud_creada.get("id"))
        except Exception as e:
            print(f"[integraciones] no pude marcar convertido el pedido "
                  f"{pedido_origen['pedido_id']}: {type(e).__name__}")

    if _id_opt(reemplaza_solicitud_id):
        from servicios.solicitudes_guia import emitir_guia_como_cliente
        resultado_emision = emitir_guia_como_cliente(
            int(solicitud_creada["id"]), cliente,
        )
        if resultado_emision.get("ok"):
            return RedirectResponse(
                url=f"/portal/envios/{solicitud_creada['id']}?ok=reemision",
                status_code=303,
            )
        return RedirectResponse(
            url=(f"/portal/envios/{solicitud_creada['id']}?error="
                 f"{quote(str(resultado_emision.get('error') or 'No se pudo emitir'))}"),
            status_code=303,
        )

    return RedirectResponse(
        url="/portal/envios?tipo=internacional&ok=solicitado",
        status_code=303,
    )


# ── Verificación compacta dentro de Mis envíos ──────────────
@router.get("/envios/{solicitud_id}/verificacion", response_class=HTMLResponse)
def envio_verificacion(
    request: Request,
    solicitud_id: int,
    cliente: str = Depends(cliente_actual),
):
    """Fragmento de sólo lectura para el modal de verificación.

    La pertenencia se valida en el handler antes de leer la recolección. Así
    un cliente nunca puede usar ids secuenciales para consultar datos ajenos.
    Tampoco se exponen costos del courier ni márgenes internos de TAURO.
    """
    s = obtener_solicitud_de_cliente(solicitud_id, cliente)
    if not s:
        return HTMLResponse(
            '<div class="msg error">Ese envío no está disponible en tu cuenta.</div>',
            status_code=404,
            headers={"Cache-Control": "private, no-store"},
        )
    if not (s.get("tracking") or s.get("tiene_label") or s.get("guia_url")):
        return HTMLResponse(
            '<div class="msg warn">La verificación estará disponible cuando se emita la guía.</div>',
            status_code=409,
            headers={"Cache-Control": "private, no-store"},
        )

    from servicios.recolecciones import obtener_de_solicitud
    recoleccion_error = False
    try:
        recoleccion = obtener_de_solicitud(cliente, solicitud_id)
    except Exception as exc:
        print(f"[portal] no pude leer el retiro del envío {solicitud_id}: "
              f"{type(exc).__name__}")
        recoleccion = None
        recoleccion_error = True

    return templates.TemplateResponse(
        request=request,
        name="portal/_envio_verificacion.html",
        context={
            "cliente": cliente,
            "s": s,
            "recoleccion": recoleccion,
            "recoleccion_error": recoleccion_error,
        },
        headers={"Cache-Control": "private, no-store"},
    )


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
    puede_emitir = bool(
        ambito_envio(s) == "internacional"
        and _cliente_puede_emitir_courier(cliente, s.get("courier") or "")
    )
    puede_corregir = False
    if ((s.get("courier") or "").upper() == "DHL"
            and s.get("estado") == "GUIA_LISTA" and s.get("tracking")
            and not s.get("reemplazada_por_solicitud_id")):
        puede_corregir = bool(
            validar_reemision_cliente(solicitud_id, cliente).get("ok")
        )
    cancelacion = validar_cancelacion_cliente(solicitud_id, cliente)

    return templates.TemplateResponse(
        request=request, name="portal/envio_detalle.html",
        context={
            "cliente": cliente, "s": s, "puede_emitir": puede_emitir,
            "puede_corregir": puede_corregir,
            "puede_cancelar": bool(cancelacion.get("ok")),
            "cancelar_bloqueo": str(cancelacion.get("error") or ""),
        },
    )


@router.post("/envios/{solicitud_id}/cancelar")
def cancelar_envio_portal(
    solicitud_id: int,
    cliente: str = Depends(cliente_actual),
):
    # La UI sólo anticipa la posibilidad. Antes de cambiar cuenta y etiqueta
    # se consulta DHL en vivo y luego el servicio vuelve a validar bajo lock.
    validacion = validar_cancelacion_cliente(
        solicitud_id, cliente, consultar_courier=True
    )
    if not validacion.get("ok"):
        return RedirectResponse(
            url=(f"/portal/envios/{solicitud_id}?error="
                 f"{quote(str(validacion.get('error') or 'No se pudo cancelar'))}"),
            status_code=303,
        )
    try:
        resultado = cancelar_solicitud_cliente(solicitud_id, cliente)
    except ValueError as exc:
        resultado = {"ok": False, "error": str(exc)}
    if not resultado.get("ok"):
        return RedirectResponse(
            url=(f"/portal/envios/{solicitud_id}?error="
                 f"{quote(str(resultado.get('error') or 'No se pudo cancelar'))}"),
            status_code=303,
        )
    return RedirectResponse(
        url=f"/portal/envios/{solicitud_id}?ok=cancelado", status_code=303
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
    # Una instalación iniciada desde Tiendanube llega ownerless y deja un
    # claim HttpOnly de un solo uso. Tras login/alta, esta vista lo consume:
    # no se listan tiendas ajenas y el merchant no debe reinstalar la app.
    claim_cookie = request.cookies.get("tn_claim") or ""
    borrar_claim = bool(claim_cookie)
    if claim_cookie:
        try:
            from servicios.tiendanube_app import instalacion, reclamar_con_token
            store_claim = reclamar_con_token(claim_cookie, cliente)
            vinculada = instalacion(store_claim) or {}
            if vinculada.get("webhooks_ready"):
                ok = ok or "tiendanube_conectada"
            else:
                error = error or (
                    "La instalación quedó asociada a tu cuenta, pero todavía "
                    "estamos verificando sus notificaciones y el medio de envío."
                )
        except Exception as exc:
            print(f"[portal] claim Tiendanube rechazado: {type(exc).__name__}")
            error = error or "No pudimos vincular la instalación de Tiendanube. Volvé a instalarla o contactá a soporte."
    # Detrás del proxy de Railway, request.base_url viene en http:// —
    # y una URL de webhook en http no sirve: Shopify exige https.
    base_url = (BASE_URL or str(request.base_url)).rstrip("/")
    if base_url.startswith("http://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
        base_url = "https://" + base_url[len("http://"):]
    tiendas = [t for t in listar_tiendas(cliente) if t["activa"]]
    pedidos = listar_pedidos(cliente, "PENDIENTE")
    convertidos = listar_pedidos(cliente, "CONVERTIDO", limite=10)
    sync_estado = None
    stock_resumen = {}
    try:
        from servicios.catalogo import (
            enriquecer_items_catalogo, estado_sincronizacion_cliente,
            resumen_stock_cliente,
        )
        for pedido in pedidos:
            pedido["items"] = enriquecer_items_catalogo(cliente, pedido.get("items") or [])
        sync_estado = estado_sincronizacion_cliente(cliente)
        stock_resumen = resumen_stock_cliente(cliente)
    except Exception as e:
        print(f"[portal] no pude enriquecer pedidos/stock de {cliente}: {type(e).__name__}")
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
                print(f"[portal] no pude verificar instalación: {type(e).__name__}")
    except Exception as e:
        print(
            "[portal] no pude listar instalaciones sin dueño: "
            f"{type(e).__name__}"
        )
    # La política de flete se configura por tienda; hoy mostramos la de la
    # primera (el caso normal es una tienda por cuenta).
    dominio_cfg = tiendas[0]["dominio"] if tiendas else ""
    try:
        from servicios.tiendanube_app import app_configurada as _tn_ok
        tiendanube_activa = _tn_ok()
    except Exception:
        tiendanube_activa = False
    try:
        from servicios.shopify_app import app_configurada as _shopify_ok
        shopify_app_activa = _shopify_ok()
    except Exception:
        shopify_app_activa = False
    respuesta = templates.TemplateResponse(
        request=request, name="portal/tienda.html",
        context={
            "cliente": cliente,
            "tiendas": tiendas,
            "pedidos": pedidos,
            "convertidos": convertidos,
            "webhook_url": f"{base_url}/integraciones/shopify/webhook",
            "dominio_cfg": dominio_cfg,
            "cfg": obtener_config(dominio_cfg),
            "huerfanas": huerfanas,
            "tiendanube_activa": tiendanube_activa,
            "shopify_app_activa": shopify_app_activa,
            "sync_estado": sync_estado,
            "stock_resumen": stock_resumen,
            "flash_ok": ok,
            "flash_error": error,
        },
    )
    if borrar_claim:
        respuesta.delete_cookie("tn_claim")
    return respuesta


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
    try:
        from servicios.shopify_catalogo import lanzar_sincronizacion
        lanzar_sincronizacion(dominio, cliente)
    except Exception as e:
        print(f"[portal] no pude lanzar sync al vincular {dominio}: {type(e).__name__}")
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

    try:
        markup_num = _numero_form(
            markup_pct, "Porcentaje de ganancia", minimo=0, maximo=300
        ) or 0
        precio_fijo_num = _numero_form(
            precio_fijo_ars, "Precio fijo", importe=True, minimo=0
        ) or 0
        tax_num = _numero_form(
            tax_pct_default, "Porcentaje de impuestos", minimo=0, maximo=100
        ) or 0
        r = guardar_config(
            dominio=dominio, cliente_id=cliente, politica=politica,
            markup_pct=markup_num, precio_fijo_ars=precio_fijo_num,
            mostrar_tax=bool(mostrar_tax), tax_pct_default=tax_num,
            etiqueta=etiqueta,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/portal/tienda?error={quote(str(exc))}", status_code=303
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
    if (plataforma or "").strip().lower() == "shopify":
        # App Store 2.3.1: Shopify se instala exclusivamente mediante OAuth
        # oficial. No alcanza con ocultar el formulario; la ruta POST también
        # debe rechazar una invocación manual directa.
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "Shopify se conecta únicamente instalando la app oficial de TAURO desde Shopify Apps."
            ),
            status_code=303,
        )
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
    from servicios.tiendanube_app import (
        app_configurada, firmar_oauth_cookie, url_instalacion,
    )
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
        key="tn_oauth", value=firmar_oauth_cookie(state, cliente),
        httponly=True, max_age=600, samesite="lax", secure=COOKIE_SECURE,
    )
    return resp


@router.get("/tienda/tiendanube/pedidos")
def tienda_tiendanube_admin_link(
    request: Request,
    cliente: str = Depends(cliente_actual),
):
    """Destino autenticado para los admin links individual y masivo."""
    store_id = str(request.query_params.get("store") or "").strip()
    order_ids = [
        str(value).strip()[:80]
        for value in request.query_params.getlist("id")[:100]
        if str(value).strip()
    ]
    if not store_id.isdigit():
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "Tiendanube no informó una tienda válida."
            ),
            status_code=303,
        )
    try:
        from servicios.tiendanube_app import instalacion

        instalada = instalacion(store_id) or {}
        owner = str(instalada.get("cliente_id") or "").strip().upper()
        operativa = (
            owner == cliente.strip().upper()
            and instalada.get("estado") == "ACTIVA"
            and bool(instalada.get("webhooks_ready"))
        )
    except Exception as exc:
        print(f"[tiendanube] admin link no validado: {type(exc).__name__}")
        operativa = False
    if not operativa:
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "La tienda indicada no está vinculada a tu cuenta TAURO."
            ),
            status_code=303,
        )
    # El link individual/masivo tiene que ejecutar una acción real sobre los
    # IDs seleccionados. La vista usa sólo pedidos ya importados, acotados al
    # owner autenticado y a esta tienda; nunca consulta ni muestra otra cuenta.
    seleccion = listar_pedidos_tiendanube_seleccionados(
        cliente,
        store_id,
        order_ids,
    )
    encontrados = {
        str(pedido.get("pedido_externo_id") or "") for pedido in seleccion
    }
    return templates.TemplateResponse(
        request=request,
        name="portal/tiendanube_pedidos.html",
        context={
            "cliente": cliente,
            "store_id": store_id,
            "pedidos": seleccion,
            "solicitados": len(order_ids),
            "faltantes": len(set(order_ids) - encontrados),
        },
    )


@router.post("/tienda/desconectar")
def tienda_desconectar(
    tienda_id: int = Form(...),
    cliente: str = Depends(cliente_actual),
):
    desconectar_tienda(cliente, tienda_id)
    return RedirectResponse(url="/portal/tienda?ok=desconectada", status_code=303)


@router.post("/tienda/reiniciar-shopify")
def tienda_reiniciar_shopify(
    dominio: str = Form(...),
    confirmar: str = Form(...),
    cliente: str = Depends(cliente_actual),
):
    if confirmar != "REINICIAR":
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "Escribí REINICIAR para confirmar el borrado del espejo Shopify."
            ),
            status_code=303,
        )
    try:
        reporte = reiniciar_integracion_shopify_cliente(cliente, dominio)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/portal/tienda?error={quote(str(exc))}", status_code=303,
        )
    except Exception as exc:
        print(f"[shopify] reinicio controlado falló: {type(exc).__name__}")
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "No pudimos reiniciar la integración. No se aplicaron cambios parciales."
            ),
            status_code=303,
        )
    mensaje = (
        f"Integración reiniciada: se retiraron {reporte['productos']} productos "
        "del espejo TAURO. Shopify no fue modificado."
    )
    return RedirectResponse(
        url=f"/portal/tienda?ok={quote(mensaje)}", status_code=303,
    )


@router.post("/tienda/limpiar-shopify-legado")
def tienda_limpiar_shopify_legado(
    confirmar: str = Form(...),
    cliente: str = Depends(cliente_actual),
):
    if confirmar != "LIMPIAR":
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "Escribí LIMPIAR para confirmar la limpieza del catálogo anterior."
            ),
            status_code=303,
        )
    try:
        reporte = limpiar_espejo_shopify_huerfano_cliente(cliente)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/portal/tienda?error={quote(str(exc))}", status_code=303,
        )
    except Exception as exc:
        print(f"[shopify] limpieza de espejo legado falló: {type(exc).__name__}")
        return RedirectResponse(
            url="/portal/tienda?error=" + quote(
                "No pudimos limpiar el catálogo anterior. No hubo cambios parciales."
            ),
            status_code=303,
        )
    mensaje = (
        f"Catálogo anterior retirado: {reporte['productos']} productos Shopify. "
        "Tus guías y tu cuenta corriente siguen intactas."
    )
    return RedirectResponse(
        url=f"/portal/tienda?ok={quote(mensaje)}", status_code=303,
    )


@router.post("/tienda/pedidos/descartar")
def tienda_pedido_descartar(
    pedido_id: int = Form(...),
    cliente: str = Depends(cliente_actual),
):
    descartar_pedido(cliente, pedido_id)
    return RedirectResponse(url="/portal/tienda?ok=descartado", status_code=303)


@router.post("/tienda/sincronizar-catalogo")
def tienda_sincronizar_catalogo(cliente: str = Depends(cliente_actual)):
    """Reimporta el catálogo desde Shopify a pedido del cliente. El alta ya
    ocurre sola al instalar; esto es para volver a traer productos nuevos."""
    try:
        from servicios.shopify_catalogo import solicitar_sincronizacion_cliente
        r = solicitar_sincronizacion_cliente(cliente)
    except Exception as e:
        print(f"[portal] error sincronizando catálogo: {type(e).__name__}")
        r = {"ok": False, "error": "No pudimos sincronizar ahora. Probá de nuevo."}
    if not r.get("ok"):
        return RedirectResponse(
            url=f"/portal/tienda?error={quote(r.get('error', 'No se pudo sincronizar.'))}",
            status_code=303)
    msg = "Sincronización iniciada. Podés seguir trabajando; el stock se actualiza en segundo plano."
    return RedirectResponse(url=f"/portal/tienda?ok={quote(msg)}", status_code=303)


# ── Mis clientes (destinatarios frecuentes) ─────────────────
@router.get("/clientes", response_class=HTMLResponse)
def clientes_view(
    request: Request,
    ok: Optional[str] = None,
    error: Optional[str] = None,
    cliente: str = Depends(cliente_actual),
):
    paises = _paises_con_nacional()
    flash_ok = None
    if ok == "1":
        flash_ok = "Cliente guardado en tu base."
    elif ok == "2":
        flash_ok = "Cliente eliminado de tu base."
    return templates.TemplateResponse(
        request=request, name="portal/clientes.html",
        context={
            "cliente": cliente,
            # listar_direcciones siempre filtra por el cliente de la sesión.
            "clientes_guardados": listar_direcciones(cliente, TIPO_DESTINATARIO),
            "paises": paises,
            "paises_por_iso": dict(paises),
            "flash_ok": flash_ok,
            "error": error,
        },
    )


@router.post("/clientes")
def clientes_add(
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
    notas: str = Form(""),
    direccion_id: str = Form(""),
    cliente: str = Depends(cliente_actual),
):
    # El tipo y el dueño no llegan desde el navegador: esta ruta sólo crea
    # destinatarios y los asigna a la cuenta autenticada.
    pais = (pais or "").strip().upper()
    if pais not in {iso for iso, _nombre in _paises_con_nacional()}:
        return RedirectResponse(
            url=f"/portal/clientes?error={quote('Elegí un país válido para el cliente.')}",
            status_code=303,
        )
    campos = dict(
        cliente_id=cliente,
        tipo=TIPO_DESTINATARIO,
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
        predeterminada=False,
        notas=notas,
    )
    try:
        dir_id = _id_opt(direccion_id)
        if dir_id:
            actual = obtener_direccion(cliente, dir_id, TIPO_DESTINATARIO)
            if not actual:
                raise ValueError("Ese cliente no existe o no pertenece a tu cuenta.")
            if not actualizar_direccion(
                dir_id, tipo_actual=TIPO_DESTINATARIO, **campos
            ):
                raise ValueError("Ese cliente no existe o no pertenece a tu cuenta.")
        else:
            crear_direccion(**campos)
    except Exception as e:
        return RedirectResponse(url=f"/portal/clientes?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url="/portal/clientes?ok=1", status_code=303)


@router.post("/clientes/{direccion_id}/eliminar")
def clientes_delete(
    direccion_id: int,
    cliente: str = Depends(cliente_actual),
):
    try:
        actual = obtener_direccion(cliente, direccion_id, TIPO_DESTINATARIO)
        if not actual:
            raise ValueError("Ese cliente no existe o no pertenece a tu cuenta.")
        if not eliminar_direccion(cliente, direccion_id, TIPO_DESTINATARIO):
            raise ValueError("Ese cliente no existe o no pertenece a tu cuenta.")
    except Exception as e:
        return RedirectResponse(url=f"/portal/clientes?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url="/portal/clientes?ok=2", status_code=303)


# ── Direcciones de origen y libreta avanzada ───────────────
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
def catalogo_view(
    request: Request,
    pagina: int = 1,
    cliente: str = Depends(cliente_actual),
):
    # Dos filas mantienen el catálogo dentro de una pantalla común. Si hay
    # más, se navegan por páginas: no hay una lista infinita para scrollear.
    por_pagina = 2
    pagina = max(1, int(pagina or 1))
    productos, productos_total = listar_stock_cliente(
        cliente, por_pagina, (pagina - 1) * por_pagina,
    )
    total_paginas = max(1, (productos_total + por_pagina - 1) // por_pagina)
    if pagina > total_paginas:
        pagina = total_paginas
        productos, productos_total = listar_stock_cliente(
            cliente, por_pagina, (pagina - 1) * por_pagina,
        )
    try:
        from servicios.catalogo import estado_sincronizacion_cliente, resumen_stock_cliente
        sync_estado = estado_sincronizacion_cliente(cliente)
        stock_resumen = resumen_stock_cliente(cliente)
    except Exception as e:
        print(f"[catalogo] no pude leer estado de stock: {type(e).__name__}")
        sync_estado = None
        stock_resumen = {}
    try:
        taxes = tax_de_productos(cliente)
    except Exception as e:
        print(f"[catalogo] no pude leer los tax: {type(e).__name__}")
        taxes = {}
    return templates.TemplateResponse(
        request=request, name="portal/catalogo.html",
        context={"cliente": cliente, "productos": productos,
                 "productos_total": productos_total,
                 "pagina": pagina, "total_paginas": total_paginas,
                 "taxes": taxes,
                 "sync_estado": sync_estado, "stock_resumen": stock_resumen,
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
    largo_cm: str = Form(...),
    ancho_cm: str = Form(...),
    alto_cm: str = Form(...),
    peso_kg: str = Form(...),
    valor_usd_default: str = Form(...),
    tax_estimado_usd: str = Form("0"),
    alias_original: str = Form(""),  # presente = editar en vez de crear
    cliente: str = Depends(cliente_actual),
):
    try:
        largo_num = _numero_form(largo_cm, "Largo", minimo=0.01)
        ancho_num = _numero_form(ancho_cm, "Ancho", minimo=0.01)
        alto_num = _numero_form(alto_cm, "Alto", minimo=0.01)
        peso_num = _numero_form(peso_kg, "Peso", minimo=0.01)
        valor_num = _numero_form(
            valor_usd_default, "Valor declarado", importe=True, minimo=0
        )
        tax_num = _numero_form(
            tax_estimado_usd, "Impuestos estimados", importe=True,
            requerido=False, minimo=0,
        ) or 0
        nuevo = ProductoNuevo(
            alias_interno=alias_interno, nombre_invoice=nombre_invoice,
            hs_code=hs_code, largo_cm=largo_num, ancho_cm=ancho_num,
            alto_cm=alto_num, peso_kg=peso_num, valor_usd_default=valor_num,
        )
        if alias_original.strip():
            if not actualizar_producto_cliente(cliente, alias_original, nuevo):
                raise ValueError("Ese producto no existe en tu catálogo.")
        else:
            agregar_producto(cliente, nuevo)
        # Campo opcional, guardado aparte para no tocar el alta del catálogo.
        try:
            guardar_tax_producto(cliente, alias_interno, tax_num)
        except Exception as e:
            print(f"[catalogo] no pude guardar el tax: {type(e).__name__}")
    except ValueError as e:
        return RedirectResponse(
            url=f"/portal/catalogo?error={quote(str(e))}", status_code=303,
        )
    except Exception as e:
        print(f"[catalogo] alta/edición falló: {type(e).__name__}")
        return RedirectResponse(
            url="/portal/catalogo?error=No%20pudimos%20guardar%20el%20producto.",
            status_code=303,
        )
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
