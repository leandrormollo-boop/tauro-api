# ============================================================
# Centinela de ventas
# ============================================================
# Cada 15 minutos se pregunta lo único que de verdad importa:
# "si a un comerciante le entrara una venta ahora mismo, ¿la
# recibiríamos y quedaría lista para generar la guía?".
#
# QUÉ CAMBIÓ (28/07): antes vigilaba que el checkout devolviera una
# tarifa, porque la app cotizaba el envío dentro del checkout del
# comprador. Esa función se retiró: el precio lo pone el comerciante
# con sus tarifas de Shopify y TAURO sólo toma la venta. Vigilar la
# cotización pasó a ser vigilar algo que ya no le pasa a nadie.
#
# Ahora se vigilan las tres formas reales de perder una venta:
#   1. No poder procesar el webhook (base caída, parseo roto).
#   2. Tiendas conectadas SIN vincular a una cuenta: sus ventas se
#      descartan y Shopify termina dando de baja el webhook.
#   3. Pedidos que entraron y quedaron pendientes demasiado tiempo:
#      llegaron bien pero nadie los está despachando.
#
# Se avisa recién al segundo fallo seguido: un parpadeo de red no es
# una caída, y un centinela que grita por cada hipo se vuelve ruido
# que nadie mira.
#
# Alcance: corre DENTRO de la app, así que detecta fallas parciales.
# Una caída total del proceso la cubre el healthcheck de Railway.
# ============================================================
from __future__ import annotations

import os
from datetime import datetime

_fallos_seguidos = 0
_ya_avise = False

# Un pedido que lleva más de esto sin convertirse en guía es una venta
# que el comprador ya pagó y nadie despachó.
HORAS_PEDIDO_VIEJO = 48


def _probar_recepcion() -> tuple[bool, str]:
    """
    Simula el camino completo de un webhook de venta, sin escribir nada:
    parsear el pedido y confirmar que la base contesta.
    """
    from servicios.integraciones_tienda import parsear_pedido_shopify
    from core.database import get_conn

    orden = {
        "id": 0, "name": "#CENTINELA", "email": "centinela@taurosolutions.ar",
        "total_price": "1000.00", "currency": "ARS",
        "shipping_address": {"name": "Centinela", "address1": "Av Corrientes 1234",
                             "city": "MIAMI", "province_code": "FL",
                             "zip": "33101", "country_code": "US"},
        "line_items": [{"title": "Test", "quantity": 1, "price": "1000",
                        "sku": "CENTINELA", "grams": 1000}],
        "shipping_lines": [{"title": "Envío", "code": "X", "price": "100.00"}],
    }
    try:
        p = parsear_pedido_shopify(orden)
    except Exception as e:
        return False, f"el parseo de un pedido falla: {type(e).__name__}: {e}"
    if not p or not p.get("destinatario", {}).get("direccion"):
        return False, "el parseo de un pedido devolvió datos incompletos"

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pedidos_tienda LIMIT 1")
                cur.fetchone()
    except Exception as e:
        return False, (f"no se puede leer la tabla de pedidos: {type(e).__name__}: {e}. "
                       f"Una venta que entre ahora se pierde.")
    return True, "recepción OK"


def _revisar_tiendas_sin_vincular() -> tuple[bool, str]:
    """
    Una tienda instalada pero sin cuenta TAURO asociada recibe webhooks que
    no se pueden atribuir a nadie. Cada venta que entra se descarta, y a las
    ocho fallas Shopify da de baja la suscripción — se pierde el canal
    entero, no una venta.
    """
    from core.database import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT dominio FROM shopify_instalaciones
                    WHERE access_token IS NOT NULL
                      AND (cliente_id IS NULL OR cliente_id = '')
                """)
                sueltas = [r["dominio"] for r in cur.fetchall()]
    except Exception:
        return True, ""      # ya lo reporta _probar_recepcion
    if sueltas:
        return False, (f"{len(sueltas)} tienda(s) instalada(s) SIN vincular a una "
                       f"cuenta: {', '.join(sueltas[:5])}. Sus ventas no se están "
                       f"guardando y Shopify puede dar de baja el webhook.")
    return True, ""


def _revisar_pedidos_viejos() -> tuple[bool, str]:
    from core.database import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM pedidos_tienda
                    WHERE estado = 'PENDIENTE'
                      AND created_at < NOW() - INTERVAL '%s hours'
                """ % int(HORAS_PEDIDO_VIEJO))
                n = int((cur.fetchone() or {}).get("n") or 0)
    except Exception:
        return True, ""
    if n:
        return False, (f"{n} pedido(s) llevan más de {HORAS_PEDIDO_VIEJO} h esperando "
                       f"que se genere la guía. El comprador ya pagó.")
    return True, ""


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
    <h2 style="margin:0 0 12px;color:#c0392b;font-size:19px;">⚠️ Hay ventas en riesgo</h2>
    <p style="line-height:1.7;color:#4a4a58;margin:0 0 18px;">
      Detectado el <b>{cuando}</b>.
    </p>
    <div style="background:#faf7ff;border-left:3px solid #7c5cf6;padding:14px 16px;
                color:#3a3a48;font-size:14px;line-height:1.6;">{motivo}</div>
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
        _enviar_mail_a(destino, "⚠️ TAURO: hay ventas en riesgo", cuerpo)
    except Exception as e:
        print(f"[centinela] no pude mandar la alerta: {e}")


def revisar_checkout() -> None:
    """Nombre histórico: lo llama el scheduler de main.py."""
    global _fallos_seguidos, _ya_avise

    problemas = []
    for chequeo in (_probar_recepcion, _revisar_tiendas_sin_vincular,
                    _revisar_pedidos_viejos):
        try:
            ok, detalle = chequeo()
        except Exception as e:
            ok, detalle = False, f"{chequeo.__name__} falló: {type(e).__name__}: {e}"
        if not ok and detalle:
            problemas.append(detalle)

    if not problemas:
        if _ya_avise:
            print("[centinela] recuperado: las ventas entran y se procesan bien")
        _fallos_seguidos = 0
        _ya_avise = False
        return

    _fallos_seguidos += 1
    detalle = " · ".join(problemas)
    print(f"[centinela] fallo {_fallos_seguidos}: {detalle}")

    if _fallos_seguidos >= 2 and not _ya_avise:
        _avisar("<br>".join(f"• {p}" for p in problemas))
        _ya_avise = True
        print("[centinela] alerta enviada por email")
