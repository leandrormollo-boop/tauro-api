# ============================================================
# Centinela del checkout
# ============================================================
# Cada 15 minutos se pregunta lo único que de verdad importa:
# "si un comprador estuviera pagando ahora mismo, ¿vería una opción
# de envío?". Si la respuesta es no dos veces seguidas, manda un mail
# de alerta a Tauro.
#
# Por qué DOS veces seguidas: un parpadeo de red no es una caída, y un
# centinela que grita por cada hipo se vuelve ruido que nadie mira.
#
# Ojo con el alcance: esto corre DENTRO de la app, así que detecta
# fallas parciales (courier caído, base lenta, tarifas rotas), que son
# las más frecuentes. Una caída total del proceso la cubre el
# healthcheck de Railway, que reinicia solo.
# ============================================================
from __future__ import annotations

import os
from datetime import datetime

# Se avisa recién al segundo fallo seguido, y no se repite el mail hasta
# que el servicio se recupere: nada de inundar la casilla.
_fallos_seguidos = 0
_ya_avise = False

UMBRAL_SEGUNDOS = 8.0   # Shopify corta cerca de 10


def _probar_checkout() -> tuple[bool, str]:
    """Simula lo que preguntaría Shopify durante un checkout real."""
    import time
    from servicios.shopify_app import cotizar_para_checkout

    payload = {"rate": {
        "destination": {"country": "US", "city": "MIAMI", "postal_code": "33101"},
        "items": [{"grams": 1000, "quantity": 1, "price": 5_000_00, "sku": "CENTINELA"}],
    }}
    t0 = time.time()
    try:
        r = cotizar_para_checkout(payload)
    except Exception as e:
        return False, f"la cotización lanzó una excepción: {type(e).__name__}: {e}"
    tardo = time.time() - t0

    rates = (r or {}).get("rates", [])
    if not rates:
        return False, f"el checkout no devolvió NINGUNA opción de envío (tardó {tardo:.1f}s)"
    if tardo > UMBRAL_SEGUNDOS:
        return False, (f"el checkout tardó {tardo:.1f}s — Shopify corta a los ~10s, "
                       f"algunos compradores ya no verían el envío")

    try:
        precio = int(rates[0].get("total_price", 0)) / 100
    except (TypeError, ValueError):
        return False, "el precio devuelto no es un número válido"
    if precio <= 0:
        return False, f"el precio devuelto es {precio} (debería ser mayor a cero)"

    return True, f"{len(rates)} opción(es), ARS {precio:,.0f}, {tardo:.2f}s"


def _avisar(motivo: str) -> None:
    destino = (os.getenv("ADMIN_RECOVERY_EMAIL") or "taurosolutionsar@gmail.com").strip()
    cuando = datetime.now().strftime("%d/%m/%Y %H:%M")
    cuerpo = f"""<html><body style="margin:0;background:#f4f4f6;">
<div style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;">
  <div style="background:#0c0a14;padding:28px 24px;text-align:center;">
    <div style="font-size:22px;font-weight:700;letter-spacing:.08em;color:#fff;">
      TAURO <span style="color:#a78bfa;">SOLUTIONS</span>
    </div>
  </div>
  <div style="padding:30px;">
    <h2 style="margin:0 0 12px;color:#c0392b;font-size:19px;">⚠️ El checkout no está respondiendo bien</h2>
    <p style="line-height:1.7;color:#4a4a58;margin:0 0 18px;">
      Detectado el <b>{cuando}</b>. Mientras dure, algunos compradores podrían
      no ver la opción de envío al pagar.
    </p>
    <div style="background:#faf7ff;border-left:3px solid #7c5cf6;padding:14px 16px;
                color:#3a3a48;font-size:14px;line-height:1.6;">{motivo}</div>
    <p style="margin:22px 0 0;line-height:1.7;color:#4a4a58;font-size:14px;">
      Qué mirar: entrá al panel y probá <b>Refrescar tarifas</b> en la Bandeja.
      Si las tarifas guardadas están al día, el checkout responde igual aunque
      el courier esté caído.
    </p>
    <p style="text-align:center;margin:26px 0 0;">
      <a href="https://taurosolutions.ar/admin/bandeja"
         style="background:#7c5cf6;color:#fff;padding:13px 30px;text-decoration:none;
                font-weight:600;border-radius:999px;display:inline-block;">Abrir el panel</a>
    </p>
  </div>
  <div style="background:#0c0a14;padding:16px;text-align:center;font-size:11px;color:#8b86a0;">
    Centinela automático · Tauro Solutions
  </div>
</div></body></html>"""
    try:
        from core.email_sender import _enviar_mail_a
        _enviar_mail_a(destino, "⚠️ TAURO: el checkout no responde bien", cuerpo)
    except Exception as e:
        print(f"[centinela] no pude mandar la alerta: {e}")


def revisar_checkout() -> None:
    global _fallos_seguidos, _ya_avise

    ok, detalle = _probar_checkout()

    if ok:
        if _ya_avise:
            print(f"[centinela] checkout recuperado — {detalle}")
        _fallos_seguidos = 0
        _ya_avise = False
        return

    _fallos_seguidos += 1
    print(f"[centinela] fallo {_fallos_seguidos}: {detalle}")

    if _fallos_seguidos >= 2 and not _ya_avise:
        _avisar(detalle)
        _ya_avise = True
        print("[centinela] alerta enviada por email")
