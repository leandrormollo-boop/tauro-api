"""Un mismo cálculo de cajas y precios para portal e integraciones."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Mapping

from psycopg2.extras import Json
from core.database import get_conn
from servicios import paquetes as pkg


def tiendas(cliente):
    from servicios.integraciones_tienda import _ensure_tablas
    _ensure_tablas()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT t.id,t.plataforma,t.dominio,t.activa,
            c.usar_paquetes,c.nacional,c.internacional,c.carrier_id,
            COALESCE(c.checkout_activo AND c.usar_paquetes AND i.webhooks_ready
                AND NOT i.token_reauth_required AND c.install_generation=i.install_generation
                AND t.secreto='oauth:shopify-app'
                AND 'write_shipping'=ANY(string_to_array(i.scopes,','))
                AND nullif(trim(i.access_token),'') IS NOT NULL, FALSE) AS checkout_activo
            FROM tiendas_conectadas t LEFT JOIN paquetes_tiendas c
            ON c.cliente_id=t.cliente_id AND c.plataforma=t.plataforma AND c.dominio=t.dominio
            LEFT JOIN shopify_instalaciones i ON i.cliente_id=t.cliente_id
                AND i.dominio=t.dominio AND t.plataforma='shopify'
            WHERE t.cliente_id=%s AND t.activa ORDER BY t.id""", (pkg._cliente(cliente),))
        return [dict(r) for r in cur.fetchall()]


def tienda_propia(cliente, tienda_id):
    return next((t for t in tiendas(cliente) if int(t["id"]) == pkg.identificador(tienda_id)), None)


def config_tienda(cliente, plataforma, dominio):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT c.* FROM paquetes_tiendas c JOIN tiendas_conectadas t
            ON t.cliente_id=c.cliente_id AND t.plataforma=c.plataforma AND t.dominio=c.dominio
            WHERE c.cliente_id=%s AND c.plataforma=%s AND c.dominio=%s AND t.activa""",
            (pkg._cliente(cliente), plataforma, dominio.lower()))
        row = cur.fetchone()
        return dict(row) if row else None


def guardar_tienda(cliente, tienda_id, data):
    nacional = pkg.validar_politica(data.get("nacional", {}))
    internacional = pkg.validar_politica(data.get("internacional", {}))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT dominio,plataforma FROM tiendas_conectadas
            WHERE cliente_id=%s AND id=%s AND activa FOR SHARE""", (pkg._cliente(cliente), pkg.identificador(tienda_id)))
        tienda = cur.fetchone()
        if not tienda:
            raise pkg.PaqueteError("La tienda no está vinculada a tu cuenta.")
        if tienda["plataforma"] == "tiendanube" and internacional["habilitado"]:
            raise pkg.PaqueteError("El medio de envío de Tiendanube actualmente admite cotizaciones nacionales. Internacional se puede cotizar desde el portal.")
        cur.execute("""INSERT INTO paquetes_tiendas (cliente_id,plataforma,dominio,usar_paquetes,nacional,internacional)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (cliente_id,plataforma,dominio)
            DO UPDATE SET usar_paquetes=EXCLUDED.usar_paquetes,nacional=EXCLUDED.nacional,
            internacional=EXCLUDED.internacional,version=paquetes_tiendas.version+1,actualizado_en=now(),
            checkout_activo=CASE WHEN EXCLUDED.usar_paquetes THEN paquetes_tiendas.checkout_activo ELSE FALSE END""",
            (pkg._cliente(cliente),tienda["plataforma"],tienda["dominio"],data.get("usar_paquetes") is True,Json(nacional),Json(internacional)))


def direccion(raw, label):
    from servicios.paises import normalizar_iso2
    if not isinstance(raw, Mapping):
        raise pkg.PaqueteError(f"Completá {label}.")
    country = normalizar_iso2(raw.get("country") or raw.get("pais") or "")
    cp = str(raw.get("postal_code") or raw.get("cp") or raw.get("zip") or "").strip().upper()
    city = str(raw.get("city") or raw.get("ciudad") or raw.get("locality") or "").strip()
    state = str(raw.get("state") or raw.get("estado") or raw.get("province") or "").strip()
    if not country or len(cp) > 16 or len(city) > 100 or len(state) > 100:
        raise pkg.PaqueteError(f"Revisá país, código postal y ciudad de {label}.")
    if country == "AR":
        if re.fullmatch(r"[A-Z]\d{4}[A-Z]{3}", cp):
            cp = cp[1:5]
        if not re.fullmatch(r"\d{4}", cp):
            raise pkg.PaqueteError(f"Ingresá el código postal argentino de {label}.")
    elif not cp or not city:
        raise pkg.PaqueteError(f"Ingresá código postal y ciudad reales de {label}.")
    return {"country": country, "pais": country, "postal_code": cp, "cp": cp,
            "city": city, "ciudad": city, "state": state, "estado": state}


def packages_neutrales(plan):
    from servicios.carrier_adapter import Package
    return tuple(Package(quantity=1, weight_kg=Decimal(str(b["peso_kg"])),
        length_cm=Decimal(str(b["largo_cm"])),width_cm=Decimal(str(b["ancho_cm"])),
        height_cm=Decimal(str(b["alto_cm"]))) for b in plan["bultos"])


def cotizar_plan(cliente, plan, origen, destino, valor_ars, politica=None):
    origen, destino = direccion(origen, "origen"), direccion(destino, "destino")
    valor = pkg.importe(valor_ars, "Valor de los productos (ARS)", minimo=.01)
    ambito = "nacional" if origen["country"] == destino["country"] == "AR" else "internacional"
    opciones = []
    if ambito == "nacional":
        from servicios.carrier_adapter import (adapter_for, registered_adapters, QuoteRequest,
            OperationState, validate_quote_request, validate_quote_result)
        from servicios.carrier_contract import Ambito, Capacidad
        req = QuoteRequest(request_id="pkg-" + hashlib.sha256(json.dumps(
            [cliente,plan,origen,destino,str(valor)],sort_keys=True).encode()).hexdigest()[:24],
            customer_id=cliente, scope=Ambito.NACIONAL,origin=origen,destination=destino,
            packages=packages_neutrales(plan),declared_value=valor,declared_currency="ARS")
        for carrier in registered_adapters():
            if carrier not in {"oca", "andreani"}:
                continue
            try:
                adapter = adapter_for(carrier, Capacidad.COTIZAR)
                validate_quote_request(req, carrier)
                for q in adapter.quote(req):
                    validate_quote_result(q, carrier)
                    if q.state == OperationState.COTIZADO and q.currency == "ARS":
                        opciones.append({"courier":carrier,"servicio":q.service_name,
                            "codigo_servicio":q.service_code,"precio_tauro_ars":str(q.customer_price),
                            "dias_estimados":q.estimated_days})
            except (RuntimeError, ValueError):
                continue
        motivo = "No hay una tarifa nacional disponible para esta ruta. Verificá con TAURO la habilitación y cobertura del operador."
    else:
        from servicios.carriers import cotizar_carriers_cliente
        from servicios.cotizador import dolar_ars
        from servicios.configuracion_couriers_cliente import configuracion_cotizacion
        acceso = configuracion_cotizacion(cliente)
        dolar = pkg.numero(dolar_ars(), "Tipo de cambio", minimo=.01, decimales=6)
        piezas = [{k:b[k] for k in ("peso_kg","largo_cm","ancho_cm","alto_cm")} for b in plan["bultos"]]
        # La declaración completa de varios artículos por caja la emite DHL.
        habilitados = acceso["couriers_habilitados"]
        if any(len(b["contenido"]) > 1 for b in plan["bultos"]):
            habilitados = [c for c in habilitados if c.lower() == "dhl"]
        for p in piezas:
            p.update(valor_unitario_usd=float(valor / dolar / len(piezas)),unidades=1)
        tarjetas = cotizar_carriers_cliente(origen=origen,destino=destino,paquete=piezas[0],
            paquetes=piezas,dolar=float(dolar),pricing_cliente=acceso["pricing_general"],
            pricing_por_courier=acceso["pricing_por_courier"],couriers_habilitados=habilitados)
        for q in tarjetas:
            if q.get("estado") == "cotizado":
                opciones.append({"courier":q["id"],"servicio":q["servicio"],
                    "codigo_servicio":q["id"],"precio_tauro_ars":str(q["precio_ars"]),
                    "dias_estimados":q.get("dias_estimados")})
        motivo = "No hay tarifa internacional disponible para estas cajas y destino. Revisá cobertura y operadores habilitados con TAURO."
    cfg = politica or {"politica":"real"}
    for op in opciones:
        op["precio_comprador_ars"] = str(pkg.precio_comprador(op["precio_tauro_ars"],cfg,valor))
        op["moneda"] = "ARS"
    opciones.sort(key=lambda o: Decimal(o["precio_tauro_ars"]))
    return {"encontrado":bool(opciones),"ambito":ambito,"plan":plan,"opciones":opciones,
            "motivo":"" if opciones else motivo}


def cotizar_carrito(cliente, data):
    catalogo = pkg.cargar_catalogo(cliente)
    plan = pkg.planificar(data.get("items"), pkg.cargar_configuracion(cliente), catalogo)
    origen = data.get("origen")
    if not origen:
        from servicios.direcciones import obtener_remitente_para_envio
        origen = obtener_remitente_para_envio(cliente, None)
    destino = data.get("destino")
    orig, dest = direccion(origen, "origen"), direccion(destino, "destino")
    cfg = None
    if data.get("tienda_id"):
        tienda = tienda_propia(cliente,data["tienda_id"])
        if not tienda:
            raise pkg.PaqueteError("La tienda no está vinculada a tu cuenta.")
        campo = "nacional" if orig["pais"] == dest["pais"] == "AR" else "internacional"
        cfg = tienda.get(campo)
    resultado = cotizar_plan(cliente,plan,orig,dest,data.get("valor_ars"),cfg)
    resultado["checkout_habilitado"] = bool(data.get("tienda_id") and tienda.get("checkout_activo")
        and tienda.get("usar_paquetes") and (cfg or {}).get("habilitado"))
    return resultado


def plan_tienda(cliente, plataforma, dominio, items):
    catalogo = pkg.cargar_catalogo(cliente)
    lineas = pkg.resolver_items_tienda(items,catalogo,plataforma,dominio)
    return pkg.planificar(lineas,pkg.cargar_configuracion(cliente),catalogo), catalogo


def guardar_plan_pedido(cliente,pedido_id,plan):
    # Sólo IDs, cajas y cantidades. No persistimos direcciones del checkout.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO paquetes_planes_pedido (cliente_id,pedido_id,plan)
            SELECT %s,id,%s FROM pedidos_tienda WHERE cliente_id=%s AND id=%s
            AND estado='PENDIENTE' AND solicitud_id IS NULL
            ON CONFLICT (cliente_id,pedido_id) DO UPDATE SET plan=EXCLUDED.plan""", (cliente,Json(plan),cliente,pedido_id))


def plan_pedido(cliente,pedido_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT plan FROM paquetes_planes_pedido WHERE cliente_id=%s AND pedido_id=%s", (cliente,pedido_id))
        row = cur.fetchone()
        return row["plan"] if row else None
