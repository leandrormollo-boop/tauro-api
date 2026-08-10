# ============================================================
# Rastreo público — el cliente pega su número y ve dónde está
# ============================================================
# Vive en la web pública (sin login), así que devuelve SÓLO lo que cualquier
# tracking muestra: courier, ciudad de origen/destino, estado y fecha. NUNCA
# nombre, dirección, teléfono, email, precio ni qué se compró — eso es del
# cliente, no del que pega un número.
#
# Qué sabe la plataforma vs. qué sabe el courier: nuestra base llega hasta
# "Despachado". El detalle en tránsito / aduana / entrega lo tiene el courier
# en vivo — por eso siempre se devuelve la URL oficial para el botón "ver
# detalle". Cuando entren las credenciales de cada courier, acá se puede sumar
# una llamada a courier.track() para traer los hitos reales (ver TODO abajo).
# ============================================================
from __future__ import annotations

from core.database import get_conn
from servicios.couriers_urls import (
    detectar_courier, nombre_courier, normalizar_tracking, url_tracking,
)

# Estado interno → (etiqueta amigable, etapa 0..4 de la barra de progreso).
# Las 5 etapas visibles: Reservado(0) · Recogida(1) · En tránsito(2) ·
# En aduana(3) · Entregado(4). Desde nuestra base sólo llegamos hasta "en
# tránsito"; de ahí en más lo cuenta el courier.
_ESTADOS = {
    "SOLICITADO": ("Reservado", 0),
    "EN_PROCESO": ("En preparación", 1),
    "EMITIENDO":  ("En preparación", 1),
    "VERIFICAR_COURIER": ("Verificando con el courier", 1),
    "GUIA_LISTA": ("Listo para despacho", 1),
    "DESPACHADO": ("Despachado — en camino", 2),
}


def rastrear_publico(nro: str) -> dict:
    """
    Devuelve el estado del envío para mostrar en la web, sin datos personales.

    - Si el número es de un envío nuestro: courier exacto + origen/destino
      (ciudad) + estado + fecha + URL del courier.
    - Si no está en la base: se adivina el courier por el formato del número y
      se da sólo la URL oficial (no tenemos nada más que mostrar).
    - Si ni el formato es concluyente: encontrado=False y courier=None.
    """
    t = normalizar_tracking(nro)
    if not t or len(t) < 6 or len(t) > 40:
        return {"ok": False, "error": "Ingresá un número de seguimiento válido."}

    fila = None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Por tracking EXACTO, sin cliente_id (es público). Se leen sólo
                # las columnas no-sensibles: nada de dest_nombre/direccion/etc.
                cur.execute("""
                    SELECT courier, estado, destino_pais, dest_ciudad,
                           remitente_ciudad, remitente_pais, tracking,
                           guia_generada_at, updated_at
                    FROM solicitudes_guia
                    WHERE tracking = %s
                    LIMIT 1
                """, (t,))
                fila = cur.fetchone()
    except Exception as e:
        print(f"[rastreo] lookup falló para {t[:6]}…: {type(e).__name__}")

    if fila:
        courier = (fila.get("courier") or "FEDEX").strip().upper()
        etiqueta, etapa = _ESTADOS.get(
            (fila.get("estado") or "").strip().upper(), ("En preparación", 0))
        origen = (fila.get("remitente_ciudad") or "").strip() or "Argentina"
        dest_ciudad = (fila.get("dest_ciudad") or "").strip()
        destino_pais = (fila.get("destino_pais") or "").strip().upper()
        destino = ", ".join(p for p in (dest_ciudad, destino_pais) if p) or destino_pais or "Destino"
        fecha = fila.get("guia_generada_at") or fila.get("updated_at")
        return {
            "ok": True,
            "encontrado": True,
            "courier": courier,
            "courier_nombre": nombre_courier(courier),
            "origen": origen,
            "destino": destino,
            "estado": etiqueta,
            "etapa": etapa,
            "fecha": fecha.strftime("%d/%m/%Y %H:%M") if fecha else "",
            "url_courier": url_tracking(courier, t),
            # TODO (credenciales): si el courier tiene .track() disponible,
            # traer los hitos reales acá y devolver una lista `hitos`.
        }

    # No es un envío nuestro: adivinar el courier por el formato del número.
    courier = detectar_courier(t)
    if courier:
        return {
            "ok": True,
            "encontrado": False,
            "courier": courier,
            "courier_nombre": nombre_courier(courier),
            "url_courier": url_tracking(courier, t),
        }

    return {
        "ok": True,
        "encontrado": False,
        "courier": None,
        "mensaje": "No pudimos identificar el courier por el número. "
                   "Revisá que esté completo o escribinos.",
    }
