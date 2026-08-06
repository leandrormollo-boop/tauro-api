# ============================================================
# Recolecciones (pickups) — que el chofer pase a buscar
# ============================================================
# Punto de la spec: "El cliente podrá crear recolecciones con todas estas
# empresas internacionales que lo permitan".
#
# Modelo: la recolección es del CLIENTE (su dirección de remitente), no de
# una guía puntual — el chofer pasa una vez y se lleva todo lo que haya.
# Por eso vive en su propia tabla y no colgada de solicitudes_guia.
#
# OJO: agendar NO es idempotente (dos llamadas = dos visitas del chofer),
# igual que emitir. Por eso hay reserva por (cliente, fecha): un cliente no
# puede agendar dos recolecciones el mismo día sin cancelar la anterior.
# ============================================================
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from core.database import get_conn

ESTADOS = ["AGENDADA", "CANCELADA", "COMPLETADA"]

_tabla_lista = False


def _ensure_tabla() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recolecciones (
                    id                SERIAL PRIMARY KEY,
                    cliente_id        TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
                    courier           TEXT NOT NULL DEFAULT 'FEDEX',
                    fecha             DATE NOT NULL,
                    ready_time        TEXT NOT NULL DEFAULT '09:00',
                    close_time        TEXT NOT NULL DEFAULT '17:00',
                    bultos            INTEGER NOT NULL DEFAULT 1,
                    peso_kg           REAL NOT NULL DEFAULT 1,
                    direccion         TEXT,
                    instrucciones     TEXT,
                    estado            TEXT NOT NULL DEFAULT 'AGENDADA',
                    confirmation_code TEXT,
                    ubicacion         TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS ix_recolecciones_cliente
                    ON recolecciones (cliente_id, fecha DESC);
                -- Una sola recolección activa por cliente y día: agendar dos
                -- veces manda al chofer dos veces (y se paga dos veces).
                CREATE UNIQUE INDEX IF NOT EXISTS uq_recoleccion_activa
                    ON recolecciones (cliente_id, fecha)
                    WHERE estado = 'AGENDADA';
            """)
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


def crear(cliente_id: str, fecha: str, ready_time: str, close_time: str,
          bultos: int, peso_kg: float, instrucciones: str = "",
          courier: str = "FEDEX") -> dict:
    """
    Agenda la recolección en el courier y la guarda. La dirección sale del
    remitente predeterminado del cliente: es donde el chofer tiene que ir.
    """
    from servicios.direcciones import obtener_remitente_para_envio

    _ensure_tabla()
    cliente_id = (cliente_id or "").strip().upper()
    courier = (courier or "FEDEX").strip().upper()

    cliente_api = _cliente_pickup(courier)
    if cliente_api is None:
        return {"ok": False, "error": f"Todavía no agendamos recolecciones de "
                                      f"{courier}. Elegí FedEx o DHL."}

    motivo = _dias_habiles_validos(fecha)
    if motivo:
        return {"ok": False, "error": motivo}

    rem = obtener_remitente_para_envio(cliente_id, None)
    if not rem or not (rem.get("direccion") or "").strip():
        return {"ok": False, "error": "Necesitás una dirección de retiro cargada en "
                                      "tu libreta para pedir una recolección."}

    # Reserva ANTES de llamar al courier: si dos pedidos entran juntos, el
    # índice único deja pasar uno solo. Agendar no se puede deshacer solo.
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO recolecciones
                        (cliente_id, fecha, ready_time, close_time, bultos, peso_kg,
                         direccion, instrucciones, estado, courier)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'AGENDADA', %s)
                    RETURNING id
                """, (cliente_id, fecha, ready_time, close_time,
                      max(int(bultos or 1), 1), float(peso_kg or 1),
                      f"{rem.get('direccion','')}, {rem.get('ciudad','')}".strip(", "),
                      (instrucciones or "")[:255], courier))
                rec_id = cur.fetchone()["id"]
            except Exception:
                return {"ok": False, "error": "Ya tenés una recolección agendada para "
                                              "ese día. Cancelala si querés cambiarla."}

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
    })

    if not resultado.get("encontrado"):
        # El courier no la tomó: se borra la reserva para que pueda reintentar.
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM recolecciones WHERE id = %s", (rec_id,))
        except Exception as e:
            print(f"[recolecciones] no pude liberar la reserva {rec_id}: {e}")
        return {"ok": False, "error": resultado.get("error") or "El courier no pudo agendarla."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE recolecciones SET confirmation_code = %s, ubicacion = %s
                WHERE id = %s
            """, (resultado.get("confirmation_code"), resultado.get("ubicacion"), rec_id))

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
                SELECT r.*, c.nombre AS cliente_nombre
                FROM recolecciones r
                LEFT JOIN clientes c ON c.cliente_id = r.cliente_id
                WHERE r.estado = 'AGENDADA' AND r.fecha >= CURRENT_DATE - 1
                ORDER BY r.fecha, r.id LIMIT %s
            """, (limite,))
            return [dict(r) for r in cur.fetchall()]


def cancelar(rec_id: int, cliente_id: Optional[str] = None) -> dict:
    """
    Cancela en el courier y marca la fila. Con cliente_id se exige que sea
    del cliente (portal); sin él, es el admin.
    """
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if cliente_id:
                cur.execute("""
                    SELECT * FROM recolecciones WHERE id = %s AND cliente_id = %s
                """, (rec_id, cliente_id.strip().upper()))
            else:
                cur.execute("SELECT * FROM recolecciones WHERE id = %s", (rec_id,))
            rec = cur.fetchone()

    if not rec:
        return {"ok": False, "error": "Esa recolección no existe."}
    if rec["estado"] != "AGENDADA":
        return {"ok": False, "error": "Esa recolección ya no está agendada."}

    if rec.get("confirmation_code"):
        cliente_api = _cliente_pickup(rec.get("courier"))
        if cliente_api is None:
            return {"ok": False, "error": f"No sé cancelar recolecciones de "
                                          f"{rec.get('courier')}."}
        r = cliente_api.cancel_pickup(
            rec["confirmation_code"], rec["fecha"].strftime("%Y-%m-%d"),
            rec.get("ubicacion") or "")
        if not r.get("ok"):
            # Si el courier no la canceló, NO se marca cancelada acá: el
            # chofer va a ir igual y el cliente tiene que saberlo.
            return {"ok": False, "error": (r.get("error") or "") +
                    " La recolección sigue activa en el courier."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE recolecciones SET estado = 'CANCELADA' WHERE id = %s",
                        (rec_id,))
    print(f"[recolecciones] {rec_id} cancelada")
    return {"ok": True}
