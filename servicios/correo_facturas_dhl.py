"""Ingesta segura de facturas DHL desde Gmail, sin aprobar cargos.

El conector usa OAuth de sólo lectura. La descarga, autenticación, dedupe y
lectura son automáticas; una factura queda en la bandeja administrativa para
revisión. Ningún camino de este módulo confirma matches, decide un tipo de
cambio ni modifica la cuenta corriente del cliente.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import requests
from cryptography.fernet import Fernet, InvalidToken
from psycopg2.extras import Json

from core.database import get_conn
from servicios.bandeja_facturas_dhl import (
    importar_entrada_dhl_correo,
    leer_entrada_dhl,
    recibir_pdf_dhl_correo,
)
from servicios.conciliacion_couriers import ConciliacionCourierError, _registrar_auditoria
from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida
from servicios.seleccion_correo_dhl import (
    AdjuntoCandidatoDHL,
    CorreoDHLInvalido,
    seleccionar_adjunto_dhl,
    validar_autenticidad_dhl,
)


GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_LOCK_NAME = "tauro:correo:dhl:sync:v1"
_ACTOR = "sistema:correo-dhl"
_ESTADOS_TERMINALES = {"ORIGEN_RECHAZADO", "REVISION_MANUAL", "PARA_REVISION", "IMPORTADO"}


class ConfiguracionCorreoDHL(ValueError):
    pass


class CorreoDHLTemporal(RuntimeError):
    pass


class CorreoDHLReautorizar(RuntimeError):
    pass


def _texto_env(nombre: str) -> str:
    return str(os.getenv(nombre) or "").strip()


def _credenciales() -> tuple[str, str]:
    return _texto_env("DHL_GMAIL_CLIENT_ID"), _texto_env("DHL_GMAIL_CLIENT_SECRET")


def _cuenta_esperada() -> str:
    return _texto_env("DHL_GMAIL_ACCOUNT").casefold()


def _cuit_esperado() -> str:
    return re.sub(r"\D", "", _texto_env("DHL_GMAIL_CUIT"))


def callback_url() -> str:
    base = (_texto_env("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
    return base + "/admin/conciliacion-couriers/entrada-dhl/gmail/callback"


def preflight_correo_dhl() -> dict[str, Any]:
    client_id, client_secret = _credenciales()
    cuenta = _cuenta_esperada()
    cuit = _cuit_esperado()
    bloqueos = []
    if not client_id or not client_secret:
        bloqueos.append("oauth_google")
    if not cuenta or "@" not in cuenta:
        bloqueos.append("cuenta_gmail")
    if not re.fullmatch(r"[0-9]{11}", cuit):
        bloqueos.append("cuit_receptor")
    if len(_texto_env("DHL_GMAIL_TOKEN_ENCRYPTION_KEY")) < 32:
        bloqueos.append("cifrado_tokens")
    if not callback_url().startswith("https://"):
        bloqueos.append("callback_https")
    return {
        "configurada": not bloqueos,
        "bloqueos": bloqueos,
        "cuenta_esperada": cuenta,
        "callback_url": callback_url(),
    }


def url_autorizacion(state: str, *, code_challenge: str) -> str:
    control = preflight_correo_dhl()
    if not control["configurada"]:
        raise ConfiguracionCorreoDHL("Falta completar la configuración segura de Gmail DHL.")
    if not isinstance(state, str) or not 32 <= len(state) <= 160:
        raise ConfiguracionCorreoDHL("Estado OAuth inválido.")
    if not isinstance(code_challenge, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge):
        raise ConfiguracionCorreoDHL("Desafío PKCE inválido.")
    params = {
        "client_id": _credenciales()[0],
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": GMAIL_READONLY,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)


def _fernets() -> list[Fernet]:
    materiales = [
        _texto_env("DHL_GMAIL_TOKEN_ENCRYPTION_KEY"),
        *[v.strip() for v in _texto_env("DHL_GMAIL_TOKEN_ENCRYPTION_KEY_PREVIOUS").split(",")],
    ]
    resultado = []
    vistos = set()
    for material in materiales:
        if not material or material in vistos:
            continue
        vistos.add(material)
        clave = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())
        resultado.append(Fernet(clave))
    return resultado


def _cifrar(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ConfiguracionCorreoDHL("Google no entregó un token OAuth completo.")
    fernets = _fernets()
    if not fernets:
        raise ConfiguracionCorreoDHL("Falta la clave exclusiva para cifrar tokens Gmail.")
    return "enc:v1:" + fernets[0].encrypt(token.encode()).decode("ascii")


def _descifrar(token: str) -> str:
    if not isinstance(token, str) or not token.startswith("enc:v1:"):
        raise ConfiguracionCorreoDHL("El token Gmail almacenado no está cifrado.")
    for fernet in _fernets():
        try:
            return fernet.decrypt(token[7:].encode("ascii")).decode()
        except InvalidToken:
            continue
    raise ConfiguracionCorreoDHL("El token Gmail no se pudo abrir con las claves configuradas.")


def _respuesta_json(respuesta, *, operacion: str) -> dict[str, Any]:
    if respuesta.status_code == 401:
        raise CorreoDHLReautorizar(f"{operacion}: Google requiere reautorizar la cuenta.")
    if respuesta.status_code == 403:
        try:
            cuerpo = respuesta.json()
            error_google = cuerpo.get("error", {}) if isinstance(cuerpo, dict) else {}
            errores = error_google.get("errors", []) if isinstance(error_google, Mapping) else []
            detalles = error_google.get("details", []) if isinstance(error_google, Mapping) else []
            razones = {
                str(error.get("reason") or "")
                for error in [*errores, *detalles] if isinstance(error, Mapping)
            }
            estado_google = str(error_google.get("status") or "")
        except (ValueError, TypeError):
            razones = set()
            estado_google = ""
        if razones & {
            "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
            "quotaExceeded", "backendError",
        } or estado_google in {"RESOURCE_EXHAUSTED", "UNAVAILABLE"}:
            raise CorreoDHLTemporal(f"{operacion}: límite temporal de Google alcanzado.")
        if razones & {"authError", "insufficientPermissions"}:
            raise CorreoDHLReautorizar(f"{operacion}: Google requiere reautorizar la cuenta.")
        raise ConfiguracionCorreoDHL(f"{operacion}: Google rechazó la operación.")
    if respuesta.status_code == 429 or respuesta.status_code >= 500:
        raise CorreoDHLTemporal(f"{operacion}: servicio temporalmente no disponible.")
    if respuesta.status_code < 200 or respuesta.status_code >= 300:
        raise ConfiguracionCorreoDHL(f"{operacion}: Google rechazó la operación.")
    try:
        datos = respuesta.json()
    except (ValueError, TypeError) as exc:
        raise CorreoDHLTemporal(f"{operacion}: respuesta inválida de Google.") from exc
    if not isinstance(datos, dict):
        raise CorreoDHLTemporal(f"{operacion}: respuesta inválida de Google.")
    return datos


def _post_token(datos: dict[str, str], *, operacion: str) -> dict[str, Any]:
    try:
        respuesta = requests.post(
            GOOGLE_TOKEN_URL,
            data=datos,
            headers={"Accept": "application/json"},
            timeout=(5, 20),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise CorreoDHLTemporal(f"{operacion}: no se pudo contactar Google.") from exc
    return _respuesta_json(respuesta, operacion=operacion)


def _gmail_get(token: str, ruta: str, *, params=None) -> dict[str, Any]:
    if not ruta.startswith("/") or ".." in ruta:
        raise ValueError("Ruta Gmail inválida.")
    try:
        respuesta = requests.get(
            GMAIL_API + ruta,
            params=params,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            timeout=(5, 20),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise CorreoDHLTemporal("Gmail: no se pudo consultar el buzón.") from exc
    return _respuesta_json(respuesta, operacion="Gmail")


def conectar_desde_codigo(codigo: str, *, code_verifier: str, actor: str) -> dict[str, Any]:
    control = preflight_correo_dhl()
    if not control["configurada"]:
        raise ConfiguracionCorreoDHL("Falta completar la configuración segura de Gmail DHL.")
    if not isinstance(codigo, str) or not 8 <= len(codigo) <= 4096:
        raise ConfiguracionCorreoDHL("Código OAuth inválido o ausente.")
    if not isinstance(code_verifier, str) or not re.fullmatch(
        r"[A-Za-z0-9._~-]{43,128}", code_verifier,
    ):
        raise ConfiguracionCorreoDHL("Verificador PKCE inválido o ausente.")
    client_id, client_secret = _credenciales()
    tokens = _post_token({
        "code": codigo,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": callback_url(),
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }, operacion="OAuth Gmail")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    token_type = str(tokens.get("token_type") or "").casefold()
    scopes = str(tokens.get("scope") or "")
    try:
        duracion = int(tokens.get("expires_in"))
        expira = datetime.now(timezone.utc) + timedelta(seconds=duracion)
    except (TypeError, ValueError) as exc:
        raise ConfiguracionCorreoDHL("Google no entregó un vencimiento OAuth válido.") from exc
    if (not isinstance(access, str) or not access or duracion <= 0 or token_type != "bearer"
            or set(scopes.split()) != {GMAIL_READONLY} or not isinstance(refresh, str)
            or not refresh):
        raise ConfiguracionCorreoDHL("Google no entregó acceso offline de sólo lectura.")
    perfil = _gmail_get(access, "/users/me/profile")
    cuenta = str(perfil.get("emailAddress") or "").strip().casefold()
    if cuenta != _cuenta_esperada():
        raise ConfiguracionCorreoDHL("La cuenta autorizada no es el buzón DHL configurado.")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_LOCK_NAME + ":oauth",))
        cur.execute("""INSERT INTO integracion_correo_dhl
            (id, cuenta_email, access_token_cifrado, refresh_token_cifrado,
             token_expira_at, scopes, estado, conectado_por)
            VALUES (1,%s,%s,%s,%s,%s,'CONECTADA',%s)
            ON CONFLICT (id) DO UPDATE SET cuenta_email=EXCLUDED.cuenta_email,
              access_token_cifrado=EXCLUDED.access_token_cifrado,
              refresh_token_cifrado=EXCLUDED.refresh_token_cifrado,
              token_expira_at=EXCLUDED.token_expira_at, scopes=EXCLUDED.scopes,
              estado='CONECTADA', conectado_por=EXCLUDED.conectado_por,
              conectado_at=NOW(), ultimo_error_codigo=NULL, updated_at=NOW()""",
            (cuenta, _cifrar(str(access)), _cifrar(str(refresh)), expira, scopes, actor))
        _registrar_auditoria(
            cur, evento="DHL_GMAIL_CONECTADO", actor=actor,
            metadata={"cuenta_sha256": hashlib.sha256(cuenta.encode()).hexdigest(),
                      "scope": GMAIL_READONLY},
        )
    return {"cuenta": cuenta, "conectada": True}


def estado_integracion() -> dict[str, Any]:
    control = preflight_correo_dhl()
    resultado = {**control, "conectada": False, "estado": "SIN_CONFIGURAR", "conteos": {}}
    if not control["configurada"]:
        return resultado
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT cuenta_email, token_expira_at, estado, conectado_at,
                ultimo_sync_at, ultimo_error_codigo, ultimo_resultado
                FROM integracion_correo_dhl WHERE id=1""")
            fila = cur.fetchone()
            cur.execute("SELECT estado, count(*) AS n FROM correos_dhl_procesados GROUP BY estado")
            resultado["conteos"] = {r["estado"]: int(r["n"]) for r in cur.fetchall()}
    except Exception:
        resultado.update(estado="ERROR_BASE", error="No se pudo leer el estado de Gmail.")
        return resultado
    if not fila:
        resultado["estado"] = "SIN_CONECTAR"
        return resultado
    resultado.update({
        "conectada": fila["estado"] == "CONECTADA",
        "estado": fila["estado"],
        "cuenta": fila["cuenta_email"],
        "conectado_at": fila["conectado_at"],
        "ultimo_sync_at": fila["ultimo_sync_at"],
        "ultimo_error_codigo": fila["ultimo_error_codigo"],
        "ultimo_resultado": fila["ultimo_resultado"] or {},
    })
    return resultado


def _marcar_integracion(*, estado=None, error=None, resultado=None) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        campos = ["updated_at=NOW()"]
        valores = []
        if estado is not None:
            campos.append("estado=%s")
            valores.append(estado)
        if error is not None:
            campos.append("ultimo_error_codigo=%s")
            valores.append(error)
        if resultado is not None:
            campos.extend(["ultimo_resultado=%s", "ultimo_sync_at=NOW()", "ultimo_error_codigo=NULL"])
            valores.append(Json(resultado))
        valores.append(1)
        cur.execute(f"UPDATE integracion_correo_dhl SET {', '.join(campos)} WHERE id=%s", valores)


def _marcar_resultado_historico(resultado: Mapping[str, Any]) -> None:
    """Registra el backfill sin alterar la ventana incremental periódica."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE integracion_correo_dhl
               SET estado='CONECTADA', ultimo_resultado=%s,
                   ultimo_error_codigo=NULL, updated_at=NOW()
               WHERE id=1""",
            (Json(dict(resultado)),),
        )


def _token_acceso(*, forzar_refresh=False) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_LOCK_NAME + ":token",))
        cur.execute("SELECT * FROM integracion_correo_dhl WHERE id=1 FOR UPDATE")
        fila = cur.fetchone()
        if not fila or fila["estado"] != "CONECTADA":
            raise CorreoDHLReautorizar("La cuenta Gmail DHL no está conectada.")
        ahora = datetime.now(timezone.utc)
        if not forzar_refresh and fila["token_expira_at"] > ahora + timedelta(minutes=2):
            return _descifrar(fila["access_token_cifrado"])
        client_id, client_secret = _credenciales()
        try:
            refresh_actual = _descifrar(fila["refresh_token_cifrado"])
            tokens = _post_token({
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_actual,
                "grant_type": "refresh_token",
            }, operacion="Renovación Gmail")
        except (CorreoDHLReautorizar, ConfiguracionCorreoDHL) as exc:
            # La transacción que protege el token hará rollback al propagar el
            # error. El sincronizador persiste REAUTORIZAR en otra transacción.
            raise CorreoDHLReautorizar(
                "La autorización Gmail DHL venció o dejó de ser válida."
            ) from exc
        access = str(tokens.get("access_token") or "")
        try:
            expira = ahora + timedelta(seconds=int(tokens.get("expires_in")))
        except (TypeError, ValueError) as exc:
            raise CorreoDHLTemporal("Renovación Gmail incompleta.") from exc
        if not access:
            raise CorreoDHLTemporal("Renovación Gmail incompleta.")
        refresh = str(tokens.get("refresh_token") or "") or refresh_actual
        cur.execute("""UPDATE integracion_correo_dhl SET access_token_cifrado=%s,
            refresh_token_cifrado=%s, token_expira_at=%s, updated_at=NOW() WHERE id=1""",
            (_cifrar(access), _cifrar(refresh), expira))
        return access


class _ClienteGmail:
    def __init__(self):
        self.token = _token_acceso()
        self.renovado = False
        try:
            perfil = _gmail_get(self.token, "/users/me/profile")
        except CorreoDHLReautorizar:
            self.token = _token_acceso(forzar_refresh=True)
            self.renovado = True
            perfil = _gmail_get(self.token, "/users/me/profile")
        self.cuenta = str(perfil.get("emailAddress") or "").strip().casefold()
        if self.cuenta != _cuenta_esperada():
            raise ConfiguracionCorreoDHL("El token Gmail no pertenece al buzón DHL configurado.")

    def get(self, ruta: str, *, params=None) -> dict[str, Any]:
        try:
            return _gmail_get(self.token, ruta, params=params)
        except CorreoDHLReautorizar:
            if self.renovado:
                raise
            self.token = _token_acceso(forzar_refresh=True)
            self.renovado = True
            return _gmail_get(self.token, ruta, params=params)


def _parte_por_id(mensaje: Mapping[str, Any], part_id: str) -> Mapping[str, Any] | None:
    payload = mensaje.get("payload") if isinstance(mensaje, Mapping) else None
    pendientes = [payload]
    vistos = 0
    while pendientes:
        parte = pendientes.pop()
        vistos += 1
        if vistos > 200 or not isinstance(parte, Mapping):
            raise CorreoDHLInvalido("Estructura MIME inválida o demasiado compleja.")
        if parte.get("partId") == part_id:
            return parte
        hijos = parte.get("parts", [])
        if isinstance(hijos, list):
            pendientes.extend(hijos)
    return None


def _decodificar_base64url(valor: Any) -> bytes:
    if not isinstance(valor, str) or not valor or len(valor) > 12 * 1024 * 1024:
        raise CorreoDHLInvalido("Contenido del adjunto inválido.")
    try:
        relleno = "=" * (-len(valor) % 4)
        return base64.b64decode((valor + relleno).encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError) as exc:
        raise CorreoDHLInvalido("Contenido del adjunto inválido.") from exc


def descargar_adjunto(
    cliente: _ClienteGmail,
    mensaje: Mapping[str, Any],
    candidato: AdjuntoCandidatoDHL,
) -> bytes:
    parte = _parte_por_id(mensaje, candidato.parte_id)
    if not parte or not isinstance(parte.get("body"), Mapping):
        raise CorreoDHLInvalido("No se encontró la parte MIME seleccionada.")
    cuerpo = parte["body"]
    esperado = cuerpo.get("size")
    if type(esperado) is not int or not 0 < esperado <= 8 * 1024 * 1024:
        raise CorreoDHLInvalido("Tamaño del adjunto inválido.")
    if candidato.adjunto_id:
        mid = urllib.parse.quote(candidato.mensaje_id, safe="")
        aid = urllib.parse.quote(candidato.adjunto_id, safe="")
        datos = cliente.get(f"/users/me/messages/{mid}/attachments/{aid}").get("data")
    else:
        datos = cuerpo.get("data")
    pdf = _decodificar_base64url(datos)
    if len(pdf) != esperado or not pdf.startswith(b"%PDF"):
        raise CorreoDHLInvalido("El adjunto descargado no coincide con el PDF anunciado.")
    return pdf


def _correo_guardado(mensaje_id: str) -> dict[str, Any] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM correos_dhl_procesados WHERE gmail_mensaje_id=%s", (mensaje_id,))
        fila = cur.fetchone()
        return dict(fila) if fila else None


def _guardar_correo(
    mensaje: Mapping[str, Any], *, estado: str, entrada_id=None,
    candidato: AdjuntoCandidatoDHL | None = None, autenticacion=None,
    error_codigo=None, incrementar_intento=False,
) -> None:
    mensaje_id = str(mensaje.get("id") or "")
    thread_id = str(mensaje.get("threadId") or "")[:255] or None
    history_id = str(mensaje.get("historyId") or "")[:255] or None
    adjunto_id = None
    if candidato:
        adjunto_id = candidato.adjunto_id or "inline:" + candidato.parte_id
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO correos_dhl_procesados
            (gmail_mensaje_id, gmail_thread_id, gmail_history_id, gmail_adjunto_id,
             archivo_nombre, numero_documento, estado, autenticacion, entrada_id,
             error_codigo, intentos)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (gmail_mensaje_id) DO UPDATE SET
              gmail_thread_id=COALESCE(correos_dhl_procesados.gmail_thread_id,EXCLUDED.gmail_thread_id),
              gmail_history_id=COALESCE(correos_dhl_procesados.gmail_history_id,EXCLUDED.gmail_history_id),
              gmail_adjunto_id=COALESCE(correos_dhl_procesados.gmail_adjunto_id,EXCLUDED.gmail_adjunto_id),
              archivo_nombre=COALESCE(correos_dhl_procesados.archivo_nombre,EXCLUDED.archivo_nombre),
              numero_documento=COALESCE(correos_dhl_procesados.numero_documento,EXCLUDED.numero_documento),
              estado=EXCLUDED.estado,
              autenticacion=CASE WHEN correos_dhl_procesados.autenticacion='{}'::jsonb
                THEN EXCLUDED.autenticacion ELSE correos_dhl_procesados.autenticacion END,
              entrada_id=COALESCE(correos_dhl_procesados.entrada_id,EXCLUDED.entrada_id),
              error_codigo=EXCLUDED.error_codigo,
              intentos=correos_dhl_procesados.intentos + CASE WHEN %s THEN 1 ELSE 0 END,
              updated_at=NOW()""",
            (mensaje_id, thread_id, history_id, adjunto_id,
             candidato.archivo_nombre if candidato else None,
             candidato.numero_documento if candidato else None,
             estado, Json(autenticacion or {}), entrada_id, error_codigo,
             1 if incrementar_intento else 0, bool(incrementar_intento)))
        _registrar_auditoria(
            cur, evento="DHL_CORREO_" + estado, actor=_ACTOR,
            metadata={"mensaje_sha256": hashlib.sha256(mensaje_id.encode()).hexdigest(),
                      "entrada_dhl_id": entrada_id, "error_codigo": error_codigo},
        )


def _auto_import_habilitado() -> bool:
    return _texto_env("DHL_GMAIL_AUTO_IMPORT").casefold() in {"1", "true", "si", "sí", "on"}


def _max_intentos() -> int:
    try:
        valor = int(_texto_env("DHL_GMAIL_MAX_ATTEMPTS") or "5")
    except ValueError:
        valor = 5
    return max(1, min(valor, 20))


def _espera_reintento(intentos: int) -> timedelta:
    """Backoff de 30 min, 1 h, 2 h, 4 h... con tope de un día."""
    exponente = max(0, min(int(intentos or 0) - 1, 10))
    return timedelta(minutes=min(30 * (2 ** exponente), 24 * 60))


def _marcar_reintentos_agotados(entrada_id: int | None) -> None:
    if not entrada_id:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, archivo_sha256, estado FROM entradas_pdf_dhl WHERE id=%s FOR UPDATE",
            (int(entrada_id),),
        )
        entrada = cur.fetchone()
        if not entrada or entrada["estado"] in {"IMPORTADA", "PARA_REVISION", "REVISION_MANUAL"}:
            return
        cur.execute(
            """UPDATE entradas_pdf_dhl
               SET estado='REVISION_MANUAL', error_lectura=%s, updated_at=NOW()
               WHERE id=%s""",
            ("Se agotaron los reintentos automáticos. Requiere revisión manual.", entrada_id),
        )
        _registrar_auditoria(
            cur, evento="DHL_REINTENTOS_AGOTADOS", actor=_ACTOR,
            metadata={"entrada_dhl_id": int(entrada_id),
                      "archivo_sha256": entrada["archivo_sha256"]},
        )


def _actualizar_estado_por_entrada(
    mensaje: Mapping[str, Any], candidato, entrada_id: int, autenticacion,
) -> str:
    estado = leer_entrada_dhl(
        entrada_id, numero=candidato.numero_documento, cuit=_cuit_esperado(), actor=_ACTOR,
    )
    estado_correo = "IMPORTADO" if estado == "IMPORTADA" else estado
    _guardar_correo(
        mensaje, estado=estado_correo, entrada_id=entrada_id, candidato=candidato,
        autenticacion=autenticacion,
        error_codigo="LECTOR_NO_DISPONIBLE" if estado == "REINTENTAR" else None,
    )
    return estado_correo


def _ventana_busqueda() -> tuple[str, str | None]:
    try:
        dias = int(_texto_env("DHL_GMAIL_INITIAL_LOOKBACK_DAYS") or "7")
    except ValueError:
        dias = 7
    dias = max(1, min(dias, 90))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT ultimo_sync_at, conectado_at, sync_page_token,
                    sync_query_after FROM integracion_correo_dhl WHERE id=1""")
        fila = cur.fetchone()
    if fila and fila["sync_page_token"] and fila["sync_query_after"]:
        return fila["sync_query_after"].strftime("%Y/%m/%d"), fila["sync_page_token"]
    ultimo_sync = fila["ultimo_sync_at"] if fila else None
    conectado = fila["conectado_at"] if fila else None
    desde = ultimo_sync or conectado or datetime.now(timezone.utc)
    solapamiento = 2 if ultimo_sync else dias
    return (desde - timedelta(days=solapamiento)).date().strftime("%Y/%m/%d"), None


def _listar_mensajes(cliente: _ClienteGmail) -> tuple[list[dict[str, str]], str | None, str]:
    try:
        limite = int(_texto_env("DHL_GMAIL_SYNC_LIMIT") or "500")
    except ValueError:
        limite = 500
    limite = max(1, min(limite, 5000))
    fecha_inicio, pagina = _ventana_busqueda()
    consulta = (
        'from:AR.E-Billing@dhl.com subject:"DHL Invoice services" '
        f'has:attachment filename:pdf after:{fecha_inicio}'
    )
    encontrados = []
    while len(encontrados) < limite:
        params = {"q": consulta, "maxResults": min(500, limite - len(encontrados)),
                  "includeSpamTrash": "false"}
        if pagina:
            params["pageToken"] = pagina
        datos = cliente.get("/users/me/messages", params=params)
        mensajes = datos.get("messages", [])
        if not isinstance(mensajes, list):
            raise CorreoDHLTemporal("Gmail devolvió un listado inválido.")
        for mensaje in mensajes:
            mensaje_id = mensaje.get("id") if isinstance(mensaje, Mapping) else None
            if isinstance(mensaje_id, str) and 0 < len(mensaje_id) <= 255:
                encontrados.append({"id": mensaje_id,
                                    "threadId": str(mensaje.get("threadId") or "")[:255]})
        pagina = datos.get("nextPageToken")
        if not isinstance(pagina, str) or not pagina:
            break
    return encontrados[:limite], pagina if isinstance(pagina, str) and pagina else None, fecha_inicio


def _listar_mensajes_historicos(
    cliente: _ClienteGmail, anio: int,
) -> tuple[list[dict[str, str]], bool]:
    """Lista un año calendario sin mover el checkpoint del job periódico.

    El tope evita una descarga accidentalmente ilimitada. La identidad y la
    autenticidad de cada mensaje siguen validándose en ``_procesar_mensaje``.
    """
    actual = datetime.now(timezone.utc).year
    if type(anio) is not int or not 2020 <= anio <= actual:
        raise ConfiguracionCorreoDHL("Año histórico DHL inválido.")
    limite = 5000
    inicio = int(datetime(anio, 1, 1, tzinfo=timezone.utc).timestamp()) - 1
    fin = int(datetime(anio + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    consulta = (
        'from:AR.E-Billing@dhl.com subject:"DHL Invoice services" '
        f'has:attachment filename:pdf after:{inicio} before:{fin}'
    )
    encontrados = []
    pagina = None
    while len(encontrados) < limite:
        params = {
            "q": consulta,
            "maxResults": min(500, limite - len(encontrados)),
            "includeSpamTrash": "false",
        }
        if pagina:
            params["pageToken"] = pagina
        datos = cliente.get("/users/me/messages", params=params)
        mensajes = datos.get("messages", [])
        if not isinstance(mensajes, list):
            raise CorreoDHLTemporal("Gmail devolvió un listado histórico inválido.")
        for mensaje in mensajes:
            mensaje_id = mensaje.get("id") if isinstance(mensaje, Mapping) else None
            if isinstance(mensaje_id, str) and 0 < len(mensaje_id) <= 255:
                encontrados.append({
                    "id": mensaje_id,
                    "threadId": str(mensaje.get("threadId") or "")[:255],
                })
        pagina = datos.get("nextPageToken")
        if not isinstance(pagina, str) or not pagina:
            break
    return encontrados[:limite], bool(pagina)


def _guardar_cursor_sync(page_token: str | None, fecha_inicio: str | None) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE integracion_correo_dhl
               SET sync_page_token=%s, sync_query_after=%s, updated_at=NOW()
               WHERE id=1""",
            (page_token, fecha_inicio.replace("/", "-") if fecha_inicio else None),
        )


def _reintentos_pendientes() -> list[dict[str, str]]:
    """Los reintentos no dependen de que el mensaje siga dentro de la ventana Gmail."""
    try:
        limite = int(_texto_env("DHL_GMAIL_RETRY_LIMIT") or "100")
    except ValueError:
        limite = 100
    limite = max(1, min(limite, 500))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT gmail_mensaje_id, gmail_thread_id
               FROM correos_dhl_procesados
               WHERE estado='REINTENTAR'
               ORDER BY updated_at, gmail_mensaje_id
               LIMIT %s""",
            (limite,),
        )
        return [
            {"id": fila["gmail_mensaje_id"],
             "threadId": str(fila["gmail_thread_id"] or "")}
            for fila in cur.fetchall()
        ]


def _procesar_mensaje(
    cliente: _ClienteGmail, resumen: Mapping[str, str], *,
    reintentar_patron_no_admitido: bool = False,
    forzar_reintento_operativo: bool = False,
) -> str:
    mensaje_id = str(resumen.get("id") or "")
    existente = _correo_guardado(mensaje_id)
    reintento_patron = bool(
        reintentar_patron_no_admitido
        and existente
        and existente["estado"] == "REVISION_MANUAL"
        and existente.get("error_codigo") == "PATRON_NO_ADMITIDO"
    )
    if existente and existente["estado"] in _ESTADOS_TERMINALES and not reintento_patron:
        return "YA_PROCESADO"
    if existente and int(existente.get("intentos") or 0) >= _max_intentos():
        _marcar_reintentos_agotados(existente.get("entrada_id"))
        _guardar_correo(
            {"id": mensaje_id, "threadId": existente.get("gmail_thread_id"),
             "historyId": existente.get("gmail_history_id")},
            estado="REVISION_MANUAL", entrada_id=existente.get("entrada_id"),
            error_codigo="REINTENTOS_AGOTADOS",
        )
        return "REVISION_MANUAL"
    if (existente and existente["estado"] == "REINTENTAR"
            and not forzar_reintento_operativo
            and existente.get("updated_at")
            and datetime.now(timezone.utc) < existente["updated_at"] + _espera_reintento(
                int(existente.get("intentos") or 0)
            )):
        return "REINTENTO_DIFERIDO"
    mid = urllib.parse.quote(mensaje_id, safe="")
    mensaje = cliente.get(f"/users/me/messages/{mid}", params={"format": "full"})
    if mensaje.get("id") != mensaje_id:
        raise CorreoDHLTemporal("Gmail devolvió un mensaje inconsistente.")
    estado_inicio = existente["estado"] if existente and existente.get("entrada_id") else "DETECTADO"
    _guardar_correo(
        mensaje, estado=estado_inicio, entrada_id=existente.get("entrada_id") if existente else None,
        incrementar_intento=True,
    )
    try:
        autenticidad = validar_autenticidad_dhl(mensaje)
    except CorreoDHLInvalido:
        _guardar_correo(mensaje, estado="ORIGEN_RECHAZADO", error_codigo="ORIGEN_NO_AUTENTICADO")
        return "ORIGEN_RECHAZADO"
    try:
        candidato = seleccionar_adjunto_dhl(
            mensaje,
            cuenta_autenticada=cliente.cuenta,
            cuenta_esperada=_cuenta_esperada(),
            cuit_esperado=_cuit_esperado(),
        )
        if candidato is None:
            _guardar_correo(
                mensaje, estado="REVISION_MANUAL",
                autenticacion=autenticidad.__dict__, error_codigo="PATRON_NO_ADMITIDO",
            )
            return "REVISION_MANUAL"
        pdf = descargar_adjunto(cliente, mensaje, candidato)
        adjunto_id = candidato.adjunto_id or "inline:" + candidato.parte_id
        entrada = recibir_pdf_dhl_correo(
            pdf=pdf, nombre=candidato.archivo_nombre,
            numero=candidato.numero_documento, cuit=_cuit_esperado(), actor=_ACTOR,
            correo_mensaje_id=candidato.mensaje_id, correo_adjunto_id=adjunto_id,
        )
        _guardar_correo(
            mensaje, estado="RECIBIDO", entrada_id=entrada["id"], candidato=candidato,
            autenticacion=autenticidad.__dict__,
        )
        estado = _actualizar_estado_por_entrada(
            mensaje, candidato, entrada["id"], autenticidad.__dict__,
        )
        if estado == "PARA_REVISION" and _auto_import_habilitado():
            importar_entrada_dhl_correo(
                entrada["id"], cuenta_correo=cliente.cuenta, actor=_ACTOR,
            )
            _guardar_correo(
                mensaje, estado="IMPORTADO", entrada_id=entrada["id"],
                candidato=candidato, autenticacion=autenticidad.__dict__,
            )
            return "IMPORTADO"
        return estado
    except (CorreoDHLInvalido, ExtraccionDHLInvalida, ConciliacionCourierError):
        _guardar_correo(
            mensaje, estado="REVISION_MANUAL",
            autenticacion=autenticidad.__dict__, error_codigo="DOCUMENTO_REQUIERE_REVISION",
        )
        return "REVISION_MANUAL"


def sincronizar_facturas_dhl() -> dict[str, Any]:
    control = preflight_correo_dhl()
    if not control["configurada"]:
        return {"estado": "DESHABILITADA", "procesados": 0, "bloqueos": control["bloqueos"]}
    with get_conn() as lock_conn, lock_conn.cursor() as lock_cur:
        lock_cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS adquirido", (_LOCK_NAME,))
        if not lock_cur.fetchone()["adquirido"]:
            return {"estado": "EN_CURSO", "procesados": 0}
        try:
            estado = estado_integracion()
            if not estado.get("conectada"):
                return {"estado": "SIN_CONECTAR", "procesados": 0}
            cliente = _ClienteGmail()
            mensajes, siguiente_pagina, fecha_inicio = _listar_mensajes(cliente)
            # Un cursor puede avanzar más allá de un mensaje con error temporal.
            # Se lo trae también desde el checkpoint durable para que nunca quede
            # varado al salir de la ventana de búsqueda de Gmail.
            combinados = [*_reintentos_pendientes(), *mensajes]
            vistos = set()
            mensajes_unicos = []
            for resumen in combinados:
                if resumen["id"] in vistos:
                    continue
                vistos.add(resumen["id"])
                mensajes_unicos.append(resumen)
            mensajes = mensajes_unicos
            conteos: dict[str, int] = {}
            for resumen in mensajes:
                resultado = _procesar_mensaje(cliente, resumen)
                conteos[resultado] = conteos.get(resultado, 0) + 1
            estado_salida = "PAGINACION_PENDIENTE" if siguiente_pagina else "OK"
            salida = {"estado": estado_salida, "procesados": len(mensajes), "resultados": conteos}
            _guardar_cursor_sync(siguiente_pagina, fecha_inicio if siguiente_pagina else None)
            if siguiente_pagina:
                _marcar_integracion(estado="CONECTADA", error="PAGINACION_PENDIENTE")
            else:
                _marcar_integracion(estado="CONECTADA", resultado=salida)
            return salida
        except CorreoDHLReautorizar:
            _marcar_integracion(estado="REAUTORIZAR", error="REAUTORIZAR")
            return {"estado": "REAUTORIZAR", "procesados": 0}
        except CorreoDHLTemporal:
            _marcar_integracion(error="ERROR_TEMPORAL")
            return {"estado": "ERROR_TEMPORAL", "procesados": 0}
        except ConfiguracionCorreoDHL:
            _marcar_integracion(estado="ERROR", error="CONFIGURACION")
            return {"estado": "ERROR_CONFIGURACION", "procesados": 0}
        finally:
            lock_cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))


def sincronizar_facturas_dhl_seguro() -> dict[str, Any]:
    """Wrapper del scheduler: no filtra mensajes, tokens ni respuestas."""
    try:
        resultado = sincronizar_facturas_dhl()
        if resultado.get("estado") not in {"DESHABILITADA", "SIN_CONECTAR", "EN_CURSO"}:
            print("[correo-dhl] sincronización: " + str(resultado.get("estado")))
        return resultado
    except Exception as exc:
        print(f"[correo-dhl] sincronización falló: {type(exc).__name__}")
        return {"estado": "ERROR", "procesados": 0}


def sincronizar_facturas_dhl_historicas(anio: int) -> dict[str, Any]:
    """Prepara un año completo para revisión sin aplicar saldos ni diferencias."""
    control = preflight_correo_dhl()
    if not control["configurada"]:
        return {"estado": "DESHABILITADA", "procesados": 0,
                "bloqueos": control["bloqueos"]}
    actual = datetime.now(timezone.utc).year
    if type(anio) is not int or not 2020 <= anio <= actual:
        return {"estado": "ANIO_INVALIDO", "procesados": 0}
    with get_conn() as lock_conn, lock_conn.cursor() as lock_cur:
        lock_cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS adquirido", (_LOCK_NAME,))
        if not lock_cur.fetchone()["adquirido"]:
            return {"estado": "EN_CURSO", "procesados": 0}
        try:
            estado = estado_integracion()
            if not estado.get("conectada"):
                return {"estado": "SIN_CONECTAR", "procesados": 0}
            cliente = _ClienteGmail()
            mensajes, limite_alcanzado = _listar_mensajes_historicos(cliente, anio)
            vistos = set()
            mensajes_unicos = []
            for resumen in mensajes:
                if resumen["id"] in vistos:
                    continue
                vistos.add(resumen["id"])
                mensajes_unicos.append(resumen)
            conteos: dict[str, int] = {}
            for resumen in mensajes_unicos:
                resultado = _procesar_mensaje(
                    cliente, resumen, reintentar_patron_no_admitido=True,
                    forzar_reintento_operativo=True,
                )
                conteos[resultado] = conteos.get(resultado, 0) + 1
            estado_salida = "LIMITE_ALCANZADO" if limite_alcanzado else "OK"
            salida = {
                "estado": estado_salida,
                "anio": anio,
                "procesados": len(mensajes_unicos),
                "resultados": conteos,
                "historico": True,
            }
            _marcar_resultado_historico(salida)
            with get_conn() as conn, conn.cursor() as cur:
                _registrar_auditoria(
                    cur, evento="DHL_GMAIL_HISTORICO_PROCESADO", actor=_ACTOR,
                    metadata={"anio": anio, "procesados": len(mensajes_unicos),
                              "resultados": conteos, "limite_alcanzado": limite_alcanzado},
                )
            return salida
        except CorreoDHLReautorizar:
            _marcar_integracion(estado="REAUTORIZAR", error="REAUTORIZAR")
            return {"estado": "REAUTORIZAR", "procesados": 0, "anio": anio}
        except CorreoDHLTemporal:
            _marcar_integracion(error="ERROR_TEMPORAL")
            return {"estado": "ERROR_TEMPORAL", "procesados": 0, "anio": anio}
        except ConfiguracionCorreoDHL:
            _marcar_integracion(estado="ERROR", error="CONFIGURACION")
            return {"estado": "ERROR_CONFIGURACION", "procesados": 0, "anio": anio}
        finally:
            lock_cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))


def sincronizar_facturas_dhl_historicas_seguro(anio: int) -> dict[str, Any]:
    """Wrapper del backfill: no expone mensajes, tokens ni respuestas de Gmail."""
    try:
        resultado = sincronizar_facturas_dhl_historicas(anio)
        if resultado.get("estado") not in {"DESHABILITADA", "SIN_CONECTAR", "EN_CURSO"}:
            print("[correo-dhl] histórico: " + str(resultado.get("estado")))
        return resultado
    except Exception as exc:
        print(f"[correo-dhl] histórico falló: {type(exc).__name__}")
        return {"estado": "ERROR", "procesados": 0, "anio": anio}
