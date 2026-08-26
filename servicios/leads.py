"""Cotizaciones públicas persistidas y entrega por correo verificable.

La tarifa que llega al correo siempre sale de un snapshot creado por
``/cotizar-web``. El navegador sólo conserva una referencia opaca: no puede
reescribir carrier, servicio ni precio al pedir el email.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import secrets
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.database import get_conn
from core.email_transport import canonical_email_address, send_transactional_email
from servicios.paises import nombre as nombre_pais, normalizar_iso2

_QUOTE_ID_RE = re.compile(r"^Q-[A-Za-z0-9_-]{20,64}$")
_BA = ZoneInfo("America/Argentina/Buenos_Aires")
_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    autoescape=select_autoescape(("html", "xml")),
)


def _max_cotizaciones_email_hora() -> int:
    """Tope global durable incluso si falla el rate-limit del proxy/edge."""
    try:
        valor = int((os.getenv("EMAIL_COTIZACIONES_MAX_HORA") or "100").strip())
    except (TypeError, ValueError):
        valor = 100
    return min(max(valor, 10), 1000)


@dataclass(frozen=True)
class _ReclamoEntrega:
    """Resultado del claim transaccional previo a tocar SMTP."""

    estado: str
    lead_id: int | None = None
    claim: str = ""


def _decimal(valor, campo: str, *, maximo: Decimal) -> Decimal:
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{campo} no es válido") from None
    if not numero.is_finite() or numero <= 0 or numero > maximo:
        raise ValueError(f"{campo} no es válido")
    return numero


def _vigencia_horas() -> int:
    try:
        horas = int((os.getenv("COTIZACION_WEB_VIGENCIA_HORAS") or "24").strip())
    except (TypeError, ValueError):
        horas = 24
    return min(max(horas, 1), 72)


def _opciones_publicas(carriers: list[dict], recomendado: str) -> list[dict]:
    opciones: list[dict] = []
    for carrier in (carriers or [])[:8]:
        if not isinstance(carrier, dict) or carrier.get("estado") != "cotizado":
            continue
        precio_ars = _decimal(
            carrier.get("precio_ars"), "Precio ARS", maximo=Decimal("999999999999")
        )
        precio_usd = _decimal(
            carrier.get("precio_usd"), "Precio USD", maximo=Decimal("999999999")
        )
        carrier_id = str(carrier.get("id") or "").strip().lower()[:30]
        nombre = str(carrier.get("nombre") or "").strip()[:60]
        servicio = str(carrier.get("servicio") or "").strip()[:100]
        dias = str(carrier.get("dias_estimados") or "A confirmar").strip()[:30]
        if not carrier_id or not nombre or not servicio:
            continue
        opciones.append({
            "id": carrier_id,
            "nombre": nombre,
            "servicio": servicio,
            "dias_estimados": dias,
            "precio_ars": str(precio_ars.quantize(Decimal("0.01"))),
            "precio_usd": str(precio_usd.quantize(Decimal("0.01"))),
            "recomendada": carrier_id == (recomendado or "").strip().lower(),
        })
    if not opciones:
        raise ValueError("No hay una tarifa válida para guardar")
    if not any(o["recomendada"] for o in opciones):
        menor = min(opciones, key=lambda o: Decimal(o["precio_ars"]))
        menor["recomendada"] = True
    return opciones


def guardar_cotizacion(
    *,
    origen: str,
    destino: str,
    peso_kg,
    largo_cm,
    ancho_cm,
    alto_cm,
    valor_declarado_usd,
    carriers: list[dict],
    recomendado: str,
) -> dict:
    """Persiste el resultado exacto que el backend acaba de calcular."""
    origen_iso = normalizar_iso2(origen)
    destino_iso = normalizar_iso2(destino)
    if not origen_iso or not destino_iso or (origen_iso == destino_iso == "AR"):
        raise ValueError("La ruta de la cotización no es válida")

    peso = _decimal(peso_kg, "Peso", maximo=Decimal("1000"))
    largo = _decimal(largo_cm, "Largo", maximo=Decimal("1000"))
    ancho = _decimal(ancho_cm, "Ancho", maximo=Decimal("1000"))
    alto = _decimal(alto_cm, "Alto", maximo=Decimal("1000"))
    valor = _decimal(
        valor_declarado_usd, "Valor declarado", maximo=Decimal("999999999")
    )
    opciones = _opciones_publicas(carriers, recomendado)
    ahora = datetime.now(_BA)

    for _intento in range(3):
        public_id = "Q-" + secrets.token_urlsafe(24)
        referencia = f"TW-{ahora:%Y%m%d}-{secrets.token_hex(3).upper()}"
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO cotizaciones_web (
                            public_id, referencia, origen, destino, peso_kg,
                            largo_cm, ancho_cm, alto_cm, valor_declarado_usd,
                            recomendado, resumen, vigente_hasta
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            NOW() + (%s * INTERVAL '1 hour')
                        )
                        RETURNING id, created_at, vigente_hasta
                    """, (
                        public_id, referencia, origen_iso, destino_iso, peso,
                        largo, ancho, alto, valor, (recomendado or "")[:30],
                        json.dumps(opciones, ensure_ascii=False), _vigencia_horas(),
                    ))
                    fila = cur.fetchone()
            return {
                "id": fila["id"],
                "quote_id": public_id,
                "referencia": referencia,
                "emitida_en": fila["created_at"].isoformat(),
                "vigente_hasta": fila["vigente_hasta"].isoformat(),
            }
        except Exception as exc:
            # Sólo se reintenta una colisión de los dos identificadores
            # aleatorios. Cualquier otro error conserva su traceback real.
            if getattr(exc, "pgcode", "") != "23505":
                raise
    raise RuntimeError("No se pudo generar una referencia de cotización")


def _base_url() -> str:
    raw = (os.getenv("BASE_URL") or "https://taurosolutions.ar").strip().rstrip("/")
    if not re.fullmatch(r"https://(?:www\.)?taurosolutions\.ar", raw, re.I):
        return "https://taurosolutions.ar"
    return raw


def _fmt_decimal(valor, decimales: int = 2) -> str:
    numero = Decimal(str(valor or 0))
    texto = f"{numero:,.{decimales}f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def _presentar(fila: dict) -> dict:
    raw = fila.get("resumen") or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    opciones = []
    for item in raw:
        opciones.append({
            "id": str(item.get("id") or ""),
            "nombre": str(item.get("nombre") or ""),
            "servicio": str(item.get("servicio") or ""),
            "dias_texto": (
                f"{item.get('dias_estimados')} días"
                if str(item.get("dias_estimados") or "").strip().replace("-", "").isdigit()
                else str(item.get("dias_estimados") or "Plazo a confirmar")
            ),
            "precio_ars_texto": _fmt_decimal(item.get("precio_ars"), 2),
            "precio_usd_texto": _fmt_decimal(item.get("precio_usd"), 2),
            "precio_ars": str(item.get("precio_ars") or ""),
            "precio_usd": str(item.get("precio_usd") or ""),
            "recomendada": bool(item.get("recomendada")),
        })
    creada = fila["created_at"]
    vigente = fila["vigente_hasta"]
    if creada.tzinfo is None:
        creada = creada.replace(tzinfo=timezone.utc)
    if vigente.tzinfo is None:
        vigente = vigente.replace(tzinfo=timezone.utc)
    creada_ba = creada.astimezone(_BA)
    vigente_ba = vigente.astimezone(_BA)
    return {
        "id": fila["id"],
        "quote_id": fila["public_id"],
        "referencia": fila["referencia"],
        "origen": fila["origen"].strip(),
        "destino": fila["destino"].strip(),
        "origen_nombre": nombre_pais(fila["origen"].strip()),
        "destino_nombre": nombre_pais(fila["destino"].strip()),
        "peso_texto": _fmt_decimal(fila["peso_kg"], 3).rstrip("0").rstrip(","),
        "peso_kg": str(fila["peso_kg"]),
        "largo_cm": str(fila["largo_cm"]),
        "ancho_cm": str(fila["ancho_cm"]),
        "alto_cm": str(fila["alto_cm"]),
        "valor_declarado_usd": str(fila["valor_declarado_usd"]),
        "medidas_texto": " × ".join(
            _fmt_decimal(fila[campo], 2).rstrip("0").rstrip(",")
            for campo in ("largo_cm", "ancho_cm", "alto_cm")
        ),
        "valor_texto": _fmt_decimal(fila["valor_declarado_usd"], 2),
        "opciones": opciones,
        "emitida_texto": creada_ba.strftime("%d/%m/%Y %H:%M"),
        "vigente_texto": vigente_ba.strftime("%d/%m/%Y %H:%M"),
        "emitida_en": creada.isoformat(),
        "vigente_hasta": vigente.isoformat(),
        "expirada": datetime.now(timezone.utc) > vigente.astimezone(timezone.utc),
        "url": f"{_base_url()}/cotizacion/{fila['public_id']}",
    }


def obtener_cotizacion(quote_id: str, *, exigir_vigente: bool = False) -> dict | None:
    quote_id = (quote_id or "").strip()
    if not _QUOTE_ID_RE.fullmatch(quote_id):
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, public_id, referencia, origen, destino, peso_kg,
                       largo_cm, ancho_cm, alto_cm, valor_declarado_usd,
                       recomendado, resumen, created_at, vigente_hasta
                  FROM cotizaciones_web
                 WHERE public_id = %s
            """, (quote_id,))
            fila = cur.fetchone()
    if not fila:
        return None
    presentada = _presentar(fila)
    if exigir_vigente and presentada["expirada"]:
        return None
    return presentada


def renderizar_cotizacion(cotizacion: dict) -> tuple[str, str, str]:
    """Devuelve asunto, texto plano y HTML autoescapado."""
    contexto = {"cotizacion": cotizacion}
    asunto = (
        f"Presupuesto {cotizacion['referencia']} · "
        f"{cotizacion['origen']} → {cotizacion['destino']} · TAURO"
    )
    texto = _TEMPLATES.get_template("email/cotizacion.txt").render(**contexto)
    html = _TEMPLATES.get_template("email/cotizacion.html").render(**contexto)
    return asunto, texto, html


def renderizar_cotizacion_publica(cotizacion: dict) -> str:
    return _TEMPLATES.get_template("public/cotizacion.html").render(
        cotizacion=cotizacion
    )


def _reclamar_entrega(email: str, cotizacion: dict) -> _ReclamoEntrega:
    """Reserva una única entrega sin permitir carreras entre workers.

    El advisory lock serializa por destinatario normalizado incluso cuando se
    usan varios procesos. Esto hace durable el límite de una entrega aceptada
    (o todavía incierta) cada 24 horas, independientemente del ``quote_id``.

    Un ``ENVIANDO`` abandonado es deliberadamente incierto: pudo haber sido
    aceptado por SMTP antes de que el proceso muriera. Se mueve a
    ``VERIFICAR_EMAIL`` y nunca se reenvía automáticamente.
    """
    claim = secrets.token_urlsafe(24)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pg_advisory_xact_lock(
                    hashtextextended(LOWER(BTRIM(%s)), 73492051)
                )
            """, (email,))

            # Recuperación fail-closed: no podemos saber si SMTP aceptó un
            # mensaje cuando el proceso murió antes de guardar el resultado.
            cur.execute("""
                UPDATE leads_cotizacion
                   SET email_estado = 'VERIFICAR_EMAIL',
                       email_error_codigo = 'SMTP_OUTCOME_UNKNOWN',
                       email_claim = NULL, email_actualizado_at = NOW()
                 WHERE LOWER(email) = LOWER(%s)
                   AND email_estado = 'ENVIANDO'
                   AND email_actualizado_at <= NOW() - INTERVAL '5 minutes'
            """, (email,))

            cur.execute("""
                SELECT id, email_estado
                  FROM leads_cotizacion
                 WHERE cotizacion_id = %s AND LOWER(email) = LOWER(%s)
                 FOR UPDATE
            """, (cotizacion["id"], email))
            lead = cur.fetchone()
            if lead:
                if lead["email_estado"] == "ENVIADO":
                    return _ReclamoEntrega("enviado", lead["id"])
                if lead["email_estado"] == "ENVIANDO":
                    return _ReclamoEntrega("procesando", lead["id"])
                if lead["email_estado"] == "VERIFICAR_EMAIL":
                    return _ReclamoEntrega("verificar", lead["id"])
                if lead["email_estado"] not in {"PENDIENTE", "FALLIDO"}:
                    return _ReclamoEntrega("verificar", lead["id"])

            # Se consulta después de tomar el advisory lock. Dos requests para
            # quotes distintos del mismo email no pueden superar juntos este
            # control ni aun en procesos/hosts diferentes.
            cur.execute("""
                SELECT id
                  FROM leads_cotizacion
                 WHERE LOWER(email) = LOWER(%s)
                   AND id <> %s
                   AND (
                        (
                            email_estado = 'ENVIADO'
                            AND email_enviado_at > NOW() - INTERVAL '24 hours'
                        )
                        OR (
                            email_estado IN (
                                'PENDIENTE', 'ENVIANDO', 'FALLIDO',
                                'VERIFICAR_EMAIL'
                            )
                            AND email_actualizado_at > NOW() - INTERVAL '24 hours'
                        )
                   )
                 LIMIT 1
            """, (email, lead["id"] if lead else 0))
            if cur.fetchone():
                return _ReclamoEntrega("limitado")

            if not lead:
                # Segunda barrera independiente de la IP. Serializa altas de
                # destinatarios distintos y evita un relay masivo aunque el
                # origen Railway no estuviera cerrado a Cloudflare.
                cur.execute("""
                    SELECT pg_advisory_xact_lock(
                        hashtextextended('tauro:cotizaciones-email:global', 73492052)
                    )
                """)
                cur.execute("""
                    SELECT COUNT(*) AS total
                      FROM leads_cotizacion
                     WHERE created_at > NOW() - INTERVAL '1 hour'
                """)
                total_hora = int((cur.fetchone() or {}).get("total") or 0)
                if total_hora >= _max_cotizaciones_email_hora():
                    return _ReclamoEntrega("limitado")
                cur.execute("""
                    INSERT INTO leads_cotizacion (
                        email, origen, destino, peso_kg, resumen, cotizacion_id,
                        email_estado, email_actualizado_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'PENDIENTE', NOW())
                    RETURNING id
                """, (
                    email, cotizacion["origen"], cotizacion["destino"],
                    Decimal(
                        cotizacion["peso_texto"].replace(".", "").replace(",", ".")
                    ),
                    json.dumps(cotizacion["opciones"], ensure_ascii=False),
                    cotizacion["id"],
                ))
                lead = cur.fetchone()
                if not lead:
                    raise RuntimeError("No se pudo registrar el pedido de cotización")

            cur.execute("""
                UPDATE leads_cotizacion
                   SET email_estado = 'ENVIANDO', email_claim = %s,
                       email_intentos = email_intentos + 1,
                       email_error_codigo = NULL, email_actualizado_at = NOW()
                 WHERE id = %s
                   AND email_estado IN ('PENDIENTE', 'FALLIDO')
            """, (claim, lead["id"]))
            if cur.rowcount != 1:
                return _ReclamoEntrega("verificar", lead["id"])
    return _ReclamoEntrega("reclamado", lead["id"], claim)


def guardar_lead(email: str, quote_id: str) -> dict:
    """Envía la cotización y sólo confirma éxito tras aceptación SMTP."""
    email = canonical_email_address(email)
    if not email:
        return {"ok": False, "estado": "fallido", "error": "Ese email no parece válido."}

    cotizacion = obtener_cotizacion(quote_id, exigir_vigente=True)
    if not cotizacion:
        return {
            "ok": False,
            "estado": "expirada",
            "error": "La cotización venció o no existe. Cotizá nuevamente.",
        }

    reclamo = _reclamar_entrega(email, cotizacion)
    if reclamo.estado == "enviado":
        return {
            "ok": True,
            "estado": "enviado",
            "referencia": cotizacion["referencia"],
        }
    if reclamo.estado == "procesando":
        return {
            "ok": False,
            "estado": "procesando",
            "error": "Ya estamos procesando ese correo. Esperá un momento antes de reintentar.",
        }
    if reclamo.estado == "limitado":
        return {
            "ok": False,
            "estado": "limitado",
            "error": (
                "Por seguridad, sólo enviamos una cotización por correo cada "
                "24 horas. Podés abrir o imprimir el presupuesto desde esta página."
            ),
        }
    if reclamo.estado != "reclamado":
        return {
            "ok": False,
            "estado": "verificar",
            "error": (
                "No podemos confirmar el estado del envío anterior y no vamos a "
                "duplicarlo. Contactanos si no lo recibís."
            ),
        }

    asunto, texto, html = renderizar_cotizacion(cotizacion)
    resultado = send_transactional_email(
        recipient=email,
        subject=asunto,
        text_body=texto,
        html_body=html,
        reply_to=(os.getenv("EMAIL_REPLY_TO") or "operaciones@taurosolutions.ar"),
        dedupe_key=f"cotizacion:{cotizacion['quote_id']}:{email}",
    )
    resultado_incierto = (
        not resultado.accepted
        and resultado.code in {"SMTP_TIMEOUT", "SMTP_NETWORK", "SMTP_ERROR"}
    )
    estado_persistido = (
        "ENVIADO" if resultado.accepted
        else "VERIFICAR_EMAIL" if resultado_incierto
        else "FALLIDO"
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE leads_cotizacion
                   SET email_estado = %s, email_error_codigo = %s,
                       email_message_id = %s,
                       email_enviado_at = CASE WHEN %s THEN NOW() ELSE NULL END,
                       email_actualizado_at = NOW(), email_claim = NULL
                 WHERE id = %s AND email_claim = %s
                   AND email_estado = 'ENVIANDO'
            """, (
                estado_persistido,
                None if resultado.accepted else resultado.code,
                resultado.message_id or None,
                resultado.accepted,
                reclamo.lead_id,
                reclamo.claim,
            ))
            actualizado = cur.rowcount == 1

    # SMTP y base de datos forman dos sistemas sin transacción distribuida. Si
    # perdimos el claim, jamás afirmamos éxito ni forzamos un segundo envío.
    if not actualizado:
        print(
            f"[leads] cotización {cotizacion['referencia']} con resultado "
            "SMTP no conciliado"
        )
        return {
            "ok": False,
            "estado": "verificar",
            "error": (
                "No pudimos confirmar el estado final del correo y no vamos a "
                "duplicarlo. Contactanos si no lo recibís."
            ),
        }

    if resultado.accepted:
        print(f"[leads] cotización {cotizacion['referencia']} aceptada por SMTP")
        return {
            "ok": True,
            "estado": "enviado",
            "referencia": cotizacion["referencia"],
        }

    print(
        f"[leads] cotización {cotizacion['referencia']} no aceptada por SMTP "
        f"({resultado.code})"
    )
    return {
        "ok": False,
        "estado": (
            "verificar" if resultado_incierto
            else "pendiente" if resultado.retryable
            else "fallido"
        ),
        "error": (
            "No podemos confirmar si el servidor aceptó el correo y no vamos a "
            "duplicarlo. Contactanos si no lo recibís."
            if resultado_incierto else
            "El servidor pidió reintentar más tarde. La cotización quedó guardada "
            "y vamos a reintentar automáticamente."
            if resultado.retryable else
            "No pudimos enviar el correo ahora. La cotización quedó guardada; "
            "revisá la dirección o probá nuevamente en unos minutos."
        ),
    }


def estado_entregas_email() -> dict:
    """Resumen operativo para admin, sin destinatarios ni secretos."""
    from core.email_transport import email_config_status

    estado = dict(email_config_status())
    estado.update({
        "enviados_24h": 0,
        "fallidos_24h": 0,
        "procesando": 0,
        "requieren_verificacion": 0,
        "reset_requieren_verificacion": 0,
    })
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (
                            WHERE email_estado = 'ENVIADO'
                              AND email_enviado_at > NOW() - INTERVAL '24 hours'
                        ) AS enviados_24h,
                        COUNT(*) FILTER (
                            WHERE email_estado = 'FALLIDO'
                              AND email_actualizado_at > NOW() - INTERVAL '24 hours'
                        ) AS fallidos_24h,
                        COUNT(*) FILTER (
                            WHERE email_estado = 'ENVIANDO'
                              AND email_actualizado_at > NOW() - INTERVAL '10 minutes'
                        ) AS procesando,
                        COUNT(*) FILTER (
                            WHERE email_estado = 'VERIFICAR_EMAIL'
                        ) AS requieren_verificacion,
                        (
                            SELECT COUNT(*)
                              FROM password_reset_requests
                             WHERE estado = 'VERIFICAR_EMAIL'
                        ) AS reset_requieren_verificacion
                    FROM leads_cotizacion
                """)
                fila = cur.fetchone() or {}
        estado.update({
            "enviados_24h": int(fila.get("enviados_24h") or 0),
            "fallidos_24h": int(fila.get("fallidos_24h") or 0),
            "procesando": int(fila.get("procesando") or 0),
            "requieren_verificacion": int(
                fila.get("requieren_verificacion") or 0
            ),
            "reset_requieren_verificacion": int(
                fila.get("reset_requieren_verificacion") or 0
            ),
        })
    except Exception:
        estado["database_status"] = "unavailable"
    else:
        estado["database_status"] = "ok"
    return estado


def procesar_reintentos_email(limite: int = 10) -> dict:
    """Reintenta rechazos 4xx inequívocos; nunca resultados inciertos."""
    from core.email_transport import email_config_status

    limite = min(max(int(limite or 1), 1), 25)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # El worker existente corre periódicamente: también reconcilia
            # claims abandonados, sin intentar entregarlos otra vez.
            cur.execute("""
                UPDATE leads_cotizacion
                   SET email_estado = 'VERIFICAR_EMAIL',
                       email_error_codigo = 'SMTP_OUTCOME_UNKNOWN',
                       email_claim = NULL, email_actualizado_at = NOW()
                 WHERE email_estado = 'ENVIANDO'
                   AND email_actualizado_at <= NOW() - INTERVAL '5 minutes'
            """)
            if not email_config_status()["configured"]:
                return {
                    "procesados": 0,
                    "enviados": 0,
                    "motivo": "smtp_no_configurado",
                }
            cur.execute("""
                SELECT l.email, q.public_id
                  FROM leads_cotizacion l
                  JOIN cotizaciones_web q ON q.id = l.cotizacion_id
                 WHERE l.email_estado = 'FALLIDO'
                   AND l.email_error_codigo = 'SMTP_TEMPORARY'
                   AND l.email_intentos < 4
                   AND l.email_actualizado_at < NOW() - INTERVAL '5 minutes'
                   AND q.vigente_hasta > NOW()
                 ORDER BY l.email_actualizado_at
                 LIMIT %s
            """, (limite,))
            filas = cur.fetchall() or []
    enviados = 0
    for fila in filas:
        try:
            resultado = guardar_lead(fila["email"], fila["public_id"])
            enviados += int(bool(resultado.get("ok")))
        except Exception as exc:
            print(f"[leads] reintento falló: {type(exc).__name__}")
    return {"procesados": len(filas), "enviados": enviados}


def limpiar_retencion_cotizaciones(
    *, leads_dias: int = 365, cotizaciones_dias: int = 30
) -> dict:
    """Elimina PII vencida y snapshots expirados sin referencias.

    Esta función no se agenda por sí sola. El scheduler debe ejecutarla una vez
    por día después de aplicar el DDL de cotizaciones/leads en producción.
    """
    leads_dias = min(max(int(leads_dias), 30), 730)
    cotizaciones_dias = min(max(int(cotizaciones_dias), 7), 365)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM leads_cotizacion
                 WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                   AND email_estado <> 'ENVIANDO'
            """, (leads_dias,))
            leads_eliminados = max(int(cur.rowcount or 0), 0)
            cur.execute("""
                DELETE FROM cotizaciones_web AS q
                 WHERE q.vigente_hasta < NOW() - (%s * INTERVAL '1 day')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM leads_cotizacion AS l
                        WHERE l.cotizacion_id = q.id
                   )
            """, (cotizaciones_dias,))
            cotizaciones_eliminadas = max(int(cur.rowcount or 0), 0)
    return {
        "leads_eliminados": leads_eliminados,
        "cotizaciones_eliminadas": cotizaciones_eliminadas,
    }
