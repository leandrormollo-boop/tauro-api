# ============================================================
# De venta de la tienda a solicitud de guía, sin intervención.
# ============================================================
# Decisión de producto (28/07): cuando entra una venta por Shopify o
# Tiendanube, la solicitud se arma SOLA y queda esperando en el portal.
# El comerciante ya no tiene que apretar "Preparar envío" ni retipear
# nada: sólo revisa y genera la guía.
#
# LA GUÍA NO SE EMITE SOLA, a propósito. Emitir cuesta plata real en el
# courier y no es reversible: una dirección mal cargada por el comprador
# se convertiría en una guía facturada que hay que cancelar a mano.
#
# CUANDO FALTA UN DATO NO SE INVENTA NADA. El pedido queda PENDIENTE con
# el motivo escrito, visible en el portal, para que el comerciante lo
# complete. Los tres motivos reales:
#   - un SKU del carrito no está en el catálogo (sin medidas no hay cómo
#     cotizar ni declarar en aduana),
#   - el cliente no tiene remitente cargado,
#   - no hay ruta activa para ese país.
# ============================================================
from __future__ import annotations

from typing import Optional

from core.database import get_conn


def _motivo(pedido_id: int, texto: str) -> dict:
    """Deja el porqué escrito en el pedido: sin esto, 'no se armó' es mudo."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pedidos_tienda SET motivo_pendiente = %s WHERE id = %s",
                    (texto[:400], pedido_id),
                )
    except Exception as e:
        print(f"[solicitud_auto] no pude guardar el motivo del pedido {pedido_id}: {e}")
    print(f"[solicitud_auto] pedido {pedido_id} queda PENDIENTE: {texto}")
    return {"ok": False, "solicitud_id": None, "motivo": texto}


def crear_desde_pedido(pedido_id: int) -> dict:
    """
    Arma la solicitud de guía a partir de un pedido ya guardado.

    Idempotente: si el pedido ya se convirtió, no hace nada. Eso importa
    porque Shopify reintenta webhooks y manda `orders/updated` seguido.
    """
    from servicios.api_b2b import obtener_precio_envio_multi
    from servicios.catalogo import get_productos
    from servicios.direcciones import obtener_remitente_para_envio
    from servicios.integraciones_tienda import marcar_convertido
    from servicios.solicitudes_guia import crear_solicitud_guia

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, cliente_id, estado, numero, destinatario, items,
                       valor_total, moneda, solicitud_id
                FROM pedidos_tienda WHERE id = %s
            """, (pedido_id,))
            ped = cur.fetchone()

    if not ped:
        return {"ok": False, "solicitud_id": None, "motivo": "el pedido no existe"}
    if ped["estado"] != "PENDIENTE" or ped["solicitud_id"]:
        return {"ok": True, "solicitud_id": ped["solicitud_id"], "motivo": "ya convertido"}

    cliente = (ped["cliente_id"] or "").strip()
    if not cliente:
        return _motivo(pedido_id, "La tienda no está vinculada a una cuenta TAURO.")

    dest = ped["destinatario"] or {}
    pais = (dest.get("pais") or "").upper()
    if not pais:
        return _motivo(pedido_id, "El pedido no trae país de destino.")

    # ── Productos: cada SKU del carrito tiene que existir en el catálogo ──
    # El catálogo es lo único que tiene medidas y HS code; sin eso no se
    # puede ni cotizar ni armar la factura de aduana.
    catalogo = {p.alias_interno.strip().upper(): p for p in get_productos(cliente)}
    filas, faltantes = [], []
    for it in (ped["items"] or []):
        sku = str(it.get("sku") or "").strip().upper()
        cant = max(int(it.get("cantidad") or 1), 1)
        if sku and sku in catalogo:
            filas.append({"producto": catalogo[sku].alias_interno, "cantidad": cant})
        else:
            faltantes.append(it.get("sku") or it.get("titulo") or "(sin SKU)")

    if faltantes:
        return _motivo(pedido_id,
                       f"Estos productos no están en tu catálogo: "
                       f"{', '.join(str(f) for f in faltantes[:6])}. "
                       f"Cargalos con el MISMO SKU que usás en tu tienda y el envío "
                       f"se arma solo la próxima vez.")
    if not filas:
        return _motivo(pedido_id, "El pedido no tiene productos despachables.")

    # ── Remitente ──
    remitente = obtener_remitente_para_envio(cliente, None)
    if not remitente or not (remitente.get("direccion") or "").strip():
        return _motivo(pedido_id,
                       "No tenés un remitente cargado. Agregá tu dirección en la "
                       "libreta del portal y el envío se arma solo.")

    # ── Precio ──
    try:
        precio = obtener_precio_envio_multi(cliente, pais, filas)
    except Exception as e:
        return _motivo(pedido_id, f"No se pudo cotizar: {type(e).__name__}: {e}")
    if not precio.get("encontrado"):
        return _motivo(pedido_id,
                       f"No se pudo cotizar el envío a {pais}: "
                       f"{precio.get('motivo') or 'sin tarifa disponible'}.")

    bultos = precio.get("bultos") or []
    primero = bultos[0] if bultos else {}

    try:
        creada = crear_solicitud_guia(
            cliente_id=cliente,
            producto_alias=primero.get("producto_alias") or filas[0]["producto"],
            cantidad=sum(b.get("cantidad", 1) for b in bultos) or len(filas),
            destino_pais=pais,
            remitente_alias=remitente.get("alias") or remitente.get("label") or "",
            remitente_nombre=remitente.get("nombre") or "",
            remitente_documento=remitente.get("documento") or "",
            remitente_email=remitente.get("email") or "",
            remitente_telefono=remitente.get("telefono") or "",
            remitente_direccion=remitente.get("direccion") or "",
            remitente_ciudad=remitente.get("ciudad") or "",
            remitente_estado=remitente.get("estado") or "",
            remitente_zip=remitente.get("cp") or "",
            remitente_pais=remitente.get("pais") or "AR",
            dest_nombre=dest.get("nombre") or "",
            dest_documento="",
            dest_email=dest.get("email") or "",
            dest_telefono=dest.get("telefono") or "",
            # La línea 2 (piso/depto) va pegada acá porque crear_solicitud_guia
            # no la recibe aparte; separarlas es lo que hace que el paquete
            # llegue al edificio pero no al departamento.
            dest_direccion=" - ".join(x for x in [dest.get("direccion"),
                                                  dest.get("direccion2")] if x),
            dest_ciudad=dest.get("ciudad") or "",
            dest_estado=dest.get("estado") or "",
            dest_zip=dest.get("cp") or "",
            observaciones=f"Generado automáticamente desde la venta {ped['numero'] or ''}".strip(),
            peso_kg=precio.get("peso_total_kg") or 0.5,
            largo_cm=primero.get("largo_cm") or 30,
            ancho_cm=primero.get("ancho_cm") or 20,
            alto_cm=primero.get("alto_cm") or 15,
            valor_declarado_usd=precio.get("valor_total_usd") or 100,
            ruta_id=precio["ruta_id"],
            coti_id=precio["coti_id"],
            precio_tauro_ars=precio["precio_ars"],
            precio_tauro_usd=precio["precio_usd"],
            bultos=bultos or None,
        )
    except Exception as e:
        return _motivo(pedido_id, f"No se pudo crear la solicitud: {type(e).__name__}: {e}")

    sid = creada.get("id")
    try:
        marcar_convertido(cliente, pedido_id, solicitud_id=sid)
    except Exception as e:
        print(f"[solicitud_auto] solicitud {sid} creada pero no pude marcar "
              f"convertido el pedido {pedido_id}: {e}")

    print(f"[solicitud_auto] pedido {pedido_id} ({ped['numero']}) → solicitud {sid}, "
          f"lista para generar la guía")
    return {"ok": True, "solicitud_id": sid, "motivo": ""}


def intentar_en_segundo_plano(pedido_id: int) -> None:
    """
    Se dispara desde el webhook. Va en un hilo porque la tienda espera un
    200 rápido: cotizar puede tardar segundos y demasiados timeouts hacen
    que la plataforma dé de baja el webhook.
    """
    import threading

    def _run():
        try:
            crear_desde_pedido(pedido_id)
        except Exception as e:
            print(f"[solicitud_auto] falló armando el pedido {pedido_id}: "
                  f"{type(e).__name__}: {e}")

    threading.Thread(target=_run, daemon=True).start()
