# ============================================================
# App de TAURO para Tiendanube
# ============================================================
# Diferencias con Shopify que importan:
#   · El OAuth devuelve un token que NO vence y un `user_id` que es el ID
#     numérico de la tienda (Shopify usa el dominio).
#   · Los webhooks se firman con HMAC-SHA256 en HEXADECIMAL (Shopify usa
#     base64) y con el client_secret de la app.
#   · La API exige cabecera `Authentication: bearer <token>` (así, con esa
#     grafía) y un User-Agent identificable, o rechaza la llamada.
#
# Se enciende sola cuando estén TIENDANUBE_CLIENT_ID y
# TIENDANUBE_CLIENT_SECRET en Railway → Variables.
# ============================================================
from __future__ import annotations

import os
import base64
import hashlib
import hmac
import json
import secrets
import threading
from typing import Optional

import requests
from cryptography.fernet import Fernet, InvalidToken
from psycopg2.extras import Json

from core.database import get_conn

API_BASE = "https://api.tiendanube.com/v1"
OAUTH_URL = "https://www.tiendanube.com/apps/authorize/token"
USER_AGENT = "TAURO Solutions (cotizaciones@taurosolutions.ar)"
OAUTH_SECRET_MARKER = "oauth:tiendanube-app"

WEBHOOKS_REQUERIDOS = (
    "order/created",
    "order/updated",
    "order/cancelled",
    "app/uninstalled",
    "app/suspended",
    "store/redact",
    "customers/redact",
    "customers/data_request",
)
EVENTOS_PEDIDOS = {"order/created", "order/updated", "order/cancelled"}
EVENTOS_PRIVACIDAD = {"store/redact", "customers/redact", "customers/data_request"}
EVENTOS_LIFECYCLE = {"app/uninstalled", "app/suspended"}
EVENTOS_ACEPTADOS = set(WEBHOOKS_REQUERIDOS)


class TiendanubeError(RuntimeError):
    pass


class TiendanubeWebhookError(TiendanubeError):
    pass


class TiendanubeClaimError(TiendanubeError):
    pass


class TiendanubeRetryableError(TiendanubeError):
    pass


def _fernets() -> list[Fernet]:
    """Clave activa, claves previas y fallback estable para rotación."""
    materiales = [
        os.getenv("TIENDANUBE_TOKEN_ENCRYPTION_KEY") or "",
        *(os.getenv("TIENDANUBE_TOKEN_ENCRYPTION_KEY_PREVIOUS") or "").split(","),
        os.getenv("TIENDANUBE_CLIENT_SECRET") or "",
    ]
    resultado: list[Fernet] = []
    vistos: set[bytes] = set()
    for material_crudo in materiales:
        material = str(material_crudo or "").strip().encode("utf-8")
        if not material or material in vistos:
            continue
        vistos.add(material)
        clave = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
        resultado.append(Fernet(clave))
    return resultado


def _cifrar_token(token: str) -> str:
    token = str(token or "")
    if not token or token.startswith("enc:v1:"):
        return token
    fernets = _fernets()
    if not fernets:
        raise TiendanubeError("Falta clave para cifrar el token Tiendanube.")
    return "enc:v1:" + fernets[0].encrypt(token.encode("utf-8")).decode("ascii")


def _descifrar_token(token_guardado: str) -> str:
    token_guardado = str(token_guardado or "")
    if not token_guardado.startswith("enc:v1:"):
        return token_guardado
    ultimo_error = None
    for fernet in _fernets():
        try:
            return fernet.decrypt(token_guardado[7:].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            ultimo_error = exc
    raise TiendanubeError("El token cifrado Tiendanube no se pudo abrir.") from ultimo_error

_tabla_lista = False


def _ensure_tabla() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tiendanube_instalaciones (
                    id                   SERIAL PRIMARY KEY,
                    store_id             TEXT NOT NULL UNIQUE,
                    access_token         TEXT NOT NULL,
                    cliente_id           TEXT,
                    nombre               TEXT,
                    estado               TEXT NOT NULL DEFAULT 'ACTIVA',
                    install_generation   TEXT,
                    webhooks_ready       BOOLEAN NOT NULL DEFAULT FALSE,
                    webhooks_verified_at TIMESTAMPTZ,
                    claim_token_hash     TEXT,
                    claim_expires_at     TIMESTAMPTZ,
                    instalada_en         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    actualizada_en       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    suspendida_en        TIMESTAMPTZ,
                    desinstalada_en      TIMESTAMPTZ,
                    redactada_en         TIMESTAMPTZ
                );
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'ACTIVA';
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS install_generation TEXT;
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS webhooks_ready BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS webhooks_verified_at TIMESTAMPTZ;
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS claim_token_hash TEXT;
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ;
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS actualizada_en TIMESTAMPTZ NOT NULL DEFAULT NOW();
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS suspendida_en TIMESTAMPTZ;
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS desinstalada_en TIMESTAMPTZ;
                ALTER TABLE tiendanube_instalaciones
                    ADD COLUMN IF NOT EXISTS redactada_en TIMESTAMPTZ;
                UPDATE tiendanube_instalaciones
                   SET install_generation = md5(
                       store_id || ':' || instalada_en::text || ':' || random()::text
                   )
                 WHERE install_generation IS NULL OR BTRIM(install_generation) = '';
                ALTER TABLE tiendanube_instalaciones
                    ALTER COLUMN install_generation SET NOT NULL;

                CREATE TABLE IF NOT EXISTS tiendanube_lifecycle_eventos (
                    id                 BIGSERIAL PRIMARY KEY,
                    store_id           TEXT NOT NULL,
                    evento             TEXT NOT NULL,
                    install_generation TEXT NOT NULL,
                    recibido_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS ix_tiendanube_lifecycle_store
                    ON tiendanube_lifecycle_eventos(store_id, recibido_at DESC);

                CREATE TABLE IF NOT EXISTS tiendanube_webhook_eventos (
                    evento_id          TEXT PRIMARY KEY,
                    store_id           TEXT NOT NULL,
                    evento             TEXT NOT NULL,
                    recurso_id         TEXT NOT NULL DEFAULT '',
                    install_generation TEXT NOT NULL,
                    payload            JSONB NOT NULL,
                    estado             TEXT NOT NULL DEFAULT 'PENDIENTE',
                    intentos           INTEGER NOT NULL DEFAULT 0,
                    proximo_intento_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ultimo_error       TEXT,
                    recibido_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at         TIMESTAMPTZ,
                    procesado_at       TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS ix_tiendanube_webhook_pendientes
                    ON tiendanube_webhook_eventos(estado, proximo_intento_at, recibido_at);

                CREATE TABLE IF NOT EXISTS tiendanube_privacidad_solicitudes (
                    id                 BIGSERIAL PRIMARY KEY,
                    request_id         TEXT NOT NULL,
                    store_id           TEXT NOT NULL,
                    tipo               TEXT NOT NULL,
                    customer_id        TEXT NOT NULL DEFAULT '',
                    recursos           JSONB NOT NULL DEFAULT '[]'::jsonb,
                    estado             TEXT NOT NULL DEFAULT 'PENDIENTE',
                    creado_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resuelto_at        TIMESTAMPTZ,
                    UNIQUE(store_id, tipo, request_id)
                );
                CREATE INDEX IF NOT EXISTS ix_tiendanube_privacidad_pendientes
                    ON tiendanube_privacidad_solicitudes(estado, creado_at);
            """)
        conn.commit()
    _tabla_lista = True


def app_configurada() -> bool:
    return bool(os.getenv("TIENDANUBE_CLIENT_ID") and os.getenv("TIENDANUBE_CLIENT_SECRET"))


def url_instalacion(state: str = "") -> str:
    """
    Link para que un comerciante instale la app desde su Tiendanube. El `state`
    (si se pasa) viaja a la vuelta para atar la instalación al navegador que
    inició el flujo — anti-CSRF de la vinculación.
    """
    cid = os.getenv("TIENDANUBE_CLIENT_ID", "")
    url = f"https://www.tiendanube.com/apps/{cid}/authorize"
    if state:
        from urllib.parse import quote
        url += f"?state={quote(state)}"
    return url


def callback_url() -> str:
    base = (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
    return f"{base}/integraciones/tiendanube/callback"


def firmar_oauth_cookie(state: str, cliente_id: str) -> str:
    payload = f"{str(state)}:{str(cliente_id).strip().upper()}"
    secreto = (os.getenv("TIENDANUBE_CLIENT_SECRET") or "").encode("utf-8")
    if not secreto:
        raise TiendanubeError("La app Tiendanube no está configurada.")
    firma = hmac.new(secreto, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{firma}"


def validar_oauth_cookie(cookie: str, state: str) -> str:
    partes = str(cookie or "").rsplit(":", 2)
    if len(partes) != 3:
        return ""
    state_cookie, cliente_id, firma = partes
    if not state_cookie or not state or not secrets.compare_digest(state_cookie, state):
        return ""
    try:
        esperado = firmar_oauth_cookie(state_cookie, cliente_id).rsplit(":", 1)[1]
    except TiendanubeError:
        return ""
    return cliente_id if secrets.compare_digest(esperado, firma) else ""


def canjear_token(code: str) -> Optional[dict]:
    """El `code` del redirect se cambia por un token permanente."""
    try:
        r = requests.post(OAUTH_URL, json={
            "client_id": os.getenv("TIENDANUBE_CLIENT_ID", ""),
            "client_secret": os.getenv("TIENDANUBE_CLIENT_SECRET", ""),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback_url(),
        }, headers={"User-Agent": USER_AGENT}, timeout=25)
        if r.status_code != 200:
            print(f"[tiendanube] canje de token falló {r.status_code}: {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[tiendanube] error canjeando token: {e}")
        return None


def guardar_instalacion(store_id: str, access_token: str, nombre: str = "") -> str:
    """Persiste una generación OAuth y devuelve un secreto de claim ownerless.

    El secreto sólo vuelve al navegador que completó OAuth. En PostgreSQL se
    guarda su hash, de modo que una lectura de la tabla no permite reclamar la
    tienda. Si la instalación ya tenía dueño, se preserva y no se emite claim.
    """
    _ensure_tabla()
    store_id = str(store_id or "").strip()
    if not store_id or not str(access_token or "").strip():
        raise TiendanubeError("Instalación Tiendanube inválida.")
    generation = secrets.token_urlsafe(18)
    claim = secrets.token_urlsafe(32)
    claim_hash = hashlib.sha256(claim.encode("utf-8")).hexdigest()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tiendanube_instalaciones
                    (store_id, access_token, nombre, estado, install_generation,
                     webhooks_ready, claim_token_hash, claim_expires_at)
                VALUES (%s, %s, %s, 'ACTIVA', %s, FALSE, %s,
                        NOW() + INTERVAL '24 hours')
                ON CONFLICT (store_id) DO UPDATE
                    SET access_token = EXCLUDED.access_token,
                        nombre = COALESCE(NULLIF(EXCLUDED.nombre, ''),
                                          tiendanube_instalaciones.nombre),
                        estado = 'ACTIVA',
                        install_generation = EXCLUDED.install_generation,
                        webhooks_ready = FALSE,
                        webhooks_verified_at = NULL,
                        claim_token_hash = CASE
                            WHEN tiendanube_instalaciones.cliente_id IS NULL
                            THEN EXCLUDED.claim_token_hash ELSE NULL END,
                        claim_expires_at = CASE
                            WHEN tiendanube_instalaciones.cliente_id IS NULL
                            THEN EXCLUDED.claim_expires_at ELSE NULL END,
                        instalada_en = NOW(),
                        actualizada_en = NOW(),
                        suspendida_en = NULL,
                        desinstalada_en = NULL,
                        redactada_en = NULL
                RETURNING cliente_id
            """, (
                store_id, _cifrar_token(access_token), nombre,
                generation, claim_hash,
            ))
            fila = cur.fetchone() or {}
            cur.execute("""
                INSERT INTO tiendanube_lifecycle_eventos
                    (store_id, evento, install_generation)
                VALUES (%s, 'INSTALADA', %s)
            """, (store_id, generation))
        conn.commit()
    return "" if str(fila.get("cliente_id") or "").strip() else f"{store_id}.{claim}"


def instalacion(store_id: str) -> Optional[dict]:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tiendanube_instalaciones WHERE store_id = %s",
                        (str(store_id),))
            row = cur.fetchone()
    if not row:
        return None
    datos = dict(row)
    guardado = str(datos.get("access_token") or "")
    datos["access_token"] = _descifrar_token(guardado)
    # Migración oportunista de filas legacy en texto plano.
    if guardado and not guardado.startswith("enc:v1:"):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE tiendanube_instalaciones
                       SET access_token = %s, actualizada_en = NOW()
                     WHERE store_id = %s AND access_token = %s
                """, (_cifrar_token(guardado), str(store_id), guardado))
            conn.commit()
    return datos


def vincular_cliente(store_id: str, cliente_id: str) -> None:
    """
    Ata la tienda a la cuenta TAURO y la registra en `tiendas_conectadas`,
    que es la tabla que lee todo el portal — así Tiendanube y Shopify
    conviven sin que el resto del sistema tenga que distinguirlos.
    """
    _ensure_tabla()
    store_id = str(store_id or "").strip()
    cliente_id = str(cliente_id or "").strip().upper()
    if not store_id or not cliente_id:
        raise TiendanubeClaimError("Faltan datos para vincular la tienda.")
    anterior = ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cliente_id, estado, webhooks_ready
                  FROM tiendanube_instalaciones
                 WHERE store_id = %s
                 FOR UPDATE
            """, (store_id,))
            fila = cur.fetchone()
            if not fila or fila.get("estado") != "ACTIVA":
                raise TiendanubeClaimError("La instalación ya no está activa.")
            anterior = str(fila.get("cliente_id") or "").strip().upper()
            if anterior and anterior != cliente_id:
                raise TiendanubeClaimError("La tienda ya pertenece a otra cuenta TAURO.")
            cur.execute("""
                UPDATE tiendanube_instalaciones
                   SET cliente_id = %s, actualizada_en = NOW()
                 WHERE store_id = %s
            """, (cliente_id, store_id))
        conn.commit()

    # El owner puede quedar asociado mientras Operaciones repara suscripciones
    # o Shipping, pero el binding que consume pedidos no se activa antes del
    # readiness completo.
    if not fila.get("webhooks_ready"):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE tiendanube_instalaciones
                       SET claim_token_hash = NULL, claim_expires_at = NULL
                     WHERE store_id = %s AND cliente_id = %s
                """, (store_id, cliente_id))
            conn.commit()
        return

    try:
        from servicios.integraciones_tienda import conectar_tienda
        resultado = conectar_tienda(
            cliente_id, "tiendanube", f"{store_id}.tiendanube",
            OAUTH_SECRET_MARKER,
        )
        if not resultado.get("ok"):
            raise TiendanubeClaimError(resultado.get("error") or "No se pudo vincular.")
    except Exception as e:
        if not anterior:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE tiendanube_instalaciones
                           SET cliente_id = NULL
                         WHERE store_id = %s AND cliente_id = %s
                    """, (store_id, cliente_id))
                conn.commit()
        if isinstance(e, TiendanubeClaimError):
            raise
        raise TiendanubeClaimError("No se pudo vincular la tienda.") from e

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tiendanube_instalaciones
                   SET claim_token_hash = NULL, claim_expires_at = NULL,
                       actualizada_en = NOW()
                 WHERE store_id = %s AND cliente_id = %s
            """, (store_id, cliente_id))
        conn.commit()


def reclamar_con_token(claim_cookie: str, cliente_id: str) -> str:
    """Consume el claim emitido tras un OAuth iniciado en Tiendanube."""
    valor = str(claim_cookie or "").strip()
    if "." not in valor:
        raise TiendanubeClaimError("El enlace de vinculación es inválido.")
    store_id, token = valor.split(".", 1)
    if not store_id or not token:
        raise TiendanubeClaimError("El enlace de vinculación es inválido.")
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cliente_id, estado, claim_token_hash,
                       claim_expires_at > NOW() AS claim_vigente
                  FROM tiendanube_instalaciones
                 WHERE store_id = %s
            """, (store_id,))
            fila = cur.fetchone()
    if not fila or fila.get("estado") != "ACTIVA" or not fila.get("claim_vigente"):
        raise TiendanubeClaimError("El enlace de vinculación venció.")
    esperado = str(fila.get("claim_token_hash") or "")
    recibido = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not esperado or not secrets.compare_digest(esperado, recibido):
        raise TiendanubeClaimError("El enlace de vinculación es inválido.")
    vincular_cliente(store_id, cliente_id)
    return store_id


def _api(store_id: str, token: str, metodo: str, path: str,
         payload: dict | None = None, timeout: int = 20):
    url = f"{API_BASE}/{store_id}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Authentication": f"bearer {token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    try:
        return requests.request(metodo, url, headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        print(f"[tiendanube] {metodo} {path} falló: {e}")
        return None


def registrar_webhooks(store_id: str, token: str) -> list[str]:
    """Asegura el conjunto requerido consultando antes y verificando después.

    Un 422 no se interpreta como éxito: también representa un evento/URL
    inválido. La única evidencia válida es que GET /webhooks devuelva cada par
    evento+URL requerido.
    """
    base = (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
    destino = f"{base}/integraciones/tiendanube/webhook"

    def _listar() -> set[tuple[str, str]]:
        r = _api(store_id, token, "GET", "webhooks?per_page=200")
        if r is None or r.status_code != 200:
            raise TiendanubeWebhookError("No se pudieron listar los webhooks.")
        try:
            filas = r.json()
        except Exception as exc:
            raise TiendanubeWebhookError("Tiendanube devolvió webhooks inválidos.") from exc
        if not isinstance(filas, list):
            raise TiendanubeWebhookError("Tiendanube devolvió webhooks inválidos.")
        return {
            (str(f.get("event") or "").strip(), str(f.get("url") or "").rstrip("/"))
            for f in filas if isinstance(f, dict)
        }

    existentes = _listar()
    for evento in WEBHOOKS_REQUERIDOS:
        if (evento, destino) in existentes:
            continue
        r = _api(
            store_id, token, "POST", "webhooks",
            {"event": evento, "url": destino},
        )
        if r is None or r.status_code not in (200, 201, 422):
            raise TiendanubeWebhookError(
                f"No se pudo registrar el webhook {evento}."
            )

    verificados = _listar()
    faltantes = [
        evento for evento in WEBHOOKS_REQUERIDOS
        if (evento, destino) not in verificados
    ]
    if faltantes:
        raise TiendanubeWebhookError(
            "No quedaron verificados todos los webhooks requeridos."
        )
    return list(WEBHOOKS_REQUERIDOS)


def confirmar_webhooks(store_id: str, eventos: list[str]) -> bool:
    if set(eventos or []) != set(WEBHOOKS_REQUERIDOS):
        return False
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tiendanube_instalaciones
                   SET webhooks_ready = TRUE,
                       webhooks_verified_at = NOW(),
                       actualizada_en = NOW()
                 WHERE store_id = %s AND estado = 'ACTIVA'
                 RETURNING cliente_id
            """, (str(store_id),))
            fila = cur.fetchone()
        conn.commit()
    if not fila:
        return False
    cliente_id = str(fila.get("cliente_id") or "").strip()
    if cliente_id:
        vincular_cliente(str(store_id), cliente_id)
    return True


def reconciliar_instalaciones_pendientes(limite: int = 5) -> dict:
    """Completa instalaciones parciales sin exigir otro OAuth/reinstall."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT store_id, access_token
                  FROM tiendanube_instalaciones
                 WHERE estado = 'ACTIVA'
                   AND webhooks_ready = FALSE
                   AND NULLIF(BTRIM(access_token), '') IS NOT NULL
                 ORDER BY actualizada_en
                 LIMIT %s
            """, (max(1, min(int(limite), 20)),))
            filas = [dict(f) for f in cur.fetchall()]
    completadas = errores = 0
    for fila in filas:
        store_id = str(fila.get("store_id") or "")
        try:
            token = _descifrar_token(str(fila.get("access_token") or ""))
            eventos = registrar_webhooks(store_id, token)
            from servicios.tiendanube_shipping import registrar_shipping_carrier
            shipping = registrar_shipping_carrier(store_id, token)
            if not shipping.get("ready") or not confirmar_webhooks(store_id, eventos):
                raise TiendanubeWebhookError("Readiness Tiendanube incompleto.")
            completadas += 1
        except Exception as exc:
            errores += 1
            print(
                "[tiendanube] instalación pendiente no reconciliada: "
                f"{type(exc).__name__}"
            )
    return {"completadas": completadas, "errores": errores}


def datos_tienda(store_id: str, token: str) -> dict:
    r = _api(store_id, token, "GET", "store")
    if r is not None and r.status_code == 200:
        try:
            return r.json()
        except Exception:
            pass
    return {}


def parsear_pedido(order: dict) -> Optional[dict]:
    """
    Reduce un pedido de Tiendanube a lo que TAURO necesita. Devuelve None
    si no hay dirección de envío (retiro en local / producto digital).
    """
    envio = order.get("shipping_address") or {}
    if not envio.get("address"):
        return None

    items = [
        {
            "titulo": (it.get("name") or "")[:180],
            "cantidad": int(it.get("quantity") or 1),
            "precio": it.get("price"),
            "sku": (it.get("sku") or "")[:80],
            "peso_gr": int(float(it.get("weight") or 0) * 1000) or None,
        }
        for it in (order.get("products") or [])
    ]

    nombre = (envio.get("name")
              or f"{envio.get('first_name', '')} {envio.get('last_name', '')}".strip()
              or (order.get("customer") or {}).get("name", ""))

    # Número + piso van separados: fusionarlos hace que el courier los
    # trunque y el paquete llegue al edificio pero no al departamento.
    calle = " ".join(x for x in [envio.get("address"), str(envio.get("number") or "")] if x).strip()

    # Flete que le cobró la tienda al comprador: la misma regla de negocio que
    # en Shopify (comparar lo cobrado vs lo que sale la guía). Tiendanube lo
    # trae en shipping_cost_customer, con el método en shipping_option.
    try:
        flete_cobrado = round(float(order.get("shipping_cost_customer") or 0), 2)
    except (TypeError, ValueError):
        flete_cobrado = 0.0
    flete_detalle = []
    if flete_cobrado or order.get("shipping_option"):
        flete_detalle.append({
            "titulo": (order.get("shipping_option") or "Envío")[:120],
            "codigo": (order.get("shipping") or "")[:80],
            "precio": flete_cobrado,
        })

    # Provincia: Tiendanube manda el nombre (no un código de 2 letras como
    # Shopify). Para envíos nacionales AR el nombre es lo correcto; si algún día
    # se despacha internacional desde una venta Tiendanube, revisar el mapeo a
    # código que piden los couriers (FL, CA…).
    return {
        "pedido_externo_id": str(order.get("id") or ""),
        "numero": str(order.get("number") or order.get("id") or ""),
        "destinatario": {
            "nombre": nombre[:160],
            "empresa": "",
            "email": ((order.get("customer") or {}).get("email") or "")[:160],
            "telefono": (envio.get("phone")
                         or (order.get("customer") or {}).get("phone") or "")[:40],
            "direccion": calle[:300],
            "direccion2": (envio.get("floor") or "")[:150],
            "ciudad": (envio.get("city") or "")[:120],
            "estado": (envio.get("province") or "")[:120],
            "cp": (envio.get("zipcode") or "")[:24],
            "pais": (envio.get("country") or "")[:3],
        },
        "items": items,
        "valor_total": order.get("total"),
        "moneda": (order.get("currency") or "")[:6],
        "flete_cobrado": flete_cobrado,
        "flete_detalle": flete_detalle,
        "cancelado": bool(order.get("cancelled_at")),
        "estado_pago": (order.get("payment_status") or "")[:30],
    }


def marcar_enviado(store_id: str, pedido_externo_id: str, tracking: str,
                   url_tracking: str = "") -> bool:
    """
    Cierra el ciclo: el pedido pasa a enviado en Tiendanube y el comprador
    recibe su seguimiento sin que el comerciante toque nada.
    """
    inst = instalacion(store_id)
    if (not inst or not inst.get("access_token")
            or inst.get("estado") != "ACTIVA"
            or not inst.get("webhooks_ready")):
        return False
    payload = {"status": "shipped", "shipping_tracking_number": tracking}
    if url_tracking:
        payload["shipping_tracking_url"] = url_tracking
    r = _api(store_id, inst["access_token"], "PUT",
             f"orders/{pedido_externo_id}", payload)
    ok = r is not None and r.status_code in (200, 201)
    if not ok:
        print(f"[tiendanube] no pude marcar enviado {pedido_externo_id}: "
              f"{r.status_code if r is not None else 'sin respuesta'}")
    return ok


def _registrar_lifecycle(
    cur, store_id: str, evento: str, install_generation: str,
) -> None:
    cur.execute("""
        INSERT INTO tiendanube_lifecycle_eventos
            (store_id, evento, install_generation)
        VALUES (%s, %s, %s)
    """, (store_id, evento, install_generation or "sin-instalacion"))


def _desactivar_binding(cur, store_id: str) -> None:
    cur.execute("""
        UPDATE tiendas_conectadas
           SET activa = FALSE
         WHERE dominio = %s AND plataforma = 'tiendanube'
    """, (f"{store_id}.tiendanube",))


def _desactivar_shipping(store_id: str) -> None:
    from servicios.tiendanube_shipping import desactivar
    desactivar(str(store_id))


def desinstalar(store_id: str, install_generation: str = "") -> bool:
    """Tombstone durable: no borra antes de recibir store/redact."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tiendanube_instalaciones
                   SET estado = 'DESINSTALADA', access_token = '',
                       webhooks_ready = FALSE, webhooks_verified_at = NULL,
                       claim_token_hash = NULL, claim_expires_at = NULL,
                       desinstalada_en = NOW(), actualizada_en = NOW()
                 WHERE store_id = %s
                   AND (%s = '' OR install_generation = %s)
                RETURNING install_generation
            """, (str(store_id), install_generation, install_generation))
            fila = cur.fetchone()
            if not fila:
                return False
            _desactivar_binding(cur, str(store_id))
            _registrar_lifecycle(
                cur, str(store_id), "APP_UNINSTALLED",
                str(fila.get("install_generation") or install_generation),
            )
        conn.commit()
    _desactivar_shipping(str(store_id))
    return True


def suspender(store_id: str, install_generation: str = "") -> bool:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tiendanube_instalaciones
                   SET estado = 'SUSPENDIDA', webhooks_ready = FALSE,
                       suspendida_en = NOW(), actualizada_en = NOW()
                 WHERE store_id = %s
                   AND (%s = '' OR install_generation = %s)
                RETURNING install_generation
            """, (str(store_id), install_generation, install_generation))
            fila = cur.fetchone()
            if not fila:
                return False
            _desactivar_binding(cur, str(store_id))
            _registrar_lifecycle(
                cur, str(store_id), "APP_SUSPENDED",
                str(fila.get("install_generation") or install_generation),
            )
        conn.commit()
    _desactivar_shipping(str(store_id))
    return True


def _normalizar_ids(valores) -> list[str]:
    if not isinstance(valores, list):
        return []
    return [str(v)[:80] for v in valores[:500] if str(v or "").strip()]


def sanitizar_payload_webhook(datos: dict) -> dict:
    """Elimina email/teléfono/identificación antes de escribir la cola."""
    evento = str((datos or {}).get("event") or "").strip().lower()
    base = {
        "store_id": str((datos or {}).get("store_id") or ""),
        "event": evento,
    }
    if evento in EVENTOS_PEDIDOS:
        base["id"] = str((datos or {}).get("id") or "")
    elif evento == "customers/redact":
        customer = (datos or {}).get("customer") or {}
        base["customer_id"] = str(customer.get("id") or "")[:80]
        base["orders_to_redact"] = _normalizar_ids(
            (datos or {}).get("orders_to_redact")
        )
    elif evento == "customers/data_request":
        customer = (datos or {}).get("customer") or {}
        solicitud = (datos or {}).get("data_request") or {}
        base["customer_id"] = str(customer.get("id") or "")[:80]
        base["request_id"] = str(solicitud.get("id") or "")[:80]
        base["orders_requested"] = _normalizar_ids(
            (datos or {}).get("orders_requested")
        )
        base["checkouts_requested"] = _normalizar_ids(
            (datos or {}).get("checkouts_requested")
        )
        base["draft_orders_requested"] = _normalizar_ids(
            (datos or {}).get("draft_orders_requested")
            or (datos or {}).get("drafts_orders_requested")
        )
    return base


def webhook_evento_id(datos: dict, cuerpo: bytes, header_id: str = "") -> str:
    if str(header_id or "").strip():
        return str(header_id).strip()[:200]
    # Tiendanube no garantiza un identificador de entrega. Hashear sólo
    # store/event/order convertía todas las actualizaciones futuras del mismo
    # pedido en un único evento. Sin un ID explícito se persiste cada entrega;
    # la idempotencia de negocio vive en el upsert del pedido.
    return f"generated-{secrets.token_hex(20)}"


def encolar_webhook(evento_id: str, datos: dict) -> bool:
    """ACK durable: retorna sólo después del commit PostgreSQL."""
    _ensure_tabla()
    payload = sanitizar_payload_webhook(datos)
    store_id = str(payload.get("store_id") or "").strip()
    evento = str(payload.get("event") or "").strip().lower()
    recurso_id = str(payload.get("id") or payload.get("request_id") or "")
    if not store_id or evento not in EVENTOS_ACEPTADOS:
        raise TiendanubeWebhookError("Payload Tiendanube inválido.")
    if evento in EVENTOS_PEDIDOS and not recurso_id:
        raise TiendanubeWebhookError("El webhook de pedido no incluye id.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT install_generation
                  FROM tiendanube_instalaciones
                 WHERE store_id = %s
            """, (store_id,))
            inst = cur.fetchone() or {}
            generation = str(inst.get("install_generation") or "sin-instalacion")
            cur.execute("""
                INSERT INTO tiendanube_webhook_eventos
                    (evento_id, store_id, evento, recurso_id,
                     install_generation, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (evento_id) DO NOTHING
                RETURNING evento_id
            """, (
                str(evento_id), store_id, evento, recurso_id,
                generation, Json(payload),
            ))
            nuevo = cur.fetchone() is not None
            cur.execute("""
                DELETE FROM tiendanube_webhook_eventos
                 WHERE estado = 'COMPLETADO'
                   AND procesado_at < NOW() - INTERVAL '180 days'
            """)
        conn.commit()
    return nuevo


def _tomar_evento() -> Optional[dict]:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH elegido AS (
                    SELECT evento_id
                      FROM tiendanube_webhook_eventos
                     WHERE (estado = 'PENDIENTE' AND proximo_intento_at <= NOW())
                        OR (estado = 'PROCESANDO'
                            AND started_at < NOW() - INTERVAL '10 minutes')
                     ORDER BY recibido_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE tiendanube_webhook_eventos e
                   SET estado = 'PROCESANDO', started_at = NOW(),
                       intentos = intentos + 1
                  FROM elegido
                 WHERE e.evento_id = elegido.evento_id
                RETURNING e.*
            """)
            fila = cur.fetchone()
        conn.commit()
    return dict(fila) if fila else None


def _resolver_instalacion_operativa(evento: dict) -> dict:
    inst = instalacion(str(evento.get("store_id") or "")) or {}
    if str(inst.get("install_generation") or "") != str(
        evento.get("install_generation") or ""
    ):
        raise TiendanubeError("GENERACION_OBSOLETA")
    if (inst.get("estado") != "ACTIVA" or not inst.get("webhooks_ready")
            or not inst.get("access_token") or not inst.get("cliente_id")):
        raise TiendanubeRetryableError("TIENDA_NO_OPERATIVA")
    return inst


def _procesar_pedido_evento(evento: dict) -> str:
    from servicios.integraciones_tienda import (
        cancelar_pedido_externo, guardar_pedido, id_de_pedido,
        tienda_por_dominio,
    )
    store_id = str(evento["store_id"])
    payload = evento.get("payload") or {}
    pedido_id = str(payload.get("id") or "")
    inst = _resolver_instalacion_operativa(evento)
    r = _api(store_id, inst["access_token"], "GET", f"orders/{pedido_id}")
    if r is None or r.status_code != 200:
        codigo = r.status_code if r is not None else "SIN_RESPUESTA"
        raise TiendanubeRetryableError(f"ORDER_GET_{codigo}")
    try:
        orden = r.json()
    except Exception as exc:
        raise TiendanubeRetryableError("ORDER_JSON_INVALIDO") from exc
    pedido = parsear_pedido(orden)
    if not pedido:
        return "SIN_DIRECCION"
    tienda = tienda_por_dominio(f"{store_id}.tiendanube")
    owner_inst = str(inst.get("cliente_id") or "").strip().upper()
    owner_tienda = str((tienda or {}).get("cliente_id") or "").strip().upper()
    if (not tienda or tienda.get("plataforma") != "tiendanube"
            or tienda.get("activa") is not True
            or owner_tienda != owner_inst):
        raise TiendanubeRetryableError("BINDING_NO_OPERATIVO")
    if evento["evento"] == "order/cancelled" or pedido.get("cancelado"):
        cancelar_pedido_externo(tienda["id"], pedido["pedido_externo_id"])
        return "CANCELADO"
    creado = guardar_pedido(owner_inst, tienda["id"], "tiendanube", pedido)
    if creado:
        interno = id_de_pedido(tienda["id"], pedido["pedido_externo_id"])
        if interno:
            try:
                from servicios.solicitud_automatica import intentar_en_segundo_plano
                intentar_en_segundo_plano(interno)
            except Exception as exc:
                print(f"[tiendanube] armado automático no iniciado: {type(exc).__name__}")
    return "CREADO" if creado else "ACTUALIZADO"


def _procesar_customers_redact(evento: dict) -> str:
    payload = evento.get("payload") or {}
    ids = _normalizar_ids(payload.get("orders_to_redact"))
    store_id = str(evento["store_id"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            if ids:
                cur.execute("""
                    UPDATE pedidos_tienda p
                       SET destinatario = '{}'::jsonb
                      FROM tiendas_conectadas t
                     WHERE p.tienda_id = t.id
                       AND t.dominio = %s
                       AND p.pedido_externo_id = ANY(%s)
                """, (f"{store_id}.tiendanube", ids))
            cur.execute("""
                DELETE FROM pedidos_huerfanos
                 WHERE dominio = %s
                   AND (%s = '{}'::TEXT[] OR pedido_externo_id = ANY(%s))
            """, (f"{store_id}.tiendanube", ids, ids))
            _registrar_lifecycle(
                cur, store_id, "CUSTOMERS_REDACT",
                str(evento.get("install_generation") or "sin-instalacion"),
            )
        conn.commit()
    return "REDACTADO"


def _procesar_data_request(evento: dict) -> str:
    payload = evento.get("payload") or {}
    recursos = (
        _normalizar_ids(payload.get("orders_requested"))
        + _normalizar_ids(payload.get("checkouts_requested"))
        + _normalizar_ids(payload.get("draft_orders_requested"))
    )
    request_id = str(payload.get("request_id") or evento.get("evento_id") or "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tiendanube_privacidad_solicitudes
                    (request_id, store_id, tipo, customer_id, recursos)
                VALUES (%s, %s, 'customers/data_request', %s, %s)
                ON CONFLICT (store_id, tipo, request_id) DO NOTHING
            """, (
                request_id, str(evento["store_id"]),
                str(payload.get("customer_id") or ""), Json(recursos),
            ))
        conn.commit()
    return "SOLICITUD_PERSISTIDA"


def _procesar_store_redact(evento: dict) -> str:
    store_id = str(evento["store_id"])
    generation = str(evento.get("install_generation") or "sin-instalacion")
    _ensure_tabla()
    _desactivar_shipping(store_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # El DELETE del binding elimina pedidos_tienda por su FK CASCADE.
            cur.execute(
                "DELETE FROM tiendas_conectadas WHERE dominio = %s "
                "AND plataforma = 'tiendanube'",
                (f"{store_id}.tiendanube",),
            )
            cur.execute(
                "DELETE FROM pedidos_huerfanos WHERE dominio = %s",
                (f"{store_id}.tiendanube",),
            )
            cur.execute(
                "DELETE FROM tiendanube_privacidad_solicitudes WHERE store_id = %s",
                (store_id,),
            )
            cur.execute(
                "DELETE FROM tiendanube_shipping_config WHERE store_id = %s",
                (store_id,),
            )
            cur.execute("""
                UPDATE tiendanube_instalaciones
                   SET access_token = '', cliente_id = NULL, nombre = NULL,
                       estado = 'REDACTADA', webhooks_ready = FALSE,
                       webhooks_verified_at = NULL, claim_token_hash = NULL,
                       claim_expires_at = NULL, redactada_en = NOW(),
                       actualizada_en = NOW()
                 WHERE store_id = %s
            """, (store_id,))
            _registrar_lifecycle(cur, store_id, "STORE_REDACT", generation)
        conn.commit()
    return "STORE_REDACTADA"


def _procesar_evento(evento: dict) -> str:
    topic = str(evento.get("evento") or "")
    if topic in EVENTOS_PEDIDOS:
        return _procesar_pedido_evento(evento)
    if topic == "app/uninstalled":
        desinstalar(str(evento["store_id"]), str(evento["install_generation"]))
        return "DESINSTALADA"
    if topic == "app/suspended":
        suspender(str(evento["store_id"]), str(evento["install_generation"]))
        return "SUSPENDIDA"
    if topic == "customers/redact":
        return _procesar_customers_redact(evento)
    if topic == "customers/data_request":
        return _procesar_data_request(evento)
    if topic == "store/redact":
        return _procesar_store_redact(evento)
    raise TiendanubeError("EVENTO_NO_SOPORTADO")


def procesar_cola_eventos(limite: int = 20) -> dict:
    procesados = errores = 0
    for _ in range(max(1, min(int(limite), 100))):
        evento = _tomar_evento()
        if not evento:
            break
        try:
            resultado = _procesar_evento(evento)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE tiendanube_webhook_eventos
                           SET estado = 'COMPLETADO', procesado_at = NOW(),
                               ultimo_error = NULL
                         WHERE evento_id = %s
                    """, (evento["evento_id"],))
                conn.commit()
            procesados += 1
            print(f"[tiendanube] webhook procesado: {resultado}")
        except Exception as exc:
            errores += 1
            codigo = str(exc or type(exc).__name__)[:160]
            obsoleto = codigo == "GENERACION_OBSOLETA"
            intentos = int(evento.get("intentos") or 1)
            reintentar = isinstance(exc, TiendanubeRetryableError) and intentos < 12
            estado = "COMPLETADO" if obsoleto else ("PENDIENTE" if reintentar else "ERROR")
            demora = min(3600, 30 * (2 ** min(intentos - 1, 7)))
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE tiendanube_webhook_eventos
                           SET estado = %s, ultimo_error = %s,
                               proximo_intento_at = NOW() + (%s * INTERVAL '1 second'),
                               procesado_at = CASE WHEN %s THEN NULL ELSE NOW() END
                         WHERE evento_id = %s
                    """, (
                        estado, codigo, demora, reintentar,
                        evento["evento_id"],
                    ))
                conn.commit()
            if obsoleto:
                errores -= 1
                procesados += 1
                continue
            print(f"[tiendanube] webhook falló: {type(exc).__name__}")
            break
    return {"procesados": procesados, "errores": errores}


_worker_lock = threading.Lock()


def lanzar_procesamiento_eventos() -> None:
    def _run():
        if not _worker_lock.acquire(blocking=False):
            return
        try:
            procesar_cola_eventos()
        except Exception as exc:
            print(f"[tiendanube] worker falló: {type(exc).__name__}")
        finally:
            _worker_lock.release()

    threading.Thread(
        target=_run, daemon=True, name="tiendanube-webhook-worker",
    ).start()
