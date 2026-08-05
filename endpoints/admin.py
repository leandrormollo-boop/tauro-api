# ============================================================
# Panel de administración — /admin/...
# ============================================================
# Autenticación simple por contraseña (ADMIN_PASSWORD en env).
# Cookie httponly "admin_token" durante 8hs.
# ============================================================

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, Cookie, Depends, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from core.database import get_conn
from servicios.cuenta_corriente import (
    registrar_pago, registrar_envio, cancelar_envio,
    get_envios_cliente, get_pagos,
    get_facturado_real, total_pagado, saldo,
    get_resumen_clientes_bulk,
)
from servicios.catalogo import (
    get_productos_pendientes, get_todos_productos,
    aprobar_producto, rechazar_producto,
)
from servicios.rutas import get_todas_las_rutas, upsert_ruta, toggle_ruta
from servicios.impuestos import normalizar as normalizar_tax


def _courier_valido(valor: str) -> str:
    """fedex | dhl | ups, o '' = el cliente elige en cada envío."""
    v = (valor or "").strip().lower()
    return v if v in ("fedex", "dhl", "ups") else ""
from servicios.pricing import (
    PRICING_MODES, describir_pricing, parse_monto_ars, parse_pricing_value,
)
from servicios.solicitudes_guia import (
    ESTADOS_SOLICITUD,
    actualizar_solicitud_guia,
    contar_solicitudes_pendientes,
    listar_solicitudes_admin,
    generar_guia,
    obtener_label_pdf,
)
from servicios.tracking_fedex_tauro import (
    fedex_environment,
    get_tracking_summary,
    load_state as load_tracking_state,
    reset_tracking_checkpoint,
    run_tracking,
)
from modelos.ruta import Ruta
from servicios.rate_limit import check_rate, reset_rate, client_ip


router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="templates")

from servicios.couriers_urls import es_nacional, url_tracking
templates.env.globals["url_tracking"] = url_tracking
templates.env.globals["es_nacional"] = es_nacional


def _pendientes_admin() -> int:
    """Globo rojo del menú: guías esperando que Tauro las emita."""
    try:
        from servicios.bandeja_admin import total_pendiente_tauro
        return total_pendiente_tauro()
    except Exception:
        return 0


templates.env.globals["pendientes_admin"] = _pendientes_admin

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
if not ADMIN_PASSWORD:
    # Nunca un default público. Si no hay contraseña configurada, generamos una
    # aleatoria efímera y la dejamos en el log (visible en Railway) para que el
    # dueño la vea una vez y configure ADMIN_PASSWORD en las variables de entorno.
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    print("[admin] ⚠️  ADMIN_PASSWORD no está configurada en el entorno.")
    print(f"[admin] ⚠️  Contraseña temporal (cambia en cada restart): {ADMIN_PASSWORD}")
    print("[admin] ⚠️  Configurá ADMIN_PASSWORD en Railway → Variables cuanto antes.")

# En producción (HTTPS) las cookies deben ir con Secure. Default seguro: activado
# salvo que se apague explícitamente para desarrollo local por HTTP.
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"

# Token en memoria (se regenera en cada restart — suficiente para un solo admin)
_ADMIN_TOKEN: str = secrets.token_urlsafe(32)

_MIGRATION_LOCK = threading.Lock()
_MIGRATION_STATUS = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "output": "",
}

_TRACKING_LOCK = threading.Lock()
_TRACKING_STATUS = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "output": "",
    "result": None,
}


# ── Auth ────────────────────────────────────────────────────

def admin_actual(admin_token: Optional[str] = Cookie(None)) -> bool:
    if admin_token and admin_token == _ADMIN_TOKEN:
        return True
    raise Exception("no auth")


def check_admin(admin_token: Optional[str] = Cookie(None)) -> bool:
    return admin_token == _ADMIN_TOKEN


def require_admin(admin_token: Optional[str] = Cookie(None)):
    if admin_token != _ADMIN_TOKEN:
        raise Exception("redirect")


# ── Helpers internos ─────────────────────────────────────────

def _get_clientes_lista():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM clientes ORDER BY cliente_id")
            clientes = [dict(r) for r in cur.fetchall()]
    for cliente in clientes:
        cliente["pricing_desc"] = describir_pricing(cliente)
    return clientes


def _get_config():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM config ORDER BY parametro")
            return [dict(r) for r in cur.fetchall()]


def _redirect_login():
    return RedirectResponse(url="/admin/login", status_code=303)


def _is_auth(admin_token: Optional[str]) -> bool:
    return admin_token == _ADMIN_TOKEN


def _migration_snapshot() -> dict:
    with _MIGRATION_LOCK:
        return dict(_MIGRATION_STATUS)


def _run_sheets_migration():
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "migrate_sheets_to_postgres.py"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=15 * 60,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        with _MIGRATION_LOCK:
            _MIGRATION_STATUS.update({
                "running": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "returncode": result.returncode,
                "output": output[-40000:],
            })
    except Exception as e:
        with _MIGRATION_LOCK:
            _MIGRATION_STATUS.update({
                "running": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "returncode": -1,
                "output": f"Error ejecutando migración: {e}",
            })


def _tracking_snapshot() -> dict:
    with _TRACKING_LOCK:
        return dict(_TRACKING_STATUS)


def _run_tracking_fedex_job(mode: str, limit: int | None, dry_run: bool, target: str):
    try:
        result = run_tracking(mode=mode, limit=limit, dry_run=dry_run, target=target)
        output = json_dumps_pretty(result)
        with _TRACKING_LOCK:
            _TRACKING_STATUS.update({
                "running": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "returncode": 0,
                "output": output[-40000:],
                "result": result,
            })
    except Exception as e:
        with _TRACKING_LOCK:
            _TRACKING_STATUS.update({
                "running": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "returncode": -1,
                "output": f"Error ejecutando tracking FedEx: {e}",
                "result": None,
            })


def json_dumps_pretty(value: dict) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


# ── Login ───────────────────────────────────────────────────

def _totp_secret() -> str:
    """Segundo factor del admin. Vacío = apagado (login sólo con contraseña)."""
    return os.getenv("ADMIN_TOTP_SECRET", "").strip()


@router.get("/login", response_class=HTMLResponse)
def admin_login_form(request: Request, error: Optional[str] = None):
    ctx = {"totp_activo": bool(_totp_secret())}
    if error == "link_vencido":
        ctx["error"] = "Ese link ya se usó o venció. Pedí uno nuevo."
    return templates.TemplateResponse(
        request=request, name="admin/login.html", context=ctx
    )


@router.post("/login", response_class=HTMLResponse)
def admin_login(request: Request, password: str = Form(...),
                codigo: str = Form(default="")):
    ip = client_ip(request)
    totp_activo = bool(_totp_secret())
    if not check_rate(f"admin_login:{ip}", max_attempts=5, window_seconds=300):
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "Demasiados intentos. Esperá unos minutos e intentá de nuevo.",
                     "totp_activo": totp_activo},
            status_code=429,
        )
    # Comparación de tiempo constante para no filtrar la contraseña por timing.
    password_ok = secrets.compare_digest(password, ADMIN_PASSWORD)

    # Segundo factor (si ADMIN_TOTP_SECRET está cargada). El código se CONSUME
    # (anti-replay) sólo si la contraseña ya es correcta: así un atacante no
    # puede 'quemar' el código del dueño mandándolo con una contraseña basura.
    # El error es UNO solo: no se revela cuál de los dos factores falló.
    totp_ok = True
    if totp_activo:
        if password_ok:
            from servicios.totp import verificar_codigo
            totp_ok = verificar_codigo(_totp_secret(), codigo)
        else:
            totp_ok = False

    if not (password_ok and totp_ok):
        from servicios.auditoria import registrar_desde_request
        registrar_desde_request(request, event="admin.login", actor_type="admin",
                                success=False, status_code=401)
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "Contraseña o código incorrecto." if totp_activo
                     else "Contraseña incorrecta.",
                     "totp_activo": totp_activo},
            status_code=401,
        )
    reset_rate(f"admin_login:{ip}")
    from servicios.auditoria import registrar_desde_request
    registrar_desde_request(request, event="admin.login", actor_type="admin",
                            success=True, status_code=303)
    response = RedirectResponse(url="/admin/home", status_code=303)
    response.set_cookie(
        key="admin_token", value=_ADMIN_TOKEN,
        httponly=True, max_age=60 * 60 * 8,
        samesite="lax", secure=COOKIE_SECURE,
    )
    return response


# ── Recuperar acceso al admin ───────────────────────────────
# Si el dueño olvidó ADMIN_PASSWORD, se manda un link de un solo uso al
# email oficial de TAURO (no a uno que escriba quien pide: así el link
# sólo le llega a quien controla esa casilla). Dura 15 minutos.
#
# Los tokens van a la BASE DE DATOS, no a memoria: un deploy o un
# reinicio de Railway invalidaría todos los links en vuelo, y el momento
# en que más se necesita esto es justamente cuando algo se reinició.
_RECUPERO_MINUTOS = 15


def _ensure_tabla_recupero() -> None:
    from core.database import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_recupero (
                    token   TEXT PRIMARY KEY,
                    vence   TIMESTAMPTZ NOT NULL,
                    usado   BOOLEAN NOT NULL DEFAULT FALSE,
                    creado  TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
        conn.commit()


def _guardar_token_recupero(token: str) -> None:
    from core.database import get_conn
    _ensure_tabla_recupero()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admin_recupero (token, vence) "
                "VALUES (%s, now() + interval '%s minutes')",
                (token, _RECUPERO_MINUTOS),
            )
            # Limpieza oportunista de los vencidos.
            cur.execute("DELETE FROM admin_recupero WHERE vence < now() - interval '1 day'")
        conn.commit()


def _canjear_token_recupero(token: str) -> bool:
    """True sólo la primera vez que se usa un token válido y vigente."""
    from core.database import get_conn
    _ensure_tabla_recupero()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE admin_recupero SET usado = TRUE
                WHERE token = %s AND usado = FALSE AND vence > now()
                RETURNING token
            """, (token,))
            ok = cur.fetchone() is not None
        conn.commit()
    return ok


def _borrar_token_recupero(token: str) -> None:
    from core.database import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admin_recupero WHERE token = %s", (token,))
            conn.commit()
    except Exception as e:
        print(f"[admin] no pude borrar el token de recuperación: {e}")


# La casilla oficial de TAURO. Es el ÚNICO destino posible del link de
# recuperación: así no importa quién apriete el botón, el acceso sólo le
# llega a quien controla este mail.
EMAIL_OFICIAL_TAURO = "taurosolutionsar@gmail.com"


def _email_oficial() -> str:
    return (os.getenv("ADMIN_RECOVERY_EMAIL") or EMAIL_OFICIAL_TAURO).strip()


def _smtp_listo() -> bool:
    return bool(os.getenv("EMAIL_REMITENTE") and os.getenv("EMAIL_PASSWORD"))


templates.env.globals["smtp_listo"] = _smtp_listo
templates.env.globals["email_recupero"] = _email_oficial


@router.post("/recuperar", response_class=HTMLResponse)
def admin_recuperar(request: Request):
    """Manda un link de acceso temporal al email oficial de TAURO."""
    ip = client_ip(request)
    # Más restrictivo que el login: 3 pedidos por hora.
    if not check_rate(f"admin_recuperar:{ip}", max_attempts=3, window_seconds=3600):
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "Ya pediste varios links. Esperá un rato."},
            status_code=429,
        )

    destino = _email_oficial()
    if not destino or not _smtp_listo():
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "El envío de mails todavía no está configurado. "
                              "Cambiá ADMIN_PASSWORD en Railway → Variables."},
            status_code=503,
        )

    token = secrets.token_urlsafe(32)
    try:
        _guardar_token_recupero(token)
    except Exception as e:
        print(f"[admin] no pude guardar el token de recuperación: {e}")
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "No pudimos generar el link. Cambiá ADMIN_PASSWORD "
                              "en Railway → Variables."},
            status_code=500,
        )

    base = (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
    link = f"{base}/admin/recuperar/{token}"
    try:
        from core.email_sender import enviar_link_magico
        enviado = enviar_link_magico(destino, link, "equipo Tauro",
                                     vence_en=f"{_RECUPERO_MINUTOS} minutos")
    except Exception as e:
        print(f"[admin] no pude mandar el link de recuperación: {e}")
        enviado = False

    if not enviado:
        # El link ya no sirve si nadie lo recibió: lo quemamos.
        _borrar_token_recupero(token)
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "No pudimos enviar el mail (revisá la config de SMTP). "
                              "Mientras tanto, cambiá ADMIN_PASSWORD en Railway → Variables."},
            status_code=502,
        )

    tapado = destino[:2] + "•••" + destino[destino.find("@"):] if "@" in destino else "tu email"
    return templates.TemplateResponse(
        request=request, name="admin/login.html",
        context={"aviso": f"Listo — te mandamos un link de acceso a {tapado}. "
                          f"Vence en {_RECUPERO_MINUTOS} minutos."},
    )


@router.get("/recuperar/{token}")
def admin_recuperar_usar(token: str):
    """Canjea el link por una sesión de admin. Un solo uso."""
    try:
        valido = _canjear_token_recupero(token)
    except Exception as e:
        print(f"[admin] error canjeando token: {e}")
        valido = False
    if not valido:
        return RedirectResponse(
            url="/admin/login?error=link_vencido", status_code=303
        )
    response = RedirectResponse(url="/admin/home", status_code=303)
    response.set_cookie(
        key="admin_token", value=_ADMIN_TOKEN,
        httponly=True, max_age=60 * 60 * 8,
        samesite="lax", secure=COOKIE_SECURE,
    )
    print("[admin] acceso recuperado por link de email")
    return response


@router.get("/logout")
def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_token")
    return response


# ── Dashboard ────────────────────────────────────────────────

@router.get("/home", response_class=HTMLResponse)
def admin_home(request: Request, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()

    # Stats generales — 4 COUNT en una sola conexión
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM clientes WHERE activo=TRUE) AS clientes_activos,
                    (SELECT COUNT(*) FROM envios)                     AS total_envios,
                    (SELECT COUNT(*) FROM pagos)                      AS total_pagos,
                    (SELECT COUNT(*) FROM productos WHERE activo=FALSE) AS productos_pendientes
            """)
            stats_row = cur.fetchone()
            clientes_activos     = stats_row["clientes_activos"]
            total_envios         = stats_row["total_envios"]
            total_pagos          = stats_row["total_pagos"]
            productos_pendientes = stats_row["productos_pendientes"]
    solicitudes_pendientes = contar_solicitudes_pendientes()

    # Resumen por cliente — bulk query (reemplaza N+1)
    resumen = get_resumen_clientes_bulk(solo_activos=True)

    return templates.TemplateResponse(
        request=request, name="admin/home.html",
        context={
            "seccion": "home",
            "stats": {
                "clientes_activos": clientes_activos,
                "total_envios": total_envios,
                "total_pagos": total_pagos,
                "productos_pendientes": productos_pendientes,
                "solicitudes_pendientes": solicitudes_pendientes,
            },
            "resumen_clientes": resumen,
        },
    )


@router.get("/seguridad", response_class=HTMLResponse)
def admin_seguridad(request: Request, admin_token: Optional[str] = Cookie(None)):
    """
    Registro de accesos y acciones sensibles: quién entró (y quién falló), y
    qué se tocó de dinero/credenciales/accesos, con IP y momento. Alimenta el
    template que ya existía; los eventos los graba servicios/auditoria.py.
    """
    if not _is_auth(admin_token):
        return _redirect_login()

    stats = {"eventos_24h": 0, "fallos_24h": 0, "csrf_24h": 0}
    eventos = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS eventos_24h,
                        COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours'
                                         AND success = FALSE) AS fallos_24h,
                        COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours'
                                         AND event LIKE '%%csrf%%') AS csrf_24h
                    FROM security_audit
                """)
                fila = cur.fetchone()
                if fila:
                    stats = dict(fila)
                cur.execute("""
                    SELECT created_at, event, actor_type, actor_ref, ip, method,
                           path, status_code, success, request_id
                    FROM security_audit
                    ORDER BY created_at DESC
                    LIMIT 200
                """)
                eventos = cur.fetchall()
    except Exception as e:
        # La tabla se crea en init_db; si por lo que sea no está, la página
        # abre igual (vacía) en vez de tirar 500.
        print(f"[admin] /seguridad sin datos: {type(e).__name__}: {e}")

    return templates.TemplateResponse(
        request=request, name="admin/seguridad.html",
        context={"seccion": "seguridad", "stats": stats, "eventos": eventos},
    )


# ── Centro comercial / agentes ──────────────────────────────

_FLASH_COMERCIAL = {
    "cuenta-creada": "Cuenta comercial creada.",
    "descubrimiento-encolado": "Investigacion de mercado agregada a la cola.",
    "investigacion-encolada": "Investigacion de empresa agregada a la cola.",
    "propuesta-encolada": "Redaccion y revision agregadas a la cola.",
    "contacto-verificado": "Email comercial marcado como verificado.",
    "mensaje-aprobado": "Borrador aprobado. Todavia no fue enviado.",
    "mensaje-enviado": "Correo enviado y registrado.",
    "mensaje-cancelado": "Mensaje cancelado.",
}


def _redirect_comercial(resultado: str):
    return RedirectResponse(url=f"/admin/comercial?resultado={resultado}", status_code=303)


@router.get("/comercial", response_class=HTMLResponse)
def admin_comercial(request: Request, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.crm_comercial import obtener_dashboard

    try:
        datos = obtener_dashboard()
        error = None
    except Exception as exc:
        print(f"[admin] centro comercial sin datos: {type(exc).__name__}: {exc}")
        datos = {
            "stats": {"cuentas": 0, "calificadas": 0, "por_aprobar": 0, "enviados": 0, "trabajos_pendientes": 0},
            "cuentas": [], "mensajes": [], "trabajos": [], "leads_web_pendientes": 0,
            "icp": {}, "ia_configurada": False,
        }
        error = "No se pudo abrir el CRM. Revisa la inicializacion de la base de datos."
    resultado = request.query_params.get("resultado", "")
    return templates.TemplateResponse(
        request=request,
        name="admin/comercial.html",
        context={
            "seccion": "comercial",
            **datos,
            "flash_ok": _FLASH_COMERCIAL.get(resultado),
            "flash_error": error or ("La accion no pudo completarse." if resultado == "error" else None),
        },
    )


@router.post("/comercial/cuentas/nueva")
def admin_comercial_cuenta_nueva(
    request: Request,
    empresa: str = Form(...),
    dominio: str = Form(""),
    pais: str = Form("AR"),
    segmento: str = Form("OTRO"),
    contacto_nombre: str = Form(""),
    contacto_cargo: str = Form(""),
    contacto_email: str = Form(""),
    email_verificado: Optional[str] = Form(None),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import crear_cuenta
        from servicios.auditoria import registrar_desde_request

        cuenta_id = crear_cuenta(
            empresa=empresa,
            dominio=dominio,
            pais=pais,
            segmento=segmento,
            contacto_nombre=contacto_nombre,
            contacto_cargo=contacto_cargo,
            contacto_email=contacto_email,
            email_verificado=bool(email_verificado),
        )
        registrar_desde_request(
            request, event="crm.cuenta_creada", actor_type="admin",
            actor_ref="admin", metadata={"cuenta_id": cuenta_id},
        )
        return _redirect_comercial("cuenta-creada")
    except Exception as exc:
        print(f"[admin] crear cuenta comercial fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


@router.post("/comercial/descubrir")
def admin_comercial_descubrir(
    request: Request,
    brief: str = Form(...),
    limite: int = Form(10),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import encolar_trabajo
        from servicios.auditoria import registrar_desde_request

        job_id = encolar_trabajo("DESCUBRIR", payload={"brief": brief[:3000], "limite": max(1, min(limite, 20))})
        registrar_desde_request(
            request, event="crm.descubrimiento_encolado", actor_type="admin",
            actor_ref="admin", metadata={"job_id": job_id, "limite": max(1, min(limite, 20))},
        )
        return _redirect_comercial("descubrimiento-encolado")
    except Exception as exc:
        print(f"[admin] encolar descubrimiento fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


@router.post("/comercial/cuentas/{cuenta_id}/investigar")
def admin_comercial_investigar(
    request: Request,
    cuenta_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import encolar_trabajo
        job_id = encolar_trabajo("INVESTIGAR", cuenta_id=cuenta_id)
        from servicios.auditoria import registrar_desde_request
        registrar_desde_request(
            request, event="crm.investigacion_encolada", actor_type="admin",
            actor_ref="admin", metadata={"cuenta_id": cuenta_id, "job_id": job_id},
        )
        return _redirect_comercial("investigacion-encolada")
    except Exception as exc:
        print(f"[admin] encolar investigacion fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


@router.post("/comercial/cuentas/{cuenta_id}/propuesta")
def admin_comercial_propuesta(
    request: Request,
    cuenta_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import encolar_trabajo
        job_id = encolar_trabajo("PROPUESTA", cuenta_id=cuenta_id)
        from servicios.auditoria import registrar_desde_request
        registrar_desde_request(
            request, event="crm.propuesta_encolada", actor_type="admin",
            actor_ref="admin", metadata={"cuenta_id": cuenta_id, "job_id": job_id},
        )
        return _redirect_comercial("propuesta-encolada")
    except Exception as exc:
        print(f"[admin] encolar propuesta fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


@router.post("/comercial/contactos/{contacto_id}/verificar")
def admin_comercial_verificar_contacto(
    request: Request,
    contacto_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import verificar_contacto
        from servicios.auditoria import registrar_desde_request
        verificar_contacto(contacto_id)
        registrar_desde_request(
            request, event="crm.contacto_verificado", actor_type="admin",
            actor_ref="admin", metadata={"contacto_id": contacto_id},
        )
        return _redirect_comercial("contacto-verificado")
    except Exception as exc:
        print(f"[admin] verificar contacto fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


@router.post("/comercial/mensajes/{mensaje_id}/aprobar")
def admin_comercial_aprobar_mensaje(
    request: Request,
    mensaje_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import aprobar_mensaje
        from servicios.auditoria import registrar_desde_request
        aprobar_mensaje(mensaje_id)
        registrar_desde_request(
            request, event="crm.mensaje_aprobado", actor_type="admin",
            actor_ref="admin", metadata={"mensaje_id": mensaje_id},
        )
        return _redirect_comercial("mensaje-aprobado")
    except Exception as exc:
        print(f"[admin] aprobar mensaje fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


@router.post("/comercial/mensajes/{mensaje_id}/enviar")
def admin_comercial_enviar_mensaje(
    request: Request,
    mensaje_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import enviar_mensaje
        from servicios.auditoria import registrar_desde_request
        enviado = enviar_mensaje(mensaje_id)
        registrar_desde_request(
            request, event="crm.mensaje_envio", actor_type="admin",
            actor_ref="admin", success=enviado, metadata={"mensaje_id": mensaje_id},
        )
        return _redirect_comercial("mensaje-enviado" if enviado else "error")
    except Exception as exc:
        print(f"[admin] enviar mensaje fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


@router.post("/comercial/mensajes/{mensaje_id}/cancelar")
def admin_comercial_cancelar_mensaje(
    request: Request,
    mensaje_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    try:
        from servicios.crm_comercial import cancelar_mensaje
        cancelar_mensaje(mensaje_id)
        return _redirect_comercial("mensaje-cancelado")
    except Exception as exc:
        print(f"[admin] cancelar mensaje fallo: {type(exc).__name__}: {exc}")
        return _redirect_comercial("error")


# ── Clientes ─────────────────────────────────────────────────

@router.get("/clientes", response_class=HTMLResponse)
def admin_clientes(request: Request, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    clientes = _get_clientes_lista()
    return templates.TemplateResponse(
        request=request, name="admin/clientes.html",
        context={"seccion": "clientes", "clientes": clientes},
    )


@router.get("/bandeja", response_class=HTMLResponse)
def admin_bandeja(request: Request, admin_token: Optional[str] = Cookie(None)):
    """Carga de trabajo por cliente: quién tiene qué esperando."""
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.bandeja_admin import resumen_por_cliente
    from servicios.tarifas_cache import estado_cache
    try:
        filas = resumen_por_cliente()
    except Exception as e:
        print(f"[admin] bandeja falló: {e}")
        filas = []
    return templates.TemplateResponse(
        request=request, name="admin/bandeja.html",
        context={
            "seccion": "bandeja",
            "filas": filas,
            "total_tauro": sum(f["pendiente_tauro"] for f in filas),
            "total_cliente": sum(f["pendiente_cliente"] for f in filas),
            "cache": estado_cache(),
        },
    )


@router.get("/backup.json")
def admin_backup(admin_token: Optional[str] = Cookie(None)):
    """Descarga un snapshot de todos los datos del negocio."""
    if not _is_auth(admin_token):
        return _redirect_login()
    from datetime import datetime as _dt
    from servicios.backup import generar_backup_json
    try:
        contenido = generar_backup_json()
    except Exception as e:
        print(f"[admin] backup falló: {e}")
        return JSONResponse({"ok": False, "error": "No se pudo generar el backup."},
                            status_code=500)
    nombre = f"tauro-backup-{_dt.now().strftime('%Y%m%d-%H%M')}.json"
    return Response(
        content=contenido,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.post("/tarifas/refrescar")
def admin_refrescar_tarifas(admin_token: Optional[str] = Cookie(None)):
    """
    Llena la tabla de tarifas del checkout a mano. Tarda (son ~66
    cotizaciones), así que corre en segundo plano y la pantalla vuelve
    enseguida: el resultado se ve en el estado del caché.
    """
    if not _is_auth(admin_token):
        return _redirect_login()
    import threading
    from servicios.tarifas_cache import refrescar_cache
    threading.Thread(target=refrescar_cache, daemon=True).start()
    return RedirectResponse(url="/admin/bandeja?ok=tarifas", status_code=303)


@router.get("/bandeja/{cliente_id}", response_class=HTMLResponse)
def admin_bandeja_cliente(
    request: Request,
    cliente_id: str,
    admin_token: Optional[str] = Cookie(None),
):
    """La pestaña de un cliente: todo lo suyo que está esperando."""
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.bandeja_admin import detalle_cliente
    try:
        datos = detalle_cliente(cliente_id)
    except Exception as e:
        print(f"[admin] detalle de {cliente_id} falló: {e}")
        datos = {"solicitudes": [], "pedidos_tienda": []}
    return templates.TemplateResponse(
        request=request, name="admin/bandeja_cliente.html",
        context={
            "seccion": "bandeja",
            "cliente_id": cliente_id.upper(),
            "solicitudes": datos["solicitudes"],
            "pedidos_tienda": datos["pedidos_tienda"],
        },
    )


@router.get("/clientes/nuevo", response_class=HTMLResponse)
def admin_cliente_nuevo_form(request: Request, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    return templates.TemplateResponse(
        request=request, name="admin/cliente_form.html",
        context={"seccion": "cliente_nuevo", "cliente": None, "pricing_modes": PRICING_MODES},
    )


@router.post("/clientes/nuevo")
def admin_cliente_nuevo(
    request: Request,
    cliente_id: str = Form(...),
    email: str = Form(...),
    password: str = Form(""),
    nombre: str = Form(""),
    cuit: str = Form(""),
    direccion: str = Form(""),
    cp: str = Form(""),
    ciudad: str = Form(""),
    pais: str = Form("AR"),
    telefono: str = Form(""),
    markup_pct: float = Form(25.0),
    markup_tipo: str = Form("PCT"),
    markup_valor: str = Form(""),
    markup_nac_tipo: str = Form(""),
    markup_nac_valor: str = Form(""),
    puede_emitir: str = Form(""),
    tope_deuda_ars: str = Form(""),
    # Quién paga los impuestos de destino por defecto en los envíos de este
    # cliente. Se puede pisar por envío desde el wizard del portal.
    tax_paga: str = Form(""),
    # Courier preferido: WAIMAO opera por DHL y lo deja fijado.
    courier_default: str = Form(""),
    notas: str = Form(""),
    activo: str = Form("true"),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    cliente_id = cliente_id.strip().upper()
    try:
        pricing = parse_pricing_value(markup_valor, markup_tipo, fallback_pct=markup_pct)
        markup_pct_db = pricing["valor"] if pricing["tipo"] == "PCT" else markup_pct
        # Margen nacional aparte (opcional): vacío = usa la regla internacional.
        nac_tipo, nac_valor = None, None
        if markup_nac_tipo.strip():
            nac = parse_pricing_value(markup_nac_valor, markup_nac_tipo,
                                      fallback_pct=markup_pct)
            nac_tipo, nac_valor = nac["tipo"], nac["valor"]
        tope_db = parse_monto_ars(tope_deuda_ars)
        # Hashear password si vino una
        from servicios.auth import hash_password
        password_hash_db = hash_password(password.strip()) if password.strip() else None
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clientes
                        (cliente_id, email, password_hash, markup_pct, markup_tipo, markup_valor, activo,
                         nombre, cuit, direccion, cp, ciudad, pais, telefono, notas,
                         markup_nac_tipo, markup_nac_valor, puede_emitir, tope_deuda_ars, tax_paga, courier_default)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cliente_id, email.strip().lower(), password_hash_db, markup_pct_db,
                        pricing["tipo"], pricing["valor"],
                        activo.lower() == "true",
                        nombre or None, cuit or None, direccion or None,
                        cp or None, ciudad or None, pais or "AR",
                        telefono or None, notas or None,
                        nac_tipo, nac_valor,
                        puede_emitir == "1", tope_db,
                        normalizar_tax(tax_paga),
                        _courier_valido(courier_default),
                    ),
                )
        return RedirectResponse(url=f"/admin/clientes/{cliente_id}?ok=creado", status_code=303)
    except Exception as e:
        return templates.TemplateResponse(
            request=request, name="admin/cliente_form.html",
            context={"seccion": "cliente_nuevo", "cliente": None, "pricing_modes": PRICING_MODES, "flash_error": str(e)},
        )


@router.post("/clientes/{cliente_id}/password")
def admin_cliente_set_password(
    request: Request,
    cliente_id: str,
    new_password: str = Form(...),
    admin_token: Optional[str] = Cookie(None),
):
    """Resetea/setea la contraseña del cliente desde el admin."""
    if not _is_auth(admin_token):
        return _redirect_login()
    cliente_id = cliente_id.strip().upper()
    new_password = new_password.strip()
    if len(new_password) < 6:
        return RedirectResponse(
            url=f"/admin/clientes/{cliente_id}?pwd_error=corta", status_code=303,
        )
    from servicios.auth import set_cliente_password
    set_cliente_password(cliente_id, new_password)
    from servicios.auditoria import registrar_desde_request
    registrar_desde_request(request, event="admin.reset_password", actor_type="admin",
                            actor_ref=cliente_id, metadata={"cliente": cliente_id})
    return RedirectResponse(
        url=f"/admin/clientes/{cliente_id}?ok=pwd_actualizada", status_code=303,
    )


@router.post("/clientes/{cliente_id}/api-key", response_class=HTMLResponse)
def admin_cliente_regenerar_api_key(
    request: Request,
    cliente_id: str,
    admin_token: Optional[str] = Cookie(None),
):
    """
    Genera (o rota) la API key B2B del cliente. La clave se muestra UNA sola
    vez acá — en la base queda sólo el hash, así que no hay forma de volver
    a verla: si se pierde, se regenera. A propósito NO redirige: un redirect
    obligaría a pasar la clave por la URL y quedaría en los access logs.
    """
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.api_b2b import generar_api_key
    cliente_id = cliente_id.strip().upper()
    try:
        clave = generar_api_key(cliente_id)
    except ValueError:
        return RedirectResponse(url="/admin/clientes", status_code=303)
    from servicios.auditoria import registrar_desde_request
    registrar_desde_request(request, event="admin.regenerar_api_key", actor_type="admin",
                            actor_ref=cliente_id, metadata={"cliente": cliente_id})
    return templates.TemplateResponse(
        request=request, name="admin/api_key_creada.html",
        context={"cliente_id": cliente_id, "clave": clave},
    )


@router.get("/clientes/{cliente_id}", response_class=HTMLResponse)
def admin_cliente_detail(
    request: Request, cliente_id: str,
    ok: Optional[str] = None,
    pwd_error: Optional[str] = None,
    page: int = 1,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    cliente_id = cliente_id.strip().upper()
    # Paginación: 50 envíos por página
    PAGE_SIZE = 50
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    # Todo en UNA sola conexión (1 round trip al pool en vez de 3)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM clientes WHERE cliente_id = %s", (cliente_id,))
            row = cur.fetchone()
            if not row:
                return RedirectResponse(url="/admin/clientes", status_code=303)

            # Total envíos del cliente (para pagination meta)
            cur.execute(
                "SELECT COUNT(*) AS n FROM envios WHERE cliente_id = %s",
                (cliente_id,),
            )
            total_envios = cur.fetchone()["n"]

            # Envíos paginados
            cur.execute(
                "SELECT * FROM envios WHERE cliente_id = %s "
                "ORDER BY fecha DESC, id DESC LIMIT %s OFFSET %s",
                (cliente_id, PAGE_SIZE, offset),
            )
            envios = [dict(r) for r in cur.fetchall()]

            # Pagos (suelen ser pocos, no paginamos)
            cur.execute(
                "SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha DESC LIMIT 200",
                (cliente_id,),
            )
            pagos = [dict(r) for r in cur.fetchall()]

    cliente = dict(row)
    cliente["pricing_desc"] = describir_pricing(cliente)
    facturado = get_facturado_real(cliente_id)
    saldo_data = saldo(cliente_id, facturado)

    flash_ok = None
    if ok == "creado":
        flash_ok = "Cliente creado."
    elif ok == "pwd_actualizada":
        flash_ok = "Contraseña actualizada. Pasala al cliente."
    if pwd_error == "corta":
        flash_ok = None  # priorizar error
        # (no hay flash_error context aquí — lo paso por flash_ok como mensaje crudo)

    total_pages = max(1, (total_envios + PAGE_SIZE - 1) // PAGE_SIZE)
    pagination = {
        "page": page,
        "total_pages": total_pages,
        "total_envios": total_envios,
        "page_size": PAGE_SIZE,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_url": f"/admin/clientes/{cliente_id}?page={page - 1}" if page > 1 else None,
        "next_url": f"/admin/clientes/{cliente_id}?page={page + 1}" if page < total_pages else None,
    }

    return templates.TemplateResponse(
        request=request, name="admin/cliente_detail.html",
        context={
            "seccion": "clientes",
            "cliente": cliente,
            "saldo": saldo_data,
            "envios": envios,
            "pagos": pagos,
            "pagination": pagination,
            "flash_ok": flash_ok,
        },
    )


@router.get("/clientes/{cliente_id}/editar", response_class=HTMLResponse)
def admin_cliente_editar_form(
    request: Request, cliente_id: str,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM clientes WHERE cliente_id = %s", (cliente_id.upper(),))
            row = cur.fetchone()

    if not row:
        return RedirectResponse(url="/admin/clientes", status_code=303)

    return templates.TemplateResponse(
        request=request, name="admin/cliente_form.html",
        context={"seccion": "clientes", "cliente": dict(row), "pricing_modes": PRICING_MODES},
    )


@router.post("/clientes/{cliente_id}/editar")
def admin_cliente_editar(
    request: Request,
    cliente_id: str,
    email: str = Form(...),
    nombre: str = Form(""),
    cuit: str = Form(""),
    direccion: str = Form(""),
    cp: str = Form(""),
    ciudad: str = Form(""),
    pais: str = Form("AR"),
    telefono: str = Form(""),
    markup_pct: float = Form(25.0),
    markup_tipo: str = Form("PCT"),
    markup_valor: str = Form(""),
    markup_nac_tipo: str = Form(""),
    markup_nac_valor: str = Form(""),
    puede_emitir: str = Form(""),
    tope_deuda_ars: str = Form(""),
    # Quién paga los impuestos de destino por defecto en los envíos de este
    # cliente. Se puede pisar por envío desde el wizard del portal.
    tax_paga: str = Form(""),
    # Courier preferido: WAIMAO opera por DHL y lo deja fijado.
    courier_default: str = Form(""),
    notas: str = Form(""),
    activo: str = Form("true"),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    try:
        pricing = parse_pricing_value(markup_valor, markup_tipo, fallback_pct=markup_pct)
        markup_pct_db = pricing["valor"] if pricing["tipo"] == "PCT" else markup_pct
        # Margen nacional (opcional). Elegir "Igual que internacional" en el
        # form LIMPIA la regla nacional — sin esto no habría forma de volver
        # atrás una vez cargada.
        nac_tipo, nac_valor = None, None
        if markup_nac_tipo.strip():
            nac = parse_pricing_value(markup_nac_valor, markup_nac_tipo,
                                      fallback_pct=markup_pct)
            nac_tipo, nac_valor = nac["tipo"], nac["valor"]
        tope_db = parse_monto_ars(tope_deuda_ars)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE clientes SET
                        email=%s, markup_pct=%s, markup_tipo=%s, markup_valor=%s, activo=%s, nombre=%s, cuit=%s,
                        direccion=%s, cp=%s, ciudad=%s, pais=%s, telefono=%s, notas=%s,
                        markup_nac_tipo=%s, markup_nac_valor=%s,
                        puede_emitir=%s, tope_deuda_ars=%s, tax_paga=%s, courier_default=%s
                    WHERE cliente_id=%s
                    """,
                    (
                        email.strip().lower(), markup_pct_db,
                        pricing["tipo"], pricing["valor"],
                        activo.lower() == "true",
                        nombre or None, cuit or None, direccion or None,
                        cp or None, ciudad or None, pais or "AR",
                        telefono or None, notas or None,
                        nac_tipo, nac_valor,
                        puede_emitir == "1", tope_db,
                        normalizar_tax(tax_paga),
                        _courier_valido(courier_default),
                        cliente_id.strip().upper(),
                    ),
                )
    except Exception as e:
        cliente_form = {
            "cliente_id": cliente_id.strip().upper(),
            "email": email,
            "nombre": nombre,
            "cuit": cuit,
            "direccion": direccion,
            "cp": cp,
            "ciudad": ciudad,
            "pais": pais,
            "telefono": telefono,
            "markup_pct": markup_pct,
            "markup_tipo": markup_tipo,
            "markup_valor": markup_valor,
            "notas": notas,
            "activo": activo.lower() == "true",
        }
        return templates.TemplateResponse(
            request=request, name="admin/cliente_form.html",
            context={
                "seccion": "clientes",
                "cliente": cliente_form,
                "pricing_modes": PRICING_MODES,
                "flash_error": str(e),
            },
        )
    return RedirectResponse(url=f"/admin/clientes/{cliente_id.upper()}", status_code=303)


# ── Envíos ───────────────────────────────────────────────────

@router.get("/envios/nuevo", response_class=HTMLResponse)
def admin_envio_form(
    request: Request,
    cliente: Optional[str] = None,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    clientes = _get_clientes_lista()
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request=request, name="admin/envio_form.html",
        context={
            "seccion": "envio_nuevo",
            "clientes": clientes,
            "today": today,
            "preselect_cliente": (cliente or "").upper(),
        },
    )


@router.post("/envios/nuevo")
async def admin_envio_nuevo(
    request: Request,
    cliente_id: str = Form(...),
    fecha: str = Form(...),
    nro_fc: str = Form(""),
    monto_ars: float = Form(...),
    descripcion: str = Form(""),
    tracking: str = Form(""),
    estado: str = Form("ACTIVO"),
    factura_pdf: Optional[UploadFile] = File(None),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    try:
        from servicios.cuenta_corriente import leer_comprobante_con_tope
        contenido_fc = await leer_comprobante_con_tope(factura_pdf)
        registrar_envio(
            cliente_id=cliente_id.upper(),
            fecha=fecha,
            monto_ars=monto_ars,
            nro_fc=nro_fc,
            estado=estado,
            descripcion=descripcion,
            tracking=tracking,
            factura_pdf=contenido_fc or None,
            factura_nombre=(factura_pdf.filename if factura_pdf else "") or "",
        )
        return RedirectResponse(url=f"/admin/clientes/{cliente_id.upper()}", status_code=303)
    except Exception as e:
        clientes = _get_clientes_lista()
        today = datetime.now().strftime("%Y-%m-%d")
        return templates.TemplateResponse(
            request=request, name="admin/envio_form.html",
            context={
                "seccion": "envio_nuevo",
                "clientes": clientes,
                "today": today,
                "preselect_cliente": cliente_id.upper(),
                "flash_error": str(e),
            },
        )


@router.get("/envios/{envio_id}/factura")
def admin_ver_factura(envio_id: int, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.cuenta_corriente import get_factura_pdf
    dato = get_factura_pdf(envio_id)
    if not dato:
        return Response(content="Sin PDF adjunto", status_code=404)
    contenido, nombre = dato
    return Response(content=contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{nombre}"'})


@router.post("/envios/{envio_id}/cancelar")
def admin_envio_cancelar(
    envio_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    # Obtener cliente_id para redirigir
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id FROM envios WHERE id=%s", (envio_id,))
            row = cur.fetchone()

    cancelar_envio(envio_id)
    cliente_id = row["cliente_id"] if row else ""
    return RedirectResponse(url=f"/admin/clientes/{cliente_id}", status_code=303)


# ── Solicitudes de guía ─────────────────────────────────────

@router.get("/pedidos", response_class=HTMLResponse)
def admin_pedidos(
    request: Request,
    estado: str = "",
    ok: Optional[str] = None,
    guia_error: Optional[str] = None,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    estado = (estado or "").strip().upper()
    if estado and estado not in ESTADOS_SOLICITUD:
        estado = ""

    flash_ok = None
    if ok == "actualizado":
        flash_ok = "Solicitud actualizada."
    elif ok == "guia_generada":
        flash_ok = "✅ Guía generada en FedEx. Ya podés descargar el PDF."

    solicitudes = listar_solicitudes_admin(estado=estado)
    return templates.TemplateResponse(
        request=request, name="admin/pedidos.html",
        context={
            "seccion": "pedidos",
            "solicitudes": solicitudes,
            "estados": ESTADOS_SOLICITUD,
            "estado_filtro": estado,
            "flash_ok": flash_ok,
            "flash_error": guia_error,
        },
    )


def _notificar_estado_async(solicitud_id: int, estado: str):
    """
    Avisa por email al cliente dueño de la solicitud, en un thread aparte:
    el flujo del admin no espera al SMTP y un fallo de mail nunca lo rompe.
    """
    def _job():
        try:
            from servicios.solicitudes_guia import obtener_solicitud
            from core.email_sender import enviar_notificacion_estado
            sol = obtener_solicitud(solicitud_id)
            if not sol:
                return
            email = sol.get("dest_email_cliente")
            if not email:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT email FROM clientes WHERE cliente_id = %s",
                            (sol["cliente_id"],),
                        )
                        row = cur.fetchone()
                email = row["email"] if row else None
            if email:
                enviar_notificacion_estado(
                    email_destino=email,
                    cliente=sol["cliente_id"],
                    solicitud_id=solicitud_id,
                    estado=estado,
                    tracking=sol.get("tracking") or "",
                )
        except Exception as e:
            print(f"[admin] Error notificando estado de solicitud {solicitud_id}: {e}")

    threading.Thread(target=_job, daemon=True).start()


@router.post("/pedidos/{solicitud_id}/estado")
def admin_pedido_estado(
    solicitud_id: int,
    estado: str = Form(...),
    tracking: str = Form(""),
    guia_url: str = Form(""),
    pisar: str = Form(""),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    actualizar_solicitud_guia(
        solicitud_id,
        estado=estado,
        tracking=tracking,
        guia_url=guia_url,
        # El checkbox "pisar valores" permite corregir (o vaciar) un tracking
        # mal tipeado sin SQL a mano. Es opt-in por fila, nunca el default.
        pisar=(pisar == "1"),
    )
    _notificar_estado_async(solicitud_id, estado)
    return RedirectResponse(url="/admin/pedidos?ok=actualizado", status_code=303)


@router.get("/pedidos/{solicitud_id}/editar", response_class=HTMLResponse)
def admin_pedido_editar_form(
    request: Request,
    solicitud_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    """Corrección de datos ANTES de emitir (destinatario, medidas, valor)."""
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.solicitudes_guia import obtener_solicitud
    sol = obtener_solicitud(solicitud_id)
    if not sol:
        return RedirectResponse(url="/admin/pedidos", status_code=303)
    return templates.TemplateResponse(
        request=request, name="admin/pedido_editar.html",
        context={"seccion": "pedidos", "s": sol,
                 "emitida": bool(sol.get("tracking"))},
    )


@router.post("/pedidos/{solicitud_id}/editar")
async def admin_pedido_editar(
    request: Request,
    solicitud_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.solicitudes_guia import (
        CAMPOS_EDITABLES_PRE_EMISION, editar_solicitud_pre_emision,
    )

    form = await request.form()
    campos = {}
    for k in CAMPOS_EDITABLES_PRE_EMISION:
        if k not in form:
            continue
        v = str(form.get(k) or "").strip()
        if k in ("cantidad",):
            campos[k] = max(int(v or 1), 1)
        elif k in ("peso_kg", "largo_cm", "ancho_cm", "alto_cm", "valor_declarado_usd"):
            campos[k] = float((v or "0").replace(",", "."))
        else:
            campos[k] = v
    try:
        editar_solicitud_pre_emision(solicitud_id, campos)
    except ValueError as e:
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/admin/pedidos/{solicitud_id}/editar?error={quote(str(e))}",
            status_code=303)
    return RedirectResponse(url="/admin/pedidos?ok=editado", status_code=303)


@router.post("/pedidos/{solicitud_id}/generar-guia")
def admin_pedido_generar_guia(
    request: Request,
    solicitud_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    """Emite la guía real (FedEx internacional o envia.com nacional según
    el courier de la solicitud) y guarda el label PDF."""
    if not _is_auth(admin_token):
        return _redirect_login()

    resultado = generar_guia(solicitud_id)
    if resultado.get("ok"):
        from servicios.auditoria import registrar_desde_request
        registrar_desde_request(request, event="admin.emitir_guia", actor_type="admin",
                                actor_ref=str(solicitud_id), metadata={"solicitud_id": solicitud_id})
        _notificar_estado_async(solicitud_id, "GUIA_LISTA")
        return RedirectResponse(url="/admin/pedidos?ok=guia_generada", status_code=303)

    from urllib.parse import quote
    return RedirectResponse(
        url=f"/admin/pedidos?guia_error={quote(resultado.get('error', 'error') [:200])}",
        status_code=303,
    )


@router.get("/pedidos/{solicitud_id}/guia.pdf")
def admin_pedido_guia_pdf(
    solicitud_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    """Descarga el label PDF de la guía emitida (visible en el admin)."""
    if not _is_auth(admin_token):
        return _redirect_login()

    pdf = obtener_label_pdf(solicitud_id)
    if not pdf:
        return RedirectResponse(url="/admin/pedidos?guia_error=Esta+solicitud+no+tiene+guia", status_code=303)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="guia-{solicitud_id}.pdf"'},
    )


# ── Tracking FedEx TAURO 2026 ───────────────────────────────

@router.get("/tracking-fedex", response_class=HTMLResponse)
def admin_tracking_fedex(
    request: Request,
    started: Optional[str] = None,
    reset: Optional[str] = None,
    error: Optional[str] = None,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    summary = None
    summary_error = None
    try:
        summary = get_tracking_summary()
    except Exception as e:
        summary_error = str(e)

    return templates.TemplateResponse(
        request=request, name="admin/tracking_fedex.html",
        context={
            "seccion": "tracking_fedex",
            "summary": summary,
            "state": load_tracking_state(),
            "status": _tracking_snapshot(),
            "summary_error": summary_error,
            "flash_ok": (
                "Corrida iniciada. Esta pantalla se actualiza mientras trabaja."
                if started else
                "Checkpoint reiniciado."
                if reset else None
            ),
            "flash_error": (
                "Ya hay una corrida en curso."
                if error == "running" else
                "No se puede reiniciar el checkpoint mientras hay una corrida en curso."
                if error == "reset_running" else
                "FedEx está en sandbox: podés simular, pero no escribir ESTADO hasta pasar a production/prod."
                if error == "sandbox_requires_dry_run" else None
            ),
        },
    )


@router.post("/tracking-fedex/run")
def admin_tracking_fedex_run(
    mode: str = Form("resume"),
    limit: str = Form(""),
    target: str = Form("test"),
    dry_run: str = Form(""),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    mode = mode if mode in {"initial", "resume"} else "resume"
    target = target if target in {"source", "test"} else "test"
    try:
        parsed_limit = int(limit) if str(limit).strip() else 0
    except ValueError:
        parsed_limit = 0
    parsed_limit = parsed_limit if parsed_limit > 0 else None
    dry_run_bool = dry_run == "1"
    if target == "source" and not dry_run_bool and fedex_environment() == "sandbox":
        return RedirectResponse(url="/admin/tracking-fedex?error=sandbox_requires_dry_run", status_code=303)

    with _TRACKING_LOCK:
        if _TRACKING_STATUS["running"]:
            return RedirectResponse(url="/admin/tracking-fedex?error=running", status_code=303)
        _TRACKING_STATUS.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "returncode": None,
            "output": "Tracking FedEx en curso...",
            "result": None,
        })

    thread = threading.Thread(
        target=_run_tracking_fedex_job,
        args=(mode, parsed_limit, dry_run_bool, target),
        daemon=True,
    )
    thread.start()
    return RedirectResponse(url="/admin/tracking-fedex?started=1", status_code=303)


@router.post("/tracking-fedex/reset")
def admin_tracking_fedex_reset(admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    with _TRACKING_LOCK:
        if _TRACKING_STATUS["running"]:
            return RedirectResponse(url="/admin/tracking-fedex?error=reset_running", status_code=303)
    reset_tracking_checkpoint()
    return RedirectResponse(url="/admin/tracking-fedex?reset=1", status_code=303)


# ── Pagos ────────────────────────────────────────────────────

@router.get("/pagos/nuevo", response_class=HTMLResponse)
def admin_pago_form(
    request: Request,
    cliente: Optional[str] = None,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    clientes = _get_clientes_lista()
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request=request, name="admin/pago_form.html",
        context={
            "seccion": "pago_nuevo",
            "clientes": clientes,
            "today": today,
            "preselect_cliente": (cliente or "").upper(),
        },
    )


@router.post("/pagos/nuevo")
async def admin_pago_nuevo(
    request: Request,
    cliente_id: str = Form(...),
    fecha: str = Form(...),
    monto_ars: float = Form(...),
    metodo: str = Form("transferencia"),
    referencia: str = Form(""),
    nota: str = Form(""),
    comprobante: Optional[UploadFile] = File(None),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    try:
        from servicios.cuenta_corriente import leer_comprobante_con_tope
        contenido = await leer_comprobante_con_tope(comprobante)
        registrar_pago(
            cliente_id=cliente_id.upper(),
            fecha=fecha,
            monto_ars=monto_ars,
            metodo=metodo,
            referencia=referencia,
            nota=nota,
            # El admin carga APROBADO: impacta el saldo al instante. El
            # circuito PENDIENTE es sólo para pagos informados por clientes.
            estado="APROBADO",
            comprobante=contenido or None,
            comprobante_nombre=(comprobante.filename if comprobante else "") or "",
        )
        return RedirectResponse(url=f"/admin/clientes/{cliente_id.upper()}", status_code=303)
    except Exception as e:
        clientes = _get_clientes_lista()
        today = datetime.now().strftime("%Y-%m-%d")
        return templates.TemplateResponse(
            request=request, name="admin/pago_form.html",
            context={
                "seccion": "pago_nuevo",
                "clientes": clientes,
                "today": today,
                "preselect_cliente": cliente_id.upper(),
                "flash_error": str(e),
            },
        )


# ── Recolecciones ───────────────────────────────────────────

@router.get("/recolecciones", response_class=HTMLResponse)
def admin_recolecciones(request: Request, admin_token: Optional[str] = Cookie(None)):
    """Qué recolecciones hay agendadas: lo que el chofer va a buscar."""
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.recolecciones import listar_admin
    try:
        recolecciones = listar_admin()
    except Exception as e:
        print(f"[admin] no pude listar recolecciones: {e}")
        recolecciones = []
    return templates.TemplateResponse(
        request=request, name="admin/recolecciones.html",
        context={"seccion": "recolecciones", "recolecciones": recolecciones},
    )


@router.post("/recolecciones/{rec_id}/cancelar")
def admin_recoleccion_cancelar(rec_id: int, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    from urllib.parse import quote

    from servicios.recolecciones import cancelar
    r = cancelar(rec_id)
    if r.get("ok"):
        return RedirectResponse(url="/admin/recolecciones?ok=1", status_code=303)
    return RedirectResponse(
        url=f"/admin/recolecciones?error={quote(str(r.get('error') or 'Error'))}",
        status_code=303)


# ── Cargar un envío ya realizado (canal externo) ────────────

@router.get("/envios-realizados/nuevo", response_class=HTMLResponse)
def admin_envio_realizado_form(request: Request, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    return templates.TemplateResponse(
        request=request, name="admin/envio_realizado_form.html",
        context={"seccion": "envio_realizado", "clientes": _get_clientes_lista()},
    )


@router.post("/envios-realizados/nuevo")
async def admin_envio_realizado_post(
    request: Request,
    cliente_id: str = Form(...),
    dest_nombre: str = Form(...),
    dest_ciudad: str = Form(""),
    destino_pais: str = Form(...),
    dest_direccion: str = Form(""),
    producto: str = Form(...),
    cantidad: int = Form(1),
    peso_kg: str = Form("1"),
    tracking: str = Form(...),
    precio_ars: str = Form(...),
    observaciones: str = Form(""),
    guia_pdf: Optional[UploadFile] = File(None),
    admin_token: Optional[str] = Cookie(None),
):
    """
    Envío que YA salió por el canal externo → aparece en el portal del
    cliente como uno más (courier FedEx, tracking real, PDF si se adjunta)
    y el cargo entra solo a su cuenta corriente.
    """
    if not _is_auth(admin_token):
        return _redirect_login()

    from urllib.parse import quote

    from servicios.cuenta_corriente import leer_comprobante_con_tope
    from servicios.solicitudes_guia import cargar_envio_externo

    try:
        pdf = await leer_comprobante_con_tope(guia_pdf)
        resultado = cargar_envio_externo(
            cliente_id=cliente_id,
            dest_nombre=dest_nombre,
            dest_ciudad=dest_ciudad,
            destino_pais=destino_pais,
            dest_direccion=dest_direccion,
            producto=producto,
            cantidad=cantidad,
            peso_kg=float((peso_kg or "1").replace(",", ".")),
            tracking=tracking,
            precio_tauro_ars=parse_monto_ars(precio_ars) or 0,
            label_pdf=pdf or None,
            observaciones=observaciones,
        )
    except Exception as e:
        resultado = {"ok": False, "error": str(e)}

    if resultado.get("ok"):
        return RedirectResponse(
            url=f"/admin/clientes/{cliente_id.strip().upper()}?ok=envio_cargado",
            status_code=303)
    return RedirectResponse(
        url=f"/admin/envios-realizados/nuevo?error={quote(str(resultado.get('error') or 'error'))}",
        status_code=303)


# ── Verificación de pagos informados por clientes ───────────

@router.get("/pagos/pendientes", response_class=HTMLResponse)
def admin_pagos_pendientes(request: Request, admin_token: Optional[str] = Cookie(None)):
    """Cola de pagos que los clientes informaron y esperan verificación."""
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.cuenta_corriente import pagos_pendientes
    return templates.TemplateResponse(
        request=request, name="admin/pagos_pendientes.html",
        context={"seccion": "pagos_pendientes", "pagos": pagos_pendientes()},
    )


@router.get("/pagos/{pago_id}/comprobante")
def admin_ver_comprobante(pago_id: int, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.cuenta_corriente import get_comprobante
    dato = get_comprobante(pago_id)
    if not dato:
        return Response(content="Sin comprobante", status_code=404)
    contenido, tipo, nombre = dato
    return Response(content=contenido, media_type=tipo,
                    headers={"Content-Disposition": f'inline; filename="{nombre}"'})


@router.post("/pagos/{pago_id}/resolver")
def admin_resolver_pago(
    request: Request,
    pago_id: int,
    decision: str = Form(...),
    admin_token: Optional[str] = Cookie(None),
):
    """
    Aprueba (entra al saldo) o rechaza (no cuenta, queda el registro) un
    pago informado. Sólo resuelve PENDIENTES: un doble click no re-resuelve.
    """
    if not _is_auth(admin_token):
        return _redirect_login()
    from servicios.cuenta_corriente import resolver_pago
    aprobar = (decision == "aprobar")
    cambio = resolver_pago(pago_id, aprobar=aprobar)
    if not cambio:
        print(f"[admin] pago {pago_id}: ya estaba resuelto, no se toca")
    elif cambio:
        from servicios.auditoria import registrar_desde_request
        registrar_desde_request(
            request, event="admin.resolver_pago", actor_type="admin",
            actor_ref=str(pago_id),
            metadata={"pago_id": pago_id, "decision": "aprobado" if aprobar else "rechazado"})
    return RedirectResponse(url="/admin/pagos/pendientes", status_code=303)


# ── Productos ────────────────────────────────────────────────

@router.get("/productos", response_class=HTMLResponse)
def admin_productos(request: Request, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()

    pendientes = get_productos_pendientes()
    todos = get_todos_productos()
    return templates.TemplateResponse(
        request=request, name="admin/productos.html",
        context={"seccion": "productos", "pendientes": pendientes, "todos": todos},
    )


@router.post("/productos/{producto_id}/aprobar")
def admin_aprobar_producto(
    producto_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    aprobar_producto(producto_id)
    return RedirectResponse(url="/admin/productos", status_code=303)


@router.post("/productos/{producto_id}/rechazar")
def admin_rechazar_producto(
    producto_id: int,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()
    rechazar_producto(producto_id)
    return RedirectResponse(url="/admin/productos", status_code=303)


# ── Rutas ────────────────────────────────────────────────────

@router.get("/rutas", response_class=HTMLResponse)
def admin_rutas(request: Request, admin_token: Optional[str] = Cookie(None)):
    if not _is_auth(admin_token):
        return _redirect_login()

    rutas = get_todas_las_rutas()
    return templates.TemplateResponse(
        request=request, name="admin/rutas.html",
        context={"seccion": "rutas", "rutas": rutas},
    )


@router.post("/rutas/nueva")
def admin_ruta_nueva(
    ruta_id: str = Form(...),
    origen_pais: str = Form(...),
    origen_ciudad: str = Form(...),
    origen_zip: str = Form(...),
    destino_pais: str = Form(...),
    destino_ciudad: str = Form(...),
    destino_zip: str = Form(...),
    dias_estimados: int = Form(5),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    ruta = Ruta(
        ruta_id=ruta_id.strip().upper(),
        origen_pais=origen_pais.strip().upper(),
        origen_ciudad=origen_ciudad.strip().upper(),
        origen_zip=origen_zip.strip(),
        destino_pais=destino_pais.strip().upper(),
        destino_ciudad=destino_ciudad.strip().upper(),
        destino_zip=destino_zip.strip(),
        dias_estimados=dias_estimados,
        activa=True,
    )
    upsert_ruta(ruta)
    return RedirectResponse(url="/admin/rutas", status_code=303)


@router.post("/rutas/{ruta_id}/toggle")
def admin_ruta_toggle(
    ruta_id: str,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    # Leer estado actual y hacer toggle
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT activa FROM rutas WHERE ruta_id=%s", (ruta_id.upper(),))
            row = cur.fetchone()

    if row:
        toggle_ruta(ruta_id, not row["activa"])
    return RedirectResponse(url="/admin/rutas", status_code=303)


# ── Config ───────────────────────────────────────────────────

@router.get("/config", response_class=HTMLResponse)
def admin_config(
    request: Request,
    ok: Optional[str] = None,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    config_items = _get_config()
    return templates.TemplateResponse(
        request=request, name="admin/config.html",
        context={
            "seccion": "config",
            "config_items": config_items,
            "flash_ok": "Configuración guardada." if ok else None,
        },
    )


@router.post("/config")
async def admin_config_save(
    request: Request,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    form = await request.form()
    data = dict(form)

    nuevo_param = data.pop("_nuevo_parametro", "").strip()
    nuevo_valor = data.pop("_nuevo_valor", "").strip()

    with get_conn() as conn:
        with conn.cursor() as cur:
            for param, valor in data.items():
                cur.execute(
                    "UPDATE config SET valor=%s WHERE parametro=%s",
                    (str(valor).strip(), param),
                )
            if nuevo_param and nuevo_valor:
                cur.execute(
                    "INSERT INTO config (parametro, valor) VALUES (%s, %s) ON CONFLICT (parametro) DO UPDATE SET valor=EXCLUDED.valor",
                    (nuevo_param.upper(), nuevo_valor),
                )

    return RedirectResponse(url="/admin/config?ok=1", status_code=303)


# ── Migración Sheets → PostgreSQL ─────────────────────────────

@router.get("/migracion", response_class=HTMLResponse)
def admin_migracion(
    request: Request,
    error: Optional[str] = None,
    started: Optional[str] = None,
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    flash_error = None
    if error == "running":
        flash_error = "La migración ya está corriendo."
    elif error == "confirm":
        flash_error = 'Para ejecutar la migración escribí "MIGRAR".'

    return templates.TemplateResponse(
        request=request, name="admin/migracion.html",
        context={
            "seccion": "migracion",
            "status": _migration_snapshot(),
            "flash_ok": "Migración iniciada." if started else None,
            "flash_error": flash_error,
        },
    )


@router.post("/migracion/run")
def admin_migracion_run(
    confirmacion: str = Form(""),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    if confirmacion.strip().upper() != "MIGRAR":
        return RedirectResponse(url="/admin/migracion?error=confirm", status_code=303)

    with _MIGRATION_LOCK:
        if _MIGRATION_STATUS["running"]:
            return RedirectResponse(url="/admin/migracion?error=running", status_code=303)
        _MIGRATION_STATUS.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "returncode": None,
            "output": "Migración iniciada. Actualizá esta pantalla en unos segundos.",
        })

    thread = threading.Thread(target=_run_sheets_migration, daemon=True)
    thread.start()

    return RedirectResponse(url="/admin/migracion?started=1", status_code=303)


@router.post("/migracion/numeric")
def admin_migracion_numeric(
    confirmacion: str = Form(""),
    admin_token: Optional[str] = Cookie(None),
):
    """
    Aplica la migración de columnas de dinero REAL → NUMERIC(14,2).
    Idempotente (solo migra columnas que siguen siendo 'real'); aun así
    pide confirmación explícita porque reescribe tablas.
    """
    if not _is_auth(admin_token):
        return _redirect_login()

    if confirmacion.strip().upper() != "NUMERIC":
        return RedirectResponse(url="/admin/migracion?error=confirm_numeric", status_code=303)

    sql_path = Path(__file__).resolve().parent.parent / "scripts" / "migrar_dinero_numeric.sql"
    if not sql_path.exists():
        return RedirectResponse(url="/admin/migracion?error=numeric_script_missing", status_code=303)

    try:
        sql = sql_path.read_text(encoding="utf-8")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        print("[admin] Migración NUMERIC aplicada OK.")
        return RedirectResponse(url="/admin/migracion?numeric_ok=1", status_code=303)
    except Exception as e:
        print(f"[admin] Error en migración NUMERIC: {e}")
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/admin/migracion?error=numeric_fail&det={quote(str(e)[:150])}",
            status_code=303,
        )
