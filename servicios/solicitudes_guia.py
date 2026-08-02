# ============================================================
# Servicio de solicitudes de guía — PostgreSQL
# ============================================================

import json
import uuid
from typing import Optional

import psycopg2

from core.database import get_conn


ESTADOS_SOLICITUD = [
    "SOLICITADO",
    "EN_PROCESO",
    "GUIA_LISTA",
    "DESPACHADO",
    "CANCELADO",
]

# Estado transitorio: dura los segundos que tarda el courier en emitir.
# Es la reserva que impide que dos clicks generen dos guías reales, y por
# eso no se ofrece en el desplegable del admin — lo pone y lo saca el
# sistema solo.
ESTADO_EMITIENDO = "EMITIENDO"
ESTADOS_VALIDOS = ESTADOS_SOLICITUD + [ESTADO_EMITIENDO]


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def _sin_label(row: dict) -> dict:
    """Reemplaza el PDF (bytea) por un booleano en los listados, para no cargar
    los bytes del label en cada fila de la tabla."""
    row["tiene_label"] = bool(row.get("label_pdf"))
    row.pop("label_pdf", None)
    return row


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
    observaciones: str,
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
    precio_cliente_final_ars: Optional[float] = None,
    bultos: Optional[list] = None,
    courier: str = "FEDEX",
    servicio_courier: Optional[str] = None,
) -> dict:
    """Crea una solicitud de guía pendiente para gestión operativa.

    bultos (multi-bulto): lista [{producto_alias, cantidad, peso_kg, largo_cm,
    ancho_cm, alto_cm, valor_unitario_usd, hs_code, descripcion_en}, ...].
    Los campos legacy (producto_alias, cantidad, peso/dims/valor) guardan el
    primer bulto + totales para que listados y admin sigan andando."""
    cliente_id = cliente_id.strip().upper()
    cantidad = max(int(cantidad or 1), 1)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, cantidad, destino_pais,
                    remitente_alias, remitente_nombre, remitente_documento,
                    remitente_email, remitente_telefono, remitente_direccion,
                    remitente_ciudad, remitente_estado, remitente_zip, remitente_pais,
                    dest_nombre, dest_documento, dest_email, dest_telefono,
                    dest_direccion, dest_ciudad, dest_estado, dest_zip,
                    observaciones, peso_kg, largo_cm, ancho_cm, alto_cm,
                    valor_declarado_usd, ruta_id, coti_id, precio_tauro_ars,
                    precio_tauro_usd, precio_cliente_final_ars, bultos,
                    courier, servicio_courier
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                RETURNING *
                """,
                (
                    cliente_id,
                    producto_alias.strip(),
                    cantidad,
                    destino_pais.strip().upper(),
                    _clean(remitente_alias),
                    _clean(remitente_nombre),
                    _clean(remitente_documento),
                    _clean(remitente_email),
                    _clean(remitente_telefono),
                    _clean(remitente_direccion),
                    _clean(remitente_ciudad),
                    _clean(remitente_estado),
                    _clean(remitente_zip),
                    _clean(remitente_pais) or "AR",
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
                ),
            )
            return dict(cur.fetchone())


def listar_solicitudes_cliente(cliente_id: str, limite: int = 100) -> list[dict]:
    """Solicitudes de guía de un cliente, últimas primero."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM solicitudes_guia
                WHERE cliente_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (cliente_id.strip().upper(), limite),
            )
            return [_sin_label(dict(r)) for r in cur.fetchall()]


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
                FROM solicitudes_guia
                WHERE cliente_id = %s AND estado = 'GUIA_LISTA'
                """,
                (cliente_id.strip().upper(),),
            )
            return int(cur.fetchone()["n"])


def listar_solicitudes_admin(estado: str = "", limite: int = 300) -> list[dict]:
    """Solicitudes para la bandeja operativa del admin."""
    estado = (estado or "").strip().upper()
    params: list = []
    where = ""
    if estado:
        where = "WHERE s.estado = %s"
        params.append(estado)
    params.append(limite)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.*, c.nombre AS cliente_nombre, c.email AS cliente_email
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                {where}
                ORDER BY
                    CASE s.estado
                        WHEN 'SOLICITADO' THEN 1
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
            return [_sin_label(dict(r)) for r in cur.fetchall()]


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
                    SET estado=%s, tracking=%s, guia_url=%s, updated_at=NOW()
                    WHERE id=%s
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
                    """,
                    (estado, _clean(tracking), _clean(guia_url), solicitud_id),
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
                WHERE id=%s AND tracking IS NULL AND estado <> %s
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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM solicitudes_guia
                WHERE estado IN ('SOLICITADO', 'EN_PROCESO')
                """
            )
            row = cur.fetchone()
    return int(row["n"] if row else 0)


def obtener_solicitud_de_cliente(solicitud_id: int, cliente_id: str) -> Optional[dict]:
    """Una solicitud del cliente logueado (para la página de detalle del
    portal). Chequea pertenencia y no carga los bytes del label."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM solicitudes_guia
                WHERE id = %s AND cliente_id = %s
                """,
                (solicitud_id, cliente_id.strip().upper()),
            )
            row = cur.fetchone()
    return _sin_label(dict(row)) if row else None


# ── Emisión de guía real (FedEx Ship API) ───────────────────

def obtener_solicitud(solicitud_id: int) -> Optional[dict]:
    """Una solicitud con los datos del cliente (para el remitente por defecto)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*,
                       c.nombre AS cliente_nombre, c.telefono AS cliente_telefono,
                       c.direccion AS cliente_direccion, c.ciudad AS cliente_ciudad,
                       c.cp AS cliente_cp, c.pais AS cliente_pais
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                WHERE s.id = %s
                """,
                (solicitud_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def guardar_guia_generada(solicitud_id: int, tracking: str, label_pdf: Optional[bytes],
                          courier: str = "FEDEX") -> None:
    """Persiste la guía emitida: tracking, label PDF y estado GUIA_LISTA."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitudes_guia
                SET estado='GUIA_LISTA', tracking=%s, label_pdf=%s, courier=%s,
                    guia_generada_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (tracking, psycopg2.Binary(label_pdf) if label_pdf else None, courier, solicitud_id),
            )
    # DÉBITO AUTOMÁTICO (decisión de Leandro 28/07): la guía emitida carga
    # sola su costo a la cuenta corriente del cliente. Es idempotente (índice
    # único por solicitud) y un fallo acá NO tumba la emisión: la guía ya
    # existe en el courier y eso es lo que no se puede deshacer — el cargo,
    # en el peor caso, se carga a mano y el log lo dice.
    try:
        from servicios.cuenta_corriente import cargar_guia_emitida
        cargar_guia_emitida(solicitud_id)
    except Exception as e:
        print(f"[solicitudes] guía {tracking} emitida pero el cargo automático "
              f"falló ({e}): FACTURAR A MANO la solicitud {solicitud_id}")

    # Si el envío nació de una venta de Shopify, avisamos a la tienda:
    # el pedido queda "Enviado" con su tracking y el comprador recibe el
    # mail solo. Nunca dejamos que un fallo acá tumbe la emisión de la
    # guía — la guía ya está hecha y es lo que importa.
    # En un hilo aparte: avisarle a la tienda puede tardar (API de un
    # tercero, con timeout). El admin no puede quedarse colgado mirando
    # una pantalla en blanco después de emitir — la guía ya está hecha.
    import threading
    threading.Thread(
        target=_avisar_tienda_origen,
        args=(solicitud_id, tracking, courier),
        daemon=True,
    ).start()


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
                    "SELECT label_pdf FROM solicitudes_guia WHERE id=%s AND cliente_id=%s",
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
        )
    except Exception as e:
        return {"ok": False, "error": f"No se pudo crear el envío: {e}"}

    sid = creada.get("id")
    # guardar_guia_generada hace el resto: GUIA_LISTA + label + cargo
    # automático en cuenta corriente. El aviso a tienda sale limpio porque
    # no hay pedido vinculado.
    guardar_guia_generada(sid, tracking, label_pdf, courier="FEDEX")
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

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.cliente_id, s.tracking, s.estado,
                       c.puede_emitir, c.tope_deuda_ars
                FROM solicitudes_guia s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                WHERE s.id = %s
            """, (solicitud_id,))
            fila = cur.fetchone()

    if not fila or fila["cliente_id"] != cliente_id:
        return {"ok": False, "error": "Esa solicitud no existe o no es tuya."}
    if fila["tracking"]:
        return {"ok": False, "error": "Esa solicitud ya tiene guía emitida."}
    if not fila["puede_emitir"]:
        return {"ok": False, "error": "Tu cuenta no tiene habilitada la emisión "
                                      "directa. Reenviá la solicitud a Tauro y la "
                                      "emitimos nosotros."}

    tope = fila["tope_deuda_ars"]
    if tope is not None and float(tope) >= 0:
        from servicios.cuenta_corriente import get_facturado_real, saldo
        deuda = saldo(cliente_id, get_facturado_real(cliente_id))["saldo_pendiente_ars"]
        if deuda > float(tope):
            return {"ok": False, "error":
                    f"Tu saldo pendiente (ARS {deuda:,.0f}) supera el límite "
                    f"de tu cuenta (ARS {float(tope):,.0f}). Registrá un pago "
                    f"para seguir emitiendo, o reenviá la solicitud a Tauro."}

    print(f"[solicitudes] emisión del CLIENTE {cliente_id} para la solicitud "
          f"{solicitud_id} (autorizada: flag + tope OK)")
    return generar_guia(solicitud_id)


def generar_guia(solicitud_id: int) -> dict:
    """
    Despachador de emisión: mira el courier de la solicitud y emite por
    el canal que corresponde. FEDEX = cuenta propia (internacional);
    ENVIA = envia.com (nacionales AR, debita el wallet prepago).
    """
    sol = obtener_solicitud(solicitud_id)
    if not sol:
        return {"ok": False, "error": "Solicitud no encontrada."}

    courier = (sol.get("courier") or "FEDEX").upper()

    if courier == "ENVIA":
        return generar_guia_envia(sol)
    if courier == "FEDEX":
        return generar_guia_fedex(solicitud_id)

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
            f"La cotización de {courier} sí funciona; la emisión está en "
            f"desarrollo. Emitila a mano o elegí otro courier."
        ),
    }


def generar_guia_envia(sol: dict) -> dict:
    """
    Emite una guía NACIONAL vía envia.com con el carrier/servicio que
    eligió el cliente al crear la solicitud. Cuidado: debita el wallet
    prepago de envia — si no hay saldo, envia rechaza y lo informamos.
    """
    if sol.get("estado") == "CANCELADO":
        return {"ok": False, "error": "La solicitud está cancelada."}
    if sol.get("tracking") and sol.get("label_pdf"):
        return {"ok": False, "error": "Esta solicitud ya tiene una guía generada."}

    servicio_courier = (sol.get("servicio_courier") or "").strip()
    if "/" not in servicio_courier:
        return {"ok": False, "error": "La solicitud no tiene carrier/servicio nacional elegido."}
    carrier, servicio = servicio_courier.split("/", 1)

    bultos = sol.get("bultos") or []
    if isinstance(bultos, str):
        try:
            import json as _json
            bultos = _json.loads(bultos)
        except Exception:
            bultos = []

    origen = {
        "nombre": sol.get("remitente_nombre") or sol.get("cliente_nombre") or sol["cliente_id"],
        "empresa": sol.get("cliente_nombre") or "",
        "email": sol.get("remitente_email") or "",
        "telefono": sol.get("remitente_telefono") or sol.get("cliente_telefono") or "",
        "direccion": sol.get("remitente_direccion") or sol.get("cliente_direccion") or "",
        "ciudad": sol.get("remitente_ciudad") or sol.get("cliente_ciudad") or "Buenos Aires",
        "provincia": sol.get("remitente_estado") or "CABA",
        "cp": sol.get("remitente_zip") or sol.get("cliente_cp") or "",
    }
    destino = {
        "nombre": sol.get("dest_nombre") or "",
        "email": sol.get("dest_email") or "",
        "telefono": sol.get("dest_telefono") or "",
        "direccion": sol.get("dest_direccion") or "",
        "ciudad": sol.get("dest_ciudad") or "",
        "provincia": sol.get("dest_estado") or "",
        "cp": sol.get("dest_zip") or "",
    }

    from servicios.cotizador import _get_dolar_ars
    dolar = _get_dolar_ars()
    piezas = [
        {
            "descripcion": b.get("descripcion_en") or b.get("producto_alias") or "Merchandise",
            "cantidad": max(int(b.get("cantidad") or 1), 1),
            "largo_cm": b.get("largo_cm") or 30,
            "ancho_cm": b.get("ancho_cm") or 20,
            "alto_cm": b.get("alto_cm") or 10,
            "peso_kg": b.get("peso_kg") or 0.5,
            "valor_declarado_ars": round(float(b.get("valor_unitario_usd") or 0) * dolar, 2),
        }
        for b in bultos
    ] or [{
        "descripcion": sol.get("producto_alias") or "Merchandise",
        "cantidad": sol.get("cantidad") or 1,
        "largo_cm": sol.get("largo_cm") or 30,
        "ancho_cm": sol.get("ancho_cm") or 20,
        "alto_cm": sol.get("alto_cm") or 10,
        "peso_kg": sol.get("peso_kg") or 0.5,
        "valor_declarado_ars": round(float(sol.get("valor_declarado_usd") or 0) * dolar, 2),
    }]

    from core.envia_client import EnviaClient
    resultado = EnviaClient().create_shipment_nacional(
        origen=origen, destino=destino, bultos=piezas,
        carrier=carrier, servicio=servicio,
    )
    if not resultado.get("encontrado"):
        return {"ok": False, "error": resultado.get("error", "envia.com no emitió la guía.")}

    guardar_guia_generada(
        sol["id"],
        resultado["tracking"],
        resultado.get("label_pdf"),
        courier="ENVIA",
    )
    # Si el PDF no se pudo bajar, al menos dejamos el link de envia.
    if not resultado.get("label_pdf") and resultado.get("label_url"):
        actualizar_solicitud_guia(
            sol["id"], estado="GUIA_LISTA",
            tracking=resultado["tracking"], guia_url=resultado["label_url"],
        )
    return {
        "ok": True,
        "tracking": resultado["tracking"],
        "tiene_label": bool(resultado.get("label_pdf")),
    }


def _reservar_para_emitir(solicitud_id: int) -> bool:
    """
    Marca la solicitud como EMITIENDO, pero sólo si nadie más lo hizo.

    Es un UPDATE condicional: la base garantiza que de N intentos
    simultáneos exactamente uno modifica la fila. El que obtiene la fila
    puede llamar al courier; los demás se van con las manos vacías.

    La ventana de 10 minutos es la red por si el proceso muere en el
    medio (deploy, reinicio): pasado ese rato la solicitud vuelve a
    quedar disponible en vez de bloquearse para siempre.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes_guia
                SET estado = 'EMITIENDO', updated_at = NOW()
                WHERE id = %s
                  AND tracking IS NULL
                  AND (estado <> 'EMITIENDO'
                       OR updated_at < NOW() - INTERVAL '10 minutes')
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
                    UPDATE solicitudes_guia SET estado = %s, updated_at = NOW()
                    WHERE id = %s AND estado = 'EMITIENDO' AND tracking IS NULL
                """, (estado, solicitud_id))
            conn.commit()
    except Exception as e:
        print(f"[guia] no pude liberar la reserva de {solicitud_id}: {e}")


def generar_guia_fedex(solicitud_id: int) -> dict:
    """
    Emite la guía real en FedEx para una solicitud y guarda tracking + label PDF.
    Devuelve {ok, tracking, tiene_label} o {ok: False, error}.
    """
    sol = obtener_solicitud(solicitud_id)
    if not sol:
        return {"ok": False, "error": "Solicitud no encontrada."}
    if sol.get("estado") == "CANCELADO":
        return {"ok": False, "error": "La solicitud está cancelada."}
    if sol.get("tracking"):
        return {"ok": False, "error": "Esta solicitud ya tiene una guía generada."}

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
    if not _reservar_para_emitir(solicitud_id):
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

    from servicios.rutas import pais_a_iso2

    shipper = {
        "nombre": sol.get("remitente_nombre") or sol.get("cliente_nombre") or sol["cliente_id"],
        "empresa": sol.get("cliente_nombre") or "",
        "telefono": sol.get("remitente_telefono") or sol.get("cliente_telefono") or "",
        "calle": sol.get("remitente_direccion") or sol.get("cliente_direccion") or "",
        "ciudad": sol.get("remitente_ciudad") or sol.get("cliente_ciudad") or "Buenos Aires",
        "estado": sol.get("remitente_estado") or "",
        "zip": sol.get("remitente_zip") or sol.get("cliente_cp") or "",
        "pais": pais_a_iso2(sol.get("remitente_pais") or sol.get("cliente_pais") or "AR"),
    }
    recipient = {
        "nombre": sol.get("dest_nombre") or "",
        "telefono": sol.get("dest_telefono") or "",
        "calle": sol.get("dest_direccion") or "",
        "ciudad": sol.get("dest_ciudad") or "",
        "estado": sol.get("dest_estado") or "",
        "zip": sol.get("dest_zip") or "",
        "pais": pais_a_iso2(sol.get("destino_pais") or "US"),
    }
    datos_envio = {
        "shipper": shipper,
        "recipient": recipient,
    }
    if bultos:
        # Multi-bulto: cada caja del envío como pieza propia, con su label.
        datos_envio["bultos"] = [
            {
                "peso_kg": b.get("peso_kg") or 0.5,
                "largo": b.get("largo_cm") or 30,
                "ancho": b.get("ancho_cm") or 20,
                "alto": b.get("alto_cm") or 10,
                "valor_unitario_usd": b.get("valor_unitario_usd") or 100,
                "unidades": max(int(b.get("cantidad") or 1), 1),
                "hs_code": b.get("hs_code") or "",
                "descripcion_en": b.get("descripcion_en") or "Merchandise",
                "pais_origen": "AR",
            }
            for b in bultos
        ]
    else:
        # OJO: valor_declarado_usd viene TOTALIZADO (unitario × cantidad) desde
        # el portal. create_shipment vuelve a multiplicar unitario × cantidad,
        # así que acá se pasa el UNITARIO real para no declarar de más en aduana.
        cantidad_sol = max(int(sol.get("cantidad") or 1), 1)
        valor_total_sol = float(sol.get("valor_declarado_usd") or 100)
        datos_envio["package"] = {
            "peso_kg": sol.get("peso_kg") or 0.5,
            "largo": sol.get("largo_cm") or 30,
            "ancho": sol.get("ancho_cm") or 20,
            "alto": sol.get("alto_cm") or 10,
        }
        datos_envio["commodity"] = {
            "descripcion": descripcion_en,
            "hs_code": hs_code,
            "cantidad": cantidad_sol,
            "valor_unitario_usd": round(valor_total_sol / cantidad_sol, 2),
            "pais_origen": "AR",
        }

    from core.fedex_client import FedExClient
    try:
        resultado = FedExClient().create_shipment(datos_envio)
    except Exception as e:
        _liberar_reserva(solicitud_id)
        print(f"[guia] excepción emitiendo la solicitud {solicitud_id}: {e}")
        return {"ok": False, "error": f"No pudimos emitir la guía: {e}"}

    if not resultado.get("encontrado"):
        _liberar_reserva(solicitud_id)
        return {"ok": False, "error": resultado.get("error", "FedEx no emitió la guía.")}

    # Desde acá el envío YA EXISTE en FedEx. Si el guardado local falla,
    # el tracking no puede perderse: quedaría una guía real que TAURO no
    # sabe que emitió, y el admin podría emitir otra encima. Por eso se
    # reintenta y, en el peor caso, se grita en los logs con el número.
    tracking = resultado["tracking"]
    guardado = False
    for intento in (1, 2, 3):
        try:
            guardar_guia_generada(
                solicitud_id, tracking, resultado.get("label_pdf"), courier="FEDEX",
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
              f"envío YA existe en FedEx.")
        return {"ok": False,
                "error": f"La guía se emitió en FedEx (tracking {tracking}) pero no "
                         f"pudimos guardarla. Anotá ese número y avisá a soporte: "
                         f"NO vuelvas a generar la guía o saldría duplicada."}

    return {
        "ok": True,
        "tracking": tracking,
        "tiene_label": bool(resultado.get("label_pdf")),
    }
