"""OCA productivo en el portal: permisos, tarifa persistida y emisión reservada."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from core.database import get_conn
from servicios.carrier_adapter import OperationState, Package, QuoteRequest
from servicios.carrier_contract import Ambito
from servicios.configuracion_couriers_cliente import obtener_matriz
from servicios.cotizador_nacional import preparar_cotizacion_nacional
from servicios.oca_adapter import OCAAdapter, OCAConfig, _shipment_xml


class OCAPortalError(ValueError):
    pass


def config_productiva():
    config = OCAConfig.from_env()
    config.assert_fulfillment_ready()
    if (
        config.environment != "production"
        or config.account == "111757/001"
        or config.username.lower() == "test@oca.com.ar"
        or config.origin_mode != "domicilio"
        or config.destination_mode != "domicilio"
        or config.reverse_logistics
    ):
        raise OCAPortalError(
            "OCA puerta a puerta todavía no está habilitado en producción."
        )
    return config


def huella_config(config):
    # Cambiar contrato, seguro o modalidad obliga a cotizar de nuevo.
    fields = (
        "environment",
        "cuit",
        "account",
        "operation",
        "cost_center",
        "origin_mode",
        "destination_mode",
        "insured_operation",
        "time_slot",
    )
    return hashlib.sha256(
        json.dumps({k: getattr(config, k) for k in fields}, sort_keys=True).encode()
    ).hexdigest()


def adapter_cliente(cliente, permiso="cotizar"):
    config = config_productiva()
    matriz = obtener_matriz(cliente)
    fila = next((f for f in (matriz or {}).get("couriers", []) if f["id"] == "oca"), {})
    if not matriz or not matriz.get("activo") or not fila.get("puede_" + permiso):
        raise OCAPortalError("Tu cuenta no tiene habilitada esta operación con OCA.")
    pricing = fila.get("pricing") or {}
    if pricing.get("tipo") not in {"PCT", "MULTIPLICADOR", "FIJO_ARS"} or pricing.get(
        "tramos_usd"
    ):
        raise OCAPortalError(
            "TAURO debe configurar una tarifa nacional para tu cuenta."
        )
    return config, OCAAdapter(config, pricing_loader=lambda *_: pricing)


def preparar(form, config):
    normal = preparar_cotizacion_nacional(
        **{
            k: form.get(k, "")
            for k in (
                "origen_provincia",
                "origen_localidad",
                "origen_cp",
                "destino_provincia",
                "destino_localidad",
                "destino_cp",
                "cantidad_bultos",
                "peso_kg",
                "largo_cm",
                "ancho_cm",
                "alto_cm",
                "valor_declarado_ars",
            )
        },
        modalidad_origen="domicilio",
        modalidad_destino="domicilio",
    )
    payload = {"declared_value": normal["totales"]["valor_declarado_ars"]}
    for side, prefix in [("origin", "origen"), ("destination", "destino")]:
        address = normal[prefix]
        payload[side] = {
            "calle": str(form.get(prefix + "_calle", "")).strip(),
            "nro": str(form.get(prefix + "_numero", "")).strip(),
            "piso": str(form.get(prefix + "_piso", "")).strip(),
            "depto": str(form.get(prefix + "_depto", "")).strip(),
            "cp": address["cp4"],
            "localidad": address["localidad_input"],
            "provincia": {"C": "Capital Federal", "V": "Tierra del Fuego"}.get(
                address["provincia_codigo"], address["provincia_nombre"]
            ),
            "contacto": str(form.get(prefix + "_nombre", "")).strip(),
            "email": str(form.get(prefix + "_email", "")).strip(),
            "telefono": str(form.get(prefix + "_telefono", "")).strip(),
        }
    payload["recipient"] = {
        "first_name": str(form.get("destino_nombre", "")).strip(),
        "last_name": str(form.get("destino_apellido", "")).strip(),
    }
    p = normal["bultos"][0]
    payload["packages"] = [
        {
            "quantity": p["cantidad"],
            "weight_kg": p["peso_unitario_kg"],
            "length_cm": p["largo_cm"],
            "width_cm": p["ancho_cm"],
            "height_cm": p["alto_cm"],
        }
    ]
    _shipment_xml(
        payload,
        config,
        idempotency_key=uuid.uuid4().hex,
        today=datetime.now(timezone.utc).date(),
    )
    return payload


def request_cotizacion(cliente, payload, ident):
    return QuoteRequest(
        request_id=ident,
        customer_id=cliente,
        scope=Ambito.NACIONAL,
        origin={"pais": "AR", "codigo_postal": payload["origin"]["cp"]},
        destination={"pais": "AR", "codigo_postal": payload["destination"]["cp"]},
        packages=tuple(
            Package(
                int(p["quantity"]),
                *(
                    Decimal(p[k])
                    for k in ("weight_kg", "length_cm", "width_cm", "height_cm")
                ),
            )
            for p in payload["packages"]
        ),
        declared_value=Decimal(payload["declared_value"]),
        declared_currency="ARS",
    )


def cotizar(cliente, form):
    config, adapter = adapter_cliente(cliente)
    payload = preparar(form, config)
    ident = uuid.uuid4().hex
    result = adapter.quote(request_cotizacion(cliente, payload, ident))[0]
    if (
        result.state != OperationState.COTIZADO
        or result.customer_price is None
        or result.carrier_cost is None
    ):
        raise OCAPortalError("OCA no devolvió una tarifa para este envío.")
    if result.customer_price < result.carrier_cost:
        raise OCAPortalError("La tarifa requiere revisión de TAURO.")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO oca_portal_cotizaciones
            (id,cliente_id,payload,config_hash,quote_id,precio_ars,costo_ars)
            VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s)""",
            (
                ident,
                cliente,
                json.dumps(payload),
                huella_config(config),
                result.quote_id,
                result.customer_price,
                result.carrier_cost,
            ),
        )
    return ident


def obtener(ident, cliente):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM oca_portal_cotizaciones WHERE id=%s AND cliente_id=%s",
            (ident, cliente),
        )
        row = cur.fetchone()
    if not row:
        raise OCAPortalError("La cotización no existe o no pertenece a tu cuenta.")
    return dict(row)


def validar_vigencia(row, config):
    if row["config_hash"] != huella_config(config) or row["expires_at"] <= datetime.now(
        timezone.utc
    ):
        raise OCAPortalError(
            "La cotización venció o cambió la configuración. Volvé a cotizar."
        )


def confirmar(ident, cliente):
    config, _ = adapter_cliente(cliente)
    # La solicitud y su snapshot se crean juntos; dos clics devuelven el mismo ID.
    from servicios.solicitudes_guia import _congelar_cotizacion_aceptada_con_cursor

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM oca_portal_cotizaciones WHERE id=%s AND cliente_id=%s FOR UPDATE",
            (ident, cliente),
        )
        row = cur.fetchone()
        if not row:
            raise OCAPortalError("Cotización no disponible.")
        if row["solicitud_id"]:
            return row["solicitud_id"]
        validar_vigencia(row, config)
        p = row["payload"]
        origin = p["origin"]
        dest = p["destination"]
        box = p["packages"][0]
        bultos = [
            {
                "cantidad": box["quantity"],
                "peso_kg": box["weight_kg"],
                "largo_cm": box["length_cm"],
                "ancho_cm": box["width_cm"],
                "alto_cm": box["height_cm"],
                "producto_alias": "Paquete nacional",
            }
        ]
        cur.execute(
            """INSERT INTO solicitudes_guia (
            cliente_id,producto_alias,cantidad,destino_pais,remitente_pais,ambito,courier,
            remitente_nombre,remitente_direccion,remitente_ciudad,remitente_estado,remitente_zip,
            remitente_email,remitente_telefono,dest_nombre,dest_direccion,dest_ciudad,dest_estado,
            dest_zip,dest_email,dest_telefono,peso_kg,largo_cm,ancho_cm,alto_cm,bultos,
            precio_tauro_ars,precio_tauro_usd,valor_declarado_usd,servicio_courier,asegurar_carga)
            VALUES (%s,'Paquete nacional',%s,'AR','AR','NACIONAL','OCA',
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,0,0,'Puerta a puerta',%s)
            RETURNING *""",
            (
                cliente,
                box["quantity"],
                origin["contacto"],
                direccion(origin),
                origin["localidad"],
                origin["provincia"],
                origin["cp"],
                origin["email"],
                origin["telefono"],
                p["recipient"]["first_name"] + " " + p["recipient"]["last_name"],
                direccion(dest),
                dest["localidad"],
                dest["provincia"],
                dest["cp"],
                dest["email"],
                dest["telefono"],
                Decimal(box["weight_kg"]) * box["quantity"],
                box["length_cm"],
                box["width_cm"],
                box["height_cm"],
                json.dumps(bultos),
                row["precio_ars"],
                config.insured_operation,
            ),
        )
        sol = dict(cur.fetchone())
        _congelar_cotizacion_aceptada_con_cursor(
            cur,
            sol,
            costo_estimado_manual_ars=row["costo_ars"],
            origen_costo="oca_api_portal",
        )
        cur.execute(
            "UPDATE oca_portal_cotizaciones SET solicitud_id=%s WHERE id=%s",
            (sol["id"], ident),
        )
        return sol["id"]


def direccion(a):
    return " ".join(
        str(a.get(k) or "") for k in ("calle", "nro", "piso", "depto")
    ).strip()


def cotizacion_solicitud(solicitud_id, cliente):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM oca_portal_cotizaciones WHERE solicitud_id=%s AND cliente_id=%s",
            (solicitud_id, cliente),
        )
        row = cur.fetchone()
    if not row:
        raise OCAPortalError(
            "Esta solicitud no tiene una cotización OCA validada. Creá un envío nuevo."
        )
    return dict(row)


def emitir(sol, *, ya_reservada=False):
    from servicios import solicitudes_guia as sg

    ident = sol["id"]
    cliente = sol["cliente_id"]
    if not ya_reservada:
        reserva = sg._reservar_credito_cliente(ident, cliente)
        if not reserva.get("ok"):
            return reserva
    try:
        config, adapter = adapter_cliente(cliente, "emitir")
        row = cotizacion_solicitud(ident, cliente)
        validar_vigencia(row, config)
        if Decimal(str(sol["precio_tauro_ars"])) != row["precio_ars"]:
            raise OCAPortalError("El importe de la solicitud cambió. Volvé a cotizar.")
        # Toda emisión usa el payload privado persistido, nunca campos del POST.
        fresh = adapter.quote(request_cotizacion(cliente, row["payload"], row["id"]))[0]
        if (
            fresh.state != OperationState.COTIZADO
            or fresh.customer_price != row["precio_ars"]
            or fresh.carrier_cost != row["costo_ars"]
        ):
            raise OCAPortalError(
                "La tarifa OCA cambió. Volvé a cotizar antes de emitir."
            )
        _shipment_xml(
            row["payload"],
            config,
            idempotency_key=row["id"],
            today=datetime.now(timezone.utc).date(),
        )
    except Exception as exc:
        sg._liberar_reserva(ident)
        return {
            "ok": False,
            "error": (
                str(exc)
                if isinstance(exc, OCAPortalError)
                else "No pudimos validar la tarifa OCA. No se emitió ninguna guía."
            ),
        }
    # Persistir el remito determinista antes de enviar la solicitud irreversible.
    from servicios.oca_adapter import _remittance

    reference = _remittance(row["id"])
    if not sg._persistir_referencia_courier(ident, reference):
        return {
            "ok": False,
            "error": "No se pudo reservar la referencia OCA. TAURO debe revisar esta solicitud.",
        }
    try:
        result = adapter.create_shipment(
            row["quote_id"], row["payload"], idempotency_key=row["id"]
        )
        if (
            result.state != OperationState.EMITIDO
            or not result.external_id
            or not result.tracking
        ):
            raise OCAPortalError("OCA no confirmó la guía.")
        # Guardar orden/tracking antes de recuperar PDF o registrar cargo.
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE oca_portal_cotizaciones SET external_id=%s, tracking=%s WHERE id=%s",
                (result.external_id, result.tracking, row["id"]),
            )
        sg.guardar_guia_generada(
            ident, result.tracking, None, courier="OCA", message_reference=reference
        )
    except Exception:
        sg._marcar_verificacion_courier(
            ident,
            {
                "message_reference": reference,
                "error": "OCA pudo crear la orden. Conciliar por remito antes de cualquier reintento.",
            },
        )
        return {
            "ok": False,
            "error": "La emisión OCA requiere verificación. No vuelvas a emitir esta solicitud.",
        }
    try:
        pdf = adapter.get_label(result.external_id).label_pdf
        sg.adjuntar_label_guia(ident, pdf)
    except Exception:
        return {"ok": True, "tracking": result.tracking, "etiqueta_pendiente": True}
    return {"ok": True, "tracking": result.tracking}


def recuperar_etiqueta(sol):
    from servicios.solicitudes_guia import adjuntar_label_guia

    config = config_productiva()
    row = cotizacion_solicitud(sol["id"], sol["cliente_id"])
    if (
        row["config_hash"] != huella_config(config)
        or not row["external_id"]
        or sol.get("estado") != "GUIA_LISTA"
    ):
        raise OCAPortalError("La etiqueta requiere revisión de TAURO.")
    return adjuntar_label_guia(
        sol["id"], OCAAdapter(config).get_label(row["external_id"]).label_pdf
    )


def seguimiento(sol):
    config = config_productiva()
    row = cotizacion_solicitud(sol["id"], sol["cliente_id"])
    if row["config_hash"] != huella_config(config) or not row["tracking"]:
        raise OCAPortalError("El seguimiento requiere revisión de TAURO.")
    return OCAAdapter(config).track(row["tracking"]).events
