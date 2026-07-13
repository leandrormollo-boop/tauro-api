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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, Cookie, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
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
from servicios.pricing import PRICING_MODES, describir_pricing, parse_pricing_value
from servicios.solicitudes_guia import (
    ESTADOS_SOLICITUD,
    actualizar_solicitud_guia,
    contar_solicitudes_pendientes,
    listar_solicitudes_admin,
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

@router.get("/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    return templates.TemplateResponse(
        request=request, name="admin/login.html", context={}
    )


@router.post("/login", response_class=HTMLResponse)
def admin_login(request: Request, password: str = Form(...)):
    ip = client_ip(request)
    if not check_rate(f"admin_login:{ip}", max_attempts=5, window_seconds=300):
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "Demasiados intentos. Esperá unos minutos e intentá de nuevo."},
            status_code=429,
        )
    # Comparación de tiempo constante para no filtrar la contraseña por timing.
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        return templates.TemplateResponse(
            request=request, name="admin/login.html",
            context={"error": "Contraseña incorrecta."},
            status_code=401,
        )
    reset_rate(f"admin_login:{ip}")
    response = RedirectResponse(url="/admin/tracking-fedex", status_code=303)
    response.set_cookie(
        key="admin_token", value=_ADMIN_TOKEN,
        httponly=True, max_age=60 * 60 * 8,
        samesite="lax", secure=COOKIE_SECURE,
    )
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
        # Hashear password si vino una
        from servicios.auth import hash_password
        password_hash_db = hash_password(password.strip()) if password.strip() else None
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clientes
                        (cliente_id, email, password_hash, markup_pct, markup_tipo, markup_valor, activo,
                         nombre, cuit, direccion, cp, ciudad, pais, telefono, notas)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cliente_id, email.strip().lower(), password_hash_db, markup_pct_db,
                        pricing["tipo"], pricing["valor"],
                        activo.lower() == "true",
                        nombre or None, cuit or None, direccion or None,
                        cp or None, ciudad or None, pais or "AR",
                        telefono or None, notas or None,
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
    return RedirectResponse(
        url=f"/admin/clientes/{cliente_id}?ok=pwd_actualizada", status_code=303,
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
    notas: str = Form(""),
    activo: str = Form("true"),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    try:
        pricing = parse_pricing_value(markup_valor, markup_tipo, fallback_pct=markup_pct)
        markup_pct_db = pricing["valor"] if pricing["tipo"] == "PCT" else markup_pct

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE clientes SET
                        email=%s, markup_pct=%s, markup_tipo=%s, markup_valor=%s, activo=%s, nombre=%s, cuit=%s,
                        direccion=%s, cp=%s, ciudad=%s, pais=%s, telefono=%s, notas=%s
                    WHERE cliente_id=%s
                    """,
                    (
                        email.strip().lower(), markup_pct_db,
                        pricing["tipo"], pricing["valor"],
                        activo.lower() == "true",
                        nombre or None, cuit or None, direccion or None,
                        cp or None, ciudad or None, pais or "AR",
                        telefono or None, notas or None,
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
def admin_envio_nuevo(
    request: Request,
    cliente_id: str = Form(...),
    fecha: str = Form(...),
    nro_fc: str = Form(""),
    monto_ars: float = Form(...),
    descripcion: str = Form(""),
    tracking: str = Form(""),
    estado: str = Form("ACTIVO"),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    try:
        registrar_envio(
            cliente_id=cliente_id.upper(),
            fecha=fecha,
            monto_ars=monto_ars,
            nro_fc=nro_fc,
            estado=estado,
            descripcion=descripcion,
            tracking=tracking,
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
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    estado = (estado or "").strip().upper()
    if estado and estado not in ESTADOS_SOLICITUD:
        estado = ""

    solicitudes = listar_solicitudes_admin(estado=estado)
    return templates.TemplateResponse(
        request=request, name="admin/pedidos.html",
        context={
            "seccion": "pedidos",
            "solicitudes": solicitudes,
            "estados": ESTADOS_SOLICITUD,
            "estado_filtro": estado,
            "flash_ok": "Solicitud actualizada." if ok == "actualizado" else None,
        },
    )


@router.post("/pedidos/{solicitud_id}/estado")
def admin_pedido_estado(
    solicitud_id: int,
    estado: str = Form(...),
    tracking: str = Form(""),
    guia_url: str = Form(""),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    actualizar_solicitud_guia(
        solicitud_id,
        estado=estado,
        tracking=tracking,
        guia_url=guia_url,
    )
    return RedirectResponse(url="/admin/pedidos?ok=actualizado", status_code=303)


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
def admin_pago_nuevo(
    request: Request,
    cliente_id: str = Form(...),
    fecha: str = Form(...),
    monto_ars: float = Form(...),
    metodo: str = Form("transferencia"),
    referencia: str = Form(""),
    nota: str = Form(""),
    admin_token: Optional[str] = Cookie(None),
):
    if not _is_auth(admin_token):
        return _redirect_login()

    try:
        registrar_pago(
            cliente_id=cliente_id.upper(),
            fecha=fecha,
            monto_ars=monto_ars,
            metodo=metodo,
            referencia=referencia,
            nota=nota,
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
