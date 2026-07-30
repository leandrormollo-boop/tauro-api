"""
Panel del cliente: los dos bloques que responden "¿qué falta?" y "¿qué pasó?"
en el escritorio del portal.

La idea la tomamos del portal de envia.com (auditado 29/07/2026): no es más
lindo que el nuestro, es más EXPLÍCITO. Dos cosas hacen la diferencia y son
las que replicamos acá:

  1. Un checklist de arranque con progreso real, para que el cliente nuevo
     nunca se quede pensando "¿y ahora qué hago?".
  2. Contadores del embudo que aclaran ENTRE PARÉNTESIS la condición que puso
     al envío ahí ("Listas para preparar (Sin etiqueta)"). Así el cliente
     deduce solo qué tiene que hacer para que avance.

Dónde le ganamos: nuestros contadores son CLICKEABLES (llevan a la lista ya
filtrada) y el progreso se calcula de datos reales, no es decorativo.

Nada de esto puede tumbar el escritorio: ante cualquier error se devuelven
estructuras vacías y la página sale igual, sin el bloque.
"""
from __future__ import annotations

from core.database import get_conn


# ── Embudo de envíos ─────────────────────────────────────────
# Cada paso: (clave, título, aclaración de POR QUÉ está ahí, a dónde lleva).
# El orden es el recorrido real de un envío, de izquierda a derecha.
PASOS_EMBUDO = [
    {
        "clave": "por_armar",
        "titulo": "Ventas por armar",
        "detalle": "Entraron de tu tienda",
        "url": "/portal/tienda",
        "accion_de": "cliente",
    },
    {
        "clave": "esperando_guia",
        "titulo": "Esperando guía",
        "detalle": "Ya las pediste, las emite Tauro",
        "url": "/portal/envios",
        "accion_de": "tauro",
    },
    {
        "clave": "guia_lista",
        "titulo": "Guías listas",
        "detalle": "Para descargar y despachar",
        "url": "/portal/envios",
        "accion_de": "cliente",
    },
    {
        "clave": "entregados",
        "titulo": "Entregados",
        "detalle": "Llegaron a destino",
        "url": "/portal/envios",
        "accion_de": None,
    },
]


def embudo_envios(cliente_id: str) -> list[dict]:
    """
    Cuenta en qué punto del recorrido está cada envío del cliente.

    Una sola consulta por tabla: el escritorio se abre en cada navegación y
    no puede pagar cuatro viajes a la base.
    """
    conteos = {p["clave"]: 0 for p in PASOS_EMBUDO}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT estado, COUNT(*) AS n
                    FROM solicitudes_guia
                    WHERE cliente_id = %s
                    GROUP BY estado
                    """,
                    (cliente_id,),
                )
                for fila in cur.fetchall():
                    estado = (fila["estado"] or "").upper()
                    n = int(fila["n"] or 0)
                    if estado in ("SOLICITADO", "EMITIENDO"):
                        conteos["esperando_guia"] += n
                    elif estado == "GUIA_LISTA":
                        conteos["guia_lista"] += n
                    elif estado == "ENTREGADO":
                        conteos["entregados"] += n
                    # CANCELADO no se muestra: no espera acción de nadie.

                # Ventas de tienda que todavía no se convirtieron en envío.
                # La tabla puede no existir si el cliente nunca conectó una
                # tienda — por eso va en su propio try.
                try:
                    cur.execute(
                        """
                        SELECT COUNT(*) AS n
                        FROM pedidos_tienda
                        WHERE cliente_id = %s AND estado = 'PENDIENTE'
                        """,
                        (cliente_id,),
                    )
                    fila = cur.fetchone()
                    conteos["por_armar"] = int(fila["n"] or 0) if fila else 0
                except Exception:
                    conteos["por_armar"] = 0
    except Exception as e:
        print(f"[panel] no pude armar el embudo de {cliente_id}: {e}")
        return []

    return [{**paso, "cantidad": conteos[paso["clave"]]} for paso in PASOS_EMBUDO]


# ── Checklist de arranque ────────────────────────────────────

def checklist_arranque(cliente_id: str) -> dict:
    """
    Los pasos para dejar la cuenta lista para operar, con el progreso real.

    Devuelve {"pasos": [...], "hechos": n, "total": n, "pct": n, "completo": bool}.
    Cuando está todo hecho el escritorio directamente no muestra el bloque:
    un checklist con todo tildado es ruido para el cliente que ya opera.
    """
    from servicios.catalogo import get_productos
    from servicios.direcciones import contar_direcciones
    from servicios.integraciones_tienda import listar_tiendas

    def _seguro(fn, default):
        try:
            return fn()
        except Exception as e:
            print(f"[panel] checklist de {cliente_id}: {e}")
            return default

    productos = _seguro(lambda: len(get_productos(cliente_id)), 0)
    dirs = _seguro(lambda: contar_direcciones(cliente_id), {})
    destinatarios = int(dirs.get("DESTINATARIO", 0) or 0)
    tiendas = _seguro(lambda: len(listar_tiendas(cliente_id)), 0)

    envios = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM solicitudes_guia WHERE cliente_id = %s",
                    (cliente_id,),
                )
                fila = cur.fetchone()
                envios = int(fila["n"] or 0) if fila else 0
    except Exception as e:
        print(f"[panel] no pude contar envíos de {cliente_id}: {e}")

    pasos = [
        {
            "clave": "producto",
            "titulo": "Cargá tu primer producto",
            "detalle": "Con su descripción y código de aduana, para no reescribirlo en cada envío",
            "url": "/portal/catalogo",
            "cta": "Ir al catálogo",
            "hecho": productos > 0,
            "opcional": False,
        },
        {
            "clave": "direccion",
            "titulo": "Guardá un destinatario",
            "detalle": "Los que más usás quedan en la libreta y se cargan con un click",
            "url": "/portal/direcciones",
            "cta": "Abrir libreta",
            "hecho": destinatarios > 0,
            "opcional": False,
        },
        {
            "clave": "envio",
            "titulo": "Creá tu primer envío",
            "detalle": "Compará FedEx, UPS y DHL y pedí la guía",
            "url": "/portal/envios/nuevo",
            "cta": "Crear envío",
            "hecho": envios > 0,
            "opcional": False,
        },
        {
            "clave": "tienda",
            "titulo": "Conectá tu tienda",
            "detalle": "Cada venta de Shopify entra sola, sin copiar datos a mano",
            "url": "/portal/tienda",
            "cta": "Conectar tienda",
            "hecho": tiendas > 0,
            "opcional": True,
        },
    ]

    # El progreso mide sólo lo obligatorio: los opcionales suman si están
    # hechos, pero no penalizan si el cliente decidió saltearlos.
    obligatorios = [p for p in pasos if not p["opcional"]]
    hechos_oblig = sum(1 for p in obligatorios if p["hecho"])
    hechos_total = sum(1 for p in pasos if p["hecho"])
    pct = round(hechos_oblig * 100 / len(obligatorios)) if obligatorios else 100

    return {
        "pasos": pasos,
        "hechos": hechos_total,
        "total": len(pasos),
        "pct": pct,
        "completo": hechos_oblig == len(obligatorios),
    }
