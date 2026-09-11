"""Prepara solicitudes con cajas guardadas; jamás emite una guía."""
from __future__ import annotations

from decimal import Decimal
import re

from servicios import paquetes as pkg
from servicios import paquetes_cotizacion as quotes


def bultos_invoice(plan,catalogo,*,para_revision=False):
    from servicios.invoice_comercial import normalizar_items_invoice, total_items_invoice
    productos = {p["id"]:p for p in catalogo}
    result = []
    for box in plan["bultos"]:
        items = []
        for linea in box["contenido"]:
            p = productos.get(linea["producto_id"])
            if not p or (not para_revision and (not p.get("activo") or not p.get("hs_code") or not p.get("pais_origen_tienda"))):
                raise pkg.PaqueteError("Las cajas ya están calculadas. Completá y verificá descripción, valor USD, HS y país de fabricación de los productos para preparar la guía internacional.")
            items.append({"descripcion_en":p.get("nombre_invoice") or "","unidades_aduana":linea["cantidad"],
                "valor_unitario_usd":float(p.get("valor_usd_default") or 0),"hs_code":p.get("hs_code") or "",
                "pais_origen":p.get("pais_origen_tienda") or "","peso_neto_kg":linea["peso_neto_kg"]})
        if not para_revision:
            items = normalizar_items_invoice(items,peso_total_kg=box["peso_kg"])
        result.append({"producto_alias":"", "cantidad":1,
            **{k:box[k] for k in ("peso_kg","largo_cm","ancho_cm","alto_cm")},
            **{k:items[0][k] for k in ("descripcion_en","unidades_aduana","valor_unitario_usd","hs_code","pais_origen")},
            "items_invoice":items,"valor_declarado_caja_usd":float(total_items_invoice(items)),
            "embalaje": {k:box[k] for k in ("paquete_id","paquete_version","nombre","contenido")}})
    return result


def crear_desde_pedido(ped):
    from servicios.solicitud_automatica import _motivo
    from servicios.direcciones import obtener_remitente_para_envio
    from servicios.integraciones_tienda import marcar_convertido
    from servicios.api_b2b import cotizar_couriers_cliente
    from servicios.solicitudes_guia import crear_solicitud_guia,idempotency_hash_origen_tienda
    from servicios.cotizador import dolar_ars
    cliente = ped["cliente_id"].strip().upper()
    pid = ped["id"]
    try:
        plan,catalogo = quotes.plan_tienda(cliente,ped["origen_plataforma"],ped["origen_dominio"],ped["items"])
        quotes.guardar_plan_pedido(cliente,pid,plan)
        remitente = obtener_remitente_para_envio(cliente,None)
        if not remitente or not remitente.get("direccion"):
            raise pkg.PaqueteError("Las cajas están calculadas. Cargá una dirección de remitente en tu libreta del portal.")
        dest = ped["destinatario"]
        origen,destino = quotes.direccion(remitente,"origen"),quotes.direccion(dest,"destino")
        if origen["pais"] == destino["pais"] == "AR":
            return _motivo(pid,f"Embalaje preparado: {plan['cajas_total']} caja(s). El envío nacional espera la habilitación de emisión del operador. Podés consultar las tarifas desde Embalajes y tarifas.")
        bultos = bultos_invoice(plan,catalogo)
        precio = cotizar_couriers_cliente(cliente,destino["pais"],bultos,destino_real=destino,origen_real=origen)
        opciones = precio.get("opciones") or []
        # Respeta el transportista elegido en el checkout; nunca lo sustituye
        # por otro más barato sin que el comercio revise la venta.
        elegidos = set()
        for line in ped.get("flete_detalle") or []:
            match = re.match(r"^tauro_internacional_([a-z]+)_", str(line.get("codigo") or ""))
            if match:
                elegidos.add(match.group(1))
        if len(elegidos) > 1:
            raise pkg.PaqueteError("La venta contiene más de un servicio de envío. Revisá la separación en el portal.")
        if elegidos:
            opciones = [o for o in opciones if o["id"] in elegidos]
        if not opciones:
            raise pkg.PaqueteError("Las cajas están preparadas, pero no hay una tarifa disponible para el servicio de esta venta. Revisala desde el portal.")
        elegido = opciones[0]
        dolar = pkg.numero(dolar_ars(),"Tipo de cambio",minimo=.01,decimales=6)
        valor_usd = sum(Decimal(str(b["valor_declarado_caja_usd"])) for b in bultos)
        first = bultos[0]
        flete = (float(pkg.numero(ped["flete_cobrado"],"Flete cobrado",decimales=2))
                 if ped.get("moneda") == "ARS" and ped.get("flete_cobrado") is not None else None)
        creada = crear_solicitud_guia(cliente_id=cliente,producto_alias="Embalajes de tienda",
            cantidad=plan["cajas_total"],destino_pais=destino["pais"],
            dest_nombre=dest.get("nombre") or "",dest_documento="",dest_email=dest.get("email") or "",
            dest_telefono=dest.get("telefono") or "",dest_direccion=" - ".join(x for x in (dest.get("direccion"),dest.get("direccion2")) if x),
            dest_ciudad=dest.get("ciudad") or "",dest_estado=dest.get("estado") or "",dest_zip=dest.get("cp") or "",
            remitente_alias=remitente.get("alias") or "",remitente_nombre=remitente.get("nombre") or "",
            remitente_documento=remitente.get("documento") or "",remitente_email=remitente.get("email") or "",
            remitente_telefono=remitente.get("telefono") or "",remitente_direccion=remitente["direccion"],
            remitente_ciudad=remitente.get("ciudad") or "",remitente_estado=remitente.get("estado") or "",
            remitente_zip=remitente.get("cp") or "",remitente_pais=origen["pais"],
            etiqueta_cliente=f"Pedido {ped.get('numero') or ''}".strip(),
            observaciones="Preparado con tus embalajes guardados. Revisá contenido, valores de aduana y peso antes de generar la guía.",
            peso_kg=plan["peso_total_kg"],largo_cm=first["largo_cm"],ancho_cm=first["ancho_cm"],alto_cm=first["alto_cm"],
            valor_declarado_usd=float(valor_usd),ruta_id="",coti_id="",precio_tauro_ars=elegido["precio_ars"],
            precio_tauro_usd=float(Decimal(str(elegido["precio_ars"]))/dolar),precio_cliente_final_ars=flete,
            bultos=bultos,courier=elegido["id"].upper(),
            origen_plataforma=ped["origen_plataforma"],origen_dominio=ped["origen_dominio"],
            origen_pedido_externo_id=ped["origen_pedido_externo_id"],
            idempotency_key_hash=idempotency_hash_origen_tienda(cliente_id=cliente,
                origen_plataforma=ped["origen_plataforma"],origen_dominio=ped["origen_dominio"],
                origen_pedido_externo_id=ped["origen_pedido_externo_id"]))
        marcar_convertido(cliente,pid,solicitud_id=creada["id"])
        return {"ok":True,"solicitud_id":creada["id"],"motivo":""}
    except (ValueError,pkg.PaqueteError) as exc:
        return _motivo(pid,str(exc))
    except Exception as exc:
        print(f"[paquetes] solicitud pendiente: {type(exc).__name__}")
        return _motivo(pid,"No pudimos preparar el envío con tus embalajes. Reintentá desde el portal.")
