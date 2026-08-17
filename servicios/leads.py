# ============================================================
# Leads del cotizador público — "guardá esta cotización"
# ============================================================
# La mitad que le faltaba al embudo: el cotizador da la respuesta completa
# gratis y sin login (ese diferencial NO se toca — jamás limitarlo estilo
# "5 búsquedas"), pero dejaba ir al visitante sin pedirle nada. Acá se le
# ofrece recibir SU cotización por mail DESPUÉS de ver el número, en el
# momento de máximo interés. Cada lead es pipeline: TAURO recibe el aviso.
# ============================================================
from __future__ import annotations

import json
import re

from core.database import get_conn

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

_tabla_lista = False


def _ensure_tabla() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS leads_cotizacion (
                    id         SERIAL PRIMARY KEY,
                    email      TEXT NOT NULL,
                    origen     TEXT,
                    destino    TEXT,
                    peso_kg    REAL,
                    resumen    JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute(
                "ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS origen TEXT"
            )
    _tabla_lista = True


def guardar_lead(email: str, origen: str, destino: str, peso_kg: float,
                 carriers: list[dict]) -> dict:
    """
    Guarda el lead y le manda su cotización por mail. Devuelve {ok, error}.

    El resumen que viaja en el mail se arma SÓLO con los campos esperados de
    cada carrier: el payload viene del navegador del visitante y no se le
    reenvía HTML arbitrario a nadie.
    """
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return {"ok": False, "error": "Ese email no parece válido."}

    from servicios.paises import normalizar_iso2

    origen = normalizar_iso2(origen or "AR")
    destino = normalizar_iso2(destino)
    if not origen or not destino:
        return {"ok": False, "error": "La ruta de la cotización no es válida."}
    if origen == "AR" and destino == "AR":
        return {
            "ok": False,
            "error": "Los envíos nacionales todavía no se cotizan desde este formulario.",
        }

    # UN MAIL POR DIRECCIÓN Y POR DÍA. El rate limit por IP no alcanza:
    # desde IPs rotativas esto es una primitiva para mandarle mails con
    # nuestro remitente a cualquier persona — la víctima recibe correo de
    # TAURO que nunca pidió y nuestro dominio se gana la reputación de spam.
    # Un visitante real pide su cotización una vez.
    _ensure_tabla()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM leads_cotizacion
                    WHERE email = %s AND created_at > NOW() - INTERVAL '24 hours'
                    LIMIT 1
                """, (email,))
                if cur.fetchone():
                    # No se le avisa al que abusa que el tope existe, y el
                    # visitante honesto que reintenta ve un mensaje sensato.
                    print(f"[leads] {email} ya pidió una cotización hoy: no se reenvía")
                    return {"ok": True}
    except Exception as e:
        print(f"[leads] no pude chequear el tope diario de {email}: {e}")

    limpios = []
    for c in (carriers or [])[:5]:
        if not isinstance(c, dict) or c.get("estado") != "cotizado":
            continue
        try:
            limpios.append({
                "nombre": str(c.get("nombre") or "")[:40],
                "servicio": str(c.get("servicio") or "")[:60],
                "dias": str(c.get("dias_estimados") or "")[:12],
                "precio_ars": float(c.get("precio_ars") or 0),
                "precio_usd": float(c.get("precio_usd") or 0),
            })
        except (TypeError, ValueError):
            continue
    if not limpios:
        return {"ok": False, "error": "No hay cotización para mandar. Cotizá de nuevo."}

    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO leads_cotizacion (email, origen, destino, peso_kg, resumen)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (email, origen, destino, float(peso_kg or 0),
                  json.dumps(limpios, ensure_ascii=False)))
            lead_id = cur.fetchone()["id"]

    filas = "".join(
        f"""<tr>
          <td style="padding:10px 14px;color:#1e1b2e;font-weight:600;">{c['nombre']}</td>
          <td style="padding:10px 14px;color:#4a4a58;">{c['servicio']} · {c['dias']} días</td>
          <td style="padding:10px 14px;text-align:right;color:#1e1b2e;font-weight:700;white-space:nowrap;">
            $ {c['precio_ars']:,.0f} ARS<br>
            <span style="font-weight:400;color:#8b86a0;font-size:12px;">USD {c['precio_usd']:,.2f}</span>
          </td></tr>""".replace(",", ".")
        for c in limpios)

    cuerpo = f"""<html><body style="margin:0;background:#f4f4f6;">
<div style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;">
  <div style="background:#0c0a14;padding:28px 24px;text-align:center;">
    <div style="font-size:22px;font-weight:700;letter-spacing:.08em;color:#fff;">
      TAURO <span style="color:#a78bfa;">SOLUTIONS</span>
    </div>
  </div>
  <div style="padding:30px;">
    <h2 style="margin:0 0 8px;font-size:19px;color:#1e1b2e;">Tu cotización {origen} → {destino}</h2>
    <p style="line-height:1.7;color:#4a4a58;margin:0 0 18px;">
      Envío de <b>{peso_kg:g} kg</b> desde {origen} hacia {destino}, puerta a puerta con
      despacho de aduana incluido:</p>
    <table style="width:100%;border-collapse:collapse;background:#faf9fc;border-radius:10px;overflow:hidden;">
      {filas}
    </table>
    <p style="line-height:1.7;color:#8b86a0;margin:16px 0 0;font-size:12.5px;">
      Precios finales, sin sorpresas. La cotización puede variar según las
      medidas exactas del paquete.</p>
    <p style="text-align:center;margin:26px 0 0;">
      <a href="https://taurosolutions.ar/web"
         style="background:#7c5cf6;color:#fff;padding:13px 30px;text-decoration:none;
                font-weight:600;border-radius:999px;display:inline-block;">Hacer este envío</a>
    </p>
    <p style="line-height:1.7;color:#4a4a58;margin:22px 0 0;font-size:13.5px;">
      ¿Dudas de aduana, impuestos o tiempos? Respondé este mail y te
      contestamos en el día.</p>
  </div>
  <div style="background:#0c0a14;padding:16px;text-align:center;font-size:11px;color:#8b86a0;">
    taurosolutions.ar · Logística internacional
  </div>
</div></body></html>"""

    # Los mails salen en un hilo aparte: SMTP puede tardar 10-30 segundos y
    # el visitante estaba mirando "Enviando…" todo ese tiempo (medido contra
    # producción: el POST superaba los 25s). El lead ya está guardado — la
    # respuesta no tiene por qué esperar a Gmail.
    def _mandar_mails():
        try:
            from core.email_sender import _enviar_mail_a
            _enviar_mail_a(
                email,
                f"Tu cotización {origen} → {destino} · TAURO Solutions",
                cuerpo,
            )
        except Exception as e:
            print(f"[leads] lead {lead_id} guardado pero el mail al visitante falló: {e}")
        try:
            import os as _os

            from core.email_sender import _enviar_mail_a
            mejor = min(limpios, key=lambda c: c["precio_ars"])
            _enviar_mail_a(
                _os.getenv("LEADS_AVISO_EMAIL", "cotizaciones@taurosolutions.ar"),
                f"🎣 Lead del cotizador: {email} · {origen} → {destino}",
                f"<p><b>{email}</b> cotizó {peso_kg:g} kg de <b>{origen}</b> "
                f"a <b>{destino}</b> y pidió "
                f"la cotización por mail.<br>Mejor precio mostrado: "
                f"{mejor['nombre']} $ {mejor['precio_ars']:,.0f} ARS.<br><br>"
                f"Escribile mientras está caliente.</p>".replace(",", "."))
        except Exception as e:
            print(f"[leads] aviso interno falló: {e}")

    import threading
    threading.Thread(target=_mandar_mails, daemon=True).start()

    print(
        f"[leads] nuevo lead #{lead_id}: {email} · "
        f"{origen} → {destino} ({peso_kg}kg)"
    )
    return {"ok": True}
