# ============================================================
# Servicio de solicitudes de guía — PostgreSQL
# ============================================================

import hashlib
import io
import json
import re
import unicodedata
import uuid
from decimal import Decimal
from typing import Any, Optional

import psycopg2
from pypdf import PdfReader, PdfWriter

from core.database import get_conn
from servicios.diferencias_cliente import presentar_diferencia
from servicios.couriers_urls import ambito_envio
from servicios.estados_envio import (
    ESTADO_EMITIENDO,
    ESTADOS_SOLICITUD,
    ESTADOS_VALIDOS,
    presentar_estados_envio,
)
from servicios.numeros_humanos import parse_entero_formulario, parse_float_formulario
from servicios.pesos_envio import pesos_de_solicitud


class IdempotencyConflictError(ValueError):
    """La misma clave externa intentó crear dos pedidos distintos."""


def _congelar_cotizacion_aceptada_con_cursor(
    cur,
    solicitud: dict,
    *,
    costo_estimado_manual_ars: Optional[float] = None,
    base_interna: Optional[dict] = None,
) -> bool:
    """Congela costo estimado, precio aceptado y margen para conciliar luego.

    El snapshot queda ligado a la solicitud y no cambia si el ADMIN modifica
    el markup del cliente en el futuro. Para cargas externas, el costo base
    debe ser informado explícitamente por el operador.
    """
    courier = str(solicitud.get("courier") or "").strip().upper()
    coti_id = str(solicitud.get("coti_id") or "").strip()
    if courier not in {"DHL", "FEDEX", "ANDREANI", "OCA"}:
        return False
    cotizacion = None
    if coti_id and not base_interna:
        cur.execute(
            """
            SELECT costo_fedex_usd, precio_final_usd, precio_final_ars,
                   markup_tipo, markup_valor, peso_kg, peso_usado_kg
            FROM cotizaciones
            WHERE coti_id = %s AND cliente_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (coti_id, solicitud["cliente_id"]),
        )
        cotizacion = cur.fetchone()
    precio_aceptado = Decimal(str(solicitud.get("precio_tauro_ars") or 0))
    if base_interna:
        moneda = str(base_interna.get("moneda_courier") or "").strip().upper()
        costo_nativo = Decimal(str(base_interna.get("costo_courier_estimado")))
        tipo_cambio = Decimal(str(base_interna.get("tipo_cambio_ars")))
        costo_ars = Decimal(str(base_interna.get("costo_courier_estimado_ars")))
        precio_base = Decimal(str(base_interna.get("precio_cliente_inicial_ars")))
        margen_base = Decimal(str(base_interna.get("margen_tauro_protegido_ars")))
        if moneda not in {"USD", "ARS"}:
            raise ValueError("La tarifa privada tiene una moneda no soportada.")
        if abs(precio_base - precio_aceptado) > Decimal("0.02"):
            raise ValueError("La tarifa privada no coincide con el precio aceptado.")
        if abs(costo_nativo * tipo_cambio - costo_ars) > Decimal("0.02"):
            raise ValueError("La conversión de la tarifa privada no es consistente.")
        if abs(precio_base - costo_ars - margen_base) > Decimal("0.02"):
            raise ValueError("El margen de la tarifa privada no es consistente.")
        markup_tipo = base_interna.get("markup_tipo")
        markup_valor = base_interna.get("markup_valor")
        peso_real = base_interna.get("peso_real_cotizado_kg")
        peso_volumetrico = base_interna.get("peso_volumetrico_cotizado_kg")
        peso_facturable = base_interna.get("peso_facturable_cotizado_kg")
        margen = margen_base
        fuente = "recotizacion_pre_emision"
    elif cotizacion:
        precio_usd = Decimal(str(cotizacion.get("precio_final_usd") or 0))
        precio_log_ars = Decimal(str(cotizacion.get("precio_final_ars") or 0))
        costo_nativo = Decimal(str(cotizacion.get("costo_fedex_usd") or 0))
        if precio_usd <= 0 or precio_aceptado <= 0 or costo_nativo < 0:
            raise ValueError("La cotización aceptada no tiene importes consistentes.")
        tipo_cambio = precio_log_ars / precio_usd
        costo_ars = costo_nativo * tipo_cambio
        moneda = "USD"
        markup_tipo = cotizacion.get("markup_tipo")
        markup_valor = cotizacion.get("markup_valor")
        peso_real = cotizacion.get("peso_kg")
        peso_volumetrico = None
        peso_facturable = cotizacion.get("peso_usado_kg")
        fuente = "cotizaciones"
    elif costo_estimado_manual_ars is not None:
        costo_nativo = Decimal(str(costo_estimado_manual_ars))
        tipo_cambio = Decimal("1")
        costo_ars = costo_nativo
        moneda = "ARS"
        markup_tipo = None
        markup_valor = None
        peso_real = solicitud.get("peso_kg")
        peso_volumetrico = None
        peso_facturable = solicitud.get("peso_kg")
        fuente = "carga_manual_admin"
    else:
        return False
    margen = precio_aceptado - costo_ars if not base_interna else margen
    if tipo_cambio <= 0 or costo_ars < 0 or margen < Decimal("-0.02"):
        raise ValueError("La cotización aceptada no conserva un margen válido.")
    margen = max(Decimal("0"), margen)
    huella_payload = {
        "solicitud_id": int(solicitud["id"]),
        "coti_id": coti_id,
        "courier": courier,
        "precio_ars": str(precio_aceptado),
        "costo_nativo": str(costo_nativo),
        "tipo_cambio": str(tipo_cambio),
        "margen_ars": str(margen),
    }
    huella = hashlib.sha256(
        json.dumps(huella_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cur.execute(
        """
        INSERT INTO envio_cotizacion_snapshots (
            solicitud_id, coti_id, courier, servicio_courier,
            moneda_courier, tipo_cambio_ars,
            costo_courier_estimado, costo_courier_estimado_ars,
            precio_cliente_inicial_ars, margen_tauro_protegido_ars,
            markup_tipo, markup_valor,
            peso_real_cotizado_kg, peso_volumetrico_cotizado_kg,
            peso_facturable_cotizado_kg,
            bultos, origen_calculo, aceptado_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s
        )
        ON CONFLICT (solicitud_id) DO NOTHING
        """,
        (
            int(solicitud["id"]), coti_id, courier,
            solicitud.get("servicio_courier"), moneda, tipo_cambio,
            costo_nativo, costo_ars, precio_aceptado, margen,
            markup_tipo, markup_valor, peso_real, peso_volumetrico, peso_facturable,
            json.dumps(solicitud.get("bultos") or []),
            json.dumps({"fuente": fuente, "snapshot_sha256": huella}),
            solicitud.get("created_at"),
        ),
    )
    return True


def _congelar_base_recotizada(solicitud: dict, base_interna: dict) -> bool:
    """Persiste la tarifa privada antes de la operación irreversible."""
    if not isinstance(base_interna, dict):
        return False
    from servicios.conciliacion_couriers import registrar_snapshot_cotizacion

    resultado = registrar_snapshot_cotizacion(
        solicitud_id=int(solicitud["id"]),
        coti_id=str(solicitud.get("coti_id") or "").strip() or None,
        courier=str(solicitud.get("courier") or "").strip().upper(),
        servicio_courier=solicitud.get("servicio_courier"),
        moneda_courier=base_interna.get("moneda_courier"),
        tipo_cambio_ars=base_interna.get("tipo_cambio_ars"),
        costo_courier_estimado=base_interna.get("costo_courier_estimado"),
        precio_cliente_inicial_ars=base_interna.get("precio_cliente_inicial_ars"),
        margen_tauro_protegido_ars=base_interna.get("margen_tauro_protegido_ars"),
        markup_tipo=base_interna.get("markup_tipo"),
        markup_valor=base_interna.get("markup_valor"),
        peso_real_cotizado_kg=base_interna.get("peso_real_cotizado_kg"),
        peso_volumetrico_cotizado_kg=(
            base_interna.get("peso_volumetrico_cotizado_kg")
        ),
        peso_facturable_cotizado_kg=(
            base_interna.get("peso_facturable_cotizado_kg")
        ),
        bultos=solicitud.get("bultos") or [],
        origen_calculo={"fuente": "recotizacion_pre_emision"},
        aceptado_at=solicitud.get("created_at"),
        actor="sistema:emision_courier",
    )
    return bool(resultado.get("id"))


def _reemision_tiene_snapshot(solicitud: dict) -> bool:
    """Una reemisión jamás puede llegar al courier sin su base propia."""
    if not solicitud.get("reemplaza_solicitud_id"):
        return True
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM envio_cotizacion_snapshots WHERE solicitud_id=%s",
                (int(solicitud["id"]),),
            )
            return cur.fetchone() is not None


def _error_ambito_no_emitible(solicitud: dict) -> Optional[str]:
    """Falla cerrado antes de cualquier operación irreversible.

    Hoy las únicas APIs de emisión implementadas son internacionales. Una
    solicitud nacional, histórica o con países contradictorios no puede
    llegar al courier y recién fallar cuando se intenta registrar el cargo.
    """
    from servicios.paises import normalizar_iso2

    origen_iso = normalizar_iso2(
        solicitud.get("ambito_origen")
        or solicitud.get("origen_iso")
        or solicitud.get("remitente_pais")
    )
    destino_iso = normalizar_iso2(
        solicitud.get("ambito_destino")
        or solicitud.get("destino_iso")
        or solicitud.get("destino_pais")
    )
    if not origen_iso or not destino_iso:
        return (
            "El ámbito de esta solicitud necesita revisión porque el origen y "
            "el destino no son países reconocidos. No se emitió ni se generó "
            "ningún cargo."
        )

    # `ambito_envio` también protege contradicciones entre la ruta y el valor
    # persistido. Le entregamos ISO canónicos para que un nombre completo
    # histórico (por ejemplo ESTADOS UNIDOS) jamás se lea como sus primeras
    # dos letras.
    solicitud_normalizada = {
        **solicitud,
        "ambito_origen": origen_iso,
        "origen_iso": origen_iso,
        "remitente_pais": origen_iso,
        "ambito_destino": destino_iso,
        "destino_iso": destino_iso,
        "destino_pais": destino_iso,
    }
    ambito = ambito_envio(solicitud_normalizada)
    if ambito == "internacional":
        return None
    if ambito == "nacional":
        return (
            "Los envíos nacionales todavía no se emiten desde TAURO. "
            "Se habilitarán con las conexiones directas de Andreani y OCA; "
            "no se emitió ni se generó ningún cargo."
        )
    return (
        "El ámbito de esta solicitud necesita revisión porque el origen y "
        "el destino no permiten clasificarla con seguridad. No se emitió ni "
        "se generó ningún cargo."
    )


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def reconciliar_solicitudes_con_cargo_cancelado(
    cliente_id: Optional[str] = None,
) -> int:
    """Cancela expedientes activos cuyo cargo ya fue cancelado.

    Es una reparación idempotente de consistencia: conserva ambas filas y
    deja auditoría dentro de la misma transacción. Se ejecuta antes de las
    lecturas operativas para que una escritura histórica o manual nunca deje
    una guía aparentemente activa frente al cliente o al administrador.
    """
    cliente = (cliente_id or "").strip().upper()
    parametros: tuple = (cliente,) if cliente else ()
    filtro_cliente = "AND s.cliente_id=%s" if cliente else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE solicitudes_guia s
                SET estado='CANCELADO', updated_at=NOW()
                WHERE s.estado NOT IN ('CANCELADO', 'REEMPLAZADO')
                  {filtro_cliente}
                  AND EXISTS (
                      SELECT 1 FROM envios e
                      WHERE e.solicitud_id=s.id
                        AND e.cliente_id=s.cliente_id
                        AND e.estado='CANCELADO'
                  )
                RETURNING s.id, s.cliente_id
                """,
                parametros,
            )
            reparadas = [dict(fila) for fila in cur.fetchall()]
            if reparadas:
                from servicios.auditoria import registrar_evento_con_cursor
                for fila in reparadas:
                    registrar_evento_con_cursor(
                        cur,
                        event="sistema.solicitud_cancelada_por_cargo",
                        actor_type="sistema",
                        actor_ref="consistencia_cuenta_corriente",
                        ip=None,
                        method=None,
                        path=None,
                        status_code=200,
                        success=True,
                        request_id=None,
                        metadata={
                            "solicitud_id": int(fila["id"]),
                            "cliente_id": fila["cliente_id"],
                        },
                    )
    for fila in reparadas:
        print(
            "[solicitudes] cancelada por cargo CANCELADO: "
            f"solicitud={fila['id']} cliente={fila['cliente_id']}"
        )
    return len(reparadas)


def idempotency_hash_origen_tienda(
    *,
    cliente_id: str,
    origen_plataforma: str,
    origen_dominio: str,
    origen_pedido_externo_id: str,
) -> str:
    """Clave opaca y determinística: una venta sólo crea una solicitud.

    El hash no expone dominio, cliente ni identificador del pedido en la base
    de idempotencia. El prefijo versiona el contrato para que su composición
    no se cambie accidentalmente en una futura integración.
    """
    partes = (
        (cliente_id or "").strip().upper(),
        (origen_plataforma or "").strip().lower(),
        (origen_dominio or "").strip().lower(),
        str(origen_pedido_externo_id or "").strip(),
    )
    if not all(partes):
        raise ValueError("Falta el origen verificado del pedido de la tienda.")
    canonico = json.dumps(partes, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(f"tauro-tienda-v1:{canonico}".encode("utf-8")).hexdigest()


def _sin_label(row: dict) -> dict:
    """Reemplaza PDFs por booleanos para no arrastrar BYTEA en las vistas."""
    if (row.get("cargo_estado") == "CANCELADO"
            and row.get("estado") != "REEMPLAZADO"):
        row["estado"] = "CANCELADO"
    if "tiene_label" not in row:
        row["tiene_label"] = bool(row.get("label_pdf"))
    if "tiene_factura_comercial" not in row:
        row["tiene_factura_comercial"] = bool(row.get("commercial_invoice_pdf"))
    if row.get("estado") in {"CANCELADO", "REEMPLAZADO"} or (
        row.get("reemplaza_solicitud_id")
        and (row.get("cargo_pendiente")
             or row.get("reemision_estado") != "EMITIDA")
    ):
        row["tiene_label"] = False
        row["tiene_factura_comercial"] = False
    row.pop("label_pdf", None)
    row.pop("commercial_invoice_pdf", None)
    campos = row.get("reemision_campos_modificados") or []
    if isinstance(campos, str):
        try:
            campos = json.loads(campos)
        except (TypeError, ValueError):
            campos = []
    etiquetas = {
        "remitente_nombre": "nombre del remitente",
        "remitente_contacto": "contacto del remitente",
        "remitente_documento": "documento del remitente",
        "remitente_email": "email del remitente",
        "remitente_telefono": "teléfono del remitente",
        "remitente_direccion": "dirección del remitente",
        "remitente_ciudad": "ciudad del remitente",
        "remitente_estado": "estado/provincia del remitente",
        "remitente_zip": "código postal del remitente",
        "remitente_pais": "país del remitente",
        "destino_pais": "país de destino",
        "dest_nombre": "nombre del destinatario",
        "dest_contacto": "contacto del destinatario",
        "dest_documento": "documento del destinatario",
        "dest_email": "email del destinatario",
        "dest_telefono": "teléfono del destinatario",
        "dest_direccion": "dirección del destinatario",
        "dest_ciudad": "ciudad del destinatario",
        "dest_estado": "estado/provincia del destinatario",
        "dest_zip": "código postal del destinatario",
        "producto_alias": "producto",
        "cantidad": "cantidad de cajas",
        "peso_kg": "peso",
        "largo_cm": "largo",
        "ancho_cm": "ancho",
        "alto_cm": "alto",
        "valor_declarado_usd": "valor declarado",
        "bultos": "cajas e invoice comercial",
        "tax_paga": "responsable de impuestos",
        "asegurar_carga": "protección de carga",
        "observaciones": "observaciones",
    }
    row["reemision_cambios"] = [etiquetas.get(c, c) for c in campos]
    return row


_CAMPOS_REEMISION_AUDITABLES = (
    "remitente_nombre", "remitente_contacto", "remitente_documento",
    "remitente_email", "remitente_telefono", "remitente_direccion",
    "remitente_ciudad", "remitente_estado", "remitente_zip",
    "remitente_pais", "destino_pais", "dest_nombre", "dest_contacto",
    "dest_documento", "dest_email", "dest_telefono", "dest_direccion",
    "dest_ciudad", "dest_estado", "dest_zip", "producto_alias",
    "cantidad", "peso_kg", "largo_cm", "ancho_cm", "alto_cm",
    "valor_declarado_usd", "bultos", "tax_paga", "asegurar_carga",
    "observaciones",
)


def _valor_comparable_reemision(valor: Any) -> str:
    """Representación estable sólo para detectar cambios, nunca para cobrar."""
    if isinstance(valor, str):
        return " ".join(valor.strip().split())
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, sort_keys=True, ensure_ascii=False, default=str)
    if valor is None:
        return ""
    return str(valor)


def _campos_modificados_reemision(anterior: dict, nueva: dict) -> list[str]:
    return [
        campo for campo in _CAMPOS_REEMISION_AUDITABLES
        if _valor_comparable_reemision(anterior.get(campo))
        != _valor_comparable_reemision(nueva.get(campo))
    ]


def validar_reemision_cliente(
    solicitud_id: int,
    cliente_id: str,
    *,
    consultar_courier: bool = False,
    cliente_dhl=None,
    reemision_nueva_id: Optional[int] = None,
) -> dict:
    """Valida que una guía DHL pueda descartarse y reemplazarse.

    El control se repite al abrir, al guardar la corrección y justo antes de
    emitir. Un botón visible nunca es autorización suficiente para crear un
    segundo AWB real.
    """
    cliente_id = (cliente_id or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.cliente_id, s.estado, s.courier, s.tracking,
                       s.tracking_estado, s.cargo_pendiente,
                       e.id AS cargo_id, e.estado AS cargo_estado,
                       e.nro_fc AS cargo_nro_fc, e.monto_ars AS cargo_monto_ars,
                       r.solicitud_nueva_id AS reemision_existente_id,
                       EXISTS (
                           SELECT 1 FROM ajustes_cliente a
                           WHERE a.solicitud_id=s.id
                             AND a.estado <> 'ANULADO'
                       ) AS tiene_ajustes_contables,
                       EXISTS (
                           SELECT 1 FROM recolecciones p
                           WHERE p.solicitud_id=s.id
                             AND p.estado IN ('AGENDANDO', 'AGENDADA',
                                              'CANCELANDO', 'VERIFICAR_COURIER')
                       ) AS tiene_recoleccion_activa
                FROM solicitudes_guia s
                LEFT JOIN envios e ON e.solicitud_id=s.id
                LEFT JOIN solicitudes_guia_reemisiones r
                  ON r.solicitud_anterior_id=s.id
                WHERE s.id=%s AND s.cliente_id=%s
                """,
                (int(solicitud_id), cliente_id),
            )
            fila = cur.fetchone()

    if not fila:
        return {"ok": False, "error": "Ese envío no existe o no es de tu cuenta."}
    fila = dict(fila)
    if (fila.get("courier") or "").strip().upper() != "DHL":
        return {"ok": False, "error":
                "Por ahora sólo se pueden corregir y reemitir guías DHL."}
    if (fila.get("reemision_existente_id")
            and int(fila["reemision_existente_id"]) != int(reemision_nueva_id or 0)):
        return {
            "ok": False,
            "error": "Esta guía ya tiene una corrección preparada. Abrí la guía nueva.",
            "reemision_existente_id": int(fila["reemision_existente_id"]),
        }
    if fila.get("estado") != "GUIA_LISTA" or not fila.get("tracking"):
        return {"ok": False, "error":
                "Sólo podés corregir una guía recién emitida que todavía esté lista."}
    if fila.get("tracking_estado"):
        return {"ok": False, "error":
                "DHL ya informó movimientos para esta guía. Escribile a Tauro para corregirla."}
    if not fila.get("cargo_id") or fila.get("cargo_estado") != "ACTIVO":
        return {"ok": False, "error":
                "El cargo de esta guía necesita conciliación antes de reemplazarla."}
    if fila.get("cargo_pendiente"):
        return {"ok": False, "error":
                "La guía todavía está conciliando su cargo. Probá nuevamente en unos segundos."}
    if str(fila.get("cargo_nro_fc") or "").strip():
        return {"ok": False, "error":
                "Esta guía ya fue facturada. Tauro debe corregirla con respaldo contable."}
    if fila.get("tiene_ajustes_contables"):
        return {"ok": False, "error":
                "Esta guía tiene ajustes contables. Tauro debe revisar la corrección."}
    if fila.get("tiene_recoleccion_activa"):
        return {"ok": False, "error":
                "Esta guía tiene una recolección activa. Cancelala antes de corregirla."}

    if consultar_courier:
        if cliente_dhl is None:
            from core.dhl_client import DHLClient
            cliente_dhl = DHLClient()
        try:
            respuesta = cliente_dhl.track(str(fila["tracking"]))
        except Exception as exc:
            respuesta = {"encontrado": False, "error": type(exc).__name__}
        eventos = [
            evento for evento in (respuesta.get("eventos") or [])
            if isinstance(evento, dict)
        ] if isinstance(respuesta, dict) else []
        if respuesta.get("encontrado") and eventos:
            return {"ok": False, "error":
                    "DHL ya registró movimientos para esta guía. No puede reemitirse desde el portal."}
        if not respuesta.get("encontrado"):
            # Recién emitida: MyDHL puede tardar en publicar el AWB y responde
            # 404. Eso confirma que todavía no hay movimientos. Cualquier
            # otro error se bloquea para no asumir que una falla es ausencia.
            estado_http = respuesta.get("http_status")
            error = str(respuesta.get("error") or "")
            if estado_http != 404 and error != "Sin datos de tracking":
                return {"ok": False, "error":
                        "No pudimos confirmar con DHL que la guía siga sin movimientos. "
                        "Probá nuevamente en unos minutos."}

    return {"ok": True, "tracking_anterior": str(fila["tracking"]),
            "cargo_monto_ars": fila.get("cargo_monto_ars")}


def _validar_cancelacion_desde_fila(fila: dict) -> dict:
    """Reglas determinísticas para cancelar sin borrar historia ni deuda real."""
    if not fila:
        return {"ok": False, "error": "Ese envío no existe o no es de tu cuenta."}
    estado = str(fila.get("estado") or "").upper()
    tracking = str(fila.get("tracking") or "").strip()
    if estado == "CANCELADO":
        return {"ok": False, "error": "Este envío ya está cancelado."}
    if fila.get("control_existente_id"):
        return {
            "ok": False,
            "error": "Este envío ya fue corregido o cancelado anteriormente.",
        }
    if fila.get("tiene_ajustes_contables"):
        return {
            "ok": False,
            "error": "La guía tiene ajustes contables y requiere revisión de Tauro.",
        }
    if fila.get("tiene_recoleccion_activa"):
        return {
            "ok": False,
            "error": "La guía tiene una recolección activa. Cancelala antes de cancelar el envío.",
        }

    # Una solicitud que todavía no llegó al courier puede cancelarse sin
    # control de tracking ni movimiento contable.
    if estado == "SOLICITADO" and not tracking and not fila.get("cargo_id"):
        return {"ok": True, "modo": "SOLICITUD", "tracking_anterior": ""}

    if str(fila.get("courier") or "").upper() != "DHL":
        return {
            "ok": False,
            "error": "Por ahora sólo se pueden cancelar guías emitidas por DHL.",
        }
    if estado != "GUIA_LISTA" or not tracking:
        return {
            "ok": False,
            "error": "Sólo se puede cancelar una guía DHL lista y todavía no despachada.",
        }
    if fila.get("tracking_estado"):
        return {
            "ok": False,
            "error": "DHL ya informó movimientos. El envío no puede cancelarse desde el portal.",
        }
    if not fila.get("cargo_id") or fila.get("cargo_estado") != "ACTIVO":
        return {
            "ok": False,
            "error": "El cargo necesita conciliación antes de cancelar el envío.",
        }
    if fila.get("cargo_pendiente"):
        return {
            "ok": False,
            "error": "La guía todavía está registrando su cargo. Probá nuevamente en unos segundos.",
        }
    if str(fila.get("cargo_nro_fc") or "").strip():
        return {
            "ok": False,
            "error": "La guía ya fue facturada. Tauro debe cancelarla con respaldo contable.",
        }
    return {
        "ok": True,
        "modo": "GUIA_DHL",
        "tracking_anterior": tracking,
        "cargo_monto_ars": fila.get("cargo_monto_ars"),
    }


def validar_cancelacion_cliente(
    solicitud_id: int,
    cliente_id: str,
    *,
    consultar_courier: bool = False,
    cliente_dhl=None,
) -> dict:
    """Determina si el cliente puede cancelar sin riesgo operativo/contable."""
    cliente_id = (cliente_id or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.cliente_id, s.estado, s.courier, s.tracking,
                       s.tracking_estado, s.cargo_pendiente,
                       e.id AS cargo_id, e.estado AS cargo_estado,
                       e.nro_fc AS cargo_nro_fc, e.monto_ars AS cargo_monto_ars,
                       r.id AS control_existente_id,
                       EXISTS (
                           SELECT 1 FROM ajustes_cliente a
                           WHERE a.solicitud_id=s.id AND a.estado <> 'ANULADO'
                       ) AS tiene_ajustes_contables,
                       EXISTS (
                           SELECT 1 FROM recolecciones p
                           WHERE p.solicitud_id=s.id
                             AND p.estado IN ('AGENDANDO', 'AGENDADA',
                                              'CANCELANDO', 'VERIFICAR_COURIER')
                       ) AS tiene_recoleccion_activa
                FROM solicitudes_guia s
                LEFT JOIN envios e ON e.solicitud_id=s.id
                LEFT JOIN solicitudes_guia_reemisiones r
                  ON r.solicitud_anterior_id=s.id
                WHERE s.id=%s AND s.cliente_id=%s
                """,
                (int(solicitud_id), cliente_id),
            )
            fila = cur.fetchone()

    resultado = _validar_cancelacion_desde_fila(dict(fila) if fila else {})
    if not resultado.get("ok") or resultado.get("modo") != "GUIA_DHL":
        return resultado
    if not consultar_courier:
        return resultado

    if cliente_dhl is None:
        from core.dhl_client import DHLClient
        cliente_dhl = DHLClient()
    try:
        respuesta = cliente_dhl.track(resultado["tracking_anterior"])
    except Exception as exc:
        respuesta = {"encontrado": False, "error": type(exc).__name__}
    eventos = [
        evento for evento in (respuesta.get("eventos") or [])
        if isinstance(evento, dict)
    ] if isinstance(respuesta, dict) else []
    if respuesta.get("encontrado") and eventos:
        return {
            "ok": False,
            "error": "DHL ya registró movimientos. El envío no puede cancelarse desde el portal.",
        }
    if not respuesta.get("encontrado"):
        estado_http = respuesta.get("http_status")
        error = str(respuesta.get("error") or "")
        if estado_http != 404 and error != "Sin datos de tracking":
            return {
                "ok": False,
                "error": (
                    "No pudimos confirmar con DHL que la guía siga sin movimientos. "
                    "Probá nuevamente en unos minutos."
                ),
            }
    return resultado


def cancelar_solicitud_cliente(
    solicitud_id: int,
    cliente_id: str,
    *,
    motivo: str = "Cancelado por el cliente",
) -> dict:
    """Cancela solicitud+cargo y registra el tracking viejo en una transacción.

    No borra el PDF ni la fila histórica. Las lecturas del portal dejan de
    entregar esos documentos por estado y el tracking queda en vigilancia una
    sola vez a los siete días, igual que una guía reemplazada.
    """
    from servicios.auditoria import registrar_evento_con_cursor

    solicitud_id = int(solicitud_id)
    cliente_id = (cliente_id or "").strip().upper()
    motivo = " ".join(str(motivo or "").strip().split())[:300]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.cliente_id, s.estado, s.courier, s.tracking,
                       s.tracking_estado, s.cargo_pendiente,
                       EXISTS (
                           SELECT 1 FROM solicitudes_guia_reemisiones r
                           WHERE r.solicitud_anterior_id=s.id
                       ) AS control_existente_id,
                       EXISTS (
                           SELECT 1 FROM ajustes_cliente a
                           WHERE a.solicitud_id=s.id AND a.estado <> 'ANULADO'
                       ) AS tiene_ajustes_contables,
                       EXISTS (
                           SELECT 1 FROM recolecciones p
                           WHERE p.solicitud_id=s.id
                             AND p.estado IN ('AGENDANDO', 'AGENDADA',
                                              'CANCELANDO', 'VERIFICAR_COURIER')
                       ) AS tiene_recoleccion_activa
                FROM solicitudes_guia s
                WHERE s.id=%s AND s.cliente_id=%s
                FOR UPDATE
                """,
                (solicitud_id, cliente_id),
            )
            solicitud = cur.fetchone()
            fila = dict(solicitud) if solicitud else {}
            cur.execute(
                """
                SELECT id AS cargo_id, estado AS cargo_estado,
                       nro_fc AS cargo_nro_fc, monto_ars AS cargo_monto_ars
                FROM envios
                WHERE solicitud_id=%s AND cliente_id=%s
                ORDER BY id DESC LIMIT 1
                FOR UPDATE
                """,
                (solicitud_id, cliente_id),
            )
            cargo = cur.fetchone()
            if cargo:
                fila.update(dict(cargo))
            validacion = _validar_cancelacion_desde_fila(fila)
            if not validacion.get("ok"):
                return validacion

            if validacion["modo"] == "GUIA_DHL":
                cur.execute(
                    """
                    UPDATE envios
                    SET estado='CANCELADO'
                    WHERE id=%s AND cliente_id=%s AND estado='ACTIVO'
                      AND NULLIF(BTRIM(nro_fc), '') IS NULL
                    RETURNING id
                    """,
                    (fila["cargo_id"], cliente_id),
                )
                if not cur.fetchone():
                    raise ValueError(
                        "El cargo cambió mientras cancelábamos. No se modificó el envío."
                    )

            cur.execute(
                """
                UPDATE solicitudes_guia
                SET estado='CANCELADO', updated_at=NOW()
                WHERE id=%s AND cliente_id=%s AND estado=%s
                RETURNING id
                """,
                (solicitud_id, cliente_id, fila["estado"]),
            )
            if not cur.fetchone():
                raise ValueError(
                    "El envío cambió mientras cancelábamos. No se aplicó la cancelación."
                )

            control_id = None
            if validacion["modo"] == "GUIA_DHL":
                cur.execute(
                    """
                    INSERT INTO solicitudes_guia_reemisiones (
                        cliente_id, solicitud_anterior_id, solicitud_nueva_id,
                        operacion, tracking_anterior, campos_modificados,
                        motivo, estado, riesgo_estado, completed_at
                    ) VALUES (%s, %s, NULL, 'CANCELACION', %s,
                              '[]'::jsonb, %s, 'EMITIDA', 'VIGILAR', NOW())
                    RETURNING id
                    """,
                    (
                        cliente_id, solicitud_id,
                        validacion["tracking_anterior"], motivo,
                    ),
                )
                control_id = int(cur.fetchone()["id"])

            registrar_evento_con_cursor(
                cur,
                event="portal.envio_cancelado",
                actor_type="cliente",
                actor_ref=cliente_id,
                ip=None,
                method=None,
                path=None,
                status_code=200,
                success=True,
                request_id=None,
                metadata={
                    "solicitud_id": solicitud_id,
                    "cliente_id": cliente_id,
                    "modo": validacion["modo"],
                    "tracking": validacion.get("tracking_anterior") or None,
                    "cargo_id": fila.get("cargo_id"),
                    "control_id": control_id,
                },
            )
    return {
        "ok": True,
        "solicitud_id": solicitud_id,
        "modo": validacion["modo"],
        "tracking_anterior": validacion.get("tracking_anterior") or "",
        "control_id": control_id,
    }


def crear_solicitud_guia(
    *,
    cliente_id: str,
    producto_alias: str,
    cantidad: int,
    destino_pais: str,
    dest_nombre: str,
    dest_documento: str,
    dest_email: str,
    dest_telefono: str,
    dest_direccion: str,
    dest_ciudad: str,
    dest_estado: str,
    dest_zip: str,
    dest_contacto: str = "",
    observaciones: str = "",
    peso_kg: float,
    largo_cm: float,
    ancho_cm: float,
    alto_cm: float,
    valor_declarado_usd: float,
    ruta_id: str,
    coti_id: str,
    precio_tauro_ars: float,
    precio_tauro_usd: float,
    remitente_alias: str = "",
    remitente_nombre: str = "",
    remitente_documento: str = "",
    remitente_email: str = "",
    remitente_telefono: str = "",
    remitente_direccion: str = "",
    remitente_ciudad: str = "",
    remitente_estado: str = "",
    remitente_zip: str = "",
    remitente_pais: str = "",
    remitente_contacto: str = "",
    precio_cliente_final_ars: Optional[float] = None,
    bultos: Optional[list] = None,
    courier: str = "FEDEX",
    servicio_courier: Optional[str] = None,
    # Quién paga los impuestos de destino EN ESTE envío (DESTINATARIO |
    # CLIENTE). Define el incoterm de la guía, por eso se congela acá.
    tax_paga: Optional[str] = None,
    # Protección opcional de la carga. En DHL se cotiza y emite como VAS II.
    asegurar_carga: bool = False,
    api_referencia: str = "",
    idempotency_key_hash: str = "",
    request_fingerprint: str = "",
    origen_plataforma: str = "",
    origen_dominio: str = "",
    origen_pedido_externo_id: str = "",
    costo_courier_estimado_ars: Optional[float] = None,
    reemplaza_solicitud_id: Optional[int] = None,
    reemision_motivo: str = "",
) -> dict:
    """Crea una solicitud de guía pendiente para gestión operativa.

    bultos (multi-bulto): lista [{producto_alias, cantidad, peso_kg, largo_cm,
    ancho_cm, alto_cm, valor_unitario_usd, hs_code, descripcion_en}, ...].
    Los campos legacy (producto_alias, cantidad, peso/dims/valor) guardan el
    primer bulto + totales para que listados y admin sigan andando."""
    cliente_id = cliente_id.strip().upper()
    cantidad = parse_entero_formulario(
        cantidad, "Cantidad de cajas", minimo=1, maximo=20,
    )
    from servicios.paises import normalizar_iso2
    origen_iso = normalizar_iso2(_clean(remitente_pais) or "AR")
    destino_iso = normalizar_iso2(destino_pais)
    if not origen_iso or not destino_iso:
        raise ValueError("Origen y destino deben ser países válidos para clasificar el envío.")
    ambito = "NACIONAL" if origen_iso == "AR" and destino_iso == "AR" else "INTERNACIONAL"
    origen_plataforma_norm = _clean((origen_plataforma or "").lower())
    origen_dominio_norm = _clean((origen_dominio or "").lower())
    origen_pedido_norm = (
        _clean(str(origen_pedido_externo_id))
        if origen_pedido_externo_id is not None else None
    )
    reemplaza_id = int(reemplaza_solicitud_id) if reemplaza_solicitud_id else None

    with get_conn() as conn:
        with conn.cursor() as cur:
            anterior_reemision = None
            if reemplaza_id:
                if (courier or "").strip().upper() != "DHL":
                    raise ValueError("Una corrección DHL debe volver a emitirse con DHL.")
                cur.execute(
                    """
                    SELECT id, cliente_id, estado, courier, tracking,
                           tracking_estado, cargo_pendiente,
                           remitente_nombre, remitente_contacto,
                           remitente_documento, remitente_email,
                           remitente_telefono, remitente_direccion,
                           remitente_ciudad, remitente_estado, remitente_zip,
                           remitente_pais, destino_pais, dest_nombre,
                           dest_contacto, dest_documento, dest_email,
                           dest_telefono, dest_direccion, dest_ciudad,
                           dest_estado, dest_zip, producto_alias, cantidad,
                           peso_kg, largo_cm, ancho_cm, alto_cm,
                           valor_declarado_usd, bultos, tax_paga,
                           asegurar_carga, observaciones
                    FROM solicitudes_guia
                    WHERE id=%s AND cliente_id=%s
                    FOR UPDATE
                    """,
                    (reemplaza_id, cliente_id),
                )
                anterior_reemision = cur.fetchone()
                if not anterior_reemision:
                    raise ValueError("La guía que querés corregir no existe o no es tuya.")
                anterior_reemision = dict(anterior_reemision)
                if ((anterior_reemision.get("courier") or "").upper() != "DHL"
                        or anterior_reemision.get("estado") != "GUIA_LISTA"
                        or not anterior_reemision.get("tracking")
                        or anterior_reemision.get("tracking_estado")
                        or anterior_reemision.get("cargo_pendiente")):
                    raise ValueError(
                        "La guía anterior ya no está disponible para una reemisión automática."
                    )
                cur.execute(
                    """
                    SELECT e.id, e.estado, e.nro_fc,
                           EXISTS (
                               SELECT 1 FROM ajustes_cliente a
                               WHERE a.solicitud_id=e.solicitud_id
                                 AND a.estado <> 'ANULADO'
                           ) AS tiene_ajustes_contables
                    FROM envios e
                    WHERE e.solicitud_id=%s AND e.cliente_id=%s
                    FOR UPDATE
                    """,
                    (reemplaza_id, cliente_id),
                )
                cargo_anterior = cur.fetchone()
                if (not cargo_anterior or cargo_anterior.get("estado") != "ACTIVO"
                        or str(cargo_anterior.get("nro_fc") or "").strip()
                        or cargo_anterior.get("tiene_ajustes_contables")):
                    raise ValueError(
                        "El cargo anterior necesita revisión antes de reemitir la guía."
                    )
                cur.execute(
                    """
                    SELECT solicitud_nueva_id
                    FROM solicitudes_guia_reemisiones
                    WHERE solicitud_anterior_id=%s
                    """,
                    (reemplaza_id,),
                )
                if cur.fetchone():
                    raise ValueError("Esta guía ya tiene una corrección creada.")
                cur.execute(
                    """
                    SELECT id FROM recolecciones
                    WHERE solicitud_id=%s
                      AND estado IN ('AGENDANDO', 'AGENDADA', 'CANCELANDO',
                                     'VERIFICAR_COURIER')
                    LIMIT 1
                    """,
                    (reemplaza_id,),
                )
                if cur.fetchone():
                    raise ValueError(
                        "Cancelá la recolección activa antes de corregir esta guía."
                    )
            if origen_plataforma_norm == "shopify":
                from servicios.integraciones_tienda import validar_origen_shopify_con_cursor
                if not validar_origen_shopify_con_cursor(
                    cur,
                    cliente_id=cliente_id,
                    dominio=origen_dominio_norm or "",
                    pedido_externo_id=origen_pedido_norm or "",
                ):
                    raise ValueError(
                        "El pedido de Shopify ya no está activo en esta cuenta "
                        "o fue eliminado por privacidad."
                    )
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, cantidad, destino_pais,
                    remitente_alias, remitente_nombre, remitente_documento,
                    remitente_email, remitente_telefono, remitente_direccion,
                    remitente_ciudad, remitente_estado, remitente_zip, remitente_pais,
                    ambito,
                    dest_nombre, dest_documento, dest_email, dest_telefono,
                    dest_direccion, dest_ciudad, dest_estado, dest_zip,
                    observaciones, peso_kg, largo_cm, ancho_cm, alto_cm,
                    valor_declarado_usd, ruta_id, coti_id, precio_tauro_ars,
                    precio_tauro_usd, precio_cliente_final_ars, bultos,
                    courier, servicio_courier, tax_paga,
                    remitente_contacto, dest_contacto,
                    api_referencia, idempotency_key_hash, request_fingerprint,
                    asegurar_carga,
                    origen_plataforma, origen_dominio, origen_pedido_externo_id
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (cliente_id, idempotency_key_hash)
                    WHERE idempotency_key_hash IS NOT NULL
                DO NOTHING
                RETURNING *
                """,
                (
                    cliente_id,
                    producto_alias.strip(),
                    cantidad,
                    destino_iso,
                    _clean(remitente_alias),
                    _clean(remitente_nombre),
                    _clean(remitente_documento),
                    _clean(remitente_email),
                    _clean(remitente_telefono),
                    _clean(remitente_direccion),
                    _clean(remitente_ciudad),
                    _clean(remitente_estado),
                    _clean(remitente_zip),
                    origen_iso,
                    ambito,
                    dest_nombre.strip(),
                    _clean(dest_documento),
                    _clean(dest_email),
                    _clean(dest_telefono),
                    dest_direccion.strip(),
                    dest_ciudad.strip(),
                    _clean(dest_estado),
                    dest_zip.strip(),
                    _clean(observaciones),
                    peso_kg,
                    largo_cm,
                    ancho_cm,
                    alto_cm,
                    valor_declarado_usd,
                    ruta_id,
                    coti_id,
                    precio_tauro_ars,
                    precio_tauro_usd,
                    precio_cliente_final_ars,
                    json.dumps(bultos) if bultos else None,
                    (courier or "FEDEX").strip().upper(),
                    _clean(servicio_courier),
                    _clean(tax_paga),
                    _clean(remitente_contacto),
                    _clean(dest_contacto),
                    _clean(api_referencia),
                    _clean(idempotency_key_hash),
                    _clean(request_fingerprint),
                    bool(asegurar_carga),
                    origen_plataforma_norm,
                    origen_dominio_norm,
                    origen_pedido_norm,
                ),
            )
            row = cur.fetchone()
            if row:
                resultado = dict(row)
                _congelar_cotizacion_aceptada_con_cursor(
                    cur,
                    resultado,
                    costo_estimado_manual_ars=costo_courier_estimado_ars,
                )
                if anterior_reemision:
                    campos_modificados = _campos_modificados_reemision(
                        anterior_reemision, resultado,
                    )
                    cur.execute(
                        """
                        INSERT INTO solicitudes_guia_reemisiones (
                            cliente_id, solicitud_anterior_id,
                            solicitud_nueva_id, tracking_anterior,
                            campos_modificados, motivo, estado
                        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'PENDIENTE')
                        ON CONFLICT (solicitud_anterior_id) DO NOTHING
                        RETURNING id
                        """,
                        (
                            cliente_id, reemplaza_id, resultado["id"],
                            anterior_reemision["tracking"],
                            json.dumps(campos_modificados),
                            _clean(reemision_motivo),
                        ),
                    )
                    if cur.fetchone() is None:
                        raise ValueError("Esta guía ya tiene una corrección creada.")
                    from servicios.auditoria import registrar_evento_con_cursor
                    registrar_evento_con_cursor(
                        cur,
                        event="portal.reemision_preparada",
                        actor_type="cliente",
                        actor_ref=cliente_id,
                        ip=None,
                        method=None,
                        path=None,
                        status_code=200,
                        success=True,
                        request_id=None,
                        metadata={
                            "solicitud_anterior_id": reemplaza_id,
                            "solicitud_nueva_id": int(resultado["id"]),
                            "tracking_anterior": anterior_reemision["tracking"],
                            "campos_modificados": campos_modificados,
                        },
                    )
                resultado["_idempotent_replay"] = False
                return resultado

            # Otra petición con la misma clave ganó la carrera. Recuperamos
            # esa fila dentro de la transacción para que un retry simultáneo
            # nunca cree dos solicitudes ni dos futuros cargos.
            cur.execute(
                """
                SELECT *
                FROM solicitudes_guia
                WHERE cliente_id=%s AND idempotency_key_hash=%s
                LIMIT 1
                """,
                (cliente_id, _clean(idempotency_key_hash)),
            )
            existente = cur.fetchone()
            if not existente:
                raise RuntimeError("No se pudo recuperar el pedido idempotente.")
            existente = dict(existente)
            if existente.get("request_fingerprint") != _clean(request_fingerprint):
                raise IdempotencyConflictError(
                    "La Idempotency-Key ya fue utilizada con datos diferentes."
                )
            existente["_idempotent_replay"] = True
            return existente


def listar_solicitudes_cliente(
    cliente_id: str,
    limite: Optional[int] = 100,
    *,
    desde=None,
    hasta=None,
) -> list[dict]:
    """Solicitudes de guía de un cliente, últimas primero.

    ``limite=None`` devuelve el historial completo. Las vistas de resumen
    deben pasar un número explícito para no traer filas innecesarias.
    """
    cliente = cliente_id.strip().upper()
    reconciliar_solicitudes_con_cargo_cancelado(cliente)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Columnas de listado: nunca traer el BYTEA de la guía. En el
            # historial completo eso podría transferir cientos de PDFs sólo
            # para dibujar un tilde en cada fila.
            query = """
                SELECT s.id, s.cliente_id, s.estado, s.producto_alias, s.cantidad,
                       s.remitente_pais, s.ambito, s.destino_pais, s.dest_nombre,
                       s.dest_ciudad, s.observaciones, s.peso_kg,
                       s.valor_declarado_usd, s.precio_tauro_ars,
                       s.precio_tauro_usd, s.precio_cliente_final_ars, s.tracking,
                       s.guia_url, s.created_at, s.courier, s.bultos,
                       cargo_periodo.estado AS cargo_estado,
                       COALESCE(
                           cargo_periodo.fecha,
                           (s.created_at AT TIME ZONE
                               'America/Argentina/Buenos_Aires')::date
                       ) AS fecha_operacion,
                       s.tracking_estado, s.tracking_estado_courier,
                       s.tracking_descripcion, s.tracking_consultado_at,
                       s.tracking_actualizado_at, s.tracking_finalizado_at,
                       (s.label_pdf IS NOT NULL) AS tiene_label,
                       (s.commercial_invoice_pdf IS NOT NULL)
                           AS tiene_factura_comercial,
                       COALESCE(cargo_periodo.monto_ars,
                                fin.precio_cliente_inicial_ars,
                                s.precio_tauro_ars) AS precio_inicial_cliente_ars,
                       COALESCE(fin.ajuste_cliente_ars, 0)
                           AS diferencia_cliente_ars,
                       COALESCE(fin.diferencia_flete_ars,
                                fin.ajuste_cliente_ars, 0)
                           AS diferencia_flete_ars,
                       COALESCE(fin.tax_cliente_ars, 0)
                           AS tax_cliente_ars,
                       COALESCE(cargo_periodo.monto_ars
                                    + COALESCE(fin.ajuste_cliente_ars, 0),
                                fin.precio_cliente_final_ars,
                                s.precio_tauro_ars) AS precio_final_cliente_ars,
                       fin.peso_cotizado_kg, fin.peso_final_facturado_kg,
                       fin.peso_base_facturado, fin.motivo_diferencia,
                       re_prev.solicitud_anterior_id AS reemplaza_solicitud_id,
                       re_prev.tracking_anterior,
                       re_next.solicitud_nueva_id AS reemplazada_por_solicitud_id,
                       COALESCE(re_next.tracking_nuevo, vigente.tracking)
                           AS tracking_vigente,
                       COALESCE(re_prev.campos_modificados,
                                re_next.campos_modificados)
                           AS reemision_campos_modificados,
                       COALESCE(re_prev.motivo, re_next.motivo)
                           AS reemision_motivo,
                       re_prev.estado AS reemision_estado
                FROM solicitudes_guia s
                LEFT JOIN solicitudes_guia_reemisiones re_prev
                  ON re_prev.solicitud_nueva_id=s.id
                LEFT JOIN solicitudes_guia_reemisiones re_next
                  ON re_next.solicitud_anterior_id=s.id
                LEFT JOIN solicitudes_guia vigente
                  ON vigente.id=re_next.solicitud_nueva_id
                LEFT JOIN envios cargo_periodo
                  ON cargo_periodo.solicitud_id=s.id
                LEFT JOIN LATERAL (
                    SELECT c.precio_cliente_inicial_ars,
                           c.precio_cliente_final_ars, c.ajuste_cliente_ars,
                           c.diferencia_flete_ars, c.tax_cliente_ars,
                           c.peso_cotizado_kg, c.peso_final_facturado_kg,
                           c.peso_base_facturado, c.motivo_diferencia
                    FROM conciliaciones_envio c
                    WHERE c.solicitud_id=s.id AND c.estado='CERRADA'
                    ORDER BY c.version DESC LIMIT 1
                ) fin ON TRUE
                WHERE s.cliente_id = %s AND s.test=FALSE
            """
            params = [cliente]
            if desde is not None:
                query += """
                    AND COALESCE(
                        cargo_periodo.fecha,
                        (s.created_at AT TIME ZONE
                            'America/Argentina/Buenos_Aires')::date
                    ) >= %s
                """
                params.append(desde)
            if hasta is not None:
                query += """
                    AND COALESCE(
                        cargo_periodo.fecha,
                        (s.created_at AT TIME ZONE
                            'America/Argentina/Buenos_Aires')::date
                    ) < %s
                """
                params.append(hasta)
            query += " ORDER BY fecha_operacion DESC, s.created_at DESC"
            if limite is not None:
                query += " LIMIT %s"
                params.append(max(1, int(limite)))
            cur.execute(query, tuple(params))
            return [presentar_estados_envio(_sin_label(dict(r))) for r in cur.fetchall()]


def periodos_solicitudes_cliente(cliente_id: str) -> list[tuple[int, int]]:
    """Años/meses con actividad, sin traer el historial ni sus documentos."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT
                    EXTRACT(YEAR FROM COALESCE(
                        e.fecha,
                        (s.created_at AT TIME ZONE
                            'America/Argentina/Buenos_Aires')::date
                    ))::int AS anio,
                    EXTRACT(MONTH FROM COALESCE(
                        e.fecha,
                        (s.created_at AT TIME ZONE
                            'America/Argentina/Buenos_Aires')::date
                    ))::int AS mes
                FROM solicitudes_guia s
                LEFT JOIN envios e ON e.solicitud_id=s.id
                WHERE s.cliente_id=%s AND s.test=FALSE
                ORDER BY anio DESC, mes DESC
                """,
                (cliente_id.strip().upper(),),
            )
            return [
                (int(fila["anio"]), int(fila["mes"]))
                for fila in cur.fetchall()
                if 2000 <= int(fila.get("anio") or 0) <= 2100
                and 1 <= int(fila.get("mes") or 0) <= 12
            ]


def listar_envios_api(
    cliente_id: str,
    *,
    limite: int = 100,
    offset: int = 0,
    ambito: str = "",
    estado: str = "",
) -> tuple[list[dict], int]:
    """Historial paginado y filtrado para integraciones B2B.

    La selección es explícita: nunca devuelve costos del courier, márgenes,
    errores internos, documentos ni el BYTEA de la etiqueta.
    """
    cliente_id = (cliente_id or "").strip().upper()
    limite = max(1, min(int(limite), 200))
    offset = max(0, int(offset))
    ambito = (ambito or "").strip().upper()
    estado = (estado or "").strip().upper()

    condiciones = ["cliente_id=%s", "test=FALSE"]
    params: list = [cliente_id]
    if ambito:
        condiciones.append("ambito=%s")
        params.append(ambito)
    if estado:
        condiciones.append("estado=%s")
        params.append(estado)
    where = " AND ".join(condiciones)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS total FROM solicitudes_guia WHERE {where}",
                tuple(params),
            )
            total = int(cur.fetchone()["total"])
            cur.execute(
                f"""
                SELECT id, api_referencia, estado, ambito, courier,
                       servicio_courier, producto_alias, cantidad,
                       destino_pais, dest_nombre, dest_ciudad, dest_estado,
                       peso_kg, valor_declarado_usd, precio_tauro_ars,
                       precio_tauro_usd, tracking, guia_url,
                       tracking_estado, tracking_estado_courier,
                       tracking_descripcion, tracking_actualizado_at,
                       (label_pdf IS NOT NULL) AS tiene_label,
                       (commercial_invoice_pdf IS NOT NULL)
                           AS tiene_factura_comercial,
                       created_at, updated_at, guia_generada_at
                FROM solicitudes_guia
                WHERE {where}
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limite, offset),
            )
            filas = [dict(row) for row in cur.fetchall()]
    return filas, total


def contar_guias_listas(cliente_id: str) -> int:
    """
    Cuántas guías tiene el cliente listas para descargar. Es su tarea
    pendiente real (las SOLICITADO esperan a Tauro, no a él), así que
    es lo que alimenta el globo rojo del menú.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM solicitudes_guia s
                WHERE s.cliente_id = %s AND s.estado = 'GUIA_LISTA'
                  AND s.test=FALSE
                  AND NOT EXISTS (
                      SELECT 1
                      FROM envios e
                      WHERE e.solicitud_id = s.id
                        AND e.cliente_id = s.cliente_id
                        AND e.estado = 'CANCELADO'
                  )
                """,
                (cliente_id.strip().upper(),),
            )
            return int(cur.fetchone()["n"])


def listar_solicitudes_admin(estado: str = "", limite: int = 300) -> list[dict]:
    """Solicitudes para la bandeja operativa del admin."""
    estado = (estado or "").strip().upper()
    reconciliar_solicitudes_con_cargo_cancelado()
    params: list = []
    where = "WHERE c.test=FALSE AND s.test=FALSE"
    if estado:
        where += " AND s.estado = %s"
        params.append(estado)
    params.append(limite)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.*, c.nombre AS cliente_nombre, c.email AS cliente_email,
                       (s.estado='VERIFICAR_COURIER' OR
                        (s.estado='EMITIENDO' AND s.tracking IS NULL AND
                         s.courier_message_reference IS NOT NULL AND
                         s.updated_at <= NOW() - INTERVAL '10 minutes'))
                         AS puede_conciliar_courier,
                       (s.estado='EMITIENDO' AND s.tracking IS NULL AND
                        s.courier_message_reference IS NULL AND
                        s.updated_at <= NOW() - INTERVAL '10 minutes')
                         AS puede_liberar_reserva
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                {where}
                ORDER BY
                    CASE s.estado
                        WHEN 'SOLICITADO' THEN 1
                        WHEN 'VERIFICAR_COURIER' THEN 1
                        WHEN 'EN_PROCESO' THEN 2
                        WHEN 'GUIA_LISTA' THEN 3
                        WHEN 'DESPACHADO' THEN 4
                        ELSE 5
                    END,
                    s.created_at DESC
                LIMIT %s
                """,
                params,
            )
            return [presentar_estados_envio(_sin_label(dict(r))) for r in cur.fetchall()]


def actualizar_solicitud_guia(
    solicitud_id: int,
    *,
    estado: str,
    tracking: str = "",
    guia_url: str = "",
    pisar: bool = False,
) -> None:
    """
    Actualiza estado operativo, tracking y URL/documento de guía.

    Por defecto un campo vacío NO pisa lo que ya había: el form del admin
    manda todos los campos siempre, así que cambiar sólo el estado borraba
    el tracking de una guía ya emitida — y con el tracking en blanco la
    solicitud volvía a quedar habilitada para emitir OTRA guía.

    `pisar=True` invierte esa protección A PROPÓSITO: es la salida para un
    tracking mal tipeado, que antes sólo se arreglaba con SQL a mano. Ojo
    con lo que implica dejar el tracking vacío: la solicitud vuelve a ser
    emitible, y si la guía anterior existe en el courier, se puede terminar
    con DOS guías facturadas. Por eso es un flag explícito y no el default.
    """
    estado = (estado or "").strip().upper()
    if estado not in ESTADOS_SOLICITUD:
        raise ValueError(f"Estado inválido: {estado}")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if pisar:
                cur.execute(
                    """
                    UPDATE solicitudes_guia
                    SET estado=%s, tracking=%s, guia_url=%s,
                        tracking_estado=NULL,
                        tracking_estado_courier=NULL,
                        tracking_descripcion=NULL,
                        tracking_consultado_at=NULL,
                        tracking_actualizado_at=NULL,
                        tracking_finalizado_at=NULL,
                        tracking_error=NULL,
                        tracking_error_at=NULL,
                        updated_at=NOW()
                    WHERE id=%s
                      AND estado NOT IN ('EMITIENDO', 'VERIFICAR_COURIER')
                    RETURNING id
                    """,
                    (estado, _clean(tracking), _clean(guia_url), solicitud_id),
                )
                print(f"[solicitudes] solicitud {solicitud_id}: valores PISADOS "
                      f"por el admin (tracking={tracking!r})")
            else:
                cur.execute(
                    """
                    UPDATE solicitudes_guia
                    SET estado=%s,
                        tracking = COALESCE(NULLIF(%s, ''), tracking),
                        guia_url = COALESCE(NULLIF(%s, ''), guia_url),
                        updated_at=NOW()
                    WHERE id=%s
                      AND estado NOT IN ('EMITIENDO', 'VERIFICAR_COURIER')
                    RETURNING id
                    """,
                    (estado, _clean(tracking), _clean(guia_url), solicitud_id),
                )
            if cur.fetchone() is None:
                raise ValueError(
                    "La solicitud se está emitiendo o requiere conciliación con el courier. "
                    "Usá la acción específica de verificación antes de cambiarla."
                )


# Qué puede corregir el admin ANTES de emitir. destino_pais queda afuera a
# propósito: cambiar el país invalida la ruta y el precio cotizado — eso no
# es una corrección, es otra cotización.
CAMPOS_EDITABLES_PRE_EMISION = [
    "dest_nombre", "dest_documento", "dest_email", "dest_telefono",
    "dest_direccion", "dest_ciudad", "dest_estado", "dest_zip",
    "producto_alias", "cantidad", "peso_kg", "largo_cm", "ancho_cm",
    "alto_cm", "valor_declarado_usd", "observaciones",
]


def editar_solicitud_pre_emision(solicitud_id: int, campos: dict) -> None:
    """
    Corrige los datos de una solicitud que TODAVÍA no se emitió (el caso
    típico: el comprador puso mal el piso o el código postal y el
    comerciante avisó tarde). Sobre una guía ya emitida no se toca nada:
    los datos viajaron al courier y corregirlos acá sólo escondería la
    diferencia.
    """
    limpios = {k: v for k, v in campos.items()
               if k in CAMPOS_EDITABLES_PRE_EMISION and v is not None}
    if not limpios:
        raise ValueError("No hay campos editables para guardar.")

    sets = ", ".join(f"{k}=%s" for k in limpios)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE solicitudes_guia
                SET {sets}, updated_at=NOW()
                WHERE id=%s AND tracking IS NULL
                  AND estado NOT IN (%s, 'VERIFICAR_COURIER')
                RETURNING id
                """,
                (*limpios.values(), solicitud_id, ESTADO_EMITIENDO),
            )
            if cur.fetchone() is None:
                raise ValueError(
                    "Esa solicitud ya tiene guía emitida (o se está emitiendo "
                    "ahora): no se puede editar. Si los datos están mal, hay "
                    "que anular con el courier y crear una solicitud nueva.")


def contar_solicitudes_pendientes() -> int:
    reconciliar_solicitudes_con_cargo_cancelado()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id=s.cliente_id
                WHERE s.estado IN ('SOLICITADO', 'EN_PROCESO', 'VERIFICAR_COURIER')
                  AND s.test=FALSE AND c.test=FALSE
                """
            )
            row = cur.fetchone()
    return int(row["n"] if row else 0)


def obtener_solicitud_de_cliente(solicitud_id: int, cliente_id: str) -> Optional[dict]:
    """Una solicitud del cliente logueado (para la página de detalle del
    portal). Chequea pertenencia y no carga los bytes del label."""
    cliente = cliente_id.strip().upper()
    reconciliar_solicitudes_con_cargo_cancelado(cliente)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*,
                       cargo.estado AS cargo_estado,
                       re_prev.solicitud_anterior_id AS reemplaza_solicitud_id,
                       re_prev.tracking_anterior,
                       re_next.solicitud_nueva_id AS reemplazada_por_solicitud_id,
                       COALESCE(re_next.tracking_nuevo, vigente.tracking)
                           AS tracking_vigente,
                       COALESCE(re_prev.campos_modificados,
                                re_next.campos_modificados)
                           AS reemision_campos_modificados,
                       COALESCE(re_prev.motivo, re_next.motivo)
                           AS reemision_motivo,
                       re_prev.estado AS reemision_estado,
                       COALESCE(cargo.monto_ars,
                                fin.precio_cliente_inicial_ars,
                                s.precio_tauro_ars) AS precio_inicial_cliente_ars,
                       COALESCE(fin.ajuste_cliente_ars, 0)
                           AS diferencia_cliente_ars,
                       COALESCE(fin.diferencia_flete_ars,
                                fin.ajuste_cliente_ars, 0)
                           AS diferencia_flete_ars,
                       COALESCE(fin.tax_cliente_ars, 0)
                           AS tax_cliente_ars,
                       COALESCE(cargo.monto_ars
                                    + COALESCE(fin.ajuste_cliente_ars, 0),
                                fin.precio_cliente_final_ars,
                                s.precio_tauro_ars) AS precio_final_cliente_ars,
                       fin.peso_cotizado_kg, fin.peso_final_facturado_kg,
                       fin.peso_base_facturado, fin.motivo_diferencia,
                       fin.conceptos_courier
                FROM solicitudes_guia s
                LEFT JOIN solicitudes_guia_reemisiones re_prev
                  ON re_prev.solicitud_nueva_id=s.id
                LEFT JOIN solicitudes_guia_reemisiones re_next
                  ON re_next.solicitud_anterior_id=s.id
                LEFT JOIN solicitudes_guia vigente
                  ON vigente.id=re_next.solicitud_nueva_id
                LEFT JOIN envios cargo ON cargo.solicitud_id=s.id
                LEFT JOIN LATERAL (
                    SELECT c.precio_cliente_inicial_ars,
                           c.precio_cliente_final_ars, c.ajuste_cliente_ars,
                           c.diferencia_flete_ars, c.tax_cliente_ars,
                           c.peso_cotizado_kg, c.peso_final_facturado_kg,
                           c.peso_base_facturado, c.motivo_diferencia,
                           COALESCE((
                               SELECT STRING_AGG(DISTINCT COALESCE(
                                   NULLIF(BTRIM(i.descripcion), ''),
                                   REPLACE(i.concepto_tipo, '_', ' ')
                               ), ' · ')
                               FROM factura_courier_item_matches m
                               JOIN facturas_courier_items i ON i.id=m.item_id
                               WHERE m.solicitud_id=s.id
                                 AND m.estado='CONFIRMADO'
                                 AND i.concepto_tipo <> 'FLETE'
                           ), '') AS conceptos_courier
                    FROM conciliaciones_envio c
                    WHERE c.solicitud_id=s.id AND c.estado='CERRADA'
                    ORDER BY c.version DESC LIMIT 1
                ) fin ON TRUE
                WHERE s.id = %s AND s.cliente_id = %s AND s.test=FALSE
                """,
                (solicitud_id, cliente),
            )
            row = cur.fetchone()
    if not row:
        return None
    resultado = _sin_label(dict(row))
    resultado["diferencia_detalle"] = presentar_diferencia(resultado)
    resultado["resumen_pesos"] = pesos_de_solicitud(resultado)
    return presentar_estados_envio(resultado)


def obtener_label_de_cliente(solicitud_id: int, cliente_id: str) -> Optional[bytes]:
    """Devuelve el PDF sólo si la solicitud pertenece al dueño de la API key."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT label_pdf
                FROM solicitudes_guia s
                WHERE s.id=%s AND s.cliente_id=%s
                  AND s.test=FALSE
                  AND s.estado <> 'CANCELADO'
                  AND s.estado <> 'REEMPLAZADO'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM envios e
                      WHERE e.solicitud_id = s.id
                        AND e.cliente_id = s.cliente_id
                        AND e.estado = 'CANCELADO'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM solicitudes_guia_reemisiones r
                      WHERE r.solicitud_nueva_id=s.id
                        AND (r.estado <> 'EMITIDA' OR s.cargo_pendiente=TRUE)
                  )
                """,
                (solicitud_id, cliente_id.strip().upper()),
            )
            row = cur.fetchone()
    if not row or row.get("label_pdf") is None:
        return None
    return bytes(row["label_pdf"])


# ── Emisión de guía real (FedEx Ship API) ───────────────────

def obtener_solicitud(solicitud_id: int) -> Optional[dict]:
    """Una solicitud con los datos del cliente (para el remitente por defecto)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*,
                       re_prev.solicitud_anterior_id AS reemplaza_solicitud_id,
                       re_prev.tracking_anterior,
                       re_next.solicitud_nueva_id AS reemplazada_por_solicitud_id,
                       COALESCE(re_next.tracking_nuevo, vigente.tracking)
                           AS tracking_vigente,
                       COALESCE(re_prev.campos_modificados,
                                re_next.campos_modificados)
                           AS reemision_campos_modificados,
                       COALESCE(re_prev.motivo, re_next.motivo)
                           AS reemision_motivo,
                       re_prev.estado AS reemision_estado,
                       c.nombre AS cliente_nombre, c.telefono AS cliente_telefono,
                       c.direccion AS cliente_direccion, c.ciudad AS cliente_ciudad,
                       c.cp AS cliente_cp, c.pais AS cliente_pais,
                       COALESCE(cargo.monto_ars,
                                fin.precio_cliente_inicial_ars,
                                s.precio_tauro_ars) AS precio_inicial_cliente_ars,
                       COALESCE(fin.ajuste_cliente_ars, 0)
                           AS diferencia_cliente_ars,
                       COALESCE(fin.diferencia_flete_ars,
                                fin.ajuste_cliente_ars, 0)
                           AS diferencia_flete_ars,
                       COALESCE(fin.tax_cliente_ars, 0)
                           AS tax_cliente_ars,
                       COALESCE(cargo.monto_ars
                                    + COALESCE(fin.ajuste_cliente_ars, 0),
                                fin.precio_cliente_final_ars,
                                s.precio_tauro_ars) AS precio_final_cliente_ars,
                       fin.peso_cotizado_kg, fin.peso_final_facturado_kg,
                       fin.peso_base_facturado, fin.motivo_diferencia
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                LEFT JOIN solicitudes_guia_reemisiones re_prev
                  ON re_prev.solicitud_nueva_id=s.id
                LEFT JOIN solicitudes_guia_reemisiones re_next
                  ON re_next.solicitud_anterior_id=s.id
                LEFT JOIN solicitudes_guia vigente
                  ON vigente.id=re_next.solicitud_nueva_id
                LEFT JOIN envios cargo ON cargo.solicitud_id=s.id
                LEFT JOIN LATERAL (
                    SELECT ce.precio_cliente_inicial_ars,
                           ce.precio_cliente_final_ars, ce.ajuste_cliente_ars,
                           ce.diferencia_flete_ars, ce.tax_cliente_ars,
                           ce.peso_cotizado_kg, ce.peso_final_facturado_kg,
                           ce.peso_base_facturado, ce.motivo_diferencia
                    FROM conciliaciones_envio ce
                    WHERE ce.solicitud_id=s.id AND ce.estado='CERRADA'
                    ORDER BY ce.version DESC LIMIT 1
                ) fin ON TRUE
                WHERE s.id = %s
                """,
                (solicitud_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def guardar_guia_generada(solicitud_id: int, tracking: str, label_pdf: Optional[bytes],
                          courier: str = "FEDEX",
                          message_reference: Optional[str] = None,
                          commercial_invoice_pdf: Optional[bytes] = None) -> bool:
    """Persiste tracking, documentos emitidos y estado de la guía."""
    tracking = (tracking or "").strip()[:120]
    if not tracking:
        raise ValueError("El courier no devolvió un tracking válido.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitudes_guia
                SET estado='GUIA_LISTA', tracking=%s, label_pdf=%s,
                    commercial_invoice_pdf=%s, courier=%s,
                    tracking_estado=NULL, tracking_estado_courier=NULL,
                    tracking_descripcion=NULL, tracking_consultado_at=NULL,
                    tracking_actualizado_at=NULL, tracking_finalizado_at=NULL,
                    tracking_error=NULL, tracking_error_at=NULL,
                    courier_message_reference=COALESCE(%s, courier_message_reference),
                    courier_error=NULL, cargo_pendiente=TRUE, cargo_error=NULL,
                    guia_generada_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (tracking, psycopg2.Binary(label_pdf) if label_pdf else None,
                 psycopg2.Binary(commercial_invoice_pdf)
                 if commercial_invoice_pdf else None,
                 courier, _clean(message_reference), solicitud_id),
            )
    # DÉBITO AUTOMÁTICO (decisión de Leandro 28/07): la guía emitida carga
    # sola su costo a la cuenta corriente del cliente. Es idempotente (índice
    # único por solicitud) y un fallo acá NO tumba la emisión: la guía ya
    # existe en el courier y eso es lo que no se puede deshacer — el cargo,
    # en el peor caso, se carga a mano y el log lo dice.
    cargo_confirmado = False
    try:
        from servicios.cuenta_corriente import cargar_guia_emitida
        if cargar_guia_emitida(solicitud_id) is not True:
            raise RuntimeError("No se pudo garantizar el cargo de la guía emitida")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE solicitudes_guia
                    SET cargo_pendiente=FALSE, cargo_error=NULL, updated_at=NOW()
                    WHERE id=%s
                """, (solicitud_id,))
        cargo_confirmado = True
    except Exception as e:
        error_cargo = str(e)[:500]
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE solicitudes_guia
                        SET cargo_pendiente=TRUE, cargo_error=%s, updated_at=NOW()
                        WHERE id=%s
                    """, (error_cargo, solicitud_id))
                    cur.execute("""
                        UPDATE solicitudes_guia_reemisiones
                        SET estado='VERIFICAR_COURIER', updated_at=NOW()
                        WHERE solicitud_nueva_id=%s AND estado='PENDIENTE'
                    """, (solicitud_id,))
        except Exception as persistencia_error:
            print(f"[solicitudes] tampoco pude persistir el cargo pendiente "
                  f"de {solicitud_id}: {persistencia_error}")
        print(f"[solicitudes] guía {tracking} emitida pero el cargo automático "
              f"falló ({e}): FACTURAR A MANO la solicitud {solicitud_id}")

    # Si el envío nació de una venta de Shopify, avisamos a la tienda:
    # el pedido queda "Enviado" con su tracking y el comprador recibe el
    # mail solo. Nunca dejamos que un fallo acá tumbe la emisión de la
    # guía — la guía ya está hecha y es lo que importa.
    # En un hilo aparte: avisarle a la tienda puede tardar (API de un
    # tercero, con timeout). El admin no puede quedarse colgado mirando
    # una pantalla en blanco después de emitir — la guía ya está hecha.
    if cargo_confirmado:
        import threading
        threading.Thread(
            target=_avisar_tienda_origen,
            args=(solicitud_id, tracking, courier),
            daemon=True,
        ).start()
    return cargo_confirmado


def adjuntar_label_guia(solicitud_id: int, label_pdf: bytes) -> dict:
    """Adjunta el PDF recuperado sin tocar tracking, estado ni cargo."""
    contenido = bytes(label_pdf or b"")
    if not contenido.startswith(b"%PDF"):
        return {"ok": False, "error": "El archivo no es una etiqueta PDF válida."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes_guia
                SET label_pdf=%s, updated_at=NOW()
                WHERE id=%s AND tracking IS NOT NULL
                RETURNING tracking
            """, (psycopg2.Binary(contenido), solicitud_id))
            fila = cur.fetchone()
            if not fila:
                return {"ok": False, "error":
                        "La solicitud todavía no tiene una guía confirmada."}
    return {"ok": True, "tracking": fila.get("tracking")}


def _avisar_tienda_origen(solicitud_id: int, tracking: str, courier: str) -> None:
    """
    Marca el pedido como enviado en la tienda de origen (Shopify o
    Tiendanube) para que el comprador reciba su seguimiento solo.

    Reintenta: si se pierde, el comprador nunca se entera de que su
    paquete salió y termina escribiéndole al comerciante. Si aun así
    falla, queda anotado con el tracking para poder rehacerlo a mano.
    """
    import time as _t

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.pedido_externo_id, t.dominio, t.plataforma
                    FROM pedidos_tienda p
                    JOIN tiendas_conectadas t ON t.id = p.tienda_id
                    WHERE p.solicitud_id = %s
                    ORDER BY p.id DESC
                    LIMIT 1
                """, (solicitud_id,))
                row = cur.fetchone()
    except Exception as e:
        print(f"[integraciones] no pude buscar el pedido de la solicitud {solicitud_id}: {e}")
        return

    if not row:
        return   # el envío no vino de una tienda: nada que avisar

    plataforma = row["plataforma"]
    dominio = row["dominio"]
    pedido_ext = row["pedido_externo_id"]
    courier_nombre = "FedEx" if courier.upper() == "FEDEX" else courier.title()

    for intento in (1, 2, 3):
        try:
            if plataforma == "shopify":
                from servicios.shopify_app import marcar_enviado, instalacion
                if not instalacion(dominio):
                    # Conectada en modo manual (sin app): no hay token para
                    # escribirle. El comerciante marca el envío él mismo.
                    return
                ok = marcar_enviado(dominio, pedido_ext, tracking, courier_nombre)
            elif plataforma == "tiendanube":
                from servicios.tiendanube_app import marcar_enviado as tn_enviado
                store_id = dominio.replace(".tiendanube", "")
                ok = tn_enviado(store_id, pedido_ext, tracking)
            else:
                return

            if ok:
                print(f"[integraciones] pedido {pedido_ext} de {dominio} marcado "
                      f"como enviado (tracking {tracking})")
                return
        except Exception as e:
            print(f"[integraciones] intento {intento} avisando a {dominio}: {e}")

        if intento < 3:
            _t.sleep(intento * 3)

    # Se agotaron los reintentos: que quede constancia con todo lo
    # necesario para rehacerlo a mano desde el admin de la tienda.
    print(f"[integraciones] ⚠️ NO PUDE avisar a {dominio} ({plataforma}) que el "
          f"pedido {pedido_ext} salió con tracking {tracking}. El comprador NO "
          f"recibió su seguimiento — cargalo a mano en la tienda.")


def obtener_label_pdf(solicitud_id: int, cliente_id: Optional[str] = None) -> Optional[bytes]:
    """
    Devuelve los bytes del label PDF de una solicitud, o None si no existe.
    Si se pasa cliente_id, verifica que la solicitud le pertenezca (para el portal).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if cliente_id:
                cur.execute(
                    """SELECT s.label_pdf FROM solicitudes_guia s
                       WHERE s.id=%s AND s.cliente_id=%s
                         AND s.test=FALSE
                         AND s.estado <> 'CANCELADO'
                         AND s.estado <> 'REEMPLAZADO'
                         AND NOT EXISTS (
                           SELECT 1 FROM envios e
                           WHERE e.solicitud_id=s.id
                             AND e.cliente_id=s.cliente_id
                             AND e.estado = 'CANCELADO'
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM solicitudes_guia_reemisiones r
                           WHERE r.solicitud_nueva_id=s.id
                             AND (r.estado <> 'EMITIDA'
                                  OR s.cargo_pendiente=TRUE)
                         )""",
                    (solicitud_id, cliente_id.strip().upper()),
                )
            else:
                cur.execute(
                    "SELECT label_pdf FROM solicitudes_guia WHERE id=%s",
                    (solicitud_id,),
                )
            row = cur.fetchone()
    if not row or not row["label_pdf"]:
        return None
    return bytes(row["label_pdf"])


def obtener_factura_comercial_pdf(
    solicitud_id: int, cliente_id: Optional[str] = None,
) -> Optional[bytes]:
    """Devuelve la invoice del courier aplicando el mismo control de dueño."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if cliente_id:
                cur.execute(
                    """SELECT s.commercial_invoice_pdf FROM solicitudes_guia s
                       WHERE s.id=%s AND s.cliente_id=%s
                         AND s.test=FALSE
                         AND s.estado <> 'CANCELADO'
                         AND s.estado <> 'REEMPLAZADO'
                         AND NOT EXISTS (
                           SELECT 1 FROM envios e
                           WHERE e.solicitud_id=s.id
                             AND e.cliente_id=s.cliente_id
                             AND e.estado = 'CANCELADO'
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM solicitudes_guia_reemisiones r
                           WHERE r.solicitud_nueva_id=s.id
                             AND (r.estado <> 'EMITIDA'
                                  OR s.cargo_pendiente=TRUE)
                         )""",
                    (solicitud_id, cliente_id.strip().upper()),
                )
            else:
                cur.execute(
                    "SELECT commercial_invoice_pdf FROM solicitudes_guia WHERE id=%s",
                    (solicitud_id,),
                )
            row = cur.fetchone()
    if not row or not row["commercial_invoice_pdf"]:
        return None
    return bytes(row["commercial_invoice_pdf"])


def _segmento_nombre_pdf(valor: Optional[str], respaldo: str) -> str:
    """Convierte datos visibles en un segmento ASCII seguro para HTTP."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r'[\x00-\x1f\x7f"/\\;]+', " ", texto)
    texto = re.sub(r"[^A-Za-z0-9 ._()-]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip(" ._-()").upper()
    return (texto or respaldo)[:64].rstrip(" ._-()")


def nombre_archivo_documentos_envio(
    *, cliente_nombre: Optional[str], dest_nombre: Optional[str],
    destino_pais: Optional[str],
) -> str:
    """Nombre estable: TAURO - CLIENTE - DESTINATARIO - PAIS.pdf."""
    cliente = _segmento_nombre_pdf(cliente_nombre, "CLIENTE")
    destinatario = _segmento_nombre_pdf(dest_nombre, "DESTINATARIO")
    pais = _segmento_nombre_pdf(destino_pais, "PAIS")
    return f"TAURO - {cliente} - {destinatario} - {pais}.pdf"


def unir_guia_e_invoice_pdf(
    guia_pdf: bytes, invoice_pdf: Optional[bytes] = None,
) -> bytes:
    """Une etiquetas e invoice, en ese orden, sin omitir errores de mezcla.

    Las guías históricas pueden no tener invoice; en ese caso se conserva el
    PDF original. Cuando sí existe invoice, un error debe ser visible: devolver
    sólo la etiqueta haría creer al cliente que descargó el legajo completo.
    """
    if not guia_pdf:
        raise ValueError("La guía no contiene un PDF descargable.")
    if not invoice_pdf:
        return bytes(guia_pdf)

    writer = PdfWriter()
    total_paginas = 0
    try:
        for contenido in (guia_pdf, invoice_pdf):
            reader = PdfReader(io.BytesIO(bytes(contenido)), strict=False)
            if reader.is_encrypted and reader.decrypt("") == 0:
                raise ValueError("Uno de los documentos PDF está protegido.")
            if not reader.pages:
                raise ValueError("Uno de los documentos PDF no tiene páginas.")
            for pagina in reader.pages:
                writer.add_page(pagina)
                total_paginas += 1
        salida = io.BytesIO()
        writer.write(salida)
        unificado = salida.getvalue()
        verificacion = PdfReader(io.BytesIO(unificado), strict=False)
        if len(verificacion.pages) != total_paginas:
            raise ValueError("El PDF unificado perdió páginas.")
        return unificado
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("No se pudieron unificar la guía y la invoice.") from exc


def preparar_documentos_envio_portal(
    solicitud_id: int, cliente_id: str,
) -> Optional[dict]:
    """Devuelve el legajo PDF del envío sólo a su cliente propietario.

    La consulta reúne documentos y metadatos en una sola lectura para que la
    autorización, la mezcla y el nombre correspondan siempre al mismo envío.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.label_pdf, s.commercial_invoice_pdf,
                       COALESCE(NULLIF(BTRIM(c.nombre), ''), s.cliente_id)
                           AS cliente_nombre,
                       s.dest_nombre, s.destino_pais
                FROM solicitudes_guia s
                LEFT JOIN clientes c ON c.cliente_id=s.cliente_id
                WHERE s.id=%s AND s.cliente_id=%s
                  AND s.test=FALSE
                  AND s.estado <> 'CANCELADO'
                  AND s.estado <> 'REEMPLAZADO'
                  AND NOT EXISTS (
                      SELECT 1 FROM envios e
                      WHERE e.solicitud_id=s.id
                        AND e.cliente_id=s.cliente_id
                        AND e.estado='CANCELADO'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM solicitudes_guia_reemisiones r
                      WHERE r.solicitud_nueva_id=s.id
                        AND (r.estado <> 'EMITIDA'
                             OR s.cargo_pendiente=TRUE)
                  )
                """,
                (solicitud_id, cliente_id.strip().upper()),
            )
            row = cur.fetchone()
    if not row or not row.get("label_pdf"):
        return None
    pdf = unir_guia_e_invoice_pdf(
        bytes(row["label_pdf"]),
        bytes(row["commercial_invoice_pdf"])
        if row.get("commercial_invoice_pdf") else None,
    )
    return {
        "pdf": pdf,
        "incluye_invoice": bool(row.get("commercial_invoice_pdf")),
        "filename": nombre_archivo_documentos_envio(
            cliente_nombre=row.get("cliente_nombre"),
            dest_nombre=row.get("dest_nombre"),
            destino_pais=row.get("destino_pais"),
        ),
    }


def cargar_envio_externo(
    *,
    cliente_id: str,
    dest_nombre: str,
    dest_ciudad: str,
    destino_pais: str,
    producto: str,
    cantidad: int,
    peso_kg: float,
    tracking: str,
    precio_tauro_ars: float,
    label_pdf: Optional[bytes] = None,
    dest_direccion: str = "",
    observaciones: str = "",
    courier: str = "FEDEX",
    origen_pais: str = "AR",
    costo_courier_estimado_ars: Optional[float] = None,
) -> dict:
    """
    Alta de un envío YA REALIZADO por un canal externo (hoy: los que salen
    por el proveedor mayorista mientras se negocian las cuentas directas).

    En el portal del cliente queda indistinguible de un envío emitido por
    la plataforma: courier FedEx (el tracking del mayorista ES un tracking
    FedEx real), guía PDF descargable si se adjunta, cargo automático en la
    cuenta corriente y presencia en el Excel y el sheet espejo. El nombre
    del proveedor NO se escribe en ningún campo visible al cliente — la
    discreción es un requisito de negocio, no un descuido.

    Idempotencia del cargo: la da el índice único por solicitud de
    cargar_guia_emitida, igual que en la emisión propia.
    """
    from servicios.cotizador import dolar_ars

    tracking = (tracking or "").strip()
    if not tracking:
        return {"ok": False, "error": "Falta el tracking."}
    if precio_tauro_ars <= 0:
        return {"ok": False, "error": "El precio al cliente tiene que ser mayor a cero."}

    # Mismo tracking ya cargado = doble click o doble carga: no duplicar.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM solicitudes_guia WHERE tracking = %s LIMIT 1",
                        (tracking,))
            if cur.fetchone():
                return {"ok": False, "error": f"El tracking {tracking} ya está cargado."}

    dolar = dolar_ars()
    try:
        creada = crear_solicitud_guia(
            cliente_id=cliente_id.strip().upper(),
            producto_alias=(producto or "Mercadería")[:120],
            cantidad=max(int(cantidad or 1), 1),
            destino_pais=(destino_pais or "").strip().upper()[:3],
            dest_nombre=(dest_nombre or "")[:160],
            dest_documento="",
            dest_email="",
            dest_telefono="",
            dest_direccion=(dest_direccion or "")[:300],
            dest_ciudad=(dest_ciudad or "")[:120],
            dest_estado="",
            dest_zip="",
            observaciones=(observaciones or "")[:400],
            peso_kg=max(float(peso_kg or 0.5), 0.1),
            largo_cm=0, ancho_cm=0, alto_cm=0,
            valor_declarado_usd=0,
            ruta_id=f"AR-{(destino_pais or 'XX').strip().upper()[:2]}",
            coti_id=f"EXT-{uuid.uuid4().hex[:10]}",
            precio_tauro_ars=float(precio_tauro_ars),
            precio_tauro_usd=round(float(precio_tauro_ars) / dolar, 2) if dolar else 0,
            remitente_pais=(origen_pais or "AR").strip().upper(),
            courier=(courier or "FEDEX").strip().upper(),
            costo_courier_estimado_ars=costo_courier_estimado_ars,
        )
    except Exception as e:
        return {"ok": False, "error": f"No se pudo crear el envío: {e}"}

    sid = creada.get("id")
    # guardar_guia_generada hace el resto: GUIA_LISTA + label + cargo
    # automático en cuenta corriente. El aviso a tienda sale limpio porque
    # no hay pedido vinculado.
    guardar_guia_generada(
        sid, tracking, label_pdf,
        courier=(courier or "FEDEX").strip().upper(),
    )
    print(f"[solicitudes] envío externo cargado: solicitud {sid} · {cliente_id} · "
          f"{tracking} · ARS {precio_tauro_ars:,.0f}")
    return {"ok": True, "solicitud_id": sid}


def emitir_guia_como_cliente(solicitud_id: int, cliente_id: str) -> dict:
    """
    Emisión desde el PORTAL, con las tres llaves que definió Leandro:

      1. La solicitud es de ESE cliente y no tiene guía todavía.
      2. El cliente tiene la emisión habilitada (flag por cliente, apagado
         por defecto — emitir cuesta plata real y no se puede deshacer).
      3. Si tiene tope de deuda, su saldo pendiente no lo supera: un
         cliente moroso no puede seguir generando costo en el courier.

    Pasadas las tres, delega en generar_guia(), que ya trae la reserva
    atómica anti doble-emisión. El débito en cuenta corriente sale solo
    (guardar_guia_generada), así que cada emisión del cliente queda
    facturada en el acto — sin eso, esta función sería un agujero.
    """
    cliente_id = (cliente_id or "").strip().upper()

    previa = obtener_solicitud(solicitud_id)
    if (previa and previa.get("reemplaza_solicitud_id")
            and (previa.get("courier") or "").upper() == "DHL"):
        elegible = validar_reemision_cliente(
            int(previa["reemplaza_solicitud_id"]),
            cliente_id,
            consultar_courier=True,
            reemision_nueva_id=solicitud_id,
        )
        if not elegible.get("ok"):
            return elegible

    reserva = _reservar_credito_cliente(solicitud_id, cliente_id)
    if not reserva.get("ok"):
        return reserva

    # Entre armar la solicitud y emitirla DHL puede cambiar la tarifa. La
    # reserva evita dos clicks simultáneos; si cambia el precio, actualizamos
    # la solicitud, liberamos sin llamar a /shipments y exigimos un segundo
    # click sobre el importe nuevo.
    sol = obtener_solicitud(solicitud_id)
    if sol and (sol.get("courier") or "").upper() == "DHL":
        try:
            recotizacion = _recotizar_dhl_antes_de_emitir(sol)
        except Exception as e:
            # Toda esta etapa sucede ANTES del POST irreversible a DHL. Una
            # fila legacy mal formada o un error de parseo no puede dejar la
            # reserva EMITIENDO tomada para siempre.
            print(f"[solicitudes] no pude preparar la recotizacion DHL "
                  f"de {solicitud_id}: {e}")
            _liberar_reserva(solicitud_id)
            return {"ok": False, "error":
                    "No pudimos validar los datos para recotizar con DHL. "
                    "No emitimos ni cobramos nada; revisa los bultos o pedi ayuda a Tauro."}
        if not recotizacion.get("ok"):
            _liberar_reserva(solicitud_id)
            return recotizacion

    print(f"[solicitudes] emisión del CLIENTE {cliente_id} para la solicitud "
          f"{solicitud_id} (autorizada: flag + tope OK)")
    return generar_guia(solicitud_id, ya_reservada=True)


def _recotizar_dhl_antes_de_emitir(sol: dict) -> dict:
    """Verifica que `costo DHL + regla del cliente` siga siendo el aceptado."""
    bultos_crudos = sol.get("bultos")
    bultos = bultos_crudos or []
    if isinstance(bultos, str):
        try:
            bultos = json.loads(bultos)
        except (TypeError, ValueError):
            raise ValueError("Los bultos guardados no tienen un formato válido.") from None
    if bultos and not isinstance(bultos, list):
        raise ValueError("Los bultos guardados no tienen un formato válido.")

    if bultos:
        # Se pasa como carga manual aun si nació del catálogo: la solicitud
        # congeló la invoice, medidas y valor de ESTE envío y eso es lo que
        # debe volver a cotizarse.
        filas = []
        for indice, bulto in enumerate(bultos, start=1):
            if not isinstance(bulto, dict):
                raise ValueError(f"Bulto {indice}: los datos no tienen un formato válido.")
            cantidad = parse_entero_formulario(
                bulto.get("cantidad"), f"Bulto {indice}, cantidad", minimo=1, maximo=20
            )
            # `unidades_aduana` no existía en solicitudes históricas. Sólo en
            # ese caso se recupera la cantidad física; un valor presente pero
            # inválido jamás se reemplaza silenciosamente.
            unidades_crudas = bulto.get("unidades_aduana")
            unidades = (
                cantidad
                if unidades_crudas in (None, "")
                else parse_entero_formulario(
                    unidades_crudas, f"Bulto {indice}, unidades aduaneras", minimo=1
                )
            )
            filas.append({
                "producto": "",
                "cantidad": cantidad,
                "peso_kg": parse_float_formulario(
                    bulto.get("peso_kg"), f"Bulto {indice}, peso", minimo=0.001
                ),
                "largo_cm": parse_float_formulario(
                    bulto.get("largo_cm"), f"Bulto {indice}, largo", minimo=0.001
                ),
                "ancho_cm": parse_float_formulario(
                    bulto.get("ancho_cm"), f"Bulto {indice}, ancho", minimo=0.001
                ),
                "alto_cm": parse_float_formulario(
                    bulto.get("alto_cm"), f"Bulto {indice}, alto", minimo=0.001
                ),
                "descripcion_en": bulto.get("descripcion_en") or "Merchandise",
                "valor_unitario_usd": parse_float_formulario(
                    bulto.get("valor_unitario_usd"),
                    f"Bulto {indice}, valor unitario",
                    importe=True,
                    minimo=0.001,
                ),
                "valor_declarado_caja_usd": (
                    parse_float_formulario(
                        bulto.get("valor_declarado_caja_usd"),
                        f"Bulto {indice}, valor declarado por caja",
                        importe=True,
                        minimo=0.001,
                    )
                    if bulto.get("valor_declarado_caja_usd") not in (None, "")
                    else round(
                        parse_float_formulario(
                            bulto.get("valor_unitario_usd"),
                            f"Bulto {indice}, valor unitario",
                            importe=True,
                            minimo=0.001,
                        ) * unidades / cantidad,
                        2,
                    )
                ),
                "unidades_aduana": unidades,
                "hs_code": bulto.get("hs_code") or "",
                "pais_origen": (
                    bulto.get("pais_origen") or sol.get("remitente_pais") or "AR"
                ),
            })
    else:
        cantidad = parse_entero_formulario(
            sol.get("cantidad"), "Cantidad de cajas", minimo=1, maximo=20
        )
        total = parse_float_formulario(
            sol.get("valor_declarado_usd"),
            "Valor declarado",
            importe=True,
            minimo=0.001,
        )
        peso_total = parse_float_formulario(
            sol.get("peso_kg"), "Peso total", minimo=0.001
        )
        filas = [{
            "producto": "", "cantidad": cantidad,
            # Los registros legacy sólo tenían una cantidad. En ese contrato
            # era, a la vez, la cantidad física y la comercial declarada.
            "unidades_aduana": cantidad,
            # En el legacy peso_kg es total; cada pieza debe recuperar su peso.
            "peso_kg": peso_total / cantidad,
            "largo_cm": parse_float_formulario(
                sol.get("largo_cm"), "Largo", minimo=0.001
            ),
            "ancho_cm": parse_float_formulario(
                sol.get("ancho_cm"), "Ancho", minimo=0.001
            ),
            "alto_cm": parse_float_formulario(
                sol.get("alto_cm"), "Alto", minimo=0.001
            ),
            "descripcion_en": sol.get("producto_alias") or "Merchandise",
            "valor_unitario_usd": total / cantidad,
            "valor_declarado_caja_usd": total / cantidad,
            "pais_origen": sol.get("remitente_pais") or "AR",
        }]

    try:
        from servicios.api_b2b import cotizar_couriers_cliente
        resultado = cotizar_couriers_cliente(
            sol["cliente_id"], sol.get("destino_pais") or "", filas,
            destino_real={
                "cp": sol.get("dest_zip") or "", "ciudad": sol.get("dest_ciudad") or "",
                "estado": sol.get("dest_estado") or "",
            },
            origen_real={
                "pais": sol.get("remitente_pais") or "AR",
                "ciudad": sol.get("remitente_ciudad") or "",
                "cp": sol.get("remitente_zip") or "",
                "estado": sol.get("remitente_estado") or "",
            },
            asegurar_carga=bool(sol.get("asegurar_carga")),
            incluir_base_interna=True,
        )
    except Exception as e:
        print(f"[solicitudes] no pude recotizar DHL antes de emitir {sol['id']}: {e}")
        return {"ok": False, "error":
                "No pudimos confirmar la tarifa actual de DHL. No emitimos ni cobramos nada; "
                "probá de nuevo en unos minutos."}

    opcion = next((o for o in (resultado.get("opciones") or [])
                   if (o.get("id") or "").lower() == "dhl"), None)
    if not opcion:
        return {"ok": False, "error":
                "DHL no devolvió una tarifa para este envío. No emitimos ni cobramos nada."}

    anterior = parse_float_formulario(
        sol.get("precio_tauro_ars"), "Precio aceptado", importe=True, minimo=0.001
    )
    actual = parse_float_formulario(
        opcion.get("precio_ars"), "Precio DHL actual", importe=True, minimo=0.001
    )
    if abs(actual - anterior) <= 0.5:
        base_interna = opcion.get("_base_interna")
        try:
            congelada = _congelar_base_recotizada(sol, base_interna)
        except Exception as e:
            print(f"[solicitudes] tarifa privada inconsistente para {sol['id']}: "
                  f"{type(e).__name__}")
            congelada = False
        if not congelada:
            return {"ok": False, "error":
                    "No pudimos guardar la base interna de la tarifa DHL. "
                    "No emitimos ni cobramos nada; probá de nuevo o pedí ayuda a Tauro."}
        return {"ok": True}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes_guia
                SET precio_tauro_ars=%s, precio_tauro_usd=%s,
                    servicio_courier=%s, updated_at=NOW()
                WHERE id=%s AND tracking IS NULL
            """, (actual, opcion.get("precio_usd"), opcion.get("servicio"), sol["id"]))

    anterior_txt = f"{anterior:,.0f}".replace(",", ".")
    actual_txt = f"{actual:,.0f}".replace(",", ".")
    return {"ok": False, "precio_cambio": True, "error":
            f"La tarifa DHL cambió de $ {anterior_txt} a $ {actual_txt}. "
            "Revisá el nuevo importe y volvé a emitir; todavía no generamos ni cobramos nada."}


def _reservar_credito_cliente(solicitud_id: int, cliente_id: str) -> dict:
    """Autoriza y reserva guía + crédito en una sola sección crítica.

    El lock de la fila del cliente serializa emisiones distintas de la misma
    cuenta. Además de la deuda contabilizada suma guías que están emitiéndose,
    en verificación o con cargo pendiente: dos clicks sobre solicitudes
    distintas ya no pueden gastar el mismo límite simultáneamente.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.cliente_id, s.tracking, s.estado, s.precio_tauro_ars,
                       s.courier, s.ambito, s.remitente_pais, s.destino_pais,
                       c.activo,
                       r.solicitud_anterior_id,
                       e_anterior.monto_ars AS monto_reemplazado_ars,
                       e_anterior.estado AS cargo_reemplazado_estado,
                       e_anterior.nro_fc AS cargo_reemplazado_fc,
                       CASE
                         WHEN LOWER(COALESCE(s.courier, '')) = 'dhl'
                         THEN COALESCE(cc.puede_emitir, FALSE)
                         ELSE FALSE
                       END AS puede_emitir,
                       c.tope_deuda_ars
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                LEFT JOIN cliente_courier_config cc
                  ON cc.cliente_id = s.cliente_id
                 AND cc.courier = LOWER(COALESCE(s.courier, ''))
                LEFT JOIN solicitudes_guia_reemisiones r
                  ON r.solicitud_nueva_id=s.id
                LEFT JOIN envios e_anterior
                  ON e_anterior.solicitud_id=r.solicitud_anterior_id
                WHERE s.id = %s
                FOR UPDATE OF c, s
            """, (solicitud_id,))
            fila = cur.fetchone()

            if not fila or fila["cliente_id"] != cliente_id:
                return {"ok": False, "error": "Esa solicitud no existe o no es tuya."}
            if fila["tracking"]:
                return {"ok": False, "error": "Esa solicitud ya tiene guía emitida."}
            if fila["estado"] == "VERIFICAR_COURIER":
                return {"ok": False, "error":
                        "La emisión anterior se está verificando con el courier. "
                        "No vuelvas a emitirla: Tauro te avisa cuando la concilie."}
            if fila["estado"] == ESTADO_EMITIENDO:
                return {"ok": False, "error":
                        "Ya hay una emisión en curso para esta solicitud."}
            error_ambito = _error_ambito_no_emitible(fila)
            if error_ambito:
                return {"ok": False, "error": error_ambito}
            if not fila["activo"]:
                return {"ok": False, "error":
                        "Tu cuenta está desactivada. No se puede emitir ni "
                        "generar cargos hasta que Tauro la reactive."}
            if (fila.get("courier") or "").strip().lower() == "dhl":
                from servicios.configuracion_couriers_cliente import estado_integracion
                if not estado_integracion("dhl")["operativa"]:
                    return {"ok": False, "error":
                            "DHL no está habilitado en producción en este momento. "
                            "No emitimos ni generamos ningún cargo; escribile a Tauro."}
            if not fila["puede_emitir"]:
                return {"ok": False, "error":
                        "Tu cuenta no tiene habilitada la emisión directa. "
                        "Reenviá la solicitud a Tauro y la emitimos nosotros."}

            if fila.get("solicitud_anterior_id"):
                if (fila.get("cargo_reemplazado_estado") != "ACTIVO"
                        or str(fila.get("cargo_reemplazado_fc") or "").strip()):
                    return {"ok": False, "error":
                            "La guía anterior cambió en la cuenta corriente. "
                            "Tauro debe conciliarla antes de reemitir."}

            tope = fila["tope_deuda_ars"]
            if tope is not None and Decimal(str(tope)) >= 0:
                # Una sola consulta/snapshot para deuda y reservas. Antes se
                # abrian conexiones separadas para facturado y pagos, por lo
                # que una aprobacion concurrente podia dejar una mezcla de
                # momentos distintos. Ademas no se vuelve a reservar un
                # cargo_pendiente que ya tenga asiento en `envios`.
                cur.execute("""
                    SELECT
                        COALESCE((
                            SELECT SUM(e.monto_ars)
                            FROM envios e
                            WHERE e.cliente_id=%s
                              AND e.estado NOT IN ('CANCELADO', 'NC')
                        ), 0) - COALESCE((
                            SELECT SUM(p.monto_ars)
                            FROM pagos p
                            WHERE p.cliente_id=%s
                              AND COALESCE(p.estado, 'APROBADO')='APROBADO'
                        ), 0) AS deuda,
                        COALESCE((
                            SELECT SUM(s2.precio_tauro_ars)
                            FROM solicitudes_guia s2
                            WHERE s2.cliente_id=%s AND s2.id<>%s AND (
                                (s2.tracking IS NULL AND s2.estado IN
                                    ('EMITIENDO', 'VERIFICAR_COURIER'))
                                OR (
                                    s2.cargo_pendiente=TRUE
                                    AND NOT EXISTS (
                                        SELECT 1 FROM envios e2
                                        WHERE e2.solicitud_id=s2.id
                                    )
                                )
                            )
                        ), 0) AS reservado
                """, (cliente_id, cliente_id, cliente_id, solicitud_id))
                resumen_credito = cur.fetchone() or {}
                deuda = Decimal(str(resumen_credito.get("deuda") or 0))
                reservado = Decimal(str(resumen_credito.get("reservado") or 0))
                nueva = Decimal(str(fila.get("precio_tauro_ars") or 0))
                # Al reemplazar una guía, el cargo anterior sale de la cuenta
                # en la misma transacción que entra el nuevo. El límite se
                # calcula sobre ese saldo final, no sobre dos guías activas.
                reemplazado = Decimal(str(fila.get("monto_reemplazado_ars") or 0))
                proyectado = max(Decimal("0"), deuda - reemplazado) + reservado + nueva
                tope_decimal = Decimal(str(tope))
                if proyectado > tope_decimal:
                    proyectado_txt = f"{proyectado:,.0f}".replace(",", ".")
                    tope_txt = f"{tope_decimal:,.0f}".replace(",", ".")
                    return {"ok": False, "error":
                            f"Esta guía llevaría el saldo comprometido a ARS "
                            f"{proyectado_txt}, por encima del límite de tu cuenta "
                            f"(ARS {tope_txt}). Registrá un pago o pedile "
                            "a Tauro que revise el límite."}

            cur.execute("""
                UPDATE solicitudes_guia
                SET estado='EMITIENDO', updated_at=NOW()
                WHERE id=%s AND tracking IS NULL
                  AND estado NOT IN ('EMITIENDO', 'VERIFICAR_COURIER', 'CANCELADO')
                RETURNING id
            """, (solicitud_id,))
            if cur.fetchone() is None:
                return {"ok": False, "error":
                        "La solicitud cambió de estado. Actualizá la pantalla antes de emitir."}
        conn.commit()
    return {"ok": True}


def generar_guia(solicitud_id: int, ya_reservada: bool = False) -> dict:
    """
    Despachador de emisión internacional. Las integraciones nacionales
    directas de Andreani/OCA se incorporarán como adaptadores propios.
    """
    sol = obtener_solicitud(solicitud_id)
    if not sol:
        return {"ok": False, "error": "Solicitud no encontrada."}

    courier = (sol.get("courier") or "FEDEX").upper()
    if courier == "ENVIA":
        return {
            "ok": False,
            "error": (
                "La integración nacional anterior fue retirada. Esta solicitud "
                "histórica no se emitió ni generó ningún cargo. Revisala en el "
                "admin antes de cancelarla o migrarla manualmente."
            ),
        }

    error_ambito = _error_ambito_no_emitible(sol)
    if error_ambito:
        return {"ok": False, "error": error_ambito}

    # El flujo cliente congela la base al recotizar. El admin puede llegar
    # directo al despachador: en una reemisión DHL recuperamos la tarifa acá,
    # todavía antes del POST irreversible. Ninguna guía reemplazada se emite
    # si no existe una fila propia (no se reutiliza la FK de la guía vieja).
    if not _reemision_tiene_snapshot(sol):
        if courier == "DHL":
            recotizacion = _recotizar_dhl_antes_de_emitir(sol)
            if not recotizacion.get("ok"):
                return recotizacion
            sol = obtener_solicitud(solicitud_id) or sol
        if not _reemision_tiene_snapshot(sol):
            return {"ok": False, "error":
                    "La guía de reemplazo no tiene una base interna confirmada. "
                    "No se emitió ni se generó ningún cargo."}

    # El admin también pasa por una guarda productiva. El cliente ya la
    # verifica dentro de _reservar_credito_cliente(), pero el botón del admin
    # llegaba directo a este despachador y podía emitir contra sandbox si las
    # variables existían. Una guía de prueba nunca debe terminar guardada ni
    # debitada como una operación real.
    if courier == "DHL" and not ya_reservada:
        from servicios.configuracion_couriers_cliente import estado_integracion
        if not estado_integracion("dhl")["operativa"]:
            return {
                "ok": False,
                "error": (
                    "DHL no está habilitado en producción. No se emitió ninguna "
                    "guía ni se generó ningún cargo."
                ),
            }

    # Registro de couriers internacionales: sumar uno nuevo es agregarlo acá
    # y darle un cliente con create_shipment del mismo contrato.
    if courier in ("FEDEX", "DHL", "UPS"):
        return (generar_guia_internacional(
                    solicitud_id, courier=courier, ya_reservada=True,
                ) if ya_reservada else
                generar_guia_internacional(solicitud_id, courier=courier))

    # NUNCA caer a FedEx por descarte. Antes esto era un `else` y cualquier
    # courier desconocido —DHL, UPS— se emitía con una etiqueta de FedEx:
    # el cliente pagaba precio DHL, recibía una guía FedEx, y el link de
    # tracking apuntaba al courier equivocado. Un error a la vista es
    # infinitamente mejor que una guía emitida por el courier que no es.
    print(f"[guia] solicitud {solicitud_id}: courier {courier!r} sin emisión "
          f"implementada — NO se emite nada")
    return {
        "ok": False,
        "error": (
            f"Todavía no emitimos guías de {courier} desde el sistema. "
            "No se emitió ni se generó ningún cargo."
        ),
    }


def _reservar_para_emitir(solicitud_id: int) -> bool:
    """
    Marca la solicitud como EMITIENDO, pero sólo si nadie más lo hizo.

    Es un UPDATE condicional: la base garantiza que de N intentos
    simultáneos exactamente uno modifica la fila. El que obtiene la fila
    puede llamar al courier; los demás se van con las manos vacías.

    No vence sola: si el proceso muere después de enviar el POST, no sabemos
    si el courier creó la guía. Una persona debe conciliarla antes de resetear
    el estado; desbloquear por tiempo habilitaría un duplicado facturado.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes_guia
                SET estado = 'EMITIENDO', updated_at = NOW()
                WHERE id = %s
                  AND tracking IS NULL
                  AND estado <> 'VERIFICAR_COURIER'
                  AND estado <> 'EMITIENDO'
                RETURNING id
            """, (solicitud_id,))
            gano = cur.fetchone() is not None
        conn.commit()
    return gano


def _liberar_reserva(solicitud_id: int, estado: str = "SOLICITADO") -> None:
    """Devuelve la solicitud a su estado anterior si la emisión falló."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE solicitudes_guia
                    SET estado = %s, courier_message_reference=NULL,
                        courier_error=NULL, updated_at = NOW()
                    WHERE id = %s AND estado = 'EMITIENDO' AND tracking IS NULL
                """, (estado, solicitud_id))
            conn.commit()
    except Exception as e:
        print(f"[guia] no pude liberar la reserva de {solicitud_id}: {e}")


def _marcar_verificacion_courier(solicitud_id: int, resultado: dict) -> None:
    """Bloquea un reintento cuando el POST pudo haber creado una guía real."""
    referencia = _clean(resultado.get("message_reference"))
    error = _clean(resultado.get("error"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes_guia
                SET estado='VERIFICAR_COURIER', courier_message_reference=%s,
                    courier_error=%s, updated_at=NOW()
                WHERE id=%s AND tracking IS NULL
            """, (referencia, error[:500] if error else None, solicitud_id))
        conn.commit()


def _persistir_referencia_courier(solicitud_id: int, referencia: str) -> bool:
    """Guarda la referencia DHL antes de la operación irreversible."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes_guia
                SET courier_message_reference=%s, courier_error=NULL,
                    updated_at=NOW()
                WHERE id=%s AND estado='EMITIENDO' AND tracking IS NULL
                RETURNING id
            """, (_clean(referencia), solicitud_id))
            guardada = cur.fetchone() is not None
        conn.commit()
    return guardada


def resolver_verificacion_courier(
    solicitud_id: int,
    resultado: str,
    tracking: str = "",
    label_pdf: Optional[bytes] = None,
) -> dict:
    """Concilia una emisión incierta después de revisarla en el courier.

    ``CREADA`` exige tracking y el PDF recuperado del courier, y pasa por
    ``guardar_guia_generada`` para crear el cargo idempotente. ``NO_CREADA``
    libera la solicitud para un nuevo intento. No existe una transición
    genérica desde VERIFICAR_COURIER: ésta es la única salida auditable.
    """
    decision = (resultado or "").strip().upper()
    if decision not in {"CREADA", "NO_CREADA"}:
        return {"ok": False, "error": "Resultado de conciliación inválido."}

    if decision == "NO_CREADA":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE solicitudes_guia
                    SET estado='SOLICITADO', courier_message_reference=NULL,
                        courier_error=NULL, updated_at=NOW()
                    WHERE id=%s AND tracking IS NULL
                      AND (estado='VERIFICAR_COURIER' OR
                           (estado='EMITIENDO' AND
                            courier_message_reference IS NOT NULL AND
                            updated_at <= NOW() - INTERVAL '10 minutes'))
                    RETURNING id
                """, (solicitud_id,))
                if cur.fetchone() is None:
                    return {"ok": False, "error":
                            "La solicitud ya no está pendiente de verificación."}
        return {"ok": True, "estado": "SOLICITADO"}

    tracking = (tracking or "").strip()[:120]
    if not tracking:
        return {"ok": False, "error":
                "Ingresá el tracking que encontraste en el courier."}
    if not label_pdf or not bytes(label_pdf).startswith(b"%PDF"):
        return {"ok": False, "error":
                "Adjuntá la etiqueta PDF recuperada del courier."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Una emisión puede quedar EMITIENDO si el proceso muere después
            # de persistir la referencia y antes de leer la respuesta. A los
            # 10 minutos ya no hay un request HTTP vivo (timeout 60 s), por lo
            # que se habilita la misma conciliación manual, nunca un reintento.
            cur.execute("""
                SELECT courier, courier_message_reference
                FROM solicitudes_guia
                WHERE id=%s AND tracking IS NULL
                  AND (estado='VERIFICAR_COURIER' OR
                       (estado='EMITIENDO' AND
                        courier_message_reference IS NOT NULL AND
                        updated_at <= NOW() - INTERVAL '10 minutes'))
                FOR UPDATE
            """, (solicitud_id,))
            fila = cur.fetchone()
            if not fila:
                return {"ok": False, "error":
                        "La solicitud ya no está pendiente de verificación."}
            courier_confirmado = (fila.get("courier") or "DHL").strip().upper()
            # Mensaje claro antes de que el índice único haga fallar el UPDATE.
            # La comparación es por courier: dos proveedores podrían compartir
            # por casualidad un formato numérico de tracking.
            cur.execute("""
                SELECT id FROM solicitudes_guia
                WHERE id<>%s AND UPPER(courier)=UPPER(%s)
                  AND UPPER(BTRIM(tracking))=UPPER(BTRIM(%s))
                LIMIT 1
            """, (solicitud_id, courier_confirmado, tracking))
            if cur.fetchone():
                return {"ok": False, "error":
                        f"El tracking {tracking} ya está asociado a otra solicitud de "
                        f"{courier_confirmado}."}
            cur.execute("""
                UPDATE solicitudes_guia
                SET estado='EMITIENDO', updated_at=NOW()
                WHERE id=%s AND tracking IS NULL
                RETURNING courier, courier_message_reference
            """, (solicitud_id,))
            fila = cur.fetchone()
            if not fila:
                return {"ok": False, "error":
                        "La solicitud cambió mientras se conciliaba; actualizá la pantalla."}

    try:
        guardar_guia_generada(
            solicitud_id,
            tracking,
            bytes(label_pdf),
            courier=(fila.get("courier") or "DHL").upper(),
            message_reference=fila.get("courier_message_reference"),
        )
    except Exception as e:
        # La guía ya fue confirmada como real. Volver a VERIFICAR no habilita
        # la emisión: ese estado sólo ofrece esta misma conciliación, de modo
        # que el operador puede reintentar el guardado sin SQL ni doble guía.
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE solicitudes_guia
                        SET estado='VERIFICAR_COURIER', courier_error=%s,
                            updated_at=NOW()
                        WHERE id=%s AND estado='EMITIENDO' AND tracking IS NULL
                    """, (f"Guía {tracking} confirmada; falló el guardado local: {e}"[:500],
                          solicitud_id))
        except Exception as persistencia_error:
            print(f"[solicitudes] tampoco pude restaurar la conciliación de "
                  f"{solicitud_id}: {persistencia_error}")
        print(f"[solicitudes] conciliación de guía {solicitud_id} no pudo "
              f"guardarse (tracking {tracking}): {e}")
        return {"ok": False, "error":
                f"La guía existe (tracking {tracking}) pero no pudimos guardarla. "
                "No la vuelvas a emitir; corregí el problema y repetí la conciliación."}
    return {"ok": True, "estado": "GUIA_LISTA", "tracking": tracking}


def liberar_reserva_sin_operacion_courier(solicitud_id: int) -> dict:
    """Recupera una reserva vieja que nunca llegó a llamar al courier.

    La Message-Reference se persiste antes del POST irreversible. Por eso una
    fila EMITIENDO de más de diez minutos, sin tracking NI referencia, quedó
    detenida en validación/recotización y puede volver a SOLICITADO sin riesgo
    de duplicar una guía real. Si existe referencia, esta acción falla cerrada
    y obliga a conciliar en el courier.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes_guia
                SET estado='SOLICITADO', courier_error=NULL, updated_at=NOW()
                WHERE id=%s AND estado='EMITIENDO' AND tracking IS NULL
                  AND courier_message_reference IS NULL
                  AND updated_at <= NOW() - INTERVAL '10 minutes'
                RETURNING id
            """, (solicitud_id,))
            if cur.fetchone() is None:
                return {"ok": False, "error":
                        "La reserva todavía puede estar activa o requiere verificación "
                        "en el courier. Actualizá la pantalla."}
    return {"ok": True, "estado": "SOLICITADO"}


def generar_guia_internacional(solicitud_id: int, courier: str = "FEDEX",
                               ya_reservada: bool = False) -> dict:
    """
    Emite la guía real en FedEx para una solicitud y guarda tracking + label PDF.
    Devuelve {ok, tracking, tiene_label} o {ok: False, error}.
    """
    sol = obtener_solicitud(solicitud_id)
    if not sol:
        return {"ok": False, "error": "Solicitud no encontrada."}
    if sol.get("estado") == "CANCELADO":
        return {"ok": False, "error": "La solicitud está cancelada."}
    if sol.get("estado") == "VERIFICAR_COURIER":
        referencia = sol.get("courier_message_reference") or "sin referencia visible"
        return {"ok": False, "error": (
            "La emisión anterior tuvo una respuesta incierta. No la vuelvas a "
            f"emitir: Tauro debe verificarla con el courier (ref. {referencia})."
        )}
    if sol.get("tracking"):
        return {"ok": False, "error": "Esta solicitud ya tiene una guía generada."}

    error_ambito = _error_ambito_no_emitible(sol)
    if error_ambito:
        return {"ok": False, "error": error_ambito}

    # RESERVA ATÓMICA antes de tocar FedEx.
    #
    # Emitir una guía es irreversible: crea un envío real en el courier.
    # Sin esto, dos clicks (o dos admins, o el reintento del navegador)
    # entran los dos al chequeo de arriba, los dos pasan, y salen DOS
    # guías reales para el mismo paquete. La base guarda sólo la última,
    # así que la otra queda huérfana: existe en FedEx, con su etiqueta y
    # su declaración de aduana, y nadie sabe a qué envío corresponde.
    #
    # El UPDATE condicional lo resuelve de raíz: la base decide quién
    # gana, y el que pierde ni llega a llamar al courier.
    if not ya_reservada and not _reservar_para_emitir(solicitud_id):
        return {"ok": False,
                "error": "Ya hay una emisión en curso para esta solicitud. "
                         "Esperá unos segundos y refrescá la pantalla."}

    # Bultos de la solicitud (multi-bulto). Si no hay, cae al camino legacy
    # de un solo bulto con los campos históricos.
    bultos = sol.get("bultos") or []
    if isinstance(bultos, str):
        try:
            import json as _json
            bultos = _json.loads(bultos)
        except Exception:
            bultos = []
    if not isinstance(bultos, list):
        bultos = []
    bultos = [b for b in bultos if isinstance(b, dict)]

    def _entero_requerido(valor, campo: str) -> int:
        return parse_entero_formulario(valor, campo, minimo=1)

    def _numero_requerido(valor, campo: str, *, importe: bool = False) -> float:
        return parse_float_formulario(valor, campo, importe=importe, minimo=0.001)

    # Producto del catálogo → HS code y descripción en inglés para la aduana.
    hs_code, descripcion_en = "", "Merchandise"
    try:
        from servicios.catalogo import get_producto
        prod = get_producto(sol["cliente_id"], sol.get("producto_alias") or "")
        if prod:
            hs_code = prod.hs_code or ""
            descripcion_en = prod.nombre_invoice or "Merchandise"
    except Exception as e:
        print(f"[guia] no se pudo leer el producto: {e}")

    from servicios.paises import normalizar_iso2

    shipper = {
        # personName = el CONTACTO del envío; companyName = la razón social
        # DEL REMITENTE (guía HAILU: "Yiwu Hailu Garment" + "JEFF JANG").
        # Antes empresa era el cliente de TAURO — la guía salía a nombre de
        # WAIMAO en vez del shipper real.
        "nombre": (sol.get("remitente_contacto") or sol.get("remitente_nombre")
                   or sol.get("cliente_nombre") or sol["cliente_id"]),
        "empresa": sol.get("remitente_nombre") or sol.get("cliente_nombre") or "",
        "documento": sol.get("remitente_documento") or "",
        "email": sol.get("remitente_email") or "",
        "telefono": sol.get("remitente_telefono") or sol.get("cliente_telefono") or "",
        "calle": sol.get("remitente_direccion") or sol.get("cliente_direccion") or "",
        "ciudad": sol.get("remitente_ciudad") or sol.get("cliente_ciudad") or "Buenos Aires",
        "estado": sol.get("remitente_estado") or "",
        "zip": sol.get("remitente_zip") or sol.get("cliente_cp") or "",
        "pais": normalizar_iso2(
            sol.get("remitente_pais") or sol.get("cliente_pais") or "AR"
        ),
    }
    recipient = {
        "nombre": sol.get("dest_contacto") or sol.get("dest_nombre") or "",
        "empresa": sol.get("dest_nombre") or "",
        "documento": sol.get("dest_documento") or "",
        "email": sol.get("dest_email") or "",
        "telefono": sol.get("dest_telefono") or "",
        "calle": sol.get("dest_direccion") or "",
        "ciudad": sol.get("dest_ciudad") or "",
        "estado": sol.get("dest_estado") or "",
        "zip": sol.get("dest_zip") or "",
        "pais": normalizar_iso2(sol.get("destino_pais") or "US"),
    }
    datos_envio = {
        "shipper": shipper,
        "recipient": recipient,
    }
    try:
        if bultos:
            # Multi-bulto: cada caja del envío como pieza propia, con su label.
            # Los datos ya guardados pueden ser antiguos, pero nunca se los
            # sustituye por valores "razonables": emitir sería irreversible.
            datos_envio["bultos"] = [
                {
                    "peso_kg": _numero_requerido(b.get("peso_kg"), f"Caja {i}: peso"),
                    "largo": _numero_requerido(b.get("largo_cm"), f"Caja {i}: largo"),
                    "ancho": _numero_requerido(b.get("ancho_cm"), f"Caja {i}: ancho"),
                    "alto": _numero_requerido(b.get("alto_cm"), f"Caja {i}: alto"),
                    "valor_unitario_usd": _numero_requerido(
                        b.get("valor_unitario_usd"), f"Ítem {i}: valor unitario", importe=True
                    ),
                    "valor_declarado_caja_usd": (
                        _numero_requerido(
                            b.get("valor_declarado_caja_usd"),
                            f"Caja {i}: valor declarado por caja",
                            importe=True,
                        )
                        if b.get("valor_declarado_caja_usd") not in (None, "")
                        else round(
                            _numero_requerido(
                                b.get("valor_unitario_usd"),
                                f"Ítem {i}: valor unitario",
                                importe=True,
                            ) * _entero_requerido(
                                b.get("unidades_aduana"),
                                f"Caja {i}: unidades de aduana",
                            ) / _entero_requerido(
                                b.get("cantidad"), f"Caja {i}: cantidad",
                            ),
                            2,
                        )
                    ),
                    "unidades": _entero_requerido(b.get("cantidad"), f"Caja {i}: cantidad"),
                    "unidades_aduana": _entero_requerido(
                        b.get("unidades_aduana"), f"Caja {i}: unidades de aduana"
                    ),
                    "hs_code": b.get("hs_code") or "",
                    "descripcion_en": b.get("descripcion_en") or "Merchandise",
                    "pais_origen": (b.get("pais_origen")
                                    or sol.get("remitente_pais") or "AR"),
                }
                for i, b in enumerate(bultos, start=1)
            ]
        else:
        # OJO: valor_declarado_usd viene TOTALIZADO (unitario × cantidad) desde
        # el portal. create_shipment vuelve a multiplicar unitario × cantidad,
        # así que acá se pasa el UNITARIO real para no declarar de más en aduana.
            cantidad_sol = _entero_requerido(sol.get("cantidad"), "Cantidad de cajas")
            valor_total_sol = _numero_requerido(
                sol.get("valor_declarado_usd"), "Valor declarado", importe=True
            )
            datos_envio["package"] = {
            # El campo legacy guarda el peso TOTAL. create_shipment repite
            # esta pieza `cantidad` veces, por eso necesita el peso unitario.
                "peso_kg": _numero_requerido(sol.get("peso_kg"), "Peso") / cantidad_sol,
                "largo": _numero_requerido(sol.get("largo_cm"), "Largo"),
                "ancho": _numero_requerido(sol.get("ancho_cm"), "Ancho"),
                "alto": _numero_requerido(sol.get("alto_cm"), "Alto"),
            }
            datos_envio["commodity"] = {
                "descripcion": descripcion_en,
                "hs_code": hs_code,
                "cantidad": cantidad_sol,
                "valor_unitario_usd": round(valor_total_sol / cantidad_sol, 2),
                "pais_origen": sol.get("remitente_pais") or "AR",
            }
    except ValueError as exc:
        _liberar_reserva(solicitud_id)
        return {"ok": False, "error": (
            f"Datos numéricos inválidos: {exc}. No llamamos al courier ni generamos ningún cargo."
        )}

    # Quién paga los impuestos en ESTE envío: se decidió al crearlo y se
    # congeló ahí. Sin esto FedEx vuelve al SENDER fijo con la cuenta de TAURO.
    datos_envio["tax_paga"] = sol.get("tax_paga")
    datos_envio["asegurar_carga"] = bool(sol.get("asegurar_carga"))

    # El cliente del courier se elige acá: el payload (shipper/recipient/
    # bultos) es el MISMO contrato para los dos, por eso todo el armado de
    # arriba se comparte y sumar un courier no duplica esta función.
    courier = (courier or "FEDEX").upper()
    if courier == "DHL":
        from core.dhl_client import DHLClient
        cliente_courier = DHLClient()
    elif courier == "UPS":
        from core.ups_client import UPSClient
        cliente_courier = UPSClient()
    else:
        from core.fedex_client import FedExClient
        cliente_courier = FedExClient()

    referencia_previa = None
    if courier == "DHL":
        referencia_previa = (
            f"tauro-dhl-ship-{solicitud_id}-{uuid.uuid4().hex[:12]}"
        )
        try:
            if not _persistir_referencia_courier(solicitud_id, referencia_previa):
                return {"ok": False, "error":
                        "La solicitud cambió de estado antes de emitir. "
                        "Actualizá la pantalla; no llamamos a DHL."}
        except Exception as e:
            print(f"[guia] no pude persistir la referencia DHL de "
                  f"{solicitud_id}: {e}")
            _liberar_reserva(solicitud_id)
            return {"ok": False, "error":
                    "No pudimos preparar la emisión segura. No llamamos a DHL "
                    "ni generamos ningún cargo."}
        datos_envio["message_reference"] = referencia_previa

    try:
        resultado = cliente_courier.create_shipment(datos_envio)
    except Exception as e:
        print(f"[guia] excepción emitiendo la solicitud {solicitud_id} en {courier}: {e}")
        incierto = {"incierto": True, "message_reference": referencia_previa,
                    "error": f"La respuesta de {courier} fue incierta: {e}"}
        _marcar_verificacion_courier(solicitud_id, incierto)
        return {"ok": False, "error":
                f"No pudimos confirmar la emisión en {courier}. Tauro la va a "
                "verificar; no vuelvas a emitir."}

    if not resultado.get("encontrado"):
        if resultado.get("incierto"):
            # Un timeout no significa rechazo: DHL puede haber emitido y
            # perdido la respuesta. Bloquear el reintento evita dos guías
            # facturadas para la misma solicitud.
            _marcar_verificacion_courier(solicitud_id, resultado)
            return {"ok": False,
                    "error": (resultado.get("error") or
                              "La respuesta del courier fue incierta.") +
                             " Tauro la va a verificar; no vuelvas a emitir."}
        _liberar_reserva(solicitud_id)
        return {"ok": False,
                "error": resultado.get("error", f"{courier} no emitió la guía.")}

    # Desde acá el envío YA EXISTE en FedEx. Si el guardado local falla,
    # el tracking no puede perderse: quedaría una guía real que TAURO no
    # sabe que emitió, y el admin podría emitir otra encima. Por eso se
    # reintenta y, en el peor caso, se grita en los logs con el número.
    tracking = resultado["tracking"]
    guardado = False
    cargo_confirmado = False
    for intento in (1, 2, 3):
        try:
            cargo_confirmado = guardar_guia_generada(
                solicitud_id, tracking, resultado.get("label_pdf"), courier=courier,
                message_reference=resultado.get("message_reference"),
                commercial_invoice_pdf=resultado.get("invoice_pdf"),
            )
            guardado = True
            break
        except Exception as e:
            print(f"[guia] intento {intento} de guardar el tracking {tracking} "
                  f"(solicitud {solicitud_id}) falló: {e}")
            import time as _t
            _t.sleep(1)

    if not guardado:
        print(f"[guia] ⛔ GUÍA EMITIDA SIN GUARDAR — solicitud {solicitud_id}, "
              f"tracking {tracking}. Cargalo a mano antes de reintentar: el "
              f"envío YA existe en {courier}.")
        return {"ok": False,
                "error": f"La guía se emitió en {courier} (tracking {tracking}) pero no "
                         f"pudimos guardarla. Anotá ese número y avisá a soporte: "
                         f"NO vuelvas a generar la guía o saldría duplicada."}

    if sol.get("reemplaza_solicitud_id") and not cargo_confirmado:
        return {
            "ok": False,
            "error": (
                f"La nueva guía existe en {courier} (tracking {tracking}), pero "
                "la cuenta corriente no pudo cerrar el reemplazo. El PDF queda "
                "bloqueado y Tauro debe conciliarlo; no vuelvas a emitir."
            ),
        }

    return {
        "ok": True,
        "tracking": tracking,
        "tiene_label": bool(resultado.get("label_pdf")),
        "tiene_factura_comercial": bool(resultado.get("invoice_pdf")),
    }


def generar_guia_fedex(solicitud_id: int) -> dict:
    """Alias histórico: la emisión internacional ahora es multi-courier."""
    return generar_guia_internacional(solicitud_id, courier="FEDEX")


def generar_guia_dhl(solicitud_id: int) -> dict:
    """Emisión por DHL Express — mismo camino que FedEx, otro cliente."""
    from servicios.configuracion_couriers_cliente import estado_integracion
    if not estado_integracion("dhl")["operativa"]:
        return {"ok": False, "error":
                "DHL no está habilitado en producción. No se emitió ninguna guía."}
    return generar_guia_internacional(solicitud_id, courier="DHL")


def generar_guia_ups(solicitud_id: int) -> dict:
    """Emisión por UPS — mismo camino que FedEx y DHL, otro cliente."""
    return generar_guia_internacional(solicitud_id, courier="UPS")
