# ============================================================
# App pública de TAURO para Shopify
# ============================================================
# Cubre el ciclo completo que decidimos:
#   · INSTALACIÓN  → OAuth: el comerciante da un click y queda conectado
#                    (registramos sus webhooks solos, sin pasos manuales).
#   · CHECKOUT     → la tienda conserva sus tarifas nativas de Shopify;
#                    TAURO no modifica el precio ni el orden del checkout.
#   · VENTA        → webhook orders/create → pedido pendiente en el portal.
#   · GUÍA         → al emitirla en TAURO, marcamos el pedido como enviado
#                    en Shopify con su tracking: el comprador recibe el mail
#                    de "tu pedido va en camino" sin que nadie toque nada.
#
# Las instalaciones nuevas usan SHOPIFY_PUBLIC_API_KEY / _SECRET. Durante la
# migración, SHOPIFY_API_KEY / _SECRET conservan la app histórica para que sus
# webhooks y tokens sigan funcionando hasta que cada tienda reautorice TAURO.
# Si las variables PUBLIC todavía no existen, las genéricas siguen funcionando
# como antes.
# ============================================================
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from cryptography.fernet import Fernet, InvalidToken

from core.database import get_conn

# Versión estable vigente (Shopify saca una por trimestre y las soporta 12
# meses). Estaba fijada en "2025-01", que ya salió de la tabla de versiones
# soportadas: Shopify hace "fall forward" silencioso a otra, así que la app
# corría contra una versión que nadie eligió.
API_VERSION = "2026-07"

# Permisos mínimos REALES, uno por operación GraphQL que la app usa:
#   read_orders  ....................... recibir orders/* y leer el pedido
#   read_products/read_inventory/locations  sincronizar catálogo y stock
#   write_merchant_managed_fulfillment_orders  crear el fulfillment con tracking
#
# OJO: `write_fulfillments` NO alcanza para esto. Ese scope aplica al objeto
# FulfillmentService (cuando sos el depósito), no a las fulfillment orders de
# un pedido. Con los scopes viejos, Shopify devolvía la lista de fulfillment
# orders VACÍA y marcar_enviado() cortaba en silencio: la guía se emitía pero
# el comprador nunca recibía el mail con el tracking.
#
# Se sacaron `write_orders`, `read_fulfillments` y `write_shipping` porque
# ningún endpoint que usamos los necesita — pedir permisos de más es motivo de
# rechazo en la revisión de la App Store, y asusta al comerciante en la
# pantalla de instalación.
#   · write_shipping era SÓLO para el antiguo CarrierService. Ese código
#     REST fue eliminado: /shopify/tarifas devuelve [] a propósito y el precio
#     del envío lo pone el comerciante con sus tarifas de Shopify.
# Catálogo + inventario son de lectura: Shopify sigue siendo la fuente de
# verdad y TAURO mantiene un espejo local rápido. No pedimos `write_inventory`.
SCOPES = (
    "read_orders,read_products,read_inventory,read_locations,"
    "write_merchant_managed_fulfillment_orders"
)

WEBHOOK_TOPICS = (
    # Primero el cierre de ciclo. Si una alta posterior falla, Shopify ya puede
    # avisar la desinstalación y TAURO conserva tombstone/purga deterministas.
    ("app/uninstalled", "APP_UNINSTALLED", "/shopify/webhook/desinstalada"),
    ("orders/create", "ORDERS_CREATE", "/integraciones/shopify/webhook/orders-create"),
    ("orders/updated", "ORDERS_UPDATED", "/integraciones/shopify/webhook/orders-updated"),
    ("orders/cancelled", "ORDERS_CANCELLED", "/integraciones/shopify/webhook/orders-cancelled"),
    ("products/create", "PRODUCTS_CREATE", "/integraciones/shopify/webhook/products-create"),
    ("products/update", "PRODUCTS_UPDATE", "/integraciones/shopify/webhook/products-update"),
    ("products/delete", "PRODUCTS_DELETE", "/integraciones/shopify/webhook/products-delete"),
    ("inventory_levels/update", "INVENTORY_LEVELS_UPDATE", "/integraciones/shopify/webhook/inventory-levels-update"),
    ("inventory_items/update", "INVENTORY_ITEMS_UPDATE", "/integraciones/shopify/webhook/inventory-items-update"),
)

# Shopify recomienda renovar antes del vencimiento, no esperar a que una
# request de Admin API falle. El margen también absorbe pequeños desfasajes de
# reloj entre TAURO y Shopify.
_TOKEN_REFRESH_MARGIN = timedelta(minutes=1)

_tabla_lista = False


class ShopifyWebhookVerificationError(RuntimeError):
    """Shopify no permitió confirmar el estado de las suscripciones."""


class ShopifyOwnershipConflict(RuntimeError):
    """La tienda ya está vinculada a otra cuenta TAURO."""


def _ensure_tabla() -> None:
    """Comprueba el esquema Shopify sin ejecutar DDL en OAuth/webhooks."""
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    to_regclass('shopify_instalaciones') IS NOT NULL
                    AND to_regclass('shopify_desinstalaciones') IS NOT NULL
                    AND to_regclass('shopify_shop_redact_pendientes') IS NOT NULL
                    AND (
                        SELECT COUNT(*) = 9
                          FROM information_schema.columns
                         WHERE table_schema = CURRENT_SCHEMA()
                           AND table_name = 'shopify_instalaciones'
                           AND column_name IN (
                               'app_client_id', 'install_generation',
                               'refresh_token', 'access_token_expires_at',
                               'refresh_token_expires_at',
                               'token_reauth_required',
                               'token_refresh_failed_at', 'webhooks_ready',
                               'webhooks_verified_at'
                           )
                    )
                    AND EXISTS (
                        SELECT 1 FROM information_schema.columns
                         WHERE table_schema = CURRENT_SCHEMA()
                           AND table_name = 'shopify_instalaciones'
                           AND column_name = 'install_generation'
                           AND is_nullable = 'NO'
                    ) AS schema_ready
                """
            )
            row = cur.fetchone()
    ready = bool(
        row.get("schema_ready")
        if hasattr(row, "get")
        else row[0] if row else False
    )
    if not ready:
        raise RuntimeError(
            "Shopify requiere ejecutar la migración antes del tráfico."
        )
    _tabla_lista = True


def _fernets() -> list[Fernet]:
    """Claves activa y de transición para rotar sin perder instalaciones.

    Si la clave exclusiva todavía no existe, se cifra con el API secret. Al
    agregarla más adelante, los tokens nuevos usan la exclusiva y los viejos
    siguen pudiendo abrirse con el API secret estable.
    """
    materiales = [
        os.getenv("SHOPIFY_TOKEN_ENCRYPTION_KEY") or "",
        _credenciales_publicas()[1],
        *[secreto for _client_id, secreto in _credenciales_webhook()],
    ]
    resultado: list[Fernet] = []
    vistos: set[bytes] = set()
    for material_crudo in materiales:
        material = material_crudo.encode("utf-8")
        if not material or material in vistos:
            continue
        vistos.add(material)
        clave = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
        resultado.append(Fernet(clave))
    return resultado


def _credenciales_publicas() -> tuple[str, str]:
    """Credenciales que se usan para toda instalación nueva.

    Las variables PUBLIC se tratan como un par: si sólo cargaron una, no se
    mezcla accidentalmente con la credencial histórica.
    """
    public_key = (os.getenv("SHOPIFY_PUBLIC_API_KEY") or "").strip()
    public_secret = (os.getenv("SHOPIFY_PUBLIC_API_SECRET") or "").strip()
    if public_key or public_secret:
        return public_key, public_secret
    return (
        (os.getenv("SHOPIFY_API_KEY") or "").strip(),
        (os.getenv("SHOPIFY_API_SECRET") or "").strip(),
    )


def _credenciales_webhook() -> list[tuple[str, str]]:
    """Apps cuyos webhooks pueden coexistir durante la migración.

    La app pública siempre queda primera. Las variables genéricas se conservan
    como legado cuando existen las PUBLIC; también se admite el par LEGACY
    explícito para poder retirar las genéricas más adelante sin apuro.
    """
    candidatos = [
        _credenciales_publicas(),
        (
            (os.getenv("SHOPIFY_LEGACY_API_KEY") or "").strip(),
            (os.getenv("SHOPIFY_LEGACY_API_SECRET") or "").strip(),
        ),
        (
            (os.getenv("SHOPIFY_API_KEY") or "").strip(),
            (os.getenv("SHOPIFY_API_SECRET") or "").strip(),
        ),
    ]
    resultado: list[tuple[str, str]] = []
    vistos: set[tuple[str, str]] = set()
    for client_id, secreto in candidatos:
        par = (client_id, secreto)
        if not client_id or not secreto or par in vistos:
            continue
        vistos.add(par)
        resultado.append(par)
    return resultado


def _client_id_historico() -> str:
    """Identidad de las filas previas a la columna ``app_client_id``."""
    legacy_key = (os.getenv("SHOPIFY_LEGACY_API_KEY") or "").strip()
    legacy_secret = (os.getenv("SHOPIFY_LEGACY_API_SECRET") or "").strip()
    if legacy_key and legacy_secret:
        return legacy_key
    generic_key = (os.getenv("SHOPIFY_API_KEY") or "").strip()
    generic_secret = (os.getenv("SHOPIFY_API_SECRET") or "").strip()
    if generic_key and generic_secret:
        return generic_key
    # Fail closed: una fila sin identidad nunca puede ser borrada por la firma
    # de una app que apareció después.
    return "__shopify_legacy_sin_identificar__"


def migrar_instalaciones_legacy() -> int:
    """Backfill controlado que sólo se ejecuta en el predeploy.

    ``app_client_id`` identifica qué secreto puede autenticar cada generación.
    Las filas creadas antes de esa columna no pueden quedar ambiguas, pero el
    UPDATE tampoco debe ocurrir dentro del primer webhook que llegue.
    """
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopify_instalaciones
                   SET app_client_id = %s
                 WHERE app_client_id IS NULL OR BTRIM(app_client_id) = ''
                """,
                (_client_id_historico(),),
            )
            return int(cur.rowcount or 0)


def _client_id_instalacion_efectivo(valor: object) -> str:
    return str(valor or "").strip() or _client_id_historico()


def _credenciales_para_client_id(client_id: str) -> tuple[str, str]:
    """Resuelve el secreto de la app que emitió el refresh token.

    Durante la migración pueden coexistir tokens de la app pública y la
    histórica. Nunca se intenta refrescar un token con un secreto de otra app.
    """
    esperado = (client_id or "").strip()
    for candidato, secreto in _credenciales_webhook():
        if candidato == esperado:
            return candidato, secreto
    return "", ""


def cliente_app_para_webhook(cuerpo: bytes, firma: str,
                             client_id_esperado: str = "") -> Optional[str]:
    """Devuelve qué app firmó el webhook, sin exponer sus secretos.

    Si una tienda ya reautorizó la app pública, sólo se acepta esa firma. Las
    filas históricas sin ``app_client_id`` admiten ambas durante la transición.
    """
    from servicios.integraciones_tienda import verificar_hmac_shopify

    esperado = (client_id_esperado or "").strip()
    for client_id, secreto in _credenciales_webhook():
        if esperado and client_id != esperado:
            continue
        if verificar_hmac_shopify(secreto, cuerpo, firma):
            return client_id
    return None


def firma_valida_webhook_app(cuerpo: bytes, firma: str,
                             client_id_esperado: str = "") -> bool:
    return cliente_app_para_webhook(cuerpo, firma, client_id_esperado) is not None


def _cifrar_token(token: str) -> str:
    token = str(token or "")
    if not token or token.startswith("enc:v1:"):
        return token
    fernets = _fernets()
    if not fernets:
        raise RuntimeError("Falta clave para cifrar el token de Shopify.")
    return "enc:v1:" + fernets[0].encrypt(token.encode("utf-8")).decode("ascii")


def _descifrar_token(token_guardado: str) -> str:
    token_guardado = str(token_guardado or "")
    if not token_guardado:
        return ""
    if not token_guardado.startswith("enc:v1:"):
        raise RuntimeError("Token Shopify legacy sin migrar.")
    fernets = _fernets()
    if not fernets:
        raise RuntimeError("Falta clave para descifrar el token de Shopify.")
    ultimo_error = None
    for fernet in fernets:
        try:
            return fernet.decrypt(token_guardado[7:].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            ultimo_error = exc
    raise RuntimeError("El token cifrado de Shopify no se pudo abrir.") from ultimo_error


def migrar_tokens_legacy() -> int:
    """Cifra access/refresh históricos durante predeploy, de forma idempotente."""
    _ensure_tabla()
    cambios = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT dominio, access_token, refresh_token
                  FROM shopify_instalaciones
                 WHERE (
                        NULLIF(BTRIM(access_token), '') IS NOT NULL
                        AND access_token NOT LIKE 'enc:v1:%'
                       )
                    OR (
                        NULLIF(BTRIM(COALESCE(refresh_token, '')), '') IS NOT NULL
                        AND refresh_token NOT LIKE 'enc:v1:%'
                       )
                 FOR UPDATE
                """
            )
            for fila in cur.fetchall():
                access_anterior = str(fila.get("access_token") or "")
                refresh_anterior = str(fila.get("refresh_token") or "")
                access_nuevo = _cifrar_token(access_anterior)
                refresh_nuevo = _cifrar_token(refresh_anterior)
                cur.execute(
                    """
                    UPDATE shopify_instalaciones
                       SET access_token = %s,
                           refresh_token = NULLIF(%s, '')
                     WHERE dominio = %s
                       AND access_token = %s
                       AND COALESCE(refresh_token, '') = %s
                    """,
                    (
                        access_nuevo,
                        refresh_nuevo,
                        str(fila.get("dominio") or ""),
                        access_anterior,
                        refresh_anterior,
                    ),
                )
                cambios += int(cur.rowcount or 0)
        conn.commit()
    return cambios


def app_configurada() -> bool:
    api_key, api_secret = _credenciales_publicas()
    return bool(api_key and api_secret)


def api_key_publica() -> str:
    """Client ID público; es seguro usarlo en App Bridge y URLs OAuth."""
    return _credenciales_publicas()[0]


def _base_url() -> str:
    return (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")


# ── Instalación (OAuth) ─────────────────────────────────────

def url_instalacion(dominio: str, state: str) -> str:
    """A dónde mandamos al comerciante para que autorice la app."""
    params = {
        "client_id": _credenciales_publicas()[0],
        "scope": SCOPES,
        "redirect_uri": f"{_base_url()}/shopify/callback",
        "state": state,
    }
    return f"https://{dominio}/admin/oauth/authorize?" + urllib.parse.urlencode(params)


def validar_hmac_query(params: dict) -> bool:
    """
    Shopify firma los parámetros de la redirección OAuth. Sin esta
    verificación, cualquiera podría hacerse pasar por Shopify e
    instalarnos una tienda falsa.
    """
    secreto = _credenciales_publicas()[1]
    firma = params.get("hmac", "")
    if not secreto or not firma:
        return False
    resto = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
    mensaje = "&".join(f"{k}={resto[k]}" for k in sorted(resto))
    calculada = hmac.new(secreto.encode(), mensaje.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculada, firma)


_DOMINIO_SHOPIFY_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")


def dominio_valido(dominio: str) -> bool:
    """
    Solo dominios de Shopify, con regex ESTRICTA. La versión anterior usaba
    endswith(".myshopify.com"), que dejaba pasar cosas como
    "tienda.myshopify.com@evil.com" — el navegador interpreta lo anterior a
    la arroba como usuario y REDIRIGE A evil.com: open redirect servido.
    La regex sólo admite [a-z0-9-].myshopify.com, sin @, :, /, ? ni espacios.
    """
    return bool(_DOMINIO_SHOPIFY_RE.match((dominio or "").strip().lower()))


def canjear_token(dominio: str, code: str) -> Optional[dict]:
    """Cambia el código por el par rotativo offline de esa tienda."""
    try:
        client_id, client_secret = _credenciales_publicas()
        r = requests.post(
            f"https://{dominio}/admin/oauth/access_token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "expiring": "1",
            },
            timeout=20,
        )
        if r.status_code != 200:
            # El body OAuth puede contener detalles sensibles. El log sólo
            # conserva un código estable, nunca la respuesta cruda.
            print(f"[shopify] canje de token falló HTTP {r.status_code}")
            return None
        try:
            payload = r.json()
            access_token = str(payload.get("access_token") or "")
            refresh_token = str(payload.get("refresh_token") or "")
            expires_in = int(payload.get("expires_in"))
            refresh_expires_in = int(payload.get("refresh_token_expires_in"))
            if (
                not access_token or not refresh_token
                or expires_in <= 0 or refresh_expires_in <= 0
            ):
                raise ValueError("par OAuth incompleto")
        except (AttributeError, TypeError, ValueError):
            print("[shopify] canje de token devolvió un par OAuth incompleto")
            return None
        return payload
    except Exception as e:
        print(f"[shopify] excepción canjeando token: {type(e).__name__}")
        return None


def guardar_instalacion(
    dominio: str,
    access_token: str,
    scopes: str = "",
    instalada_desde: Optional[datetime] = None,
    *,
    refresh_token: str,
    expires_in: object,
    refresh_token_expires_in: object,
) -> str:
    """Crea una generación OAuth pendiente sin heredar el tenant anterior.

    Toda instalación nace ownerless y cualquier binding previo queda inactivo
    en la misma transacción. OAuth prueba control de Shopify, no identidad en
    TAURO; esa vinculación ocurre después con verificación explícita.
    """
    _ensure_tabla()
    from servicios.integraciones_tienda import (
        _bloquear_dominio_shopify,
        _ensure_tablas,
    )

    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    if not dominio_valido(dominio):
        raise ValueError("Dominio Shopify inválido.")
    token_cifrado = _cifrar_token(access_token)
    refresh_token = str(refresh_token or "")
    try:
        access_segundos = int(expires_in)
        refresh_segundos = int(refresh_token_expires_in)
    except (TypeError, ValueError) as exc:
        raise ValueError("Shopify no entregó vencimientos OAuth válidos.") from exc
    if not refresh_token or access_segundos <= 0 or refresh_segundos <= 0:
        raise ValueError("Shopify no entregó un par OAuth rotativo completo.")
    obtenido_en = datetime.now(timezone.utc)
    access_expira_en = obtenido_en + timedelta(seconds=access_segundos)
    refresh_expira_en = obtenido_en + timedelta(seconds=refresh_segundos)
    refresh_cifrado = _cifrar_token(refresh_token)
    generation = secrets.token_urlsafe(24)
    instalada_desde = instalada_desde or datetime.now(timezone.utc)
    if instalada_desde.tzinfo is None:
        instalada_desde = instalada_desde.replace(tzinfo=timezone.utc)
    instalada_desde = instalada_desde.astimezone(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute("""
                INSERT INTO shopify_instalaciones
                    (dominio, access_token, refresh_token,
                     access_token_expires_at, refresh_token_expires_at,
                     token_reauth_required, token_refresh_failed_at,
                     webhooks_ready, webhooks_verified_at,
                     scopes, app_client_id, install_generation,
                     instalada_en, cliente_id)
                VALUES (%s, %s, %s, %s, %s, FALSE, NULL, FALSE, NULL,
                        %s, %s, %s, %s, NULLIF(%s, ''))
                ON CONFLICT (dominio) DO UPDATE
                    SET access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        access_token_expires_at = EXCLUDED.access_token_expires_at,
                        refresh_token_expires_at = EXCLUDED.refresh_token_expires_at,
                        token_reauth_required = FALSE,
                        token_refresh_failed_at = NULL,
                        webhooks_ready = FALSE,
                        webhooks_verified_at = NULL,
                        scopes = EXCLUDED.scopes,
                        -- Cada OAuth exitoso es una generación nueva, incluso
                        -- bajo la misma app. Shopify no distingue reauth de
                        -- reinstall en webhooks tardíos; nosotros sí, mediante
                        -- generación + instalada_en.
                        instalada_en = EXCLUDED.instalada_en,
                        install_generation = EXCLUDED.install_generation,
                        app_client_id = EXCLUDED.app_client_id,
                        cliente_id = EXCLUDED.cliente_id
            """, (
                dominio,
                token_cifrado,
                refresh_cifrado,
                access_expira_en,
                refresh_expira_en,
                scopes,
                _credenciales_publicas()[0],
                generation,
                instalada_desde,
                "",
            ))
            cur.execute(
                """
                UPDATE tiendas_conectadas
                   SET activa = FALSE
                 WHERE dominio = %s
                """,
                (dominio,),
            )
        conn.commit()
    return generation


def instalacion(dominio: str) -> Optional[dict]:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM shopify_instalaciones WHERE dominio = %s", (dominio,))
            row = cur.fetchone()
            if not row:
                return None
            datos = dict(row)
            datos["app_client_id"] = _client_id_instalacion_efectivo(
                datos.get("app_client_id")
            )
            datos["webhooks_ready"] = bool(datos.get("webhooks_ready"))
            # El refresh token jamás sale del servicio de credenciales. Los
            # consumidores sólo necesitan saber si deben pedir reautorización.
            refresh_expira = _fecha_utc(datos.get("refresh_token_expires_at"))
            datos["token_rotativo"] = bool(
                datos.get("refresh_token")
                and datos.get("access_token_expires_at")
                and refresh_expira
                and refresh_expira > datetime.now(timezone.utc)
                and not datos.get("token_reauth_required")
            )
            datos.pop("refresh_token", None)
            datos.pop("access_token_expires_at", None)
            datos.pop("refresh_token_expires_at", None)
            datos.pop("token_refresh_failed_at", None)
            if datos.get("token_reauth_required") or not datos["webhooks_ready"]:
                datos["access_token"] = ""
                return datos
            try:
                datos["access_token"] = _descifrar_token(datos.get("access_token") or "")
            except Exception as exc:
                print(f"[shopify] token no disponible: {type(exc).__name__}")
                datos["access_token"] = ""
            return datos


def generacion_si_es_dueno_de_la_tienda(
    dominio: str,
    cliente_id: str,
) -> str | None:
    """
    ¿El cliente TAURO es realmente el dueño de esa tienda Shopify?

    Se pregunta a la propia tienda con el access_token que nos dio al
    instalar: si el email de la cuenta Shopify coincide con el email del
    cliente en TAURO, es suyo. Es la única prueba de propiedad que no
    depende de que el que reclama diga la verdad.

    Existe porque el auto-servicio "es mi tienda" no verificaba NADA: con la
    lista de tiendas sin vincular a la vista, cualquier cliente podía
    reclamar la tienda de otro y quedarse con sus ventas — y con los datos
    personales de los compradores.
    """
    dominio = (dominio or "").strip().lower()
    cliente_id = (cliente_id or "").strip().upper()
    if not dominio or not cliente_id:
        return None

    inst = instalacion(dominio)
    if not inst or not inst.get("access_token"):
        return None
    generation = str(inst.get("install_generation") or "").strip()
    if not generation:
        return None

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email FROM clientes WHERE cliente_id = %s",
                            (cliente_id,))
                fila = cur.fetchone()
        email_cliente = str((fila or {}).get("email") or "").strip().lower()
    except Exception as e:
        print(f"[shopify] no pude leer email de ownership: {type(e).__name__}")
        return None
    if not email_cliente:
        return None

    data = _graphql(dominio, inst["access_token"], """
        query TauroShopOwnership {
          shop { email contactEmail }
        }
    """)
    if data is None:
        print("[shopify] no pude verificar ownership (GraphQL sin respuesta)")
        return None
    shop = data.get("shop") or {}

    # Shopify expone el mail de la cuenta y el de contacto: vale cualquiera.
    posibles = {str(shop.get(k) or "").strip().lower()
                for k in ("email", "contactEmail")}
    coincide = email_cliente in posibles
    print(f"[shopify] verificación de ownership: {'OK' if coincide else 'NO COINCIDE'}")
    return generation if coincide else None


def es_dueno_de_la_tienda(dominio: str, cliente_id: str) -> bool:
    """Compatibilidad booleana para listados; el claim usa la generación."""
    return bool(generacion_si_es_dueno_de_la_tienda(dominio, cliente_id))


def vincular_cliente(
    dominio: str,
    cliente_id: str,
    *,
    expected_generation: str,
) -> None:
    """
    Ata la tienda instalada a la cuenta TAURO del comerciante.

    Además la registra en `tiendas_conectadas`, que es la tabla que usa
    todo el resto del portal (pedidos pendientes, lista de tiendas,
    política de flete). Así la app y el modo manual conviven sin que
    nada más tenga que saber por dónde entró cada tienda. El "secreto"
    guardado es el API secret de la app, porque con eso firma Shopify
    los webhooks de las apps.
    """
    _ensure_tabla()
    from servicios.integraciones_tienda import (
        OAUTH_SECRET_MARKER,
        _bloquear_dominio_shopify,
        _ensure_tablas,
        volcar_huerfanos,
    )

    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    cliente_id = (cliente_id or "").strip().upper()
    expected_generation = str(expected_generation or "").strip()
    if not dominio_valido(dominio) or not cliente_id or not expected_generation:
        raise ValueError("Tienda o cliente inválido.")

    tienda_id = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT id, cliente_id, webhooks_ready, install_generation
                  FROM shopify_instalaciones
                 WHERE dominio = %s
                 FOR UPDATE
                """,
                (dominio,),
            )
            instalacion_actual = cur.fetchone()
            if not instalacion_actual:
                raise RuntimeError("La instalación Shopify ya no está activa.")
            if str(instalacion_actual.get("install_generation") or "") != expected_generation:
                raise RuntimeError(
                    "La instalación Shopify cambió durante la verificación."
                )
            if not instalacion_actual.get("webhooks_ready"):
                raise RuntimeError(
                    "La instalación Shopify todavía no verificó sus webhooks."
                )
            owner_inst = str(
                (instalacion_actual or {}).get("cliente_id") or ""
            ).strip().upper()
            if owner_inst and owner_inst != cliente_id:
                raise ShopifyOwnershipConflict(
                    "Ese dominio ya está vinculado a otra cuenta TAURO."
                )

            cur.execute(
                """
                SELECT id, cliente_id, plataforma
                  FROM tiendas_conectadas
                 WHERE dominio = %s
                 FOR UPDATE
                """,
                (dominio,),
            )
            mapping = cur.fetchone()
            owner_mapping = str((mapping or {}).get("cliente_id") or "").strip().upper()
            if owner_mapping and owner_mapping != cliente_id:
                raise ShopifyOwnershipConflict(
                    "Ese dominio ya está conectado a otra cuenta TAURO."
                )

            cur.execute(
                """
                UPDATE shopify_instalaciones
                   SET cliente_id = %s
                 WHERE dominio = %s AND install_generation = %s
                RETURNING id
                """,
                (cliente_id, dominio, expected_generation),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    "La instalación Shopify cambió durante la vinculación."
                )
            cur.execute(
                """
                INSERT INTO tiendas_conectadas
                    (cliente_id, plataforma, dominio, secreto, activa)
                VALUES (%s, 'shopify', %s, %s, TRUE)
                ON CONFLICT (dominio) DO UPDATE SET
                    plataforma = 'shopify',
                    secreto = EXCLUDED.secreto,
                    activa = TRUE
                WHERE UPPER(tiendas_conectadas.cliente_id) = EXCLUDED.cliente_id
                RETURNING id, cliente_id
                """,
                (cliente_id, dominio, OAUTH_SECRET_MARKER),
            )
            resultado = cur.fetchone()
            if not resultado or str(resultado.get("cliente_id") or "").strip().upper() != cliente_id:
                raise ShopifyOwnershipConflict(
                    "Ese dominio ya está conectado a otra cuenta TAURO."
                )
            tienda_id = int(resultado["id"])
        conn.commit()

    # Ya fuera de la transacción de ownership: una orden malformada no puede
    # revertir el vínculo, pero cada INSERT vuelve a verificar owner+instalación.
    try:
        volcar_huerfanos(cliente_id, tienda_id, dominio)
    except Exception as exc:
        print(f"[shopify] no pude recuperar pedidos huérfanos: {type(exc).__name__}")


def instalaciones_sin_dueno() -> list[dict]:
    """Tiendas que instalaron la app pero todavía no se ataron a una cuenta."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT dominio, instalada_en FROM shopify_instalaciones
                WHERE webhooks_ready = TRUE
                  AND (cliente_id IS NULL OR cliente_id = '')
                ORDER BY instalada_en DESC
            """)
            return [dict(r) for r in cur.fetchall()]


def cliente_app_instalada(dominio: str) -> Optional[str]:
    """Client ID dueño del token actual, sin abrir ni exponer el token."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_client_id FROM shopify_instalaciones WHERE dominio = %s",
                (dominio,),
            )
            fila = cur.fetchone()
    if not fila:
        return None
    return _client_id_instalacion_efectivo((fila or {}).get("app_client_id"))


def _fecha_webhook(valor: str) -> Optional[datetime]:
    """Convierte X-Shopify-Triggered-At sin aceptar fechas ambiguas."""
    valor = str(valor or "").strip()
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(timezone.utc)


def timestamp_evento_firmado(topic: str, payload: dict) -> str:
    """Extrae tiempo sólo del body cubierto por HMAC, nunca de headers."""
    topic = str(topic or "").strip().lower()
    payload = payload if isinstance(payload, dict) else {}
    candidatos: tuple[str, ...]
    if topic == "orders/create":
        candidatos = ("updated_at", "created_at")
    elif topic == "orders/cancelled":
        candidatos = ("cancelled_at", "updated_at")
    elif topic.startswith("orders/"):
        candidatos = ("updated_at", "created_at")
    elif topic in {
        "products/create", "products/update",
        "inventory_levels/update", "inventory_items/update",
    }:
        candidatos = ("updated_at", "created_at")
    else:
        return ""
    for clave in candidatos:
        valor = str(payload.get(clave) or "").strip()
        if valor and _fecha_webhook(valor):
            return valor
    return ""


def clasificar_evento_instalacion(
    inst: Optional[dict],
    topic: str,
    payload: dict,
) -> str:
    """Clasifica con datos firmados como ACTUAL, ANTERIOR o INVALIDO.

    Los headers de Shopify no forman parte del HMAC y por eso jamás prueban la
    generación. Los topics que traen fecha en el body deben demostrar que el
    recurso fue creado/actualizado después del OAuth vigente.
    """
    marca = timestamp_evento_firmado(topic, payload)
    evento = _fecha_webhook(marca)
    instalada = (inst or {}).get("instalada_en")
    if isinstance(instalada, str):
        instalada = _fecha_webhook(instalada)
    elif isinstance(instalada, datetime):
        if instalada.tzinfo is None:
            instalada = instalada.replace(tzinfo=timezone.utc)
        instalada = instalada.astimezone(timezone.utc)
    else:
        instalada = None
    # El payload de products/delete sólo contiene el id. Su worker comprueba
    # el recurso remoto y el fingerprint durable cubre replays exactos.
    sin_fecha_permitido = str(topic or "").lower() == "products/delete"
    if (
        (not evento and not sin_fecha_permitido)
        or not instalada
        or not (inst or {}).get("install_generation")
    ):
        return "INVALIDO"
    if sin_fecha_permitido:
        return "ACTUAL"
    return "ANTERIOR" if evento < instalada else "ACTUAL"


def validar_recurso_webhook_shopify(
    dominio: str,
    topic: str,
    payload: dict,
    inst: Optional[dict] = None,
) -> dict:
    """Ata órdenes y borrados al recurso visible con el token de esa tienda.

    Devuelve estado y, para órdenes, el estado remoto actual. El HMAC es
    global a la app; esta
    lectura evita que un body firmado capturado en otra tienda se atribuya sólo
    cambiando headers que Shopify no firma.
    """
    topic = str(topic or "").strip().lower()
    current = inst if isinstance(inst, dict) else (instalacion(dominio) or {})
    token = str(current.get("access_token") or "")
    if not token or not current.get("install_generation"):
        return {"estado": "REINTENTAR"}

    def _probe(query: str, variables: dict) -> tuple[str, dict]:
        """Lectura única con presupuesto compatible con el ACK de webhook."""
        try:
            response = requests.post(
                f"https://{dominio}/admin/api/{API_VERSION}/graphql.json",
                headers={
                    "X-Shopify-Access-Token": token,
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables},
                timeout=2.0,
            )
        except Exception:
            return "REINTENTAR", {}
        if response.status_code != 200:
            return "REINTENTAR", {}
        try:
            envelope = response.json()
        except Exception:
            return "REINTENTAR", {}
        if envelope.get("errors"):
            return "REINTENTAR", {}
        return "OK", envelope.get("data") or {}

    raw_id = payload.get("admin_graphql_api_id") or payload.get("id")
    if topic.startswith("orders/"):
        valor = str(raw_id or "").strip()
        gid = valor if valor.startswith("gid://shopify/Order/") else (
            f"gid://shopify/Order/{valor}" if valor.isdigit() else ""
        )
        if not gid:
            return {"estado": "IGNORAR"}
        probe, data = _probe(
            """
            query TauroWebhookOrderIdentity($id: ID!) {
              order(id: $id) {
                id updatedAt cancelledAt displayFinancialStatus
              }
            }
            """,
            {"id": gid},
        )
        if probe != "OK":
            return {"estado": probe}
        order = data.get("order")
        if not isinstance(order, dict) or str(order.get("id") or "") != gid:
            return {"estado": "IGNORAR"}
        cancelled = bool(
            order.get("cancelledAt")
            or str(order.get("displayFinancialStatus") or "").upper()
            in {"REFUNDED", "VOIDED"}
        )
        remote_at = str(order.get("updatedAt") or order.get("cancelledAt") or "")
        if topic == "orders/cancelled" and not cancelled:
            return {"estado": "IGNORAR"}
        if cancelled:
            return {
                "estado": "ACTUAL",
                "cancelado": True,
                "evento_at": remote_at,
            }
        body_at = _fecha_webhook(timestamp_evento_firmado(topic, payload))
        remote_time = _fecha_webhook(remote_at)
        if (
            body_at
            and remote_time
            and remote_time > body_at + timedelta(seconds=1)
        ):
            # El body capturado quedó atrás del estado remoto. El update más
            # nuevo será procesado por su propio webhook; éste no puede pisarlo.
            return {"estado": "IGNORAR"}
        return {"estado": "ACTUAL", "cancelado": False, "evento_at": remote_at}

    if topic in {"products/create", "products/update"}:
        valor = str(raw_id or "").strip()
        gid = valor if valor.startswith("gid://shopify/Product/") else (
            f"gid://shopify/Product/{valor}" if valor.isdigit() else ""
        )
        if not gid:
            return {"estado": "IGNORAR"}
        probe, data = _probe(
            "query TauroProductIdentity($id: ID!) { product(id: $id) { id } }",
            {"id": gid},
        )
        if probe != "OK":
            return {"estado": probe}
        product = data.get("product")
        return {
            "estado": "ACTUAL"
            if isinstance(product, dict) and str(product.get("id") or "") == gid
            else "IGNORAR"
        }

    if topic in {"inventory_items/update", "inventory_levels/update"}:
        inventory_id = payload.get("inventory_item_id") or payload.get("id")
        value = str(inventory_id or "").strip()
        inventory_gid = (
            value
            if value.startswith("gid://shopify/InventoryItem/")
            else f"gid://shopify/InventoryItem/{value}" if value.isdigit()
            else ""
        )
        if not inventory_gid:
            return {"estado": "IGNORAR"}
        variables = {"inventoryId": inventory_gid}
        query = """
            query TauroInventoryIdentity($inventoryId: ID!) {
              inventoryItem(id: $inventoryId) { id }
            }
        """
        if topic == "inventory_levels/update":
            location = str(payload.get("location_id") or "").strip()
            location_gid = (
                location
                if location.startswith("gid://shopify/Location/")
                else f"gid://shopify/Location/{location}" if location.isdigit()
                else ""
            )
            if not location_gid:
                return {"estado": "IGNORAR"}
            variables["locationId"] = location_gid
            query = """
                query TauroInventoryLevelIdentity(
                  $inventoryId: ID!, $locationId: ID!
                ) {
                  inventoryItem(id: $inventoryId) { id }
                  location(id: $locationId) { id }
                }
            """
        probe, data = _probe(query, variables)
        if probe != "OK":
            return {"estado": probe}
        item = data.get("inventoryItem")
        valid = isinstance(item, dict) and str(item.get("id") or "") == inventory_gid
        if topic == "inventory_levels/update":
            valid = valid and isinstance(data.get("location"), dict)
        return {"estado": "ACTUAL" if valid else "IGNORAR"}

    if topic == "products/delete":
        valor = str(raw_id or "").strip()
        gid = valor if valor.startswith("gid://shopify/Product/") else (
            f"gid://shopify/Product/{valor}" if valor.isdigit() else ""
        )
        if not gid:
            return {"estado": "IGNORAR"}
        probe, data = _probe(
            "query TauroDeletedProductCheck($id: ID!) { product(id: $id) { id } }",
            {"id": gid},
        )
        if probe != "OK":
            return {"estado": probe}
        # Si el producto aún existe, un delete capturado no describe el estado
        # actual de esta tienda y no debe desactivar el espejo.
        if data.get("product"):
            return {"estado": "IGNORAR"}
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1
                          FROM productos p
                          JOIN shopify_instalaciones i
                            ON LOWER(i.dominio) = LOWER(p.tienda_dominio)
                          JOIN tiendas_conectadas t
                            ON LOWER(t.dominio) = LOWER(i.dominio)
                           AND UPPER(t.cliente_id) = UPPER(i.cliente_id)
                         WHERE LOWER(p.plataforma) = 'shopify'
                           AND LOWER(p.tienda_dominio) = %s
                           AND p.external_product_id = %s
                           AND i.install_generation = %s
                           AND t.activa = TRUE
                           AND t.plataforma = 'shopify'
                           AND UPPER(p.cliente_id) = UPPER(i.cliente_id)
                         LIMIT 1
                        """,
                        (
                            dominio.lower(),
                            gid,
                            str(current.get("install_generation") or ""),
                        ),
                    )
                    owned = cur.fetchone() is not None
        except Exception:
            return {"estado": "REINTENTAR"}
        return {"estado": "ACTUAL" if owned else "IGNORAR"}
    return {"estado": "IGNORAR"}


def verificar_uninstall_remoto(
    dominio: str,
    app_client_id: str,
) -> dict:
    """Distingue uninstall actual de replay usando el token de la generación.

    Los headers del webhook no están cubiertos por HMAC. Se captura la
    generación local, se prueba su credencial contra Admin GraphQL y el caller
    sólo podrá purgar exactamente esa generación.
    """
    _ensure_tabla()
    dominio = (dominio or "").strip().lower()
    app_client_id = (app_client_id or "").strip()
    if not dominio_valido(dominio) or not app_client_id:
        return {"estado": "REINTENTAR", "generation": ""}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT access_token, access_token_expires_at,
                       refresh_token_expires_at, token_reauth_required,
                       app_client_id, install_generation
                  FROM shopify_instalaciones
                 WHERE dominio = %s
                """,
                (dominio,),
            )
            fila = cur.fetchone()
    if not fila:
        return {"estado": "YA_DESINSTALADA", "generation": ""}
    if _client_id_instalacion_efectivo(fila.get("app_client_id")) != app_client_id:
        return {"estado": "IGNORAR", "generation": ""}
    generation = str(fila.get("install_generation") or "").strip()
    if not generation:
        return {"estado": "REINTENTAR", "generation": ""}

    def _probar(token: str) -> str:
        if not token:
            return "SIN_TOKEN"
        try:
            respuesta = requests.post(
                f"https://{dominio}/admin/api/{API_VERSION}/graphql.json",
                headers={
                    "X-Shopify-Access-Token": token,
                    "Content-Type": "application/json",
                },
                json={"query": "query TauroUninstallProbe { shop { id } }"},
                # Shopify corta la entrega completa a los cinco segundos. La
                # verificacion remota debe dejar margen para DB y respuesta.
                timeout=1.5,
            )
        except Exception:
            return "REINTENTAR"
        if respuesta.status_code == 200:
            try:
                payload = respuesta.json()
            except Exception:
                return "REINTENTAR"
            if not payload.get("errors") and (payload.get("data") or {}).get("shop"):
                return "VIGENTE"
            return "REINTENTAR"
        if respuesta.status_code in {401, 403}:
            return "REVOCADO"
        return "REINTENTAR"

    try:
        token = _descifrar_token(str(fila.get("access_token") or ""))
    except Exception:
        return {"estado": "REINTENTAR", "generation": generation}
    prueba = _probar(token)
    if prueba == "VIGENTE":
        return {"estado": "VIGENTE", "generation": generation}
    if prueba == "REINTENTAR":
        return {"estado": "REINTENTAR", "generation": generation}

    ahora = datetime.now(timezone.utc)
    access_expira = _fecha_utc(fila.get("access_token_expires_at"))
    # Un token permanente o uno que localmente todavía no venció y ya recibe
    # 401/403 es evidencia fuerte de revocación por uninstall.
    if access_expira is None or access_expira > ahora:
        return {"estado": "REVOCADO", "generation": generation}

    # Si el access expiró naturalmente, primero intentamos el refresh serial.
    renovado = _token_admin_vigente(
        dominio,
        permitir_pendiente_webhooks=True,
        request_timeout=1.5,
    )
    if renovado:
        estado = _probar(renovado)
        return {
            "estado": "VIGENTE" if estado == "VIGENTE" else "REINTENTAR",
            "generation": generation,
        }
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT install_generation, token_reauth_required
                  FROM shopify_instalaciones
                 WHERE dominio = %s
                """,
                (dominio,),
            )
            actual = cur.fetchone()
    if not actual or str(actual.get("install_generation") or "") != generation:
        return {"estado": "IGNORAR", "generation": generation}
    refresh_expira = _fecha_utc(fila.get("refresh_token_expires_at"))
    if actual.get("token_reauth_required") and refresh_expira and refresh_expira > ahora:
        return {"estado": "REVOCADO", "generation": generation}
    return {"estado": "REINTENTAR", "generation": generation}


def desinstalar(
    dominio: str,
    app_client_id: str = "",
    shop_id: str = "",
    install_generation: str = "",
) -> bool:
    """Purga y desactiva exactamente la generación que recibió uninstall.

    El tombstone permite reconocer ``shop/redact`` 48 h después sin tocar una
    reinstalación posterior de la misma app. Todo ocurre en una transacción:
    nunca queda token activo con mapping purgado ni mapping activo sin token.
    """
    _ensure_tabla()
    from servicios.integraciones_tienda import (
        _bloquear_dominio_shopify,
        _borrar_datos_tienda_con_cursor,
        _ensure_tablas,
    )

    _ensure_tablas()
    dominio = (dominio or "").strip().lower()
    app_client_id = (app_client_id or "").strip()
    shop_id = str(shop_id or "").strip()
    install_generation = str(install_generation or "").strip()
    if not dominio_valido(dominio) or not app_client_id or not install_generation:
        return False

    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT id, app_client_id, install_generation, cliente_id,
                       instalada_en
                  FROM shopify_instalaciones
                 WHERE dominio = %s AND install_generation = %s
                 FOR UPDATE
                """,
                (dominio, install_generation),
            )
            fila = cur.fetchone()
            if not fila:
                return False
            if str(fila.get("install_generation") or "") != install_generation:
                return False
            guardado = _client_id_instalacion_efectivo(
                (fila or {}).get("app_client_id")
            )
            if guardado and guardado != app_client_id:
                return False

            total = _borrar_datos_tienda_con_cursor(cur, dominio)
            cur.execute(
                """
                INSERT INTO shopify_desinstalaciones
                    (dominio, shop_id, app_client_id, install_generation,
                     purge_completado_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (dominio, app_client_id, install_generation)
                DO UPDATE SET
                    shop_id = CASE
                        WHEN EXCLUDED.shop_id <> '' THEN EXCLUDED.shop_id
                        ELSE shopify_desinstalaciones.shop_id
                    END,
                    purge_completado_at = NOW()
                """,
                (
                    dominio,
                    shop_id,
                    app_client_id,
                    str(fila.get("install_generation") or ""),
                ),
            )
            cur.execute(
                """
                DELETE FROM shopify_instalaciones
                 WHERE dominio = %s AND install_generation = %s
                """,
                (dominio, str(fila.get("install_generation") or "")),
            )
        conn.commit()
    print(f"[shopify] uninstall procesado · purge operacional {total}")
    return True


def confirmar_shop_redact(
    dominio: str,
    shop_id: str,
    app_client_id: str,
) -> bool:
    """ACK durable de una obligación ya purgada al desinstalar.

    La búsqueda usa el tombstone histórico y nunca la instalación activa; por
    eso una reinstalación de la misma app queda completamente fuera del UPDATE.
    """
    _ensure_tabla()
    from servicios.integraciones_tienda import _bloquear_dominio_shopify

    dominio = (dominio or "").strip().lower()
    shop_id = str(shop_id or "").strip()
    app_client_id = (app_client_id or "").strip()
    if not dominio_valido(dominio) or not app_client_id:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                UPDATE shopify_desinstalaciones
                   SET shop_redact_ack_at = COALESCE(shop_redact_ack_at, NOW())
                 WHERE id = (
                    SELECT id
                      FROM shopify_desinstalaciones
                     WHERE dominio = %s
                       AND app_client_id = %s
                       AND (%s = '' OR shop_id = %s)
                     ORDER BY desinstalada_at DESC
                     LIMIT 1
                 )
                RETURNING id
                """,
                (dominio, app_client_id, shop_id, shop_id),
            )
            encontrada = cur.fetchone() is not None
            if encontrada:
                cur.execute(
                    """
                    UPDATE shopify_shop_redact_pendientes
                       SET estado = 'RESUELTO_POR_TOMBSTONE',
                           ultimo_intento_at = NOW()
                     WHERE dominio = %s AND shop_id = %s
                       AND app_client_id = %s
                    """,
                    (dominio, shop_id, app_client_id),
                )
        conn.commit()
    return encontrada


def registrar_shop_redact_pendiente(
    dominio: str,
    shop_id: str,
    app_client_id: str,
) -> bool:
    """Persiste una obligación cuyo evento no puede ligarse a un tombstone.

    El lock y la relectura evitan confirmar una instalación que se desinstaló
    entre el primer chequeo del endpoint y este INSERT. Nunca se guarda el
    payload ni datos del comprador; sólo la identidad mínima de la obligación
    y la generación activa que exige revisión.
    """
    _ensure_tabla()
    from servicios.integraciones_tienda import _bloquear_dominio_shopify

    dominio = (dominio or "").strip().lower()
    shop_id = str(shop_id or "").strip()
    app_client_id = (app_client_id or "").strip()
    if not dominio_valido(dominio) or not shop_id or not app_client_id:
        return False

    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT install_generation, app_client_id
                  FROM shopify_instalaciones
                 WHERE dominio = %s
                 FOR UPDATE
                """,
                (dominio,),
            )
            fila = cur.fetchone()
            if not fila:
                return False
            app_activa = _client_id_instalacion_efectivo(
                (fila or {}).get("app_client_id")
            )
            generation = str(
                (fila or {}).get("install_generation") or ""
            ).strip()
            # La obligación puede provenir de la app histórica mientras una
            # app pública ya está activa. Esa diferencia es justamente lo que
            # debe revisar operaciones; nunca se descarta con un 200 vacío.
            if not app_activa or not generation:
                return False
            cur.execute(
                """
                INSERT INTO shopify_shop_redact_pendientes
                    (dominio, shop_id, app_client_id,
                     install_generation_activa, estado)
                VALUES (%s, %s, %s, %s, 'VERIFICAR_GENERACION')
                ON CONFLICT (dominio, shop_id, app_client_id) DO UPDATE SET
                    install_generation_activa = EXCLUDED.install_generation_activa,
                    estado = 'VERIFICAR_GENERACION',
                    ultimo_intento_at = NOW()
                """,
                (dominio, shop_id, app_client_id, generation),
            )
        conn.commit()
    return True


# ── Llamadas a la API de la tienda ──────────────────────────

def _fecha_utc(valor: object) -> Optional[datetime]:
    if isinstance(valor, datetime):
        fecha = valor
    elif isinstance(valor, str):
        try:
            fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(timezone.utc)


def _invalidar_tokens_con_cursor(cur, instalacion_id: object,
                                 generation: str) -> None:
    """Falla cerrada sin tocar owner, app ni generación de instalación."""
    cur.execute(
        """
        UPDATE shopify_instalaciones
           SET access_token = '',
               refresh_token = NULL,
               access_token_expires_at = NOW(),
               refresh_token_expires_at = NOW(),
               token_reauth_required = TRUE,
               token_refresh_failed_at = NOW()
         WHERE id = %s AND install_generation = %s
        """,
        (instalacion_id, generation),
    )


def _token_admin_vigente(
    dominio: str,
    token_fallback: str = "",
    *,
    permitir_pendiente_webhooks: bool = False,
    request_timeout: float = 20,
) -> Optional[str]:
    """Devuelve un access token vigente y rota el par de forma serializada.

    El advisory lock de dominio es el mismo que usan OAuth y uninstall. La
    llamada de refresh ocurre dentro de esa transacción: un segundo worker
    espera, relee el par ya rotado y nunca reutiliza el refresh token anterior.
    """
    _ensure_tabla()
    from servicios.integraciones_tienda import _bloquear_dominio_shopify

    dominio = (dominio or "").strip().lower()
    if not dominio_valido(dominio):
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT id, access_token, refresh_token,
                       access_token_expires_at, refresh_token_expires_at,
                       token_reauth_required, app_client_id,
                       install_generation, webhooks_ready
                  FROM shopify_instalaciones
                 WHERE dominio = %s
                 FOR UPDATE
                """,
                (dominio,),
            )
            fila = cur.fetchone()
            if not fila:
                # Un uninstall o una reinstalación puede ganar entre la lectura
                # del caller y este lock. Sin fila durable jamás se usa el token
                # que el caller leyó antes: esa generación ya no está activa.
                return None
            fila = dict(fila)
            if fila.get("token_reauth_required"):
                return None
            if not fila.get("webhooks_ready") and not permitir_pendiente_webhooks:
                # Sólo registrar_webhooks puede usar el token previo al readiness.
                # Ningún flujo normal de catálogo, tracking o fulfillment opera
                # hasta que el conjunto obligatorio quede confirmado.
                return None

            token_guardado = str(fila.get("access_token") or "")
            refresh_guardado = str(fila.get("refresh_token") or "")
            access_expira = _fecha_utc(fila.get("access_token_expires_at"))
            refresh_expira = _fecha_utc(fila.get("refresh_token_expires_at"))

            app_efectiva = _client_id_instalacion_efectivo(
                fila.get("app_client_id")
            )

            # Sólo una app histórica distinta de la pública puede conservar
            # los antiguos offline tokens permanentes. Una fila de la app
            # pública sin par rotativo es una instalación incompleta y debe
            # volver a OAuth, incluso si el access token aún responde.
            if not access_expira and not refresh_guardado and not refresh_expira:
                if app_efectiva == api_key_publica():
                    generation = str(fila.get("install_generation") or "")
                    _invalidar_tokens_con_cursor(cur, fila.get("id"), generation)
                    conn.commit()
                    print("[shopify] reautorización requerida: token público no rotativo")
                    return None
                try:
                    return _descifrar_token(token_guardado) or None
                except Exception as exc:
                    print(f"[shopify] token legacy no disponible: {type(exc).__name__}")
                    return None

            generation = str(fila.get("install_generation") or "")
            ahora = datetime.now(timezone.utc)
            if access_expira and access_expira > ahora + _TOKEN_REFRESH_MARGIN:
                try:
                    return _descifrar_token(token_guardado) or None
                except Exception as exc:
                    print(f"[shopify] access token no disponible: {type(exc).__name__}")
                    return None

            if not generation or not refresh_guardado or not refresh_expira \
                    or refresh_expira <= ahora:
                _invalidar_tokens_con_cursor(cur, fila.get("id"), generation)
                conn.commit()
                print("[shopify] reautorización requerida: refresh no vigente")
                return None

            client_id = app_efectiva
            client_id, client_secret = _credenciales_para_client_id(client_id)
            if not client_id or not client_secret:
                _invalidar_tokens_con_cursor(cur, fila.get("id"), generation)
                conn.commit()
                print("[shopify] reautorización requerida: app OAuth no disponible")
                return None
            try:
                refresh_plano = _descifrar_token(refresh_guardado)
                respuesta = requests.post(
                    f"https://{dominio}/admin/oauth/access_token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_plano,
                    },
                    timeout=request_timeout,
                )
            except Exception as exc:
                # Un fallo de red no invalida un refresh token durable; esta
                # llamada sí falla cerrada y el próximo intento puede reintentar.
                print(f"[shopify] refresh no disponible: {type(exc).__name__}")
                return None

            if respuesta.status_code != 200:
                if 400 <= respuesta.status_code < 500 and respuesta.status_code != 429:
                    _invalidar_tokens_con_cursor(cur, fila.get("id"), generation)
                    conn.commit()
                    print(
                        "[shopify] reautorización requerida: refresh rechazado "
                        f"HTTP {respuesta.status_code}"
                    )
                else:
                    print(f"[shopify] refresh respondió HTTP {respuesta.status_code}")
                return None
            try:
                payload = respuesta.json()
                nuevo_access = str(payload.get("access_token") or "")
                nuevo_refresh = str(payload.get("refresh_token") or "")
                access_segundos = int(payload.get("expires_in"))
                refresh_segundos = int(payload.get("refresh_token_expires_in"))
                if (
                    not nuevo_access or not nuevo_refresh
                    or access_segundos <= 0 or refresh_segundos <= 0
                ):
                    raise ValueError("par OAuth incompleto")
            except (AttributeError, TypeError, ValueError):
                # Shopify pudo haber rotado el refresh anterior; una respuesta
                # incompleta no se puede recuperar de forma segura.
                _invalidar_tokens_con_cursor(cur, fila.get("id"), generation)
                conn.commit()
                print("[shopify] reautorización requerida: refresh incompleto")
                return None

            renovado_en = datetime.now(timezone.utc)
            cur.execute(
                """
                UPDATE shopify_instalaciones
                   SET access_token = %s,
                       refresh_token = %s,
                       access_token_expires_at = %s,
                       refresh_token_expires_at = %s,
                       token_reauth_required = FALSE,
                       token_refresh_failed_at = NULL
                 WHERE id = %s AND install_generation = %s
                """,
                (
                    _cifrar_token(nuevo_access),
                    _cifrar_token(nuevo_refresh),
                    renovado_en + timedelta(seconds=access_segundos),
                    renovado_en + timedelta(seconds=refresh_segundos),
                    fila.get("id"),
                    generation,
                ),
            )
        conn.commit()
    return nuevo_access


def _graphql(
    dominio: str,
    token: str,
    query: str,
    variables: dict | None = None,
    *,
    permitir_pendiente_webhooks: bool = False,
) -> Optional[dict]:
    """
    Cliente mínimo del Admin GraphQL API.

    Shopify considera legacy al REST Admin API y exige GraphQL para apps
    públicas nuevas. El helper nunca devuelve cuerpos de error ni tokens al
    log: esas respuestas pueden contener datos de la tienda.
    """
    token_vigente = _token_admin_vigente(
        dominio,
        token,
        permitir_pendiente_webhooks=permitir_pendiente_webhooks,
    )
    if not token_vigente:
        print("[shopify] Admin API requiere reautorización o refresh")
        return None
    url = f"https://{dominio}/admin/api/{API_VERSION}/graphql.json"
    for intento in range(5):
        try:
            r = requests.post(
                url,
                headers={
                    "X-Shopify-Access-Token": token_vigente,
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
                timeout=25,
            )
        except Exception as e:
            print(f"[shopify] GraphQL no disponible: {type(e).__name__}")
            return None
        if r.status_code == 429 and intento < 4:
            try:
                espera = float((getattr(r, "headers", {}) or {}).get("Retry-After") or 1)
            except (TypeError, ValueError):
                espera = 1
            time.sleep(max(1, min(espera, 20)))
            continue
        if r.status_code != 200:
            print(f"[shopify] GraphQL respondió HTTP {r.status_code}")
            return None
        try:
            payload = r.json()
        except Exception:
            print("[shopify] GraphQL devolvió una respuesta no JSON")
            return None
        errores = payload.get("errors") or []
        if not errores:
            return payload.get("data") or {}

        codigos = {
            str((error.get("extensions") or {}).get("code") or "UNKNOWN")
            for error in errores if isinstance(error, dict)
        }
        if "THROTTLED" in codigos and intento < 4:
            costo = (payload.get("extensions") or {}).get("cost") or {}
            throttle = costo.get("throttleStatus") or {}
            try:
                solicitado = float(costo.get("requestedQueryCost") or 0)
                disponible = float(throttle.get("currentlyAvailable") or 0)
                restauracion = float(throttle.get("restoreRate") or 0)
                espera = ((solicitado - disponible) / restauracion
                          if restauracion > 0 and solicitado > disponible else 1)
            except (TypeError, ValueError, ZeroDivisionError):
                espera = 1
            time.sleep(max(1, min(espera + 0.25, 20)))
            continue
        print(f"[shopify] GraphQL error codes={sorted(codigos)}")
        return None
    return None


def registrar_webhooks(dominio: str, token: str) -> list[str]:
    """
    Al instalar, damos de alta los webhooks solos. El comerciante no
    configura nada a mano (esa es la diferencia con el modo manual).
    """
    base = _base_url()
    topics = [(topic, topic_gql, f"{base}{path}")
              for topic, topic_gql, path in WEBHOOK_TOPICS]
    mutation = """
        mutation TauroWebhookCreate(
          $topic: WebhookSubscriptionTopic!,
          $subscription: WebhookSubscriptionInput!
        ) {
          webhookSubscriptionCreate(
            topic: $topic,
            webhookSubscription: $subscription
          ) {
            webhookSubscription { id topic uri }
            userErrors { field message }
          }
        }
    """
    consulta = """
        query TauroWebhookSubscriptions {
          webhookSubscriptions(first: 100) {
            nodes { id topic uri }
          }
        }
    """

    def _actuales() -> Optional[set[tuple[str, str]]]:
        data = _graphql(
            dominio,
            token,
            consulta,
            permitir_pendiente_webhooks=True,
        )
        if data is None:
            return None
        nodes = ((data.get("webhookSubscriptions") or {}).get("nodes") or [])
        return {
            (str(node.get("topic") or ""), str(node.get("uri") or ""))
            for node in nodes
        }

    existentes = _actuales()
    if existentes is None:
        raise ShopifyWebhookVerificationError("No se pudieron leer los webhooks actuales.")

    for topic, topic_gql, address in topics:
        if (topic_gql, address) in existentes:
            continue
        data = _graphql(
            dominio,
            token,
            mutation,
            {
                "topic": topic_gql,
                "subscription": {"uri": address, "format": "JSON"},
            },
            permitir_pendiente_webhooks=True,
        )
        resultado = (data or {}).get("webhookSubscriptionCreate") or {}
        errores = resultado.get("userErrors") or []
        if not resultado.get("webhookSubscription") or errores:
            print("[shopify] suscripción webhook no confirmada")

    verificados = _actuales()
    if verificados is None:
        raise ShopifyWebhookVerificationError("No se pudieron verificar los webhooks creados.")
    return [
        topic for topic, topic_gql, address in topics
        if (topic_gql, address) in verificados
    ]


def webhooks_requeridos() -> set[str]:
    return {topic for topic, _topic_gql, _path in WEBHOOK_TOPICS}


def confirmar_webhooks_verificados(
    dominio: str,
    install_generation: str,
    topics: list[str],
) -> bool:
    """Habilita exactamente la generación cuyo set completo fue verificado.

    OAuth guarda primero token, owner y generación como pendientes. Esta segunda
    transacción comparte el lock de dominio con reinstall/uninstall: un callback
    atrasado nunca puede habilitar una generación posterior. Si hay owner, su
    binding se activa en la misma confirmación; hasta entonces permanece cerrado.
    """
    dominio = (dominio or "").strip().lower()
    generation = str(install_generation or "").strip()
    recibidos = {str(topic or "").strip().lower() for topic in (topics or [])}
    if (
        not dominio_valido(dominio)
        or not generation
        or recibidos != webhooks_requeridos()
    ):
        return False

    _ensure_tabla()
    from servicios.integraciones_tienda import (
        OAUTH_SECRET_MARKER,
        _bloquear_dominio_shopify,
        _ensure_tablas,
    )

    _ensure_tablas()
    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT id, cliente_id
                  FROM shopify_instalaciones
                 WHERE dominio = %s AND install_generation = %s
                 FOR UPDATE
                """,
                (dominio, generation),
            )
            fila = cur.fetchone()
            if not fila:
                return False
            owner = str((fila or {}).get("cliente_id") or "").strip().upper()

            if owner:
                cur.execute(
                    """
                    UPDATE tiendas_conectadas
                       SET activa = TRUE
                     WHERE LOWER(dominio) = %s
                       AND UPPER(cliente_id) = %s
                       AND plataforma = 'shopify'
                       AND secreto = %s
                    RETURNING id
                    """,
                    (dominio, owner, OAUTH_SECRET_MARKER),
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        "No se pudo habilitar el binding Shopify verificado."
                    )

            cur.execute(
                """
                UPDATE shopify_instalaciones
                   SET webhooks_ready = TRUE,
                       webhooks_verified_at = COALESCE(webhooks_verified_at, NOW())
                 WHERE dominio = %s AND install_generation = %s
                RETURNING id
                """,
                (dominio, generation),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    "La generación Shopify cambió durante la confirmación."
                )
        conn.commit()
    return True


def marcar_enviado_resultado(
    dominio: str,
    pedido_externo_id: str,
    tracking: str,
    courier: str = "FedEx",
    *,
    solo_reconciliar: bool = False,
) -> str:
    """
    Cierra el círculo: cuando TAURO emite la guía, el pedido queda
    "Enviado" en Shopify con su número de seguimiento, y Shopify le
    manda solo el mail al comprador.
    """
    inst = instalacion(dominio)
    if not inst:
        return "MANUAL_REVIEW"
    token = inst["access_token"]

    pedido_gid = str(pedido_externo_id or "").strip()
    if pedido_gid.startswith("gid://shopify/Order/"):
        if not re.fullmatch(r"gid://shopify/Order/\d+", pedido_gid):
            return "MANUAL_REVIEW"
    elif re.fullmatch(r"\d+", pedido_gid):
        pedido_gid = f"gid://shopify/Order/{pedido_gid}"
    else:
        return "MANUAL_REVIEW"

    # 1) Qué se puede despachar de ese pedido
    data = _graphql(dominio, token, """
        query TauroFulfillmentOrders($orderId: ID!) {
          order(id: $orderId) {
            fulfillmentOrders(first: 50) { nodes { id status } }
            fulfillments(first: 50) {
              id
              status
              trackingInfo(first: 10) { number }
            }
          }
        }
    """, {"orderId": pedido_gid})
    if data is None:
        print("[shopify] no pude leer fulfillment_orders")
        return "RECONCILIAR" if solo_reconciliar else "REINTENTAR"
    order = data.get("order") or {}
    tracking_limpio = str(tracking or "").strip()
    if not tracking_limpio:
        return "MANUAL_REVIEW"
    for fulfillment in order.get("fulfillments") or []:
        numeros = {
            str(info.get("number") or "").strip()
            for info in (fulfillment.get("trackingInfo") or [])
        }
        estado_fulfillment = str(fulfillment.get("status") or "").upper()
        if (tracking_limpio in numeros
                and estado_fulfillment not in ("CANCELLED", "FAILURE", "ERROR")):
            print("[shopify] pedido ya tenía tracking")
            return "COMPLETADO"

    fos = [fo for fo in ((order.get("fulfillmentOrders") or {}).get("nodes") or [])
           if str(fo.get("status") or "").upper() in ("OPEN", "IN_PROGRESS")]
    # Alcance explícito del piloto: una sola fulfillment order elegible. No se
    # adivina una ubicación ni se despachan juntas órdenes partidas.
    if len(fos) != 1:
        print(f"[shopify] fulfillment requiere revisión: elegibles={len(fos)}")
        return "MANUAL_REVIEW"
    # Después de un timeout post-write, todos los ciclos siguientes son sólo
    # de conciliación. Una lectura negativa puede ser consistencia eventual y
    # nunca autoriza a repetir fulfillmentCreate sin clave idempotente.
    if solo_reconciliar:
        return "RECONCILIAR"

    courier_crudo = str(courier or "").strip()
    courier_mayus = courier_crudo.upper()
    tracking_url = None
    if "DHL" in courier_mayus:
        courier_shopify = "DHL Express"
        tracking_url = (
            "https://www.dhl.com/global-en/home/tracking.html?tracking-id="
            + urllib.parse.quote(tracking_limpio, safe="")
        )
    elif "FEDEX" in courier_mayus:
        courier_shopify = "FedEx"
        tracking_url = (
            "https://www.fedex.com/fedextrack/?trknbr="
            + urllib.parse.quote(tracking_limpio, safe="")
        )
    elif "UPS" in courier_mayus:
        courier_shopify = "UPS"
        tracking_url = (
            "https://www.ups.com/track?loc=es_AR&tracknum="
            + urllib.parse.quote(tracking_limpio, safe="")
        )
    else:
        courier_shopify = courier_crudo or "Otro"

    resultado = _graphql(dominio, token, """
        mutation TauroFulfillmentCreate($fulfillment: FulfillmentInput!) {
          fulfillmentCreate(fulfillment: $fulfillment) {
            fulfillment { id status }
            userErrors { field message }
          }
        }
    """, {
        "fulfillment": {
            "lineItemsByFulfillmentOrder": [
                {"fulfillmentOrderId": fos[0]["id"]}
            ],
            "trackingInfo": {
                "number": tracking_limpio,
                "company": courier_shopify,
                **({"url": tracking_url} if tracking_url else {}),
            },
            "notifyCustomer": True,
        }
    })
    if resultado is None:
        # La conexión pudo cortarse después de que Shopify aplicara la
        # mutación. Un retry ciego duplicaría el fulfillment.
        return "RECONCILIAR"
    creado = (resultado or {}).get("fulfillmentCreate") or {}
    if creado.get("fulfillment") and not (creado.get("userErrors") or []):
        print("[shopify] pedido marcado enviado")
        return "COMPLETADO"
    print("[shopify] no pude marcar pedido enviado por GraphQL")
    return "MANUAL_REVIEW"


def marcar_enviado(dominio: str, pedido_externo_id: str, tracking: str,
                   courier: str = "FedEx") -> bool:
    """Compatibilidad booleana; el worker durable usa el resultado detallado."""
    return marcar_enviado_resultado(
        dominio, pedido_externo_id, tracking, courier,
    ) == "COMPLETADO"


# ── Tarifas para el checkout ────────────────────────────────

def cotizar_para_checkout(payload: dict) -> dict:
    """
    Shopify nos manda el carrito y el destino; devolvemos las opciones
    de envío que verá el comprador. Formato fijo de Shopify: el precio
    va en centavos y como string.
    """
    from servicios.carriers import cotizar_carriers
    from servicios.politica_envio import (
        obtener_config, aplicar_politica, calcular_tax_estimado,
    )

    rate = payload.get("rate") or {}
    destino = rate.get("destination") or {}
    items = rate.get("items") or []
    dominio = (payload.get("_dominio") or "").strip().lower()

    pais = (destino.get("country") or "").upper()
    if not pais or pais == "AR":
        return {"rates": []}   # nacional no va por esta vía

    # El dólar sale de la tabla `config` (la que se edita en el admin), no de la
    # variable de entorno: si no, actualizar la cotización no movía el checkout.
    from servicios.cotizador import DolarNoConfigurado, dolar_ars
    try:
        dolar = dolar_ars()
    except DolarNoConfigurado as exc:
        # Fail-closed: sin tipo de cambio válido el checkout no muestra
        # tarifas TAURO en vez de mostrar una subvaluada.
        print(f"[shopify-rates] sin tipo de cambio válido: {exc}")
        return {"rates": []}
    markup = float(os.getenv("WEB_MARKUP_PCT", "20"))

    # Quién es el comerciante: hace falta ANTES de pesar, porque las
    # dimensiones de sus productos salen de SU catálogo.
    inst = instalacion(dominio) if dominio else None
    cliente_id = (inst or {}).get("cliente_id") or ""

    # PESO FACTURABLE, no peso real: los couriers aéreos cobran
    # max(real, L×A×H/5000). Shopify manda gramos pero no dimensiones, así
    # que se cruzan los SKU contra el catálogo del cliente. Cotizar por
    # peso real cobraba una fracción de la tarifa en productos voluminosos.
    from servicios.peso_facturable import peso_facturable
    medida = peso_facturable(cliente_id, items)
    peso_kg = medida["peso_kg"]
    largo, ancho, alto = medida["dimensiones"]

    # Shopify manda `price` en subunidades de la moneda de la TIENDA (ARS),
    # no en USD: hay que convertirlo o FedEx recibe un valor declarado
    # inflado ~1450x y devuelve una tarifa que no es la del envío.
    total_ars = sum((it.get("price") or 0) * (it.get("quantity") or 1)
                    for it in items) / 100.0
    valor_usd = max(round(total_ars / dolar, 2), 1.0) if dolar > 0 else 1.0

    origen = {
        "street": "Av. Corrientes 1234", "city": "BUENOS AIRES",
        "state": "B", "postal_code": "1043", "country": "AR",
    }
    dest = {
        "city": (destino.get("city") or "")[:35],
        "state": (destino.get("province") or "")[:35],
        "postal_code": (destino.get("postal_code") or "")[:12],
        "country": pais,
    }
    paquete = {
        "peso_kg": peso_kg, "largo": largo, "ancho": ancho, "alto": alto,
        "valor_declarado_usd": valor_usd, "descripcion_en": "Merchandise",
    }

    # CASCADA DE RESILIENCIA — Shopify corta a los ~10s y un checkout sin
    # opción de envío es una venta perdida. Por eso acá NUNCA se sale con
    # las manos vacías y NUNCA se espera a un courier si se puede evitar.
    from servicios.tarifas_cache import buscar_tarifas, tarifa_emergencia

    opciones = []
    try:
        opciones = buscar_tarifas(pais, peso_kg)          # 1. instantáneo (y fresco)
    except Exception as e:
        print(f"[shopify] cache de tarifas falló: {type(e).__name__}")

    if not opciones:
        try:
            opciones = cotizar_carriers(origen, dest, paquete, dolar, markup)  # 2. en vivo
        except Exception as e:
            print(f"[shopify] cotización en vivo falló: {type(e).__name__}")
            opciones = []

    if not [c for c in opciones if c.get("estado") == "cotizado"]:
        # 3. cache vencida: precio viejo de un courier real, mejor que una
        # fórmula inventada. Sólo se llega acá si la cotización en vivo falló.
        try:
            opciones = buscar_tarifas(pais, peso_kg, incluir_vencidas=True)
        except Exception:
            opciones = []

    if not [c for c in opciones if c.get("estado") == "cotizado"]:
        print("[shopify] sin tarifas; se usa tarifa de emergencia")
        opciones = tarifa_emergencia(pais, peso_kg, dolar)   # 4. nunca vacío

    # Qué ve el comprador lo decide el comerciante (política de flete):
    # tarifa real, +markup, precio fijo, o gratis. Y si quiere, con los
    # impuestos de destino estimados incluidos.
    config = obtener_config(dominio) if dominio else None
    if config is None:
        from servicios.politica_envio import DEFAULTS
        config = dict(DEFAULTS)

    tax_ars = round(calcular_tax_estimado(cliente_id, items, config, dolar))

    etiqueta = (config.get("etiqueta") or "").strip()

    # MONEDA: todo el cálculo interno es en ARS, pero la tienda puede vender
    # en otra. Devolver un monto en pesos etiquetado "USD" le mostraría al
    # comprador un precio 1450 veces mayor, así que:
    #   ARS  → tal cual
    #   USD  → se convierte con el dólar del sistema
    #   otra → se responde en ARS y la convierte Shopify con su propia tasa
    moneda_tienda = (rate.get("currency") or "ARS").upper()
    if moneda_tienda == "USD" and dolar > 0:
        moneda, conversion = "USD", 1.0 / dolar
    elif moneda_tienda == "ARS":
        moneda, conversion = "ARS", 1.0
    else:
        moneda, conversion = "ARS", 1.0
        if moneda_tienda:
            print("[shopify] moneda no nativa; Shopify hará la conversión")

    rates = []
    for c in opciones:
        if c.get("estado") != "cotizado":
            continue
        precio = aplicar_politica(float(c["precio_ars"]), config)
        if precio is None:
            continue

        detalle = f"Entrega estimada {c.get('dias_estimados', '3-5')} días hábiles · vía TAURO Solutions"
        # El impuesto NO se suma cuando el comerciante decidió el precio a
        # mano: si eligió "gratis" el envío tiene que salir $0 (si no, deja
        # de ser gratis), y si eligió un precio fijo tiene que ser ese.
        if tax_ars and config.get("politica") not in ("gratis", "fijo"):
            precio += tax_ars
            detalle += " · impuestos de destino estimados incluidos"

        rates.append({
            "service_name": etiqueta or f"{c['nombre']} — {c['servicio']}",
            "service_code": f"TAURO_{c['id'].upper()}",
            # Shopify espera SUBUNIDADES (centavos) y como string. Vale para
            # toda moneda, incluso las que no usan decimales. NO sacar el ×100.
            "total_price": str(int(round(precio * conversion * 100))),
            "currency": moneda,
            "description": detalle,
        })

        # Con precio fijo o gratis, todas las opciones costarían lo mismo:
        # mostramos una sola para no confundir al comprador.
        if config.get("politica") in ("fijo", "gratis"):
            break

    return {"rates": rates}


def nuevo_state() -> str:
    return secrets.token_urlsafe(24)
