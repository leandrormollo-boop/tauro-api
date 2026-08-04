# ============================================================
# Dólar oficial automático — mantiene COTIZACION_DOLAR_ARS al día
# ============================================================
# El tipo de cambio es la ÚNICA fuente de verdad para TODOS los precios
# (cotizador.dolar_ars()). Antes se cargaba a mano en /admin/config: si Leandro
# se olvidaba, se cotizaba con el dólar de hace días y se vendía barato. Este
# servicio lo actualiza solo desde el dólar oficial.
#
# Sensible por diseño: mueve todos los precios. Por eso NO pisa el valor a
# ciegas — valida rango, valida que el salto contra el valor actual sea
# razonable (una API que devuelve basura no debe reventar los precios), y ante
# cualquier duda deja el valor que había y loguea. Se puede apagar con
# DOLAR_AUTO=0 para volver al control manual.
# ============================================================
from __future__ import annotations

import os

import requests

from core.database import get_conn

# Fuente por defecto: dolarapi.com (gratis, JSON, estable). Devuelve
# {compra, venta, fechaActualizacion, ...}. Cambiable por env var.
FUENTE_URL = os.getenv("DOLAR_AUTO_FUENTE", "https://dolarapi.com/v1/dolares/oficial")
# Qué punta usar: TAURO compra USD para pagar a los couriers → "venta".
TIPO = os.getenv("DOLAR_AUTO_TIPO", "venta").strip().lower()
# Salto máximo tolerado contra el valor actual (protege de valores erróneos de
# la API). Un salto real de devaluación grande igual entra si es < este tope;
# si lo supera, NO se pisa solo y se avisa para revisar a mano.
SALTO_MAX_PCT = float(os.getenv("DOLAR_SALTO_MAX_PCT", "50"))
_RANGO = (100.0, 100_000.0)   # mismo rango sano que usa el cotizador


def auto_activo() -> bool:
    return os.getenv("DOLAR_AUTO", "1").strip() != "0"


def consultar_dolar_oficial() -> float | None:
    """Consulta la fuente y devuelve el valor (float) o None si falla/no es sano."""
    try:
        r = requests.get(FUENTE_URL, timeout=12,
                         headers={"User-Agent": "TAURO Solutions"})
        if r.status_code != 200:
            print(f"[dolar] fuente respondió {r.status_code}")
            return None
        data = r.json()
        valor = float(data.get(TIPO) or data.get("venta") or 0)
    except Exception as e:
        print(f"[dolar] no pude consultar la fuente: {type(e).__name__}: {e}")
        return None
    if not (_RANGO[0] <= valor <= _RANGO[1]):
        print(f"[dolar] valor fuera de rango, ignorado: {valor}")
        return None
    return valor


def _valor_actual() -> float | None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT valor FROM config WHERE parametro='COTIZACION_DOLAR_ARS'")
                row = cur.fetchone()
        if row and row["valor"] is not None:
            from servicios.pricing import parse_monto_ars
            return parse_monto_ars(row["valor"])
    except Exception as e:
        print(f"[dolar] no pude leer el valor actual: {e}")
    return None


def _avisar_salto(actual: float, nuevo: float, salto: float, hacia: str) -> None:
    """Mail a Leandro cuando el dólar saltó tanto que no se actualiza solo."""
    destino = (os.getenv("ADMIN_RECOVERY_EMAIL") or "cotizaciones@taurosolutions.ar").strip()
    cuerpo = f"""<html><body style="margin:0;background:#f4f4f6;">
<div style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;">
  <div style="background:#0c0a14;padding:28px 24px;text-align:center;">
    <div style="font-size:22px;font-weight:700;letter-spacing:.08em;color:#fff;">
      TAURO <span style="color:#a78bfa;">SOLUTIONS</span></div></div>
  <div style="padding:30px;">
    <h2 style="margin:0 0 12px;color:#c0392b;font-size:19px;">⚠️ El dólar saltó — revisá los precios</h2>
    <p style="line-height:1.7;color:#4a4a58;margin:0 0 18px;">
      El oficial pasó de <b>$ {actual:,.0f}</b> a <b>$ {nuevo:,.0f}</b>
      (salto de {salto:.0f}% hacia {hacia}). Como supera el tope de seguridad,
      <b>NO se actualizó solo</b>: los precios siguen con $ {actual:,.0f}.
    </p>
    <p style="line-height:1.7;color:#4a4a58;margin:0 0 18px;">
      Si el salto es real, cargá $ {nuevo:,.0f} a mano para no vender bajo costo.
      Si es un error de la fuente, ignoralo.</p>
    <p style="text-align:center;margin:26px 0 0;">
      <a href="https://taurosolutions.ar/admin/config"
         style="background:#7c5cf6;color:#fff;padding:13px 30px;text-decoration:none;
                font-weight:600;border-radius:999px;display:inline-block;">Abrir configuración</a></p>
  </div>
  <div style="background:#0c0a14;padding:16px;text-align:center;font-size:11px;color:#8b86a0;">
    Dólar automático · Tauro Solutions</div>
</div></body></html>""".replace(",", ".")
    try:
        from core.email_sender import _enviar_mail_a
        _enviar_mail_a(destino, "⚠️ TAURO: el dólar saltó, revisá los precios", cuerpo)
    except Exception as e:
        print(f"[dolar] no pude mandar la alerta de salto: {e}")


def actualizar_dolar_auto() -> dict:
    """
    Consulta el oficial y actualiza COTIZACION_DOLAR_ARS si el valor es sano y
    el salto contra el actual es razonable. Devuelve {ok, valor, motivo}.
    Nunca lanza: un fallo deja el valor que había.
    """
    if not auto_activo():
        return {"ok": False, "motivo": "dolar_auto_apagado"}

    nuevo = consultar_dolar_oficial()
    if nuevo is None:
        return {"ok": False, "motivo": "sin_dato_valido"}

    # Referencia para el chequeo de salto: el valor en DB, o —si no hay
    # (primer deploy, o lectura fallida)— el fallback de env. Así la guarda de
    # salto NUNCA se saltea: sin red, un glitch de la fuente (1515 → 15150,
    # dentro del rango amplio) repreciaría todo x10 en el primer arranque.
    actual = _valor_actual()
    referencia = actual if (actual and actual > 0) else \
        float(os.getenv("COTIZACION_DOLAR_ARS", "1450") or 1450)
    if referencia and referencia > 0:
        salto = abs(nuevo - referencia) / referencia * 100
        if salto > SALTO_MAX_PCT:
            # No se pisa solo un salto enorme (posible error de la fuente o
            # devaluación fuerte): queda el valor actual y se AVISA por mail.
            # En Argentina un salto real >50% pasa (dic-2023): dejar el dólar
            # viejo vende barato, así que la alerta tiene que llegar de verdad,
            # no sólo al log. Hacia arriba es urgente (se vende bajo costo).
            hacia = "ARRIBA (riesgo de vender bajo costo)" if nuevo > referencia else "abajo"
            print(f"[dolar] ALERTA: salto de {salto:.0f}% ({referencia} → {nuevo}) "
                  f"hacia {hacia}, supera el tope de {SALTO_MAX_PCT:.0f}%. NO se "
                  f"actualizó solo; revisar y cargar a mano en /admin/config.")
            _avisar_salto(referencia, nuevo, salto, hacia)
            return {"ok": False, "motivo": "salto_excesivo", "valor": nuevo, "actual": actual}
        if actual and abs(nuevo - actual) < 0.5:
            return {"ok": True, "motivo": "sin_cambios", "valor": nuevo}

    valor_str = str(int(round(nuevo)))
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO config (parametro, valor) VALUES ('COTIZACION_DOLAR_ARS', %s) "
                    "ON CONFLICT (parametro) DO UPDATE SET valor=EXCLUDED.valor",
                    (valor_str,),
                )
                # Rastro de origen para mostrar en el admin (auto vs manual).
                cur.execute(
                    "INSERT INTO config (parametro, valor) VALUES ('COTIZACION_DOLAR_ORIGEN', %s) "
                    "ON CONFLICT (parametro) DO UPDATE SET valor=EXCLUDED.valor",
                    (f"auto:{TIPO} · {FUENTE_URL.split('//')[-1].split('/')[0]}",),
                )
    except Exception as e:
        print(f"[dolar] no pude guardar el valor: {e}")
        return {"ok": False, "motivo": "error_guardando", "valor": nuevo}

    print(f"[dolar] actualizado a ${valor_str} (oficial {TIPO})"
          + (f" desde ${actual:.0f}" if actual else ""))
    return {"ok": True, "motivo": "actualizado", "valor": nuevo}
