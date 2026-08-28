"""Permisos y pricing por cliente + courier.

La ausencia de una fila conserva el pricing general como referencia, pero no
habilita ninguna operación. Cotizar, emitir y pedir pickups exigen una fila
explícita. Hoy la única API habilitable es DHL; FedEx y UPS quedan visibles
como pendientes.
"""
from __future__ import annotations

import math
import os
from typing import Iterable

from core.database import get_conn
from servicios.carrier_contract import Ambito, Capacidad, carrier_spec, public_catalog
from servicios.numeros_humanos import parse_importe_humano, parse_numero_humano
from servicios.pricing import PRICING_MODES, normalizar_pricing


def _metadata_operable(courier_id: str) -> dict:
    """Proyecta el contrato central al formulario operativo actual."""
    spec = carrier_spec(courier_id)
    if spec is None:  # Error de programación: nunca habilitar a ciegas.
        raise RuntimeError(f"Falta CarrierSpec para {courier_id}.")
    return {
        "id": spec.id,
        "nombre": spec.nombre,
        "logo": spec.logo,
        "permite_recoleccion": Capacidad.RECOLECTAR in spec.capacidades,
        "integracion_implementada": not spec.pendiente,
    }


# La tabla productiva hoy admite estos tres IDs. El orden se conserva para no
# mover la matriz del admin; estado, nombre, logo y capacidades vienen de la
# única fuente de verdad compartida con portal y web.
COURIERS_CLIENTE = tuple(
    _metadata_operable(courier_id) for courier_id in ("fedex", "dhl", "ups")
)
COURIER_IDS = frozenset(c["id"] for c in COURIERS_CLIENTE)


def estado_integracion(courier: str) -> dict:
    """Estado operativo real del courier, sin tocar su API.

    La presencia del código no alcanza para habilitar operaciones cobrables.
    Para el portal de clientes, DHL sólo está operativo con credenciales,
    ambas cuentas por sentido y el entorno fijado explícitamente en producción.
    """
    courier = normalizar_courier(courier)
    metadata = next(
        (c for c in COURIERS_CLIENTE if c["id"] == courier), None
    )
    if not metadata or not metadata["integracion_implementada"]:
        return {
            "operativa": False,
            "estado": "API pendiente",
            "detalle": "La integración todavía no está habilitada en TAURO.",
        }

    if courier == "dhl":
        entorno = (os.getenv("DHL_ENVIRONMENT") or "").strip().lower()
        cuenta_expo = (
            os.getenv("DHL_ACCOUNT_NUMBER_EXPO")
            or os.getenv("DHL_ACCOUNT_NUMBER")
            or ""
        ).strip()
        cuenta_impo = (
            os.getenv("DHL_ACCOUNT_NUMBER_IMPO")
            or os.getenv("DHL_ACCOUNT_NUMBER_IMPORT")
            or ""
        ).strip()
        requisitos = {
            "DHL_API_KEY": bool((os.getenv("DHL_API_KEY") or "").strip()),
            "DHL_API_SECRET": bool((os.getenv("DHL_API_SECRET") or "").strip()),
            "cuenta de exportación válida": (
                len(cuenta_expo) == 9 and cuenta_expo.isdigit()
            ),
            "cuenta de importación válida": (
                len(cuenta_impo) == 9 and cuenta_impo.isdigit()
            ),
        }
        faltantes = [nombre for nombre, presente in requisitos.items() if not presente]
        if entorno != "production" or faltantes:
            motivos = []
            if entorno != "production":
                motivos.append("DHL_ENVIRONMENT no está en production")
            if faltantes:
                motivos.append("faltan " + ", ".join(faltantes))
            return {
                "operativa": False,
                "estado": "Falta configurar producción",
                "detalle": "; ".join(motivos) + ".",
            }
        return {
            "operativa": True,
            "estado": "Configuración presente",
            "detalle": (
                "Credenciales y cuentas DHL cargadas. MyDHL valida el acceso "
                "productivo en cada cotización."
            ),
        }

    return {
        "operativa": False,
        "estado": "API pendiente",
        "detalle": "La integración todavía no está habilitada en TAURO.",
    }


def normalizar_courier(valor: str) -> str:
    courier = (valor or "").strip().lower()
    return courier if courier in COURIER_IDS else ""


def _pricing_general(cliente: dict) -> dict:
    return normalizar_pricing(
        cliente.get("markup_tipo") or "PCT",
        cliente.get("markup_valor"),
        fallback_pct=float(cliente.get("markup_pct") or 25.0),
    )


def _tramos_pricing(fila: dict | None) -> dict | None:
    """Arma los overrides por costo USD sin alterar la regla base."""
    if not fila:
        return None
    bajo_hasta = fila.get("markup_low_max_usd")
    bajo_ars = fila.get("markup_low_ars")
    alto_desde = fila.get("markup_high_min_usd")
    alto_usd = fila.get("markup_high_usd")
    if all(valor is None for valor in (bajo_hasta, bajo_ars, alto_desde, alto_usd)):
        return None
    return {
        "bajo_hasta_usd": float(bajo_hasta) if bajo_hasta is not None else None,
        "bajo_markup_ars": float(bajo_ars) if bajo_ars is not None else None,
        "alto_desde_usd": float(alto_desde) if alto_desde is not None else None,
        "alto_markup_usd": float(alto_usd) if alto_usd is not None else None,
    }


def _armar_matriz(cliente: dict, filas: Iterable[dict]) -> dict:
    especificas = {
        normalizar_courier(f.get("courier")): dict(f)
        for f in filas
        if normalizar_courier(f.get("courier"))
    }
    general = _pricing_general(cliente)
    activa = bool(cliente.get("activo"))
    couriers = []

    for metadata in COURIERS_CLIENTE:
        courier_id = metadata["id"]
        fila = especificas.get(courier_id)
        configurado = fila is not None
        integracion = estado_integracion(courier_id)
        integracion_disponible = bool(integracion["operativa"])
        tipo_especifico = (fila.get("markup_tipo") or "").strip().upper() if fila else ""
        valor_especifico = fila.get("markup_valor") if fila else None
        pricing = (
            normalizar_pricing(tipo_especifico, valor_especifico)
            if tipo_especifico
            else general
        )
        tramos = _tramos_pricing(fila)
        if tramos:
            pricing = {**pricing, "tramos_usd": tramos}
        config_puede_cotizar = bool(fila["puede_cotizar"]) if fila else False
        config_puede_emitir = bool(fila["puede_emitir"]) if fila else False
        config_puede_recolectar = bool(fila["puede_recolectar"]) if fila else False
        couriers.append({
            **metadata,
            "integracion_disponible": integracion_disponible,
            "estado_integracion": integracion["estado"],
            "detalle_integracion": integracion["detalle"],
            "configurado": configurado,
            # Configuración persistida (lo que ve/edita el admin) y permiso
            # efectivo (lo que puede ejecutar el portal) son deliberadamente
            # distintos. Si faltan credenciales, no borramos la decisión del
            # admin: sólo la dejamos inactiva hasta que vuelva la integración.
            "config_puede_cotizar": config_puede_cotizar,
            "config_puede_emitir": config_puede_emitir,
            "config_puede_recolectar": config_puede_recolectar,
            # Valores configurados/heredados para el formulario admin.
            "puede_cotizar": integracion_disponible and config_puede_cotizar,
            "puede_emitir": integracion_disponible and config_puede_emitir,
            "puede_recolectar": (
                integracion_disponible and config_puede_recolectar
            ),
            "markup_tipo": tipo_especifico,
            "markup_valor": valor_especifico,
            "markup_low_max_usd": fila.get("markup_low_max_usd") if fila else None,
            "markup_low_ars": fila.get("markup_low_ars") if fila else None,
            "markup_high_min_usd": fila.get("markup_high_min_usd") if fila else None,
            "markup_high_usd": fila.get("markup_high_usd") if fila else None,
            "pricing": pricing,
            "hereda_pricing": not bool(tipo_especifico),
            "operativo": activa,
        })

    courier_default_configurado = normalizar_courier(
        cliente.get("courier_default") or ""
    )
    courier_default = courier_default_configurado
    if courier_default:
        default_config = next(c for c in couriers if c["id"] == courier_default)
        if not default_config["integracion_disponible"]:
            courier_default = ""

    return {
        "cliente_id": cliente["cliente_id"],
        "nombre": cliente.get("nombre") or cliente["cliente_id"],
        "activo": activa,
        "pricing_general": general,
        "tope_deuda_ars": cliente.get("tope_deuda_ars"),
        "courier_default": courier_default,
        "courier_default_configurado": courier_default_configurado,
        "couriers": couriers,
    }


def leer_matriz_con_cursor(cur, cliente_id: str) -> dict | None:
    cliente_id = (cliente_id or "").strip().upper()
    cur.execute(
        """
        SELECT cliente_id, nombre, activo, markup_pct, markup_tipo, markup_valor,
               puede_emitir, puede_recolectar, tope_deuda_ars, courier_default
        FROM clientes
        WHERE cliente_id = %s
        """,
        (cliente_id,),
    )
    cliente = cur.fetchone()
    if not cliente:
        return None
    cur.execute(
        """
        SELECT courier, puede_cotizar, puede_emitir, puede_recolectar,
               markup_tipo, markup_valor,
               markup_low_max_usd, markup_low_ars,
               markup_high_min_usd, markup_high_usd
        FROM cliente_courier_config
        WHERE cliente_id = %s
        """,
        (cliente_id,),
    )
    return _armar_matriz(dict(cliente), cur.fetchall())


def obtener_matriz(cliente_id: str) -> dict | None:
    """Lee toda la configuración en una sola conexión.

    No atrapa errores de base: los consumidores operativos deben fallar
    cerrados si la configuración sensible no puede verificarse.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            return leer_matriz_con_cursor(cur, cliente_id)


def catalogo_cliente(cliente_id: str, ambito: Ambito | str) -> tuple[dict, ...]:
    """Estado seguro y efectivo de cada operador para una cuenta.

    Sólo marca ``disponible_segun_cuenta`` cuando la integración global está
    configurada y el permiso explícito de cotización está activo. Nunca expone
    credenciales, pricing ni el motivo técnico interno.
    """
    catalogo = [dict(item) for item in public_catalog(ambito)]
    matriz = obtener_matriz(cliente_id)
    filas = {
        fila["id"]: fila for fila in (matriz or {}).get("couriers", ())
    }
    for item in catalogo:
        if item["estado"] == "integracion_pendiente":
            continue
        fila = filas.get(item["id"])
        if fila and fila.get("puede_cotizar"):
            item.update({
                "estado": "disponible_segun_cuenta",
                "estado_label": "Disponible para tu cuenta",
                "estado_corto": "Disponible",
            })
        elif fila and fila.get("integracion_disponible"):
            item.update({
                "estado": "requiere_habilitacion",
                "estado_label": "Requiere habilitación de TAURO",
                "estado_corto": "Sin habilitar",
            })
        else:
            item.update({
                "estado": "configuracion_pendiente",
                "estado_label": "Configuración productiva pendiente",
                "estado_corto": "Configuración pendiente",
            })
    return tuple(catalogo)


def parsear_fila(
    courier: str,
    *,
    puede_cotizar: bool,
    puede_emitir: bool,
    puede_recolectar: bool,
    markup_tipo: str,
    markup_valor: str,
    markup_low_max_usd: str = "",
    markup_low_ars: str = "",
    markup_high_min_usd: str = "",
    markup_high_usd: str = "",
) -> dict:
    """Valida una fila enviada por el admin, sin confiar en el navegador."""
    courier = normalizar_courier(courier)
    if not courier:
        raise ValueError("Courier inválido.")
    if puede_emitir and not puede_cotizar:
        raise ValueError(
            f"{courier.upper()}: para crear guías también debe poder cotizar."
    )
    metadata = next(c for c in COURIERS_CLIENTE if c["id"] == courier)
    if puede_recolectar and not metadata["permite_recoleccion"]:
        raise ValueError(f"{metadata['nombre']} todavía no admite recolecciones.")
    if not metadata["integracion_implementada"] and (
        puede_cotizar or puede_emitir or puede_recolectar
        or (markup_tipo or "").strip() or (markup_valor or "").strip()
        or any((valor or "").strip() for valor in (
            markup_low_max_usd, markup_low_ars,
            markup_high_min_usd, markup_high_usd,
        ))
    ):
        raise ValueError(
            f"{metadata['nombre']}: la integración todavía no está habilitada "
            "en TAURO. No puede activarse."
        )

    tipo = (markup_tipo or "").strip().upper()
    valor = None
    if tipo:
        if tipo not in PRICING_MODES:
            raise ValueError(f"{metadata['nombre']}: el tipo de ganancia no es válido.")
        raw_valor = (markup_valor or "").strip()
        if not raw_valor:
            raise ValueError(f"{metadata['nombre']}: ingresá el valor de la ganancia.")
        try:
            numero = (
                parse_importe_humano(raw_valor)
                if tipo == "FIJO_ARS"
                else parse_numero_humano(raw_valor)
            )
            numero_crudo = float(numero)
        except ValueError:
            raise ValueError(
                f"{metadata['nombre']}: la ganancia debe ser un número válido."
            ) from None
        if not math.isfinite(numero_crudo):
            raise ValueError(f"{metadata['nombre']}: la ganancia debe ser un número válido.")
        if tipo == "MULTIPLICADOR" and numero_crudo < 1:
            raise ValueError(f"{metadata['nombre']}: el multiplicador no puede ser menor a 1.")
        if tipo in {"PCT", "FIJO_ARS"} and numero_crudo < 0:
            raise ValueError(f"{metadata['nombre']}: la ganancia no puede ser negativa.")
        valor = numero_crudo
        if not math.isfinite(valor):
            raise ValueError(f"{metadata['nombre']}: la ganancia debe ser un número válido.")
    elif (markup_valor or "").strip():
        raise ValueError(
            f"{metadata['nombre']}: elegí un tipo de ganancia o dejá el valor vacío."
        )

    def _par_tramo(
        limite_raw: str,
        ganancia_raw: str,
        *,
        etiqueta: str,
        ganancia_ars: bool,
    ) -> tuple[float | None, float | None]:
        limite_txt = (limite_raw or "").strip()
        ganancia_txt = (ganancia_raw or "").strip()
        if not limite_txt and not ganancia_txt:
            return None, None
        if not limite_txt or not ganancia_txt:
            raise ValueError(
                f"{metadata['nombre']}: completá el límite y la ganancia del "
                f"tramo {etiqueta}."
            )
        try:
            limite = float(parse_numero_humano(limite_txt))
            ganancia = float(
                parse_importe_humano(ganancia_txt)
                if ganancia_ars else parse_numero_humano(ganancia_txt)
            )
        except (TypeError, ValueError):
            raise ValueError(
                f"{metadata['nombre']}: los valores del tramo {etiqueta} "
                "deben ser números válidos."
            ) from None
        if not math.isfinite(limite) or limite <= 0:
            raise ValueError(
                f"{metadata['nombre']}: el límite del tramo {etiqueta} "
                "debe ser mayor a cero."
            )
        if not math.isfinite(ganancia) or ganancia < 0:
            raise ValueError(
                f"{metadata['nombre']}: la ganancia del tramo {etiqueta} "
                "no puede ser negativa."
            )
        return limite, ganancia

    low_max, low_ars = _par_tramo(
        markup_low_max_usd,
        markup_low_ars,
        etiqueta="bajo",
        ganancia_ars=True,
    )
    high_min, high_usd = _par_tramo(
        markup_high_min_usd,
        markup_high_usd,
        etiqueta="alto",
        ganancia_ars=False,
    )
    if low_max is not None and high_min is not None and low_max >= high_min:
        raise ValueError(
            f"{metadata['nombre']}: el límite bajo debe ser menor al límite alto."
        )

    return {
        "courier": courier,
        "puede_cotizar": bool(puede_cotizar),
        "puede_emitir": bool(puede_emitir),
        "puede_recolectar": bool(puede_recolectar),
        "markup_tipo": tipo or None,
        "markup_valor": valor,
        "markup_low_max_usd": low_max,
        "markup_low_ars": low_ars,
        "markup_high_min_usd": high_min,
        "markup_high_usd": high_usd,
    }


def guardar_matriz_con_cursor(cur, cliente_id: str, configuraciones: list[dict]) -> None:
    cliente_id = (cliente_id or "").strip().upper()
    ids = [c["courier"] for c in configuraciones]
    if len(ids) != len(COURIER_IDS) or set(ids) != set(COURIER_IDS):
        raise ValueError("La configuración debe incluir FedEx, DHL y UPS una sola vez.")
    for config in configuraciones:
        cur.execute(
            """
            INSERT INTO cliente_courier_config
                (cliente_id, courier, puede_cotizar, puede_emitir,
                 puede_recolectar, markup_tipo, markup_valor,
                 markup_low_max_usd, markup_low_ars,
                 markup_high_min_usd, markup_high_usd, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (cliente_id, courier) DO UPDATE SET
                puede_cotizar = EXCLUDED.puede_cotizar,
                puede_emitir = EXCLUDED.puede_emitir,
                puede_recolectar = EXCLUDED.puede_recolectar,
                markup_tipo = EXCLUDED.markup_tipo,
                markup_valor = EXCLUDED.markup_valor,
                markup_low_max_usd = EXCLUDED.markup_low_max_usd,
                markup_low_ars = EXCLUDED.markup_low_ars,
                markup_high_min_usd = EXCLUDED.markup_high_min_usd,
                markup_high_usd = EXCLUDED.markup_high_usd,
                updated_at = NOW()
            """,
            (
                cliente_id,
                config["courier"],
                config["puede_cotizar"],
                config["puede_emitir"],
                config["puede_recolectar"],
                config["markup_tipo"],
                config["markup_valor"],
                config["markup_low_max_usd"],
                config["markup_low_ars"],
                config["markup_high_min_usd"],
                config["markup_high_usd"],
            ),
        )


def permiso_courier(cliente_id: str, courier: str, permiso: str) -> bool:
    """Chequeo fail-closed para acciones operativas del cliente."""
    campo = {
        "cotizar": "puede_cotizar",
        "emitir": "puede_emitir",
        "recolectar": "puede_recolectar",
    }.get(permiso)
    courier = normalizar_courier(courier)
    if not campo or not courier:
        return False
    try:
        matriz = obtener_matriz(cliente_id)
        if not matriz or not matriz["activo"]:
            return False
        fila = next(c for c in matriz["couriers"] if c["id"] == courier)
        return bool(fila[campo])
    except Exception as exc:
        print(
            f"[config-couriers] no pude validar {permiso} de "
            f"{cliente_id}/{courier}: {type(exc).__name__}"
        )
        return False


def mapa_permisos(cliente_id: str, permiso: str) -> dict[str, bool]:
    """Mapa en una lectura, útil para renderizar listas sin N+1 queries."""
    campo = {
        "cotizar": "puede_cotizar",
        "emitir": "puede_emitir",
        "recolectar": "puede_recolectar",
    }.get(permiso)
    if not campo:
        return {courier: False for courier in COURIER_IDS}
    try:
        matriz = obtener_matriz(cliente_id)
        if not matriz or not matriz["activo"]:
            return {courier: False for courier in COURIER_IDS}
        return {c["id"]: bool(c[campo]) for c in matriz["couriers"]}
    except Exception as exc:
        print(
            f"[config-couriers] no pude listar permisos {permiso} de "
            f"{cliente_id}: {type(exc).__name__}"
        )
        return {courier: False for courier in COURIER_IDS}


def configuracion_cotizacion(cliente_id: str) -> dict:
    """Pricing y allow-list para el comparador del portal/API."""
    matriz = obtener_matriz(cliente_id)
    if not matriz or not matriz["activo"]:
        return {
            "pricing_general": normalizar_pricing("PCT", 25),
            "pricing_por_courier": {},
            "couriers_habilitados": set(),
        }
    return {
        "pricing_general": matriz["pricing_general"],
        "pricing_por_courier": {
            c["id"]: c["pricing"] for c in matriz["couriers"]
        },
        "couriers_habilitados": {
            c["id"] for c in matriz["couriers"] if c["puede_cotizar"]
        },
    }


def resumen_auditoria(configuraciones: list[dict]) -> dict:
    """Snapshot mínimo: sólo permisos/precio, sin datos personales."""
    return {
        c["courier"]: {
            "cotizar": c["puede_cotizar"],
            "emitir": c["puede_emitir"],
            "pickup": c["puede_recolectar"],
            "markup_tipo": c["markup_tipo"] or "HEREDA",
            "markup_valor": c["markup_valor"],
            "tramo_bajo": (
                {
                    "hasta_usd": c["markup_low_max_usd"],
                    "ganancia_ars": c["markup_low_ars"],
                }
                if c["markup_low_max_usd"] is not None else None
            ),
            "tramo_alto": (
                {
                    "desde_usd": c["markup_high_min_usd"],
                    "ganancia_usd": c["markup_high_usd"],
                }
                if c["markup_high_min_usd"] is not None else None
            ),
        }
        for c in configuraciones
    }
