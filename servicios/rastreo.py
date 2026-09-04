# ============================================================
# Rastreo público — el cliente pega su número y ve dónde está
# ============================================================
# Vive en la web pública (sin login), así que devuelve SÓLO lo que cualquier
# tracking muestra: courier, ciudad de origen/destino, estado y fecha. NUNCA
# nombre, dirección, teléfono, email, precio ni qué se compró — eso es del
# cliente, no del que pega un número.
#
# Qué sabe la plataforma vs. qué sabe el courier: nuestra base llega hasta
# "Recolectado". El detalle en tránsito / aduana / entrega lo tiene el courier
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
    "DESPACHADO": ("Recolectado — en camino", 2),
}


def _texto(valor, maximo: int = 240) -> str:
    """Normaliza texto proveniente de un courier sin propagar payloads crudos."""
    return str(valor or "").strip()[:maximo]


def _ubicacion_evento(evento: dict) -> str:
    ubicacion = evento.get("location") or evento.get("scanLocation") or {}
    direccion = ubicacion.get("address") or {}
    partes = [
        ubicacion.get("addressLocality"),
        ubicacion.get("city"),
        direccion.get("addressLocality"),
        direccion.get("city"),
        ubicacion.get("stateOrProvinceCode"),
        direccion.get("stateProvince"),
        direccion.get("stateOrProvinceCode"),
        ubicacion.get("countryCode"),
        direccion.get("countryCode"),
    ]
    salida = []
    for parte in partes:
        limpio = _texto(parte, 80)
        if limpio and limpio not in salida:
            salida.append(limpio)
    return ", ".join(salida)[:180]


def _evento_normalizado(evento: dict) -> dict:
    status = evento.get("status") or {}
    codigo = (
        evento.get("statusCode")
        or evento.get("typeCode")
        or evento.get("eventType")
        or status.get("type")
        or status.get("code")
    )
    descripcion = (
        evento.get("description")
        or evento.get("eventDescription")
        or status.get("description")
        or status.get("statusByLocale")
    )
    fecha = (
        evento.get("timestamp")
        or evento.get("date")
        or evento.get("dateTime")
        or evento.get("gmtDateTime")
    )
    # UPS separa fecha YYYYMMDD y hora HHMMSS.
    if evento.get("date") and evento.get("time"):
        fecha = f"{evento['date']}T{evento['time']}"
    return {
        "codigo": _texto(codigo, 40),
        "descripcion": _texto(descripcion),
        "fecha": _texto(fecha, 80),
        "ubicacion": _ubicacion_evento(evento),
    }


def _rastrear_en_courier(courier: str, tracking: str) -> dict | None:
    """Consulta read-only y devuelve un contrato mínimo común.

    Un error del operador no rompe el rastreo: la API conserva el estado que
    TAURO tiene persistido y el enlace oficial. Nunca devuelve el error crudo
    del courier ni sus credenciales/configuración.
    """
    courier = (courier or "").strip().upper()
    try:
        if courier == "DHL":
            from core.dhl_client import DHLClient
            raw = DHLClient().track(tracking)
            if not raw.get("encontrado"):
                return None
            eventos_raw = raw.get("eventos") or []
            estado = raw.get("estado")
            descripcion = raw.get("descripcion")
        elif courier == "UPS":
            from core.ups_client import UPSClient
            raw = UPSClient().track(tracking)
            if not raw.get("encontrado"):
                return None
            eventos_raw = raw.get("eventos") or []
            estado = raw.get("estado")
            descripcion = raw.get("descripcion")
        elif courier == "FEDEX":
            from core.fedex_client import FedExClient
            raw = FedExClient().track(tracking)
            if not raw or raw.get("error"):
                return None
            latest = raw.get("latestStatusDetail") or {}
            eventos_raw = raw.get("scanEvents") or []
            estado = latest.get("code") or latest.get("statusByLocale")
            descripcion = latest.get("description") or latest.get("statusByLocale")
        else:
            return None
    except Exception as exc:
        print(f"[rastreo] consulta live {courier} falló: {type(exc).__name__}")
        return None

    eventos = tuple(
        _evento_normalizado(e)
        for e in eventos_raw[:20]
        if isinstance(e, dict)
    )
    return {
        "estado": _texto(estado, 100),
        "descripcion": _texto(descripcion),
        "eventos": list(eventos),
    }


def rastrear_cliente(cliente_id: str, nro: str, *, actualizar: bool = True) -> dict:
    """Rastreo B2B privado, aislado por dueño de la API key."""
    cliente_id = (cliente_id or "").strip().upper()
    tracking = normalizar_tracking(nro)
    if not cliente_id or not tracking or len(tracking) < 6 or len(tracking) > 40:
        return {"ok": False, "encontrado": False}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, cliente_id, courier, estado, ambito, destino_pais,
                       dest_ciudad, remitente_ciudad, remitente_pais, tracking,
                       guia_generada_at, updated_at
                FROM solicitudes_guia
                WHERE cliente_id=%s AND UPPER(BTRIM(tracking))=%s
                LIMIT 1
                """,
                (cliente_id, tracking),
            )
            fila = cur.fetchone()
    if not fila:
        return {"ok": False, "encontrado": False}

    courier = (fila.get("courier") or "FEDEX").strip().upper()
    etiqueta, etapa = _ESTADOS.get(
        (fila.get("estado") or "").strip().upper(), ("En preparación", 0)
    )
    origen = (fila.get("remitente_ciudad") or "").strip() or "Argentina"
    destino = ", ".join(
        p for p in (
            (fila.get("dest_ciudad") or "").strip(),
            (fila.get("destino_pais") or "").strip().upper(),
        ) if p
    ) or "Destino"
    fecha = fila.get("guia_generada_at") or fila.get("updated_at")
    respuesta = {
        "ok": True,
        "encontrado": True,
        "solicitud_id": fila["id"],
        "tracking": tracking,
        "courier": courier,
        "courier_nombre": nombre_courier(courier),
        "ambito": (fila.get("ambito") or "").strip().lower(),
        "origen": origen,
        "destino": destino,
        "estado": etiqueta,
        "descripcion": "",
        "etapa": etapa,
        "eventos": [],
        "fuente": "tauro",
        "actualizado_en": fecha.isoformat() if hasattr(fecha, "isoformat") else fecha,
        "url_courier": url_tracking(courier, tracking),
    }
    if actualizar:
        live = _rastrear_en_courier(courier, tracking)
        if live:
            respuesta.update({
                "estado": live.get("estado") or respuesta["estado"],
                "descripcion": live.get("descripcion") or "",
                "eventos": live.get("eventos") or [],
                "fuente": "courier",
            })
    return respuesta


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
