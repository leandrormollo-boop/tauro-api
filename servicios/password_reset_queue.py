"""Cola durable para correos de restablecimiento de contraseña.

El request HTTP sólo encola una referencia al cliente. El token secreto se
crea dentro del worker, nunca se persiste en texto claro y sólo se activa
después de que el servidor SMTP acepta el mensaje.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import secrets
from urllib.parse import quote, urlparse

from core.database import get_conn
from core.email_sender import enviar_restablecimiento_password
from servicios.auth import (
    crear_password_reset_token,
    finalizar_password_reset_entregado,
    revocar_password_reset_token,
)


MAX_INTENTOS = 3
CLAIM_STALE_MINUTOS = 10
MAX_SOLICITUDES_HORA = 3
REQUEST_TTL_MINUTOS = 60
_ADVISORY_LOCK_SEED = 20260818
_QUOTE_ID_RE = re.compile(r"^Q-[A-Za-z0-9_-]{20,64}$")


@dataclass(frozen=True)
class PasswordResetEnqueueResult:
    accepted: bool
    code: str


def _base_url_oficial() -> str:
    """Devuelve exclusivamente un origen HTTPS oficial de TAURO."""
    raw = (os.getenv("BASE_URL") or "https://taurosolutions.ar").strip().rstrip("/")
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


def _password_reset_link(token: str, quote_id: str = "") -> str:
    """Arma un link cuyo secreto vive sólo en el fragmento del navegador."""
    token_seguro = quote(token, safe="")
    fragmento = f"token={token_seguro}"
    if _QUOTE_ID_RE.fullmatch(quote_id or ""):
        fragmento += f"&quote_id={quote(quote_id, safe='')}"
    return f"{_base_url_oficial()}/portal/password/reset#{fragmento}"


def encolar_password_reset(
    cliente_id: str, quote_id: str = "",
) -> PasswordResetEnqueueResult:
    """Encola como máximo tres pedidos por cuenta y hora, de forma atómica.

    Una solicitud ya pendiente cuenta como aceptada para conservar la
    respuesta idempotente del endpoint. La tabla no guarda email ni token.
    """
    cliente_id = (cliente_id or "").strip().upper()
    quote_id = quote_id if _QUOTE_ID_RE.fullmatch(quote_id or "") else ""
    if not cliente_id:
        return PasswordResetEnqueueResult(False, "INVALID_CLIENT")

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Serializa solicitudes de la misma cuenta incluso entre varios
            # workers/procesos. No depende del rate-limit en memoria.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
                (cliente_id, _ADVISORY_LOCK_SEED),
            )
            cur.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                          FROM password_reset_requests
                         WHERE cliente_id = %s
                           AND estado IN ('PENDIENTE', 'PROCESANDO')
                    ) AS activa,
                    COUNT(*) FILTER (
                        WHERE creado_at > NOW() - INTERVAL '1 hour'
                    ) AS recientes
                  FROM password_reset_requests
                 WHERE cliente_id = %s
                """,
                (cliente_id, cliente_id),
            )
            estado = cur.fetchone() or {}
            if bool(estado.get("activa")):
                return PasswordResetEnqueueResult(True, "ALREADY_PENDING")
            if int(estado.get("recientes") or 0) >= MAX_SOLICITUDES_HORA:
                return PasswordResetEnqueueResult(False, "RATE_LIMITED")

            cur.execute(
                """
                INSERT INTO password_reset_requests (
                    cliente_id, quote_id, estado, intentos, proximo_intento_at,
                    creado_at, actualizado_at
                ) VALUES (%s, %s, 'PENDIENTE', 0, NOW(), NOW(), NOW())
                ON CONFLICT (cliente_id)
                    WHERE estado IN ('PENDIENTE', 'PROCESANDO')
                DO NOTHING
                RETURNING id
                """,
                (cliente_id, quote_id or None),
            )
            insertada = cur.fetchone()
    return PasswordResetEnqueueResult(
        True,
        "QUEUED" if insertada else "ALREADY_PENDING",
    )


def uniformar_password_reset_inexistente(referencia_anonima: str) -> None:
    """Ejecuta un camino DB equivalente sin guardar identidad ni crear tarea.

    Evita que la diferencia entre «sólo lookup» y «lookup + cola» vuelva a
    convertir el tiempo de respuesta en un oráculo de cuentas existentes.
    ``referencia_anonima`` es un hash efímero y no se persiste.
    """
    referencia_anonima = (referencia_anonima or "")[:128]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
                (referencia_anonima, _ADVISORY_LOCK_SEED),
            )
            cur.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM password_reset_requests
                         WHERE cliente_id = '__CUENTA_INEXISTENTE__'
                           AND estado IN ('PENDIENTE', 'PROCESANDO')
                    ) AS activa,
                    COUNT(*) FILTER (
                        WHERE creado_at > NOW() - INTERVAL '1 hour'
                    ) AS recientes
                  FROM password_reset_requests
                 WHERE cliente_id = '__CUENTA_INEXISTENTE__'
                """
            )
            cur.fetchone()
            cur.execute("SELECT NULL::BIGINT AS id WHERE FALSE")
            cur.fetchone()


def recuperar_password_reset_claims_stale() -> int:
    """Aísla trabajos abandonados sin reenviar un email de resultado incierto.

    Un proceso puede morir antes o después de que SMTP acepte el mensaje. Sin
    una confirmación durable del proveedor no es seguro repetirlo: el cliente
    puede solicitar otro link y el admin ve el caso para conciliación.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE password_reset_requests
                   SET estado = 'VERIFICAR_EMAIL',
                       proximo_intento_at = NOW(),
                       claim_id = NULL,
                       claimed_at = NULL,
                       ultimo_error_code = 'CLAIM_EXPIRED',
                       actualizado_at = NOW()
                 WHERE estado = 'PROCESANDO'
                   AND claimed_at < NOW() - (%s * INTERVAL '1 minute')
                """,
                (CLAIM_STALE_MINUTOS,),
            )
            return cur.rowcount


def expirar_password_reset_pendientes() -> int:
    """Cierra pedidos que ya no deben poder enviarse más adelante."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE password_reset_requests
                   SET estado = 'FALLIDO',
                       ultimo_error_code = 'REQUEST_EXPIRED',
                       actualizado_at = NOW()
                 WHERE estado = 'PENDIENTE'
                   AND creado_at <= NOW() - (%s * INTERVAL '1 minute')
                """,
                (REQUEST_TTL_MINUTOS,),
            )
            return cur.rowcount


def _reclamar_siguiente() -> dict | None:
    claim_id = secrets.token_urlsafe(18)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidata AS (
                    SELECT r.id
                      FROM password_reset_requests r
                      JOIN clientes c ON c.cliente_id = r.cliente_id
                     WHERE r.estado = 'PENDIENTE'
                       AND r.intentos < %s
                       AND r.proximo_intento_at <= NOW()
                       AND r.creado_at > NOW() - (%s * INTERVAL '1 minute')
                       AND c.activo = TRUE
                       AND NULLIF(BTRIM(c.email), '') IS NOT NULL
                     ORDER BY r.creado_at, r.id
                     FOR UPDATE OF r SKIP LOCKED
                     LIMIT 1
                ), reclamada AS (
                    UPDATE password_reset_requests r
                       SET estado = 'PROCESANDO',
                           intentos = r.intentos + 1,
                           claim_id = %s,
                           claimed_at = NOW(),
                           actualizado_at = NOW()
                      FROM candidata
                     WHERE r.id = candidata.id
                    RETURNING r.id, r.cliente_id, r.quote_id, r.intentos
                )
                SELECT r.id, r.cliente_id, r.quote_id, r.intentos,
                       c.email, %s AS claim_id
                  FROM reclamada r
                  JOIN clientes c ON c.cliente_id = r.cliente_id
                """,
                (MAX_INTENTOS, REQUEST_TTL_MINUTOS, claim_id, claim_id),
            )
            fila = cur.fetchone()
    return dict(fila) if fila else None


def _marcar_fallo(
    request_id: int,
    claim_id: str,
    *,
    code: str,
    retryable: bool,
    intento: int,
) -> str:
    reintentar = bool(retryable and intento < MAX_INTENTOS)
    demora_minutos = min(30, 5 * (2 ** max(intento - 1, 0)))
    estado = "PENDIENTE" if reintentar else "FALLIDO"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE password_reset_requests
                   SET estado = %s,
                       proximo_intento_at = CASE
                           WHEN %s THEN NOW() + (%s * INTERVAL '1 minute')
                           ELSE proximo_intento_at
                       END,
                       ultimo_error_code = %s,
                       claim_id = NULL,
                       claimed_at = NULL,
                       actualizado_at = NOW()
                 WHERE id = %s
                   AND estado = 'PROCESANDO'
                   AND claim_id = %s
                """,
                (
                    estado, reintentar, demora_minutos, (code or "UNKNOWN")[:80],
                    request_id, claim_id,
                ),
            )
    return estado


def _marcar_incierto(request_id: int, claim_id: str, *, code: str) -> str:
    """Conserva el resultado ambiguo para revisión y evita duplicar emails."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE password_reset_requests
                   SET estado = 'VERIFICAR_EMAIL',
                       ultimo_error_code = %s,
                       claim_id = NULL,
                       claimed_at = NULL,
                       actualizado_at = NOW()
                 WHERE id = %s
                   AND estado = 'PROCESANDO'
                   AND claim_id = %s
                """,
                ((code or "SMTP_OUTCOME_UNKNOWN")[:80], request_id, claim_id),
            )
    return "VERIFICAR_EMAIL"


def _procesar_reclamada(fila: dict) -> str:
    """Procesa una fila ya reclamada sin exponer email ni secreto en logs."""
    token = ""
    smtp_aceptado = False
    try:
        token = crear_password_reset_token(str(fila["cliente_id"]))
        resultado = enviar_restablecimiento_password(
            str(fila["email"]),
            _password_reset_link(token, str(fila.get("quote_id") or "")),
        )
        smtp_aceptado = bool(resultado.accepted)
        if resultado.accepted:
            if finalizar_password_reset_entregado(
                token,
                request_id=int(fila["id"]),
                claim_id=str(fila["claim_id"]),
                message_id=resultado.message_id,
            ):
                return "ENVIADO"
            # El claim dejó de pertenecernos. Esta operación atómica no
            # invalidó ningún link nuevo de la cuenta.
            revocar_password_reset_token(token)
            return "CLAIM_LOST"

        revocar_password_reset_token(token)
        code = str(resultado.code or "SMTP_ERROR")
        if code in {
            "SMTP_TIMEOUT", "SMTP_NETWORK", "SMTP_ERROR",
        }:
            return _marcar_incierto(
                int(fila["id"]), str(fila["claim_id"]), code=code,
            )
        return _marcar_fallo(
            int(fila["id"]), str(fila["claim_id"]),
            code=code,
            retryable=bool(resultado.retryable or resultado.accepted),
            intento=int(fila["intentos"]),
        )
    except Exception:
        # Si SMTP aceptó y el COMMIT pudo haberse confirmado sin que llegara
        # el ACK, revocar sería capaz de matar el link ya entregado. En ese
        # caso se aísla el request; si el commit sí quedó, este UPDATE no toca
        # la fila ENVIADO. Si hubo rollback, el token sigue inactivo.
        if token and not smtp_aceptado:
            try:
                revocar_password_reset_token(token)
            except Exception:
                pass
        return _marcar_incierto(
            int(fila["id"]), str(fila["claim_id"]), code="INTERNAL_ERROR",
        )


def procesar_password_reset_requests(limite: int = 10) -> dict[str, int]:
    """Worker idempotente/concurrente para invocar desde el scheduler."""
    limite = min(max(int(limite or 0), 1), 50)
    resumen = {
        "recuperadas": recuperar_password_reset_claims_stale(),
        "expiradas": expirar_password_reset_pendientes(),
        "procesadas": 0,
        "enviadas": 0,
        "reprogramadas": 0,
        "fallidas": 0,
        "requieren_verificacion": 0,
    }
    for _ in range(limite):
        fila = _reclamar_siguiente()
        if not fila:
            break
        resumen["procesadas"] += 1
        estado = _procesar_reclamada(fila)
        if estado == "ENVIADO":
            resumen["enviadas"] += 1
        elif estado == "PENDIENTE":
            resumen["reprogramadas"] += 1
        elif estado == "VERIFICAR_EMAIL":
            resumen["requieren_verificacion"] += 1
        else:
            resumen["fallidas"] += 1
    return resumen


def limpiar_retencion_password_reset(
    *, solicitudes_dias: int = 30, tokens_dias: int = 7,
) -> dict[str, int]:
    """Poda metadatos operativos y hashes vencidos, nunca trabajos activos."""
    solicitudes_dias = min(max(int(solicitudes_dias), 7), 180)
    tokens_dias = min(max(int(tokens_dias), 1), 30)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM password_reset_requests
                 WHERE actualizado_at < NOW() - (%s * INTERVAL '1 day')
                   AND estado NOT IN ('PENDIENTE', 'PROCESANDO')
                """,
                (solicitudes_dias,),
            )
            solicitudes_eliminadas = max(int(cur.rowcount or 0), 0)
            cur.execute(
                """
                DELETE FROM password_reset_tokens
                 WHERE expira_at < NOW() - (%s * INTERVAL '1 day')
                   OR (
                       usado_at IS NOT NULL
                       AND usado_at < NOW() - (%s * INTERVAL '1 day')
                   )
                """,
                (tokens_dias, tokens_dias),
            )
            tokens_eliminados = max(int(cur.rowcount or 0), 0)
    return {
        "solicitudes_eliminadas": solicitudes_eliminadas,
        "tokens_eliminados": tokens_eliminados,
    }
