"""Edición de una solicitud propia: nunca modifica una guía emitida."""
import json
from datetime import datetime

from core.database import get_conn
from servicios.couriers_urls import ambito_envio
from servicios.solicitudes_guia import _congelar_cotizacion_aceptada_con_cursor
from servicios.auditoria import registrar_evento_con_cursor


CAMPOS = frozenset((
    "producto_alias cantidad destino_pais dest_nombre dest_documento dest_email dest_telefono "
    "dest_direccion dest_ciudad dest_estado dest_zip dest_contacto observaciones etiqueta_cliente "
    "peso_kg largo_cm ancho_cm alto_cm valor_declarado_usd ruta_id coti_id precio_tauro_ars "
    "precio_tauro_usd remitente_alias remitente_nombre remitente_documento remitente_email "
    "remitente_telefono remitente_direccion remitente_ciudad remitente_estado remitente_zip "
    "remitente_pais remitente_contacto precio_cliente_final_ars bultos courier tax_paga asegurar_carga"
).split())


def puede_editar_solicitud(s):
    return bool(s and s.get("estado") == "SOLICITADO" and ambito_envio(s) == "internacional"
                and not s.get("test") and s.get("visible_cliente", True)
                and not any(s.get(k) for k in ("tracking", "guia_url", "tiene_label", "label_pdf",
                    "cargo_pendiente", "reemplaza_solicitud_id", "reemplazada_por_solicitud_id")))


def editar_solicitud_cliente(solicitud_id, cliente, version, campos):
    """Campos ya validados y recotizados por el mismo POST de creación.

    Conserva identidad, vínculo con tienda e idempotencia. El lock compartido
    con emisión impide que una edición cambie lo que se está enviando al courier.
    """
    try:
        esperada = datetime.fromisoformat(version)
    except (ValueError, TypeError):
        raise ValueError("Volvé a abrir el envío para editar su versión actual.")
    cliente = cliente.strip().upper()
    nuevos = {k: v for k, v in campos.items() if k in CAMPOS}
    if not nuevos:
        raise ValueError("No hay cambios para guardar.")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM solicitudes_guia WHERE id=%s AND cliente_id=%s AND test=FALSE AND visible_cliente=TRUE FOR UPDATE",
                    (solicitud_id, cliente))
        row = cur.fetchone()
        if not row or not puede_editar_solicitud(dict(row)):
            raise ValueError("Este envío ya no se puede editar. Volvé a revisar su estado; no cambiamos sus datos.")
        anterior = dict(row)
        if anterior.get("updated_at") != esperada:
            raise ValueError("El envío cambió mientras lo editabas. Volvé a abrirlo para revisar la versión actual.")
        # No se permite tocar historia contable ni una reemisión pendiente.
        cur.execute("""SELECT EXISTS(SELECT 1 FROM envios WHERE solicitud_id=%s)
                      OR EXISTS(SELECT 1 FROM conciliaciones_envio WHERE solicitud_id=%s)
                      OR EXISTS(SELECT 1 FROM ajustes_cliente WHERE solicitud_id=%s)
                      OR EXISTS(SELECT 1 FROM solicitudes_guia_reemisiones
                                WHERE solicitud_nueva_id=%s OR solicitud_anterior_id=%s)
                      OR EXISTS(SELECT 1 FROM recolecciones WHERE solicitud_id=%s
                                AND estado <> 'CANCELADA') AS bloqueado""",
                    (solicitud_id,) * 6)
        if cur.fetchone()["bloqueado"]:
            raise ValueError("El envío tiene una operación asociada y necesita revisión de TAURO antes de editarse.")
        nuevos["ambito"] = "INTERNACIONAL"
        nuevos["courier"] = str(nuevos.get("courier") or anterior.get("courier") or "DHL").upper()
        if ambito_envio({**anterior, **nuevos, "ambito": None}) != "internacional":
            raise ValueError("Para un envío nacional iniciá una cotización nacional nueva.")
        valores = [json.dumps(v) if k == "bultos" else v for k, v in nuevos.items()]
        sets = ", ".join(f"{k}=%s" for k in nuevos)
        cur.execute(f"UPDATE solicitudes_guia SET {sets}, updated_at=NOW() WHERE id=%s AND cliente_id=%s RETURNING *",
                    (*valores, solicitud_id, cliente))
        actual = dict(cur.fetchone())
        # La base archiva la versión anterior y permite el cambio únicamente
        # en esta transacción, antes de cualquier emisión o cargo.
        cur.execute("SELECT set_config('tauro.edicion_solicitud', %s, true)", (str(solicitud_id),))
        if not _congelar_cotizacion_aceptada_con_cursor(cur, {**actual, "created_at": actual["updated_at"]}, actualizar_pendiente=True):
            raise ValueError("No pudimos guardar la nueva tarifa. Volvé a cotizar antes de guardar los cambios.")
        registrar_evento_con_cursor(cur, event="portal.solicitud_editada", actor_type="cliente",
            actor_ref=cliente, ip=None, method="POST", path=None, status_code=200,
            success=True, request_id=None, metadata={"solicitud_id": solicitud_id,
                "campos": sorted(k for k in nuevos if anterior.get(k) != nuevos[k]),
                "cotizacion_anterior": anterior.get("coti_id"), "cotizacion_nueva": actual.get("coti_id")})
        return actual
