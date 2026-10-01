"""Edición de una solicitud propia: nunca modifica una guía emitida."""
import json
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

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


def _registrar_tarifa_revision(cur, cliente, campos, base):
    """La recotización multibulto no trae coti_id: guardar su base privada.

    Se usa la misma transacción del envío. El log legacy normaliza a USD;
    el snapshot conserva moneda, costo y tipo de cambio originales.
    Nunca recibe importes del navegador, sólo el resultado del cotizador.
    """
    try:
        precio_ars = Decimal(str(campos['precio_tauro_ars']))
        precio_usd = Decimal(str(campos['precio_tauro_usd']))
        costo_ars = Decimal(str(base['costo_courier_estimado_ars']))
        peso = Decimal(str(base['peso_real_cotizado_kg']))
        facturable = Decimal(str(base['peso_facturable_cotizado_kg']))
        if (not all(v.is_finite() for v in (precio_ars, precio_usd, costo_ars, peso, facturable))
                or min(precio_ars, precio_usd, peso, facturable) <= 0 or costo_ars < 0):
            raise ValueError
        costo_usd = costo_ars * precio_usd / precio_ars
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ValueError('La nueva tarifa no tiene una base válida. Volvé a cotizar.')
    identidad = uuid.uuid4().hex
    origen, destino = campos.get('remitente_pais'), campos.get('destino_pais')
    ruta = campos.get('ruta_id') or f'{origen}-{destino}'
    cur.execute("""INSERT INTO cotizaciones
        (coti_id, cliente_id, ruta_id, peso_kg, peso_usado_kg,
         costo_fedex_usd, markup_tipo, markup_valor, precio_final_usd,
         precio_final_ars, courier, ambito, origen_iso, destino_iso)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'INTERNACIONAL',%s,%s)""",
        (identidad, cliente, ruta, peso, facturable, costo_usd,
         base.get('markup_tipo'), base.get('markup_valor'), precio_usd,
         precio_ars, campos['courier'], origen, destino))
    return identidad, ruta


def editar_solicitud_cliente(solicitud_id, cliente, version, campos, *, base_interna=None):
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
        if base_interna is not None:
            nuevos['coti_id'], nuevos['ruta_id'] = _registrar_tarifa_revision(
                cur, cliente, {**anterior, **nuevos}, base_interna)
        cur.execute('SELECT 1 FROM envio_cotizacion_snapshots WHERE solicitud_id=%s', (solicitud_id,))
        tenia_snapshot = bool(cur.fetchone())
        valores = [json.dumps(v) if k == "bultos" else v for k, v in nuevos.items()]
        sets = ", ".join(f"{k}=%s" for k in nuevos)
        cur.execute(f"UPDATE solicitudes_guia SET {sets}, updated_at=NOW() WHERE id=%s AND cliente_id=%s RETURNING *",
                    (*valores, solicitud_id, cliente))
        actual = dict(cur.fetchone())
        # La base archiva la versión anterior y permite el cambio únicamente
        # en esta transacción, antes de cualquier emisión o cargo.
        cur.execute("SELECT set_config('tauro.edicion_solicitud', %s, true)", (str(solicitud_id),))
        if not _congelar_cotizacion_aceptada_con_cursor(cur, {**actual, "created_at": actual["updated_at"]},
                base_interna=base_interna, actualizar_pendiente=True):
            raise ValueError("No pudimos guardar la nueva tarifa. Volvé a cotizar antes de guardar los cambios.")
        if not tenia_snapshot:
            # Algunas solicitudes anteriores sólo guardaban el precio final.
            # Conservar lo conocido sin inventar su costo o margen anterior.
            previa = dict(origen='solicitud_previa_sin_snapshot',
                precio_cliente_inicial_ars=anterior.get('precio_tauro_ars'),
                coti_id=anterior.get('coti_id'), courier=anterior.get('courier'),
                costo_courier_estimado_ars=None, margen_tauro_protegido_ars=None)
            cur.execute("""INSERT INTO solicitud_cotizacion_revisiones
                (solicitud_id, snapshot_anterior, cotizacion_nueva) VALUES(%s,%s::jsonb,%s)""",
                (solicitud_id, json.dumps(previa, default=str), actual['coti_id']))
        registrar_evento_con_cursor(cur, event="portal.solicitud_editada", actor_type="cliente",
            actor_ref=cliente, ip=None, method="POST", path=None, status_code=200,
            success=True, request_id=None, metadata={"solicitud_id": solicitud_id,
                "campos": sorted(k for k in nuevos if anterior.get(k) != nuevos[k]),
                "cotizacion_anterior": anterior.get("coti_id"), "cotizacion_nueva": actual.get("coti_id")})
        return actual
