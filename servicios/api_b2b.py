# ============================================================
# Servicio API B2B — PostgreSQL
# ============================================================

from core.database import get_conn
from modelos.cotizacion import CotizacionInput
from servicios.auth import get_markup_pct
from servicios.catalogo import get_producto
from servicios.cotizador import cotizar, _get_dolar_ars
from servicios.rutas import get_rutas_activas, pais_a_iso2


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
