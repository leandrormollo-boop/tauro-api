# ============================================================
# Servicio API B2B — PostgreSQL
# ============================================================

import hashlib
import secrets

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

# ── API keys: hasheadas, nunca en claro ─────────────────────
# La clave se guarda como sha256(clave). Un dump de la base (backup robado,
# SQL injection, laptop perdida) ya no entrega credenciales vivas de la API.
# sha256 sin salt alcanza PORQUE las claves que genera generar_api_key() son
# de alta entropía (token_urlsafe(32) ≈ 256 bits): no hay diccionario que
# las adivine. Las claves viejas cargadas a mano pueden ser débiles —
# rotarlas con el botón "Regenerar API key" del admin.

_hash_migrado = False


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _ensure_hash_migrado() -> None:
    """
    Migración perezosa y una sola vez por proceso: agrega la columna
    api_key_hash, hashea las claves en claro que existan y las BORRA.
    Idempotente: si no queda nada en claro, no hace nada.
    """
    global _hash_migrado
    if _hash_migrado:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS api_key_hash TEXT")
            cur.execute("SELECT cliente_id, api_key FROM clientes WHERE api_key IS NOT NULL")
            filas = cur.fetchall()
            for f in filas:
                cur.execute(
                    "UPDATE clientes SET api_key_hash = %s, api_key = NULL WHERE cliente_id = %s",
                    (hash_api_key(str(f["api_key"]).strip()), f["cliente_id"]),
                )
            if filas:
                print(f"[api_b2b] {len(filas)} api_key(s) hasheada(s) y borradas del claro")
    _hash_migrado = True


def generar_api_key(cliente_id: str) -> str:
    """
    Genera una clave nueva para el cliente, guarda SOLO el hash y devuelve
    la clave en claro UNA única vez (para mostrársela al dueño en el admin).
    Pisa la anterior: regenerar = rotar.
    """
    _ensure_hash_migrado()
    clave = f"tauro_{secrets.token_urlsafe(32)}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clientes SET api_key_hash = %s, api_key = NULL WHERE cliente_id = %s",
                (hash_api_key(clave), cliente_id.strip().upper()),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Cliente {cliente_id} no existe")
    return clave


def obtener_cliente_por_api_key(api_key: str) -> dict:
    """Valida una API key contra PostgreSQL y devuelve el perfil del cliente."""
    api_key = (api_key or "").strip()
    if not api_key:
        return {"encontrado": False}

    _ensure_hash_migrado()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cliente_id, email, markup_pct, markup_tipo, markup_valor, activo, nombre, cuit,
                       direccion, cp, ciudad, pais, telefono, notas
                FROM clientes
                WHERE api_key_hash = %s AND activo = TRUE
                LIMIT 1
                """,
                (hash_api_key(api_key),),
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
    cliente_id: str, destino_pais: str, bultos: list, destino_real: dict = None
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
        # destino_real: el CP del destinatario de verdad. Sin él se cotiza
        # contra el CP de referencia de la ruta y el recargo por zona remota
        # lo termina pagando TAURO. Ver _destino_para_cotizar en cotizador.py.
        resultado = cotizar_bultos(
            cliente=cliente_id.strip().upper(),
            markup_pct=get_markup_pct(cliente_id),
            ruta_id=ruta.ruta_id,
            bultos=piezas,
            destino_real=destino_real,
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


def cotizar_couriers_cliente(
    cliente_id: str, destino_pais: str, bultos: list,
    destino_real: dict = None, origen_real: dict = None,
) -> dict:
    """
    Las 3 opciones de courier para UN cliente del portal, cada una con SU
    precio final (costo del courier + la regla de ese cliente).

    NO necesita una ruta cargada. Regla de Leandro (05/08): el cliente elige
    desde dónde y hacia dónde, cualquier país — incluso China → India, donde
    Argentina ni aparece. Exigir una fila en `rutas` era pedirle al admin que
    cargara cientos de pares a mano, y cada país nuevo bloqueaba al cliente.
    La cobertura la decide el COURIER: si ninguno cotiza esa combinación, se
    devuelve "sin_cobertura" y el cliente lo ve.

    `origen_real` es la dirección del remitente (que puede ser un proveedor
    del exterior) y `destino_real` la del destinatario. Se cotiza contra ELLAS,
    no contra un CP de referencia: los recargos por zona remota dependen del
    código postal exacto.

    Devuelve {encontrado, opciones, motivo}. Cada opción trae SÓLO precio:
    nunca el costo ni el margen (ver tests/test_no_fuga_costo.py).
    """
    from servicios.carriers import cotizar_carriers_cliente
    from servicios.cotizador import dolar_ars
    from servicios.paises import existe, referencia
    from servicios.pricing import get_pricing_config

    destino_iso = (destino_pais or "").strip().upper()
    if not existe(destino_iso):
        return {"encontrado": False, "motivo": f"pais_no_soportado: {destino_iso}"}

    # Validación de productos y bultos: misma que el resto del portal, para
    # que el preview diga exactamente lo mismo que el submit.
    piezas, detalle, error = _piezas_del_catalogo(cliente_id, bultos)
    if error:
        return {"encontrado": False, "motivo": error}

    def _direccion(real: dict, iso: str) -> dict:
        """La dirección real si la hay; si no, la de referencia del país."""
        real = real or {}
        base = referencia(iso)
        cp = (real.get("cp") or real.get("postal_code") or "").strip()
        ciudad = (real.get("ciudad") or real.get("city") or "").strip()
        return {
            "country": iso,
            "city": ciudad or base.get("city", ""),
            "postal_code": cp or base.get("postal_code", ""),
            "state": (real.get("estado") or real.get("state") or "").strip(),
        }

    origen_iso = ((origen_real or {}).get("pais")
                  or (origen_real or {}).get("country") or "AR").strip().upper()
    if not existe(origen_iso):
        origen_iso = "AR"

    tarjetas = cotizar_carriers_cliente(
        origen=_direccion(origen_real, origen_iso),
        destino=_direccion(destino_real, destino_iso),
        paquete=piezas[0],
        dolar=dolar_ars(),
        pricing_cliente=get_pricing_config(cliente_id),
        paquetes=piezas,
    )

    opciones = [t for t in tarjetas if t.get("estado") == "cotizado"]
    opciones.sort(key=lambda o: o["precio_ars"])

    return {
        "encontrado": bool(opciones),
        "motivo": None if opciones else "sin_cobertura",
        "opciones": opciones,
        # Los que no cotizaron, con el porqué: el cliente merece saber si el
        # courier no llega a ese destino o si no soporta varias cajas.
        "no_disponibles": [
            {"id": t["id"], "nombre": t["nombre"],
             "motivo": t.get("error") or t["estado"]}
            for t in tarjetas if t.get("estado") != "cotizado"
        ],
        "piezas_total": sum(1 for _ in piezas),
        "peso_total_kg": round(sum(float(p["peso_kg"]) for p in piezas), 2),
        "bultos": detalle,
        "origen_pais": origen_iso,
        "destino_pais": destino_iso,
    }


def _piezas_del_catalogo(cliente_id: str, bultos: list):
    """
    Convierte las filas del form en piezas (una por caja) validando contra el
    catálogo. Devuelve (piezas, detalle, error). Una entrada POR CAJA porque
    cada una paga su propio peso volumétrico.
    """
    if not bultos:
        return [], [], "sin_bultos"

    piezas, detalle, total_cajas = [], [], 0
    for b in bultos:
        alias = str(b.get("producto") or b.get("producto_alias") or "").strip()
        cantidad = max(int(b.get("cantidad") or 1), 1)
        producto = get_producto(cliente_id, alias)
        if not producto or not producto.activo:
            return [], [], f"producto_no_encontrado: {alias}"
        if producto.peso_kg > MAX_KG_POR_CAJA:
            return [], [], (f"peso_excedido: cada caja de {alias} pesa "
                            f"{producto.peso_kg}kg y el máximo es {MAX_KG_POR_CAJA}kg.")
        total_cajas += cantidad
        for _ in range(cantidad):
            piezas.append({
                "peso_kg": producto.peso_kg, "largo_cm": producto.largo_cm,
                "ancho_cm": producto.ancho_cm, "alto_cm": producto.alto_cm,
            })
        detalle.append({
            "producto_alias": producto.alias_interno, "cantidad": cantidad,
            "peso_kg": producto.peso_kg, "largo_cm": producto.largo_cm,
            "ancho_cm": producto.ancho_cm, "alto_cm": producto.alto_cm,
            # La declaración de invoice de ESTE envío manda sobre el default
            # del catálogo (Leandro 05/08): el valor real de venta cambia
            # entre envíos. Vacío = catálogo, como siempre.
            "valor_unitario_usd": float(b.get("valor_unitario_usd")
                                        or producto.valor_usd_default),
            "hs_code": (b.get("hs_code") or producto.hs_code),
            "descripcion_en": (b.get("descripcion_en") or producto.nombre_invoice),
            **({"pais_origen": b["pais_origen"]} if b.get("pais_origen") else {}),
        })

    if total_cajas > MAX_CAJAS_POR_ENVIO:
        return [], [], (f"peso_excedido: {total_cajas} cajas superan el máximo "
                        f"de {MAX_CAJAS_POR_ENVIO} por envío.")
    return piezas, detalle, None
