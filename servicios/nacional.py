# ============================================================
# Envíos NACIONALES (AR→AR) vía envia.com — cotización con el
# pricing de cada cliente, mismo modelo que internacional:
# costo mayorista + markup TAURO = precio final del cliente.
# ============================================================

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from core.database import get_conn
from core.envia_client import EnviaClient
from servicios.auth import get_markup_pct
from servicios.catalogo import get_producto
from servicios.cotizador import _get_dolar_ars, COTIZACION_VALIDA_HORAS
from servicios.pricing import aplicar_pricing, get_pricing_config

MAX_KG_POR_CAJA_NAC = 50   # límite operativo courier nacional
MAX_CAJAS_POR_ENVIO_NAC = 20


def nacional_activo() -> bool:
    """La pata nacional se prende sola cuando ENVIA_API_KEY está en el entorno."""
    return EnviaClient().configurado


def cotizar_nacional_cliente(
    cliente_id: str,
    origen: dict,
    destino: dict,
    bultos: list,
) -> dict:
    """
    Cotiza un envío nacional para un cliente del portal.

    bultos: [{producto (alias del catálogo), cantidad}, ...] — igual que
    el multi-bulto internacional; cada fila son cajas idénticas.
    destino: {cp, ciudad, provincia} (provincia por nombre, se mapea a ISO).
    origen: remitente del cliente {nombre, direccion/calle, ciudad,
    provincia/estado, cp, telefono, email}.

    Devuelve {encontrado, opciones: [...con precio_final_ars...], coti_id}.
    """
    cliente_id = (cliente_id or "").strip().upper()
    if not bultos:
        return {"encontrado": False, "motivo": "sin_bultos"}
    if len(bultos) > MAX_CAJAS_POR_ENVIO_NAC:
        return {"encontrado": False,
                "motivo": f"máximo {MAX_CAJAS_POR_ENVIO_NAC} cajas por envío"}

    piezas, detalle = [], []
    total_cajas = 0
    peso_total = 0.0
    valor_total_ars = 0.0
    dolar = _get_dolar_ars()

    for b in bultos:
        alias = str(b.get("producto") or b.get("producto_alias") or "").strip()
        cantidad = max(int(b.get("cantidad") or 1), 1)

        # El catálogo es OPCIONAL (Leandro, 06/08). Con alias, los datos salen
        # del producto aprobado y lo que venga en la fila los PISA (override de
        # este envío). Sin alias, la caja se declara entera a mano.
        producto = get_producto(cliente_id, alias) if alias else None
        if alias and (not producto or not producto.activo):
            return {"encontrado": False, "motivo": f"producto_no_encontrado: {alias}"}

        def _num(clave, del_producto, por_defecto=0.0):
            """Lo cargado a mano manda; si no, el catálogo; si no, el default."""
            crudo = b.get(clave)
            if crudo not in (None, ""):
                try:
                    return float(crudo)
                except (TypeError, ValueError):
                    pass
            return float(del_producto if del_producto is not None else por_defecto)

        peso_kg = _num("peso_kg", producto.peso_kg if producto else None)
        largo_cm = _num("largo_cm", producto.largo_cm if producto else None)
        ancho_cm = _num("ancho_cm", producto.ancho_cm if producto else None)
        alto_cm = _num("alto_cm", producto.alto_cm if producto else None)
        valor_usd = _num("valor_unitario_usd",
                         producto.valor_usd_default if producto else None)
        descripcion = (str(b.get("descripcion_en") or "").strip()
                       or (producto.nombre_invoice or producto.alias_interno if producto else "")
                       or "Mercadería")

        etiqueta = alias or descripcion
        if peso_kg <= 0:
            return {"encontrado": False, "motivo": f"sin_peso: {etiqueta}"}
        if peso_kg > MAX_KG_POR_CAJA_NAC:
            return {"encontrado": False,
                    "motivo": f"cada caja de {etiqueta} pesa {peso_kg}kg; máximo nacional {MAX_KG_POR_CAJA_NAC}kg"}

        total_cajas += cantidad
        peso_total += peso_kg * cantidad
        valor_unitario_ars = round(valor_usd * dolar, 2)
        valor_total_ars += valor_unitario_ars * cantidad
        piezas.append({
            "descripcion": descripcion,
            "cantidad": cantidad,
            "largo_cm": largo_cm,
            "ancho_cm": ancho_cm,
            "alto_cm": alto_cm,
            "peso_kg": peso_kg,
            "valor_declarado_ars": valor_unitario_ars,
        })
        detalle.append({
            # Sin catálogo no hay alias: se guarda la descripción para que el
            # listado y el admin no muestren un renglón en blanco.
            "producto_alias": producto.alias_interno if producto else descripcion[:40],
            "cantidad": cantidad,
            "peso_kg": peso_kg,
            "largo_cm": largo_cm,
            "ancho_cm": ancho_cm,
            "alto_cm": alto_cm,
            "valor_unitario_usd": valor_usd,
            "hs_code": (str(b.get("hs_code") or "").strip()
                        or (producto.hs_code if producto else "")),
            "descripcion_en": descripcion,
        })

    if total_cajas > MAX_CAJAS_POR_ENVIO_NAC:
        return {"encontrado": False,
                "motivo": f"{total_cajas} cajas superan el máximo de {MAX_CAJAS_POR_ENVIO_NAC} por envío"}

    resultado = EnviaClient().cotizar_nacional(origen, destino, piezas)
    if not resultado.get("encontrado"):
        return {"encontrado": False, "motivo": resultado.get("error") or "sin_cobertura"}

    # Margen NACIONAL del cliente (si lo tiene cargado; si no, cae al
    # internacional de siempre). Sumar el margen internacional acá casi
    # triplicaba el precio de un envío dentro del país.
    pricing = get_pricing_config(cliente_id, fallback_pct=get_markup_pct(cliente_id),
                                 ambito="nacional")
    valida_hasta = (
        datetime.now(tz=timezone.utc) + timedelta(hours=COTIZACION_VALIDA_HORAS)
    ).isoformat(timespec="seconds")
    coti_id = uuid.uuid4().hex[:16]

    opciones = []
    for op in resultado["opciones"]:
        costo_ars = op["costo_ars"]
        costo_usd = round(costo_ars / dolar, 2) if dolar else 0.0
        precio = aplicar_pricing(
            costo_usd=costo_usd, costo_ars=costo_ars, dolar=dolar, pricing=pricing,
        )
        opciones.append({
            **op,
            "precio_final_ars": precio["precio_final_ars"],
            "precio_final_usd": precio["precio_final_usd"],
        })
    opciones.sort(key=lambda o: o["precio_final_ars"])

    # Log en cotizaciones (misma tabla; la "ruta" nacional es CP→CP)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cotizaciones
                        (coti_id, cliente_id, ruta_id, peso_kg, dimensiones, peso_usado_kg,
                         costo_fedex_usd, markup_pct, markup_tipo, markup_valor,
                         precio_final_usd, precio_final_ars, dias_estimados, valida_hasta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coti_id, cliente_id,
                        f"NAC-{destino.get('cp', '')}",
                        round(peso_total, 2),
                        " + ".join(f"{p['cantidad']}x({p['largo_cm']}x{p['ancho_cm']}x{p['alto_cm']})" for p in piezas)[:200],
                        round(peso_total, 2),
                        round(opciones[0]["costo_ars"] / dolar, 2) if dolar else 0,
                        0, pricing.get("tipo") or "PCT", pricing.get("valor"),
                        opciones[0]["precio_final_usd"], opciones[0]["precio_final_ars"],
                        None, valida_hasta,
                    ),
                )
    except Exception as e:
        print(f"[nacional] no se pudo loguear cotización: {e}")

    return {
        "encontrado": True,
        "coti_id": coti_id,
        "valida_hasta": valida_hasta,
        "piezas_total": total_cajas,
        "peso_total_kg": round(peso_total, 2),
        "valor_total_ars": round(valor_total_ars, 2),
        "bultos": detalle,
        "opciones": opciones,
    }
