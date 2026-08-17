# ============================================================
# Recolecciones (pickups) — que el chofer pase a buscar
# ============================================================
# Punto de la spec: "El cliente podrá crear recolecciones con todas estas
# empresas internacionales que lo permitan".
#
# Modelo: puede ser manual (remitente predeterminado del cliente) o ligada a
# una solicitud emitida. En ese caso se congelan el origen, courier y piezas
# de ESA guía: esencial para importadores con proveedores distintos.
#
# OJO: agendar NO es idempotente (dos llamadas = dos visitas del chofer),
# igual que emitir. Por eso hay reserva por (cliente, fecha): un cliente no
# puede agendar dos recolecciones el mismo día sin cancelar la anterior.
# ============================================================
from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import psycopg2

from core.database import get_conn
from servicios.numeros_humanos import parse_entero_formulario, parse_float_formulario

ESTADOS = [
    "AGENDANDO", "AGENDADA", "CANCELANDO", "VERIFICAR_COURIER",
    "CANCELADA", "COMPLETADA",
]

MAX_BULTOS_RECOLECCION = 20
MAX_PESO_RECOLECCION_KG = 1400.0
MAX_KG_POR_BULTO = 70.0

_tabla_lista = False
_tabla_lock = threading.Lock()


def _ensure_tabla() -> None:
    """Comprueba la migración canónica, sin ejecutar DDL en tráfico real.

    Antes cada worker hacía DROP/CREATE de índices en el primer request. En
    múltiples réplicas eso podía bloquear o dejar pasar dos visitas. El
    esquema y las garantías de unicidad se instalan exclusivamente desde
    ``sql/schema.sql`` durante el arranque.
    """
    global _tabla_lista
    if _tabla_lista:
        return
    with _tabla_lock:
        if _tabla_lista:
            return
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        to_regclass('public.recolecciones') AS tabla,
                        to_regclass('public.uq_recoleccion_cliente_fecha_abierta_v2') AS idx_fecha,
                        to_regclass('public.uq_recoleccion_solicitud_abierta_v2') AS idx_solicitud,
                        EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_schema='public' AND table_name='recolecciones'
                              AND column_name='updated_at'
                        ) AS columna_updated
                """)
                estado = cur.fetchone() or {}
                if not all((estado.get("tabla"), estado.get("idx_fecha"),
                            estado.get("idx_solicitud"), estado.get("columna_updated"))):
                    raise RuntimeError(
                        "La migración de recolecciones no está completa; "
                        "se bloqueó la operación para evitar retiros duplicados."
                    )
        _tabla_lista = True


def _dias_habiles_validos(fecha_str: str) -> Optional[str]:
    """Devuelve el motivo del rechazo, o None si la fecha sirve."""
    try:
        f = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    except ValueError:
        return "La fecha no es válida."
    hoy = date.today()
    if f < hoy:
        return "Esa fecha ya pasó."
    if f > hoy + timedelta(days=14):
        return "Sólo se puede agendar hasta 14 días adelante."
    if f.weekday() >= 5:
        return "Los couriers no recolectan sábados ni domingos."
    return None


def _ventana_horaria_valida(ready_time: str, close_time: str) -> Optional[str]:
    try:
        inicio = datetime.strptime(ready_time, "%H:%M")
        cierre = datetime.strptime(close_time, "%H:%M")
    except ValueError:
        return "El horario de recolección no es válido."
    if cierre <= inicio:
        return "El horario de cierre debe ser posterior al de inicio."
    if (cierre - inicio).total_seconds() < 2 * 60 * 60:
        return "La ventana de recolección debe ser de al menos 2 horas."
    return None


def _cliente_pickup(courier: str):
    """
    El cliente de API que agenda/cancela para ese courier. Registro chico y
    explícito: sumar un courier acá es UNA línea, y un courier que no está
    devuelve None → error claro en vez de caer a FedEx por descarte (el mismo
    principio que el despachador de emisión: hasta el 06/08 esto estaba
    cableado a FedExClient y una recolección "DHL" se agendaba en FedEx).
    """
    courier = (courier or "FEDEX").strip().upper()
    if courier == "FEDEX":
        from core.fedex_client import FedExClient
        return FedExClient()
    if courier == "DHL":
        from core.dhl_client import DHLClient
        return DHLClient()
    return None


def cliente_puede_recolectar(cliente_id: str, courier: str | None = None) -> bool:
    """Permiso opt-in para crear operaciones reales de retiro.

    Falla cerrado: si el cliente no existe, está inactivo o no puede leerse
    la configuración, no se llama a ningún courier.
    """
    from servicios.configuracion_couriers_cliente import (
        mapa_permisos, permiso_courier,
    )

    cliente_id = (cliente_id or "").strip().upper()
    if not cliente_id:
        return False
    if courier:
        return permiso_courier(cliente_id, courier, "recolectar")
    return any(mapa_permisos(cliente_id, "recolectar").values())


def datos_retiro_desde_solicitud(sol: dict) -> dict:
    """Fuente única del retiro ligado a una guía: origen y cajas reales."""
    bultos_crudos = sol.get("bultos")
    bultos = bultos_crudos or []
    if isinstance(bultos, str):
        try:
            bultos = json.loads(bultos)
        except (TypeError, ValueError):
            raise ValueError("Los datos guardados de las cajas no son válidos.") from None
    if bultos and not isinstance(bultos, list):
        raise ValueError("Los datos guardados de las cajas no son válidos.")

    def entero(valor, campo: str) -> int:
        return parse_entero_formulario(valor, campo, minimo=1)

    def numero(valor, campo: str, *, importe=False) -> float:
        return parse_float_formulario(valor, campo, importe=importe, minimo=0.001)

    paquetes = []
    if bultos:
        for indice, b in enumerate(bultos, start=1):
            if not isinstance(b, dict):
                raise ValueError(f"Caja {indice}: los datos guardados no son válidos.")
            paquetes.append({
                "peso_kg": numero(b.get("peso_kg"), f"Caja {indice}: peso"),
                "largo_cm": numero(b.get("largo_cm"), f"Caja {indice}: largo"),
                "ancho_cm": numero(b.get("ancho_cm"), f"Caja {indice}: ancho"),
                "alto_cm": numero(b.get("alto_cm"), f"Caja {indice}: alto"),
                "cantidad": entero(b.get("cantidad"), f"Caja {indice}: cantidad"),
                "unidades_aduana": entero(
                    b.get("unidades_aduana"), f"Caja {indice}: unidades de aduana",
                ),
                "valor_unitario_usd": numero(
                    b.get("valor_unitario_usd"), f"Caja {indice}: valor declarado",
                    importe=True,
                ),
            })
    else:
        cantidad = entero(sol.get("cantidad"), "Cantidad de cajas")
        peso_total = numero(sol.get("peso_kg"), "Peso total")
        valor_total = numero(sol.get("valor_declarado_usd"), "Valor declarado", importe=True)

        def repartir(total: float, decimales: int, campo: str) -> list[float]:
            """Distribuye el residuo sin cambiar el total guardado de la guía."""
            escala = 10 ** decimales
            unidades_total = int(
                (Decimal(str(total)) * escala).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP,
                )
            )
            base, residuo = divmod(unidades_total, cantidad)
            if base < 1:
                raise ValueError(
                    f"{campo} es demasiado bajo para repartirlo entre {cantidad} cajas."
                )
            return [
                (base + (1 if indice < residuo else 0)) / escala
                for indice in range(cantidad)
            ]

        pesos = repartir(peso_total, 3, "El peso total")
        valores = repartir(valor_total, 2, "El valor declarado")
        largo = numero(sol.get("largo_cm"), "Largo")
        ancho = numero(sol.get("ancho_cm"), "Ancho")
        alto = numero(sol.get("alto_cm"), "Alto")
        paquetes = [{
            "peso_kg": pesos[indice],
            "largo_cm": largo,
            "ancho_cm": ancho,
            "alto_cm": alto,
            "cantidad": 1,
            "unidades_aduana": 1,
            "valor_unitario_usd": valores[indice],
        } for indice in range(cantidad)]

    cantidad_total = sum(int(p["cantidad"]) for p in paquetes)
    peso_total = round(sum(float(p["peso_kg"]) * int(p["cantidad"])
                           for p in paquetes), 3)
    from servicios.paises import normalizar as normalizar_pais

    return {
        "solicitud_id": sol.get("id"),
        "courier": (sol.get("courier") or "FEDEX").strip().upper(),
        "tracking": sol.get("tracking"),
        "bultos": cantidad_total,
        "peso_kg": peso_total,
        "paquetes": paquetes,
        "origen": {
            "nombre": (sol.get("remitente_contacto") or
                       sol.get("remitente_nombre") or sol.get("cliente_id") or ""),
            "empresa": sol.get("remitente_nombre") or "",
            "telefono": sol.get("remitente_telefono") or "",
            "calle": sol.get("remitente_direccion") or "",
            "ciudad": sol.get("remitente_ciudad") or "",
            "estado": sol.get("remitente_estado") or "",
            "zip": sol.get("remitente_zip") or "",
            "pais": normalizar_pais(sol.get("remitente_pais") or "AR"),
        },
    }


def crear(cliente_id: str, fecha: str, ready_time: str, close_time: str,
          bultos: int, peso_kg: float, instrucciones: str = "",
          courier: str = "FEDEX", solicitud_id: Optional[int] = None) -> dict:
    """
    Agenda la recolección en el courier y la guarda. Si viene de una guía,
    usa su origen y sus cajas; si es manual, el remitente predeterminado.
    """
    from servicios.direcciones import obtener_remitente_para_envio

    _ensure_tabla()
    cliente_id = (cliente_id or "").strip().upper()
    courier = (courier or "FEDEX").strip().upper()

    retiro_envio = None
    if solicitud_id:
        # No confiar en courier/dirección/peso enviados por el navegador. La
        # solicitud se resuelve otra vez con (id + cliente) y de ahí sale TODO.
        from servicios.solicitudes_guia import obtener_solicitud_de_cliente
        sol = obtener_solicitud_de_cliente(int(solicitud_id), cliente_id)
        if not sol or not sol.get("tracking"):
            return {"ok": False, "error":
                    "Ese envío no existe, no es de tu cuenta o todavía no tiene guía."}
        from servicios.couriers_urls import ambito_envio
        ambito = ambito_envio(sol)
        if ambito != "internacional":
            return {
                "ok": False,
                "error": (
                    "Ese envío no está clasificado como internacional. "
                    "Las recolecciones nacionales se habilitarán con las "
                    "APIs directas de Andreani y OCA."
                ),
            }
        try:
            retiro_envio = datos_retiro_desde_solicitud(sol)
        except ValueError as exc:
            return {"ok": False, "error": (
                f"La guía tiene datos numéricos inválidos: {exc}. "
                "No llamamos al courier ni reservamos una recolección."
            )}
        courier = retiro_envio["courier"]
        bultos = retiro_envio["bultos"]
        peso_kg = retiro_envio["peso_kg"]

    try:
        bultos = parse_entero_formulario(
            bultos, "Cantidad de bultos", minimo=1, maximo=MAX_BULTOS_RECOLECCION
        )
        peso_kg = parse_float_formulario(
            peso_kg, "Peso total", minimo=0.001, maximo=MAX_PESO_RECOLECCION_KG
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if retiro_envio:
        for paquete in retiro_envio.get("paquetes") or []:
            try:
                peso_bulto = parse_float_formulario(
                    paquete.get("peso_kg"), "Peso de bulto", minimo=0.001,
                    maximo=MAX_KG_POR_BULTO,
                )
            except ValueError:
                return {"ok": False, "error": "La guía tiene un peso de bulto inválido."}
            if not math.isfinite(peso_bulto) or not 0 < peso_bulto <= MAX_KG_POR_BULTO:
                return {"ok": False, "error":
                        f"Cada bulto debe pesar entre 0 y {MAX_KG_POR_BULTO:g} kg."}

    # Este control vive en el servicio (no sólo en la pantalla): un POST
    # armado a mano tampoco puede reservar una visita ni llamar al courier.
    if not cliente_puede_recolectar(cliente_id, courier):
        return {"ok": False, "error":
                f"Las recolecciones de {courier} todavía no están habilitadas "
                "para tu cuenta. "
                "Escribinos y las activamos."}

    # Defensa explícita además del permiso efectivo de la matriz: una
    # confirmación de sandbox nunca puede guardarse como retiro real.
    if courier == "DHL":
        from servicios.configuracion_couriers_cliente import estado_integracion
        if not estado_integracion("dhl")["operativa"]:
            return {"ok": False, "error":
                    "DHL no está habilitado en producción. No se creó ninguna "
                    "recolección real."}

    if courier == "DHL" and not retiro_envio:
        return {"ok": False, "error": (
            "Para pedir un retiro DHL, abrí una guía emitida desde Mis envíos "
            "y elegí Retiro. Así usamos su origen, cajas, medidas y valor reales."
        )}

    cliente_api = _cliente_pickup(courier)
    if cliente_api is None:
        return {"ok": False, "error": f"Todavía no agendamos recolecciones de "
                                      f"{courier}. Elegí FedEx o DHL."}

    motivo = _dias_habiles_validos(fecha)
    if motivo:
        return {"ok": False, "error": motivo}
    motivo = _ventana_horaria_valida(ready_time, close_time)
    if motivo:
        return {"ok": False, "error": motivo}

    rem = None
    if retiro_envio:
        origen = retiro_envio["origen"]
        rem = {
            "nombre": origen["nombre"], "alias": origen["empresa"],
            "telefono": origen["telefono"], "direccion": origen["calle"],
            "ciudad": origen["ciudad"], "estado": origen["estado"],
            "cp": origen["zip"], "pais": origen["pais"],
        }
    else:
        rem = obtener_remitente_para_envio(cliente_id, None)
    if not rem or not (rem.get("direccion") or "").strip():
        return {"ok": False, "error": "Necesitás una dirección de retiro cargada en "
                                      "tu libreta para pedir una recolección."}

    referencia_previa = (
        f"tauro-dhl-pick-{uuid.uuid4().hex[:20]}" if courier == "DHL" else None
    )

    # Reserva ANTES de llamar al courier: si dos pedidos entran juntos, el
    # índice único deja pasar uno solo. Agendar no se puede deshacer solo.
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO recolecciones
                        (cliente_id, fecha, ready_time, close_time, bultos, peso_kg,
                         direccion, instrucciones, estado, courier, solicitud_id,
                         courier_message_reference)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'AGENDANDO', %s, %s, %s)
                    RETURNING id
                """, (cliente_id, fecha, ready_time, close_time,
                      bultos, peso_kg,
                      f"{rem.get('direccion','')}, {rem.get('ciudad','')}".strip(", "),
                      (instrucciones or "")[:255], courier,
                      int(solicitud_id) if solicitud_id else None,
                      referencia_previa))
                rec_id = cur.fetchone()["id"]
            except psycopg2.IntegrityError as e:
                if e.pgcode != "23505":
                    raise
                return {"ok": False, "error":
                        "Ya hay una recolección abierta para ese día o para ese envío. "
                        "Cancelala o esperá a que Tauro termine de verificarla."}

    try:
        resultado = cliente_api.create_pickup({
            "origen": {
                "nombre": rem.get("nombre") or cliente_id,
                "empresa": rem.get("alias") or "",
                "telefono": rem.get("telefono") or "",
                "calle": rem.get("direccion") or "",
                "ciudad": rem.get("ciudad") or "",
                "estado": rem.get("estado") or "",
                "zip": rem.get("cp") or "",
                "pais": rem.get("pais") or "AR",
            },
            "fecha": fecha, "ready_time": ready_time, "close_time": close_time,
            "peso_kg": peso_kg, "bultos": bultos, "instrucciones": instrucciones,
            "paquetes": retiro_envio.get("paquetes") if retiro_envio else None,
            "message_reference": referencia_previa,
        })
    except Exception as e:
        print(f"[recolecciones] respuesta incierta de {courier} para reserva "
              f"{rec_id}: {e}")
        resultado = {"encontrado": False, "incierto": True,
                     "error": f"No pudimos confirmar la recolección con {courier}."}

    if not resultado.get("encontrado"):
        if resultado.get("incierto"):
            # El courier PUDO haberla tomado. Se conserva la fila y el índice
            # impide pedir otra visita hasta conciliar la Message-Reference.
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE recolecciones
                        SET estado='VERIFICAR_COURIER',
                            courier_message_reference=COALESCE(%s, courier_message_reference),
                            error_operativo=%s, updated_at=NOW()
                        WHERE id=%s AND estado='AGENDANDO'
                    """, (resultado.get("message_reference"),
                          str(resultado.get("error") or "")[:500], rec_id))
            return {"ok": False, "incierto": True,
                    "error": (resultado.get("error") or
                              "La respuesta del courier fue incierta.") +
                             " Tauro la va a verificar; no la pidas de nuevo."}

        # Rechazo definitivo: se borra la reserva para que pueda corregir y
        # reintentar. Un timeout nunca entra en esta rama.
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM recolecciones WHERE id = %s AND estado='AGENDANDO'",
                                (rec_id,))
        except Exception as e:
            print(f"[recolecciones] no pude liberar la reserva {rec_id}: {e}")
        return {"ok": False, "error": resultado.get("error") or "El courier no pudo agendarla."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE recolecciones
                SET estado='AGENDADA', confirmation_code = %s, ubicacion = %s,
                    courier_message_reference = COALESCE(%s, courier_message_reference),
                    error_operativo = NULL, updated_at=NOW()
                WHERE id = %s AND estado='AGENDANDO'
            """, (resultado.get("confirmation_code"), resultado.get("ubicacion"),
                  resultado.get("message_reference"), rec_id))

    print(f"[recolecciones] {cliente_id} agendó {courier} para {fecha} "
          f"({resultado.get('confirmation_code')})")
    return {"ok": True, "id": rec_id,
            "confirmation_code": resultado.get("confirmation_code")}


def listar(cliente_id: str, limite: int = 50) -> list[dict]:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM recolecciones WHERE cliente_id = %s
                ORDER BY fecha DESC, id DESC LIMIT %s
            """, ((cliente_id or "").strip().upper(), limite))
            return [dict(r) for r in cur.fetchall()]


def listar_admin(limite: int = 200) -> list[dict]:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, c.nombre AS cliente_nombre,
                       (r.estado='VERIFICAR_COURIER' OR
                        (r.estado IN ('AGENDANDO', 'CANCELANDO') AND
                         r.updated_at <= NOW() - INTERVAL '10 minutes')) AS puede_conciliar
                FROM recolecciones r
                LEFT JOIN clientes c ON c.cliente_id = r.cliente_id
                WHERE r.estado IN ('AGENDANDO', 'AGENDADA', 'CANCELANDO',
                                   'VERIFICAR_COURIER')
                  AND (r.fecha >= CURRENT_DATE - 1 OR
                       r.estado IN ('AGENDANDO', 'CANCELANDO', 'VERIFICAR_COURIER'))
                ORDER BY CASE WHEN r.estado IN ('AGENDANDO', 'CANCELANDO',
                                                 'VERIFICAR_COURIER') THEN 0 ELSE 1 END,
                         r.fecha, r.id LIMIT %s
            """, (limite,))
            return [dict(r) for r in cur.fetchall()]


def cancelar(rec_id: int, cliente_id: Optional[str] = None) -> dict:
    """
    Cancela en el courier y marca la fila. Con cliente_id se exige que sea
    del cliente (portal); sin él, es el admin.
    """
    _ensure_tabla()
    # Reclamo atómico: sólo un request puede pasar AGENDADA → CANCELANDO.
    # El segundo no llega al courier, aunque ambos clicks entren juntos.
    with get_conn() as conn:
        with conn.cursor() as cur:
            if cliente_id:
                cur.execute("""
                    UPDATE recolecciones
                    SET estado='CANCELANDO', error_operativo=NULL, updated_at=NOW()
                    WHERE id=%s AND cliente_id=%s AND estado='AGENDADA'
                    RETURNING *
                """, (rec_id, cliente_id.strip().upper()))
            else:
                cur.execute("""
                    UPDATE recolecciones
                    SET estado='CANCELANDO', error_operativo=NULL, updated_at=NOW()
                    WHERE id=%s AND estado='AGENDADA'
                    RETURNING *
                """, (rec_id,))
            rec = cur.fetchone()

    if not rec:
        return {"ok": False, "error":
                "Esa recolección no existe, no es de tu cuenta o ya se está cancelando."}

    cliente_api = _cliente_pickup(rec.get("courier"))
    if cliente_api is None:
        # No hubo llamada externa: volver a AGENDADA es seguro.
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE recolecciones SET estado='AGENDADA', updated_at=NOW()
                    WHERE id=%s AND estado='CANCELANDO'
                """, (rec_id,))
        return {"ok": False, "error": f"No sé cancelar recolecciones de "
                                      f"{rec.get('courier')}."}

    if not rec.get("confirmation_code"):
        r = {"ok": False, "error":
             "La recolección no tiene código de confirmación del courier."}
    else:
        try:
            r = cliente_api.cancel_pickup(
                rec["confirmation_code"], rec["fecha"].strftime("%Y-%m-%d"),
                rec.get("ubicacion") or "")
        except Exception as e:
            print(f"[recolecciones] cancelación incierta {rec_id}: {e}")
            r = {"ok": False, "error": "No recibimos confirmación del courier."}

    if not r.get("ok"):
        # Después de enviar DELETE/PUT no sabemos con certeza si la
        # cancelación llegó. No se habilita otro retiro: queda en conciliación.
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE recolecciones
                    SET estado='VERIFICAR_COURIER', error_operativo=%s, updated_at=NOW()
                    WHERE id=%s AND estado='CANCELANDO'
                """, (str(r.get("error") or "")[:500], rec_id))
        return {"ok": False, "incierto": True,
                "error": (r.get("error") or "El courier no confirmó la cancelación.") +
                         " Tauro la va a verificar; no programes otro retiro."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE recolecciones
                SET estado='CANCELADA', error_operativo=NULL, updated_at=NOW()
                WHERE id=%s AND estado='CANCELANDO'
            """, (rec_id,))
    print(f"[recolecciones] {rec_id} cancelada")
    return {"ok": True}


def resolver_verificacion(rec_id: int, resultado: str,
                          confirmation_code: str = "") -> dict:
    """Cierra manualmente una respuesta incierta después de revisar MyDHL.

    No llama al courier. El admin debe comprobar primero la Message-Reference
    en el portal del proveedor y recién entonces marcar el retiro como activo
    o como no creado/cancelado. La transición condicional evita resolver una
    fila que cambió mientras se la estaba mirando.
    """
    _ensure_tabla()
    destino = (resultado or "").strip().upper()
    if destino not in {"AGENDADA", "CANCELADA"}:
        return {"ok": False, "error": "Resultado de conciliación inválido."}

    codigo = (confirmation_code or "").strip()[:120]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT confirmation_code, estado
                FROM recolecciones
                WHERE id=%s
                  AND (estado='VERIFICAR_COURIER' OR
                       (estado IN ('AGENDANDO', 'CANCELANDO') AND
                        updated_at <= NOW() - INTERVAL '10 minutes'))
                FOR UPDATE
            """, (rec_id,))
            actual = cur.fetchone()
            if not actual:
                return {"ok": False, "error":
                        "La recolección no está pendiente de verificación o la operación "
                        "todavía puede estar en curso. Esperá diez minutos y actualizá."}
            if destino == "AGENDADA" and not (
                codigo or str(actual.get("confirmation_code") or "").strip()
            ):
                return {"ok": False, "error":
                        "Ingresá el código que encontraste en el courier para marcarla activa."}
            cur.execute("""
                UPDATE recolecciones
                SET estado=%s,
                    confirmation_code=COALESCE(NULLIF(%s, ''), confirmation_code),
                    error_operativo=NULL, updated_at=NOW()
                WHERE id=%s AND estado=%s
                RETURNING id
            """, (destino, codigo, rec_id, actual.get("estado")))
            if cur.fetchone() is None:
                return {"ok": False, "error":
                        "La recolección cambió de estado; actualizá la pantalla."}
    return {"ok": True, "estado": destino}
