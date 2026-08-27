# ============================================================
# Servicio de solicitudes de guía — PostgreSQL
# ============================================================

import json
import uuid
from typing import Optional

import psycopg2

from core.database import get_conn
from servicios.couriers_urls import ambito_envio
from servicios.numeros_humanos import parse_entero_formulario, parse_float_formulario


ESTADOS_SOLICITUD = [
    "SOLICITADO",
    "EN_PROCESO",
    # El courier pudo haber recibido una operación irreversible, pero TAURO
    # no recibió una respuesta concluyente. No se vuelve a emitir hasta que
    # una persona concilie la Message-Reference en el portal del courier.
    "VERIFICAR_COURIER",
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


class IdempotencyConflictError(ValueError):
    """La misma clave externa intentó crear dos pedidos distintos."""


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


def _sin_label(row: dict) -> dict:
    """Reemplaza el PDF (bytea) por un booleano en los listados, para no cargar
    los bytes del label en cada fila de la tabla."""
    if "tiene_label" not in row:
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
    api_referencia: str = "",
    idempotency_key_hash: str = "",
    request_fingerprint: str = "",
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

    with get_conn() as conn:
        with conn.cursor() as cur:
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
                    api_referencia, idempotency_key_hash, request_fingerprint
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
                    %s, %s, %s
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
                ),
            )
            row = cur.fetchone()
            if row:
                resultado = dict(row)
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
    cliente_id: str, limite: Optional[int] = 100
) -> list[dict]:
    """Solicitudes de guía de un cliente, últimas primero.

    ``limite=None`` devuelve el historial completo. Las vistas de resumen
    deben pasar un número explícito para no traer filas innecesarias.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Columnas de listado: nunca traer el BYTEA de la guía. En el
            # historial completo eso podría transferir cientos de PDFs sólo
            # para dibujar un tilde en cada fila.
            query = """
                SELECT id, cliente_id, estado, producto_alias, cantidad,
                       remitente_pais, ambito, destino_pais, dest_nombre, dest_ciudad, observaciones,
                       peso_kg, valor_declarado_usd, precio_tauro_ars,
                       precio_tauro_usd, precio_cliente_final_ars, tracking,
                       guia_url, created_at, courier, bultos,
                       (label_pdf IS NOT NULL) AS tiene_label
                FROM solicitudes_guia
                WHERE cliente_id = %s
                ORDER BY created_at DESC
            """
            params = [cliente_id.strip().upper()]
            if limite is not None:
                query += " LIMIT %s"
                params.append(max(1, int(limite)))
            cur.execute(query, tuple(params))
            return [_sin_label(dict(r)) for r in cur.fetchall()]


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

    condiciones = ["cliente_id=%s"]
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
                       (label_pdf IS NOT NULL) AS tiene_label,
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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM solicitudes_guia
                WHERE estado IN ('SOLICITADO', 'EN_PROCESO', 'VERIFICAR_COURIER')
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


def obtener_label_de_cliente(solicitud_id: int, cliente_id: str) -> Optional[bytes]:
    """Devuelve el PDF sólo si la solicitud pertenece al dueño de la API key."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT label_pdf
                FROM solicitudes_guia
                WHERE id=%s AND cliente_id=%s
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
                          courier: str = "FEDEX",
                          message_reference: Optional[str] = None) -> None:
    """Persiste la guía emitida: tracking, label PDF y estado GUIA_LISTA."""
    tracking = (tracking or "").strip()[:120]
    if not tracking:
        raise ValueError("El courier no devolvió un tracking válido.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE solicitudes_guia
                SET estado='GUIA_LISTA', tracking=%s, label_pdf=%s, courier=%s,
                    courier_message_reference=COALESCE(%s, courier_message_reference),
                    courier_error=NULL, cargo_pendiente=TRUE, cargo_error=NULL,
                    guia_generada_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (tracking, psycopg2.Binary(label_pdf) if label_pdf else None,
                 courier, _clean(message_reference), solicitud_id),
            )
    # DÉBITO AUTOMÁTICO (decisión de Leandro 28/07): la guía emitida carga
    # sola su costo a la cuenta corriente del cliente. Es idempotente (índice
    # único por solicitud) y un fallo acá NO tumba la emisión: la guía ya
    # existe en el courier y eso es lo que no se puede deshacer — el cargo,
    # en el peor caso, se carga a mano y el log lo dice.
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
    import threading
    threading.Thread(
        target=_avisar_tienda_origen,
        args=(solicitud_id, tracking, courier),
        daemon=True,
    ).start()


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
            remitente_pais="AR",
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

            tope = fila["tope_deuda_ars"]
            if tope is not None and float(tope) >= 0:
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
                deuda = float(resumen_credito.get("deuda") or 0)
                reservado = float(resumen_credito.get("reservado") or 0)
                nueva = float(fila.get("precio_tauro_ars") or 0)
                proyectado = deuda + reservado + nueva
                if proyectado > float(tope):
                    proyectado_txt = f"{proyectado:,.0f}".replace(",", ".")
                    tope_txt = f"{float(tope):,.0f}".replace(",", ".")
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
                        b.get("valor_unitario_usd"), f"Caja {i}: valor declarado", importe=True
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

    # El cliente del courier se elige acá: el payload (shipper/recipient/
    # bultos) es el MISMO contrato para los dos, por eso todo el armado de
    # arriba se comparte y sumar un courier no duplica esta función.
    courier = (courier or "FEDEX").upper()
    if courier == "DHL":
        lineas_aduana = (datos_envio.get("bultos") or
                          [datos_envio.get("commodity") or {}])
        if any(not str(linea.get("hs_code") or "").strip()
               for linea in lineas_aduana):
            _liberar_reserva(solicitud_id)
            return {"ok": False, "error":
                    "Falta el HS code de la mercadería. No llamamos a DHL ni "
                    "generamos ningún cargo; completalo antes de emitir."}
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
    for intento in (1, 2, 3):
        try:
            guardar_guia_generada(
                solicitud_id, tracking, resultado.get("label_pdf"), courier=courier,
                message_reference=resultado.get("message_reference"),
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

    return {
        "ok": True,
        "tracking": tracking,
        "tiene_label": bool(resultado.get("label_pdf")),
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
