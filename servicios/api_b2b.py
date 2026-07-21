# ============================================================
# Servicio API B2B — PostgreSQL
# ============================================================

from core.database import get_conn
from modelos.cotizacion import CotizacionInput
from servicios.auth import get_markup_pct
from servicios.catalogo import get_producto
from servicios.cotizador import cotizar, cotizar_bultos, _get_dolar_ars
from servicios.rutas import get_rutas_activas, pais_a_iso2

# Límites multi-bulto: FedEx IP admite hasta 70 kg por pieza; el tope de
# cajas por envío es una guarda operativa nuestra (no de FedEx).
MAX_KG_POR_CAJA = 70
MAX_CAJAS_POR_ENVIO = 20


def obtener_cliente_por_api_key(api_key: str) -> dict:
    """Valida una API key contra PostgreSQL y devuelve el perfil del cliente."""
    api_key = (api_key or "").strip()
    if not api_key:
        return {"encontrado": False}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cliente_id, email, api_key, markup_pct, markup_tipo, markup_valor, activo, nombre, cuit,
                       direccion, cp, ciudad, pais, telefono, notas
                FROM clientes
                WHERE api_key = %s AND activo = TRUE
                LIMIT 1
                """,
                (api_key,),
            )
            row = cur.fetchone()

    if not row:
        return {"encontrado": False}

    cliente_id = str(row["cliente_id"]).strip().upper()
    return {
        "encontrado": True,
        "cliente_id": cliente_id,
        "nombre": row.get("nombre") or cliente_id,
        "cuit": row.get("cuit") or "",
        "direccion": row.get("direccion") or "",
        "cp": row.get("cp") or "",
        "ciudad": row.get("ciudad") or "BUENOS AIRES",
        "pais": row.get("pais") or "AR",
        "telefono": row.get("telefono") or "",
        "email": row.get("email") or "",
        "markup_pct": float(row.get("markup_pct") or 25.0),
        "markup_tipo": row.get("markup_tipo") or "PCT",
        "markup_valor": row.get("markup_valor"),
    }


def _normalizar_pais(valor: str) -> str:
    return pais_a_iso2((valor or "").strip().upper())


def buscar_ruta_para_destino(destino_pais: str):
    """Busca una ruta activa desde Argentina hacia el destino pedido."""
    destino_iso = _normalizar_pais(destino_pais)
    for ruta in get_rutas_activas():
        origen_iso = _normalizar_pais(ruta.origen_pais)
        ruta_destino_iso = _normalizar_pais(ruta.destino_pais)
        if origen_iso == "AR" and ruta_destino_iso == destino_iso:
            return ruta

    # Fallback: si no hay origen Argentina, usar cualquier ruta activa al destino.
    for ruta in get_rutas_activas():
        if _normalizar_pais(ruta.destino_pais) == destino_iso:
            return ruta
    return None


def obtener_datos_producto(cliente_id: str, producto_id: str) -> dict:
    producto = get_producto(cliente_id, producto_id)
    if not producto or not producto.activo:
        return {"encontrado": False}

    return {
        "encontrado": True,
        "nombre_es": producto.alias_interno,
        "nombre_en": producto.nombre_invoice,
        "hs_code": producto.hs_code,
        "valor_usd": producto.valor_usd_default,
        "unidades": 1,
        "peso_kg": producto.peso_kg,
        "largo": producto.largo_cm,
        "ancho": producto.ancho_cm,
        "alto": producto.alto_cm,
    }


def obtener_precio_envio_multi(
    cliente_id: str, destino_pais: str, bultos: list
) -> dict:
    """
    Cotiza un envío MULTI-BULTO en vivo: lista de cajas del catálogo
    [{producto (alias), cantidad (cajas idénticas)}, ...] + destino.
    Cada caja viaja como pieza con su propio label. El peso facturable se
    calcula por caja; FedEx tarifa el conjunto y se aplica el pricing del
    cliente al total.
    """
    if not bultos:
        return {"encontrado": False, "motivo": "sin_bultos"}
    # Guarda temprana: cada fila es al menos una caja, así que más filas que
    # el tope de cajas nunca puede cotizar (y evita N lookups al pedo).
    if len(bultos) > MAX_CAJAS_POR_ENVIO:
        return {
            "encontrado": False,
            "motivo": f"peso_excedido: máximo {MAX_CAJAS_POR_ENVIO} cajas por envío. Dividí en dos envíos.",
        }

    ruta = buscar_ruta_para_destino(destino_pais)
    if not ruta:
        return {"encontrado": False, "motivo": "ruta_no_encontrada"}

    piezas, detalle = [], []
    total_cajas = 0
    valor_total_usd = 0.0
    for b in bultos:
        alias = str(b.get("producto") or b.get("producto_alias") or "").strip()
        cantidad = max(int(b.get("cantidad") or 1), 1)
        producto = get_producto(cliente_id, alias)
        if not producto or not producto.activo:
            return {"encontrado": False, "motivo": f"producto_no_encontrado: {alias}"}
        if producto.peso_kg > MAX_KG_POR_CAJA:
            return {
                "encontrado": False,
                "motivo": f"peso_excedido: cada caja de {alias} pesa {producto.peso_kg}kg y el máximo por caja es {MAX_KG_POR_CAJA}kg.",
            }
        total_cajas += cantidad
        valor_total_usd += producto.valor_usd_default * cantidad
        piezas.append({
            "peso_kg": producto.peso_kg,
            "largo_cm": producto.largo_cm,
            "ancho_cm": producto.ancho_cm,
            "alto_cm": producto.alto_cm,
            "valor_unitario_usd": producto.valor_usd_default,
            "unidades": cantidad,
            "hs_code": producto.hs_code,
            "descripcion_en": producto.nombre_invoice,
        })
        detalle.append({
            "producto_alias": producto.alias_interno,
            "cantidad": cantidad,
            "peso_kg": producto.peso_kg,
            "largo_cm": producto.largo_cm,
            "ancho_cm": producto.ancho_cm,
            "alto_cm": producto.alto_cm,
            "valor_unitario_usd": producto.valor_usd_default,
            "hs_code": producto.hs_code,
            "descripcion_en": producto.nombre_invoice,
        })

    if total_cajas > MAX_CAJAS_POR_ENVIO:
        return {
            "encontrado": False,
            "motivo": f"peso_excedido: {total_cajas} cajas superan el máximo de {MAX_CAJAS_POR_ENVIO} por envío. Dividí en dos envíos.",
        }

    try:
        resultado = cotizar_bultos(
            cliente=cliente_id.strip().upper(),
            markup_pct=get_markup_pct(cliente_id),
            ruta_id=ruta.ruta_id,
            bultos=piezas,
        )
    except ValueError as e:
        return {"encontrado": False, "motivo": str(e)}

    dolar = _get_dolar_ars()
    costo_fedex_ars = round(resultado["costo_fedex_usd"] * dolar, 2)

    return {
        "encontrado": True,
        "ruta_id": ruta.ruta_id,
        "bultos": detalle,
        "piezas_total": resultado["piezas_total"],
        "cantidad": resultado["piezas_total"],
        "peso_total_kg": resultado["peso_total_kg"],
        "peso_facturable_kg": resultado["peso_facturable_kg"],
        "valor_total_usd": round(valor_total_usd, 2),
        "tarifa_lista_ars": resultado["tarifa_lista_ars"],
        "precio_ars": resultado["precio_final_ars"],
        "precio_usd": resultado["precio_final_usd"],
        "tipo_cambio_usado": dolar,
        "costo_fedex_usd": resultado["costo_fedex_usd"],
        "costo_fedex_ars": costo_fedex_ars,
        "margen_ars": round(resultado["precio_final_ars"] - costo_fedex_ars, 2),
        "markup_tipo": resultado["markup_tipo"],
        "markup_valor": resultado["markup_valor"],
        "markup_pct_equivalente": resultado["markup_pct"],
        "dias_estimados": resultado["dias_estimados"],
        "coti_id": resultado["coti_id"],
        "valida_hasta": resultado["valida_hasta"],
    }


def obtener_precio_envio(
    cliente_id: str, producto_id: str, destino_pais: str, cantidad: int = 1
) -> dict:
    """
    Cotiza producto + destino en vivo con FedEx y markup del cliente.
    cantidad multiplica peso y valor declarado (todo viaja como un solo bulto
    hasta que soportemos multi-pieza). FedEx IP admite hasta 70kg por pieza.
    """
    producto = get_producto(cliente_id, producto_id)
    if not producto or not producto.activo:
        return {"encontrado": False, "motivo": "producto_no_encontrado"}

    ruta = buscar_ruta_para_destino(destino_pais)
    if not ruta:
        return {"encontrado": False, "motivo": "ruta_no_encontrada"}

    cantidad = max(int(cantidad or 1), 1)
    peso_total = round(producto.peso_kg * cantidad, 2)
    if peso_total > 70:
        return {
            "encontrado": False,
            "motivo": f"peso_excedido: {cantidad} unidades pesan {peso_total}kg y el máximo por envío es 70kg. Dividí en envíos más chicos.",
        }

    resultado = cotizar(
        cliente=cliente_id.strip().upper(),
        markup_pct=get_markup_pct(cliente_id),
        input_data=CotizacionInput(
            ruta_id=ruta.ruta_id,
            peso_kg=peso_total,
            largo_cm=producto.largo_cm,
            ancho_cm=producto.ancho_cm,
            alto_cm=producto.alto_cm,
            valor_declarado_usd=producto.valor_usd_default,
            hs_code=producto.hs_code,
            descripcion_en=producto.nombre_invoice,
            unidades=cantidad,
        ),
    )

    dolar = _get_dolar_ars()
    costo_fedex_ars = round(resultado.costo_fedex_usd * dolar, 2)

    return {
        "encontrado": True,
        "ruta_id": ruta.ruta_id,
        "cantidad": cantidad,
        "peso_total_kg": peso_total,
        "tarifa_lista_ars": resultado.tarifa_lista_ars,
        "precio_ars": resultado.precio_final_ars,
        "precio_usd": resultado.precio_final_usd,
        "tipo_cambio_usado": dolar,
        "costo_fedex_usd": resultado.costo_fedex_usd,
        "costo_fedex_ars": costo_fedex_ars,
        "margen_ars": round(resultado.precio_final_ars - costo_fedex_ars, 2),
        "markup_tipo": resultado.markup_tipo,
        "markup_valor": resultado.markup_valor,
        "markup_pct_equivalente": resultado.markup_pct,
        "dias_estimados": resultado.dias_estimados,
        "coti_id": resultado.coti_id,
        "valida_hasta": resultado.valida_hasta,
    }
