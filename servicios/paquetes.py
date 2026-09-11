"""Embalajes y composición determinística; no ejecuta envíos ni toca stock.

Una asociación confirma que hasta N unidades de una variante caben en una
caja. Una combinación confirma un contenido exacto. No inferimos capacidad
sumando volúmenes: sólo usamos cajas y contenidos probados por el comercio.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping

from psycopg2.extras import Json
from core.database import get_conn

MAX_CAJAS = 20
MAX_UNIDADES = 100


class PaqueteError(ValueError):
    """Mensaje seguro para el comercio; nunca incluye secretos de transportistas."""


def numero(value, nombre, *, minimo=0, maximo=100000000, decimales=3):
    if isinstance(value, bool):
        raise PaqueteError(f"{nombre}: ingresá un número válido.")
    try:
        raw = str(value).strip().replace(",", ".")
        n = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise PaqueteError(f"{nombre}: ingresá un número válido.") from None
    if not n.is_finite() or not Decimal(str(minimo)) <= n <= Decimal(str(maximo)):
        raise PaqueteError(f"{nombre}: debe estar entre {minimo} y {maximo}.")
    if n.as_tuple().exponent < -decimales:
        raise PaqueteError(f"{nombre}: usá hasta {decimales} decimales.")
    return n


def entero(value, nombre="Cantidad", *, minimo=1, maximo=MAX_UNIDADES):
    return int(numero(value, nombre, minimo=minimo, maximo=maximo, decimales=0))


def identificador(value):
    return entero(value, "Identificador", maximo=2**63 - 1)


def importe(value,nombre,*,minimo=0):
    from servicios.numeros_humanos import parse_importe_humano
    if isinstance(value,bool):
        raise PaqueteError(f"{nombre}: ingresá un importe válido.")
    try:
        normalized = parse_importe_humano(value)
    except (ValueError,TypeError):
        raise PaqueteError(f"{nombre}: ingresá un importe válido.") from None
    return numero(normalized,nombre,minimo=minimo,decimales=2)


def _nombre(value):
    result = " ".join(str(value or "").split())
    if not 1 <= len(result) <= 80:
        raise PaqueteError("El nombre debe tener entre 1 y 80 caracteres.")
    return result


def validar_paquete(data):
    result = {"nombre": _nombre(data.get("nombre"))}
    for key, label in (("largo_cm", "Largo"), ("ancho_cm", "Ancho"), ("alto_cm", "Alto")):
        result[key] = numero(data.get(key), label, minimo=.01, maximo=300, decimales=2)
    for key, label in (("tara_kg", "Peso de la caja vacía"), ("proteccion_kg", "Peso de la protección")):
        result[key] = numero(data.get(key), label, maximo=70)
    result["max_kg"] = numero(data.get("max_kg"), "Peso máximo de la caja llena", minimo=.001, maximo=70)
    if result["max_kg"] <= result["tara_kg"] + result["proteccion_kg"]:
        raise PaqueteError("El peso máximo debe superar el peso de la caja y su protección.")
    return result


def _confirmado(data):
    if data.get("confirmado") is not True:
        raise PaqueteError("Confirmá que probaste físicamente el contenido en esa caja.")


def _cliente(value):
    result = str(value or "").strip().upper()
    if not result:
        raise PaqueteError("Necesitás iniciar sesión.")
    return result


def cargar_catalogo(cliente):
    """Devuelve sólo campos comerciales/operativos del dueño, sin credenciales."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, alias_interno, titulo_tienda, variante_tienda,
            imagen_url, plataforma, tienda_dominio, external_variant_id, sku_tienda,
            nombre_invoice, hs_code, pais_origen_tienda, valor_usd_default,
            precio_tienda, moneda_tienda, activo, sync_activo, source_deleted_at
            FROM productos WHERE cliente_id = %s ORDER BY alias_interno""", (_cliente(cliente),))
        return [dict(r) for r in cur.fetchall()]


def cargar_configuracion(cliente):
    cliente = _cliente(cliente)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM paquetes_guardados WHERE cliente_id = %s ORDER BY activo DESC, nombre", (cliente,))
        cajas = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM paquetes_productos WHERE cliente_id = %s", (cliente,))
        productos = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM paquetes_combinaciones WHERE cliente_id = %s ORDER BY prioridad DESC, id", (cliente,))
        reglas = [dict(r) for r in cur.fetchall()]
    return {"paquetes": cajas, "asociaciones": productos, "combinaciones": reglas}


def guardar_paquete(cliente, data, paquete_id=None):
    cliente = _cliente(cliente)
    valid = validar_paquete(data)
    with get_conn() as conn, conn.cursor() as cur:
        if paquete_id is None:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"paquetes:{cliente}",))
            cur.execute("SELECT count(*) AS n FROM paquetes_guardados WHERE cliente_id=%s AND activo", (cliente,))
            if cur.fetchone()["n"] >= 100:
                raise PaqueteError("Podés tener hasta 100 embalajes activos. Archivá los que no uses.")
            cur.execute("""INSERT INTO paquetes_guardados
                (cliente_id,nombre,largo_cm,ancho_cm,alto_cm,tara_kg,proteccion_kg,max_kg)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (cliente, *valid.values()))
        else:
            # Una modificación dimensional invalida las confirmaciones físicas.
            # Bloquear mientras esté asociado evita cambiar carritos ya configurados.
            cur.execute("""SELECT id FROM paquetes_guardados
                WHERE cliente_id=%s AND id=%s AND activo FOR UPDATE""", (cliente, identificador(paquete_id)))
            if not cur.fetchone():
                raise PaqueteError("Ese embalaje no está disponible.")
            cur.execute("""SELECT 1 FROM paquetes_productos WHERE cliente_id=%s AND paquete_id=%s
                UNION ALL SELECT 1 FROM paquetes_combinaciones
                WHERE cliente_id=%s AND paquete_id=%s AND activo LIMIT 1""", (cliente, paquete_id, cliente, paquete_id))
            if cur.fetchone():
                raise PaqueteError("Este embalaje ya tiene productos asociados. Creá otro con las nuevas medidas y reasociá el contenido.")
            cur.execute("""UPDATE paquetes_guardados SET nombre=%s,largo_cm=%s,ancho_cm=%s,
                alto_cm=%s,tara_kg=%s,proteccion_kg=%s,max_kg=%s,version=version+1,actualizado_en=now()
                WHERE cliente_id=%s AND id=%s RETURNING id""", (*valid.values(), cliente, paquete_id))
        return dict(cur.fetchone())


def archivar(cliente, tipo, objeto_id):
    tables = {"paquete": "paquetes_guardados", "combinacion": "paquetes_combinaciones"}
    if tipo not in tables:
        raise PaqueteError("No se reconoce el elemento.")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE {tables[tipo]} SET activo=FALSE,version=version+1 WHERE cliente_id=%s AND id=%s RETURNING id",
                    (_cliente(cliente), identificador(objeto_id)))
        if not cur.fetchone():
            raise PaqueteError("No se encontró ese elemento.")


def guardar_asociacion(cliente, data):
    _confirmado(data)
    cliente = _cliente(cliente)
    pid = identificador(data.get("producto_id"))
    bid = identificador(data.get("paquete_id"))
    peso = numero(data.get("peso_neto_kg"), "Peso del producto", minimo=.001, maximo=70)
    cantidad = entero(data.get("unidades_por_caja"))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT p.id FROM productos p WHERE p.cliente_id=%s AND p.id=%s
            AND p.source_deleted_at IS NULL AND (p.plataforma IS NULL OR p.sync_activo) FOR SHARE""", (cliente, pid))
        if not cur.fetchone():
            raise PaqueteError("Ese producto no está disponible en tu catálogo.")
        cur.execute("SELECT * FROM paquetes_guardados WHERE cliente_id=%s AND id=%s AND activo FOR SHARE", (cliente, bid))
        box = cur.fetchone()
        if not box:
            raise PaqueteError("Elegí un embalaje activo de tu cuenta.")
        if peso * cantidad + box["tara_kg"] + box["proteccion_kg"] > box["max_kg"]:
            raise PaqueteError("El contenido supera el peso máximo de esa caja.")
        cur.execute("""INSERT INTO paquetes_productos
            (cliente_id,producto_id,paquete_id,peso_neto_kg,unidades_por_caja) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (cliente_id,producto_id) DO UPDATE SET paquete_id=EXCLUDED.paquete_id,
            peso_neto_kg=EXCLUDED.peso_neto_kg, unidades_por_caja=EXCLUDED.unidades_por_caja,
            version=paquetes_productos.version+1""", (cliente, pid, bid, peso, cantidad))


def normalizar_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise PaqueteError("Agregá entre 1 y 100 artículos.")
    count = Counter()
    for item in items:
        if not isinstance(item, Mapping):
            raise PaqueteError("Hay un artículo inválido.")
        count[identificador(item.get("producto_id"))] += entero(item.get("cantidad"))
    if sum(count.values()) > MAX_UNIDADES:
        raise PaqueteError("Dividí los pedidos de más de 100 unidades en varios envíos.")
    return count


def guardar_combinacion(cliente, data):
    _confirmado(data)
    cliente = _cliente(cliente)
    contenido = normalizar_items(data.get("contenido"))
    if len(contenido) < 2:
        raise PaqueteError("Una combinación lleva al menos dos productos distintos.")
    nombre = _nombre(data.get("nombre"))
    prioridad = entero(data.get("prioridad", 0), "Prioridad", minimo=0)
    bid = identificador(data.get("paquete_id"))
    config = cargar_configuracion(cliente)
    catalogo = cargar_catalogo(cliente)
    receta = {"id": 0, "nombre": nombre, "paquete_id": bid, "activo": True,
              "contenido": [{"producto_id": p, "cantidad": q} for p,q in sorted(contenido.items())],
              "prioridad": prioridad, "version": 1}
    # Usa exactamente el mismo control de peso/disponibilidad que el carrito.
    planificar(receta["contenido"], {**config, "combinaciones": [receta]}, catalogo)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"paquetes:{cliente}",))
        cur.execute("SELECT count(*) AS n FROM paquetes_combinaciones WHERE cliente_id=%s AND activo", (cliente,))
        if cur.fetchone()["n"] >= 200:
            raise PaqueteError("Podés guardar hasta 200 combinaciones activas.")
        cur.execute("""INSERT INTO paquetes_combinaciones (cliente_id,nombre,paquete_id,contenido,prioridad)
            VALUES (%s,%s,%s,%s,%s) RETURNING id""", (cliente,nombre,bid,Json(receta["contenido"]),prioridad))
        return dict(cur.fetchone())


def planificar(items, config, catalogo):
    restantes = normalizar_items(items)
    cajas = {int(b["id"]): b for b in config["paquetes"] if b.get("activo")}
    asociaciones = {int(a["producto_id"]): a for a in config["asociaciones"]}
    productos = {int(p["id"]): p for p in catalogo
                 if not p.get("source_deleted_at") and p.get("sync_activo", True)}
    for pid in restantes:
        if pid not in productos:
            raise PaqueteError("Uno de los productos ya no está disponible en tu catálogo.")
        if pid not in asociaciones:
            raise PaqueteError(f"Asociá un embalaje y peso a {productos[pid]['alias_interno']}.")
    plan = []

    def agregar(bid, contenido, regla=None):
        box = cajas.get(int(bid))
        if not box:
            raise PaqueteError("Un embalaje necesario está archivado. Reasociá los productos.")
        neto = sum(Decimal(str(asociaciones[pid]["peso_neto_kg"])) * q for pid,q in contenido.items())
        bruto = neto + Decimal(str(box["tara_kg"])) + Decimal(str(box["proteccion_kg"]))
        if bruto > Decimal(str(box["max_kg"])):
            raise PaqueteError(f"El contenido supera el peso máximo de {box['nombre']}. Revisá la asociación o combinación.")
        if len(plan) >= MAX_CAJAS:
            raise PaqueteError("El pedido necesita más de 20 cajas. Dividilo en varios envíos.")
        dims = {k: float(box[k]) for k in ("largo_cm", "ancho_cm", "alto_cm")}
        volumetrico = Decimal(str(box["largo_cm"])) * Decimal(str(box["ancho_cm"])) * Decimal(str(box["alto_cm"])) / Decimal(5000)
        plan.append({"paquete_id": int(box["id"]), "paquete_version": box["version"],
            "nombre": box["nombre"], "cantidad": 1, **dims,
            "peso_kg": float(bruto), "peso_neto_kg": float(neto),
            "peso_facturable_kg": float(max(bruto, volumetrico)),
            "combinacion_id": regla.get("id") if regla else None,
            "combinacion_version": regla.get("version") if regla else None,
            "contenido": [{"producto_id": pid, "cantidad": q,
                "alias": productos[pid]["alias_interno"],
                "asociacion_version": asociaciones[pid]["version"],
                "peso_neto_kg": float(Decimal(str(asociaciones[pid]["peso_neto_kg"])) * q)}
                for pid,q in sorted(contenido.items())]})

    reglas = [r for r in config["combinaciones"] if r.get("activo")]
    reglas.sort(key=lambda r: (-int(r["prioridad"]), -sum(int(i["cantidad"]) for i in r["contenido"]), int(r["id"])))
    for regla in reglas:
        contenido = normalizar_items(regla["contenido"])
        while all(restantes[p] >= q for p,q in contenido.items()):
            agregar(regla["paquete_id"], contenido, regla)
            restantes.subtract(contenido)
    for pid, qty in sorted(restantes.items()):
        while qty > 0:
            unidades = min(qty, int(asociaciones[pid]["unidades_por_caja"]))
            agregar(asociaciones[pid]["paquete_id"], {pid: unidades})
            qty -= unidades
    return {"version_motor": 1, "bultos": plan, "cajas_total": len(plan),
            "unidades_total": sum(normalizar_items(items).values()),
            "peso_total_kg": float(sum(Decimal(str(b["peso_kg"])) for b in plan)),
            "peso_facturable_kg": float(sum(Decimal(str(b["peso_facturable_kg"])) for b in plan))}


def resolver_items_tienda(items, catalogo, plataforma, dominio):
    """La variante se busca dentro de una tienda exacta; SKU ambiguo se rechaza."""
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise PaqueteError("El carrito no contiene artículos válidos.")
    candidatos = [p for p in catalogo if str(p.get("plataforma") or "").lower() == plataforma
        and str(p.get("tienda_dominio") or "").lower() == dominio.lower()
        and p.get("sync_activo", True) and not p.get("source_deleted_at")]
    result = []
    for item in items:
        if not isinstance(item, Mapping):
            raise PaqueteError("El carrito contiene un artículo inválido.")
        if item.get("requires_shipping") is False:
            continue
        variante = str(item.get("variant_id") or item.get("external_variant_id") or "")
        if plataforma == "shopify" and variante.isdigit():
            variante = "gid://shopify/ProductVariant/" + variante
        sku = str(item.get("sku") or "").strip().upper()
        if variante:
            matches = [p for p in candidatos if str(p.get("external_variant_id") or "") == variante]
        else:
            manuales = [p for p in catalogo if not p.get("plataforma") or p.get("plataforma") == "manual"]
            matches = [p for p in candidatos + manuales if sku and sku in {
                str(p.get("sku_tienda") or "").strip().upper(), str(p["alias_interno"]).strip().upper()}]
        if len(matches) != 1:
            raise PaqueteError("Sincronizá el catálogo y asociá cada variante de esta tienda a un embalaje.")
        result.append({"producto_id": int(matches[0]["id"]),
                       "cantidad": entero(item.get("quantity", item.get("cantidad")))})
    normalizar_items(result)
    return result


def validar_politica(data):
    if not isinstance(data, Mapping):
        raise PaqueteError("La configuración de envío es inválida.")
    politica = data.get("politica", "real")
    if politica not in {"real", "gratis", "markup", "fijo"}:
        raise PaqueteError("Elegí una política de envío válida.")
    fixed = importe(data.get("precio_fijo_ars", 0), "Precio fijo")
    markup = numero(data.get("markup_pct", 0), "Porcentaje adicional", maximo=300, decimales=2)
    threshold = importe(data.get("gratis_desde_ars", 0), "Envío gratis desde")
    if politica == "fijo" and fixed <= 0:
        raise PaqueteError("El precio fijo debe ser mayor a cero. Para bonificarlo, elegí envío gratis.")
    if politica == "markup" and markup <= 0:
        raise PaqueteError("Ingresá el porcentaje adicional.")
    return {"habilitado": data.get("habilitado") is True, "politica": politica,
            "precio_fijo_ars": str(fixed), "markup_pct": str(markup), "gratis_desde_ars": str(threshold)}


def precio_comprador(precio_tauro, config, subtotal_ars):
    base = numero(precio_tauro, "Tarifa TAURO", minimo=.01, decimales=2)
    cfg = validar_politica(config)
    subtotal = numero(subtotal_ars, "Valor de los productos", decimales=2)
    if cfg["politica"] == "gratis" or (Decimal(cfg["gratis_desde_ars"]) > 0 and subtotal >= Decimal(cfg["gratis_desde_ars"])):
        return Decimal("0.00")
    if cfg["politica"] == "fijo":
        base = Decimal(cfg["precio_fijo_ars"])
    elif cfg["politica"] == "markup":
        base *= 1 + Decimal(cfg["markup_pct"]) / 100
    return base.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
