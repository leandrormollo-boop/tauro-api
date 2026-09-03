# ============================================================
# Servicio de cotización — PostgreSQL
# ============================================================

import math
import os
import uuid
from datetime import datetime, timedelta, timezone

from core.database import get_conn
from core.fedex_client import FedExClient
from modelos.cotizacion import (
    CotizacionInput, CotizacionOutput, calcular_peso_volumetrico,
)
from servicios.pricing import (
    PricingNoConfigurado, aplicar_pricing, parse_monto_ars,
)
from servicios.rutas import get_ruta, pais_a_iso2, ciudad_a_state
from servicios.numeros_humanos import parse_entero_formulario, parse_float_formulario


COTIZACION_VALIDA_HORAS = 24
# Rango sano del dólar ARS. Fuera de él el valor guardado es basura (0, mal
# tipeado, con separadores rotos) y ningún precio debe calcularse con él.
DOLAR_ARS_MIN = 100.0
DOLAR_ARS_MAX = 100_000.0


class DolarNoConfigurado(RuntimeError):
    """No hay un tipo de cambio válido en ``config``; no se cotiza.

    Antes se caía a un 1450 fijo del entorno y se seguía vendiendo con un
    dólar viejo o inventado, subvaluando cada envío en silencio. Ahora el
    cotizador falla cerrado y avisa; el dólar se corrige en /admin/config o
    lo repone el job de dólar oficial.
    """


MENSAJE_SIN_DOLAR = (
    "No hay un tipo de cambio válido configurado. TAURO debe cargarlo antes "
    "de cotizar."
)


def _get_dolar_ars() -> float:
    """
    Lee el tipo de cambio de la tabla ``config`` tolerando formato argentino
    (1.450 -> 1450). Sin fila, sin valor o fuera de rango levanta
    ``DolarNoConfigurado``: no existe fallback numérico.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT valor FROM config WHERE parametro = 'COTIZACION_DOLAR_ARS'",
                )
                row = cur.fetchone()
    except Exception as e:
        print(f"[cotizador] ALERTA: no se pudo leer COTIZACION_DOLAR_ARS: {e}")
        raise DolarNoConfigurado(MENSAJE_SIN_DOLAR) from e

    crudo = row["valor"] if row else None
    try:
        valor = parse_monto_ars(crudo) if crudo is not None else None
    except ValueError:
        valor = None
    if valor is None or not (DOLAR_ARS_MIN <= valor <= DOLAR_ARS_MAX):
        print(
            f"[cotizador] ALERTA: COTIZACION_DOLAR_ARS ausente o fuera de rango "
            f"({crudo!r} -> {valor}); cotización bloqueada hasta corregirlo"
        )
        raise DolarNoConfigurado(MENSAJE_SIN_DOLAR)
    return valor


def dolar_ars() -> float:
    """
    Tipo de cambio vigente — ÚNICA fuente de verdad para todo el sistema.

    Existe porque el checkout, la cache de tarifas y el cotizador público leían
    el dólar directo de `os.getenv`, mientras el portal lo leía de la tabla
    `config`. Resultado: Leandro actualizaba la cotización desde el admin y los
    precios del checkout seguían con el valor viejo de Railway, sin ningún aviso.
    """
    return _get_dolar_ars()


def _pricing_courier_cliente(cliente: str, courier: str) -> dict:
    """Regla efectiva y permiso antes de tocar un courier legacy.

    Sin regla propia ni general se falla cerrado: no hay 25 % implícito.
    """
    from servicios.configuracion_couriers_cliente import configuracion_cotizacion

    acceso = configuracion_cotizacion(cliente)
    courier = (courier or "").strip().lower()
    if courier not in acceso["couriers_habilitados"]:
        raise ValueError(
            f"{courier.upper()} no está habilitado para cotizar en esta cuenta."
        )
    pricing = (
        acceso["pricing_por_courier"].get(courier)
        or acceso["pricing_general"]
    )
    if not pricing:
        raise PricingNoConfigurado(
            f"{courier.upper()} no tiene una regla de precio configurada en "
            "esta cuenta."
        )
    return pricing


def cotizar_opciones(
    cliente: str,
    markup_pct: float,
    input_data: CotizacionInput,
) -> list:
    """
    Cotiza TODOS los servicios FedEx disponibles para la ruta en una sola
    llamada (Priority, Economy, etc.) y aplica el pricing del cliente a cada
    uno. Devuelve una lista de dicts ordenada por precio (más barato primero):
      {servicio, servicio_nombre, precio_final_ars, precio_final_usd,
       tarifa_lista_ars, dias_estimados, peso_usado_kg, coti_id, valida_hasta}
    Lanza ValueError si la ruta no existe o FedEx no devuelve tarifas.
    ``markup_pct`` se conserva por compatibilidad de firma; la regla efectiva
    sale siempre de la configuración del cliente.
    """
    pricing = _pricing_courier_cliente(cliente, "fedex")
    ruta = get_ruta(input_data.ruta_id)
    if not ruta:
        raise ValueError(f"Ruta '{input_data.ruta_id}' no existe o está inactiva")

    peso_volumetrico = calcular_peso_volumetrico(
        input_data.largo_cm, input_data.ancho_cm, input_data.alto_cm
    )
    peso_usado = max(input_data.peso_kg, peso_volumetrico)

    fedex = FedExClient()
    rate_resp = fedex.get_rates(
        origen={
            "city": ruta.origen_ciudad,
            "state": ciudad_a_state(ruta.origen_ciudad),
            "postal_code": ruta.origen_zip,
            "country": pais_a_iso2(ruta.origen_pais),
        },
        destino={
            "city": ruta.destino_ciudad,
            "state": ciudad_a_state(ruta.destino_ciudad),
            "postal_code": ruta.destino_zip,
            "country": pais_a_iso2(ruta.destino_pais),
        },
        paquete={
            "peso_kg": peso_usado,
            "largo": input_data.largo_cm,
            "ancho": input_data.ancho_cm,
            "alto": input_data.alto_cm,
            "valor_declarado_usd": input_data.valor_declarado_usd,
            "hs_code": input_data.hs_code or "",
            "descripcion_en": input_data.descripcion_en or "Merchandise",
            "unidades": input_data.unidades or 1,
        },
        todos_los_servicios=True,
    )
    if not rate_resp.get("encontrado"):
        raise ValueError(
            f"FedEx no devolvió tarifas: {rate_resp.get('error', 'sin detalles')}"
        )

    dolar = _get_dolar_ars()
    valida_hasta = (
        datetime.now(tz=timezone.utc) + timedelta(hours=COTIZACION_VALIDA_HORAS)
    ).isoformat(timespec="seconds")

    opciones = []
    for op in rate_resp.get("opciones") or []:
        costo = float(op["costo"])
        moneda = str(op.get("moneda", "USD")).upper()
        if moneda == "USD":
            costo_usd = round(costo, 2)
            costo_ars = round(costo * dolar, 2)
        else:
            costo_ars = costo
            costo_usd = round(costo_ars / dolar, 2) if dolar else 0.0

        precio = aplicar_pricing(
            costo_usd=costo_usd, costo_ars=costo_ars, dolar=dolar, pricing=pricing,
        )

        lista_ars = None
        if op.get("costo_lista"):
            lista = float(op["costo_lista"])
            lista_ars = round(lista * dolar, 2) if moneda == "USD" else round(lista, 2)
            if lista_ars <= precio["precio_final_ars"]:
                lista_ars = None

        coti_id = uuid.uuid4().hex[:16]
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO cotizaciones
                            (coti_id, cliente_id, ruta_id, peso_kg, dimensiones, peso_usado_kg,
                             costo_fedex_usd, markup_pct, markup_tipo, markup_valor,
                             precio_final_usd, precio_final_ars, dias_estimados, valida_hasta)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            coti_id, cliente, ruta.ruta_id, input_data.peso_kg,
                            f"{input_data.largo_cm}x{input_data.ancho_cm}x{input_data.alto_cm}",
                            peso_usado, costo_usd, precio["markup_pct_equivalente"],
                            precio["markup_tipo"], precio["markup_valor"],
                            precio["precio_final_usd"], precio["precio_final_ars"],
                            ruta.dias_estimados, valida_hasta,
                        ),
                    )
        except Exception as e:
            print(f"[cotizador] No se pudo loguear cotización de opción: {e}")

        opciones.append({
            "servicio": op["servicio"],
            "servicio_nombre": op["servicio_nombre"],
            "precio_final_ars": precio["precio_final_ars"],
            "precio_final_usd": precio["precio_final_usd"],
            "tarifa_lista_ars": lista_ars,
            "dias_estimados": op.get("dias_estimados") or ruta.dias_estimados,
            "peso_usado_kg": peso_usado,
            "peso_real_kg": input_data.peso_kg,
            "peso_volumetrico_kg": peso_volumetrico,
            "ruta": ruta.ruta_id,
            "coti_id": coti_id,
            "valida_hasta": valida_hasta,
        })

    opciones.sort(key=lambda o: o["precio_final_ars"])
    return opciones


def cotizar_referencia_couriers(
    cliente: str,
    origen_pais: str,
    destino_pais: str,
    peso_kg: float,
    largo_cm: float,
    ancho_cm: float,
    alto_cm: float,
    valor_declarado_usd: float,
    paquetes: list[dict] | None = None,
) -> dict:
    """Compara una opción principal por courier para el cotizador rápido.

    Esta pantalla ya exige peso, medidas y valor declarado, pero todavía no
    tiene la dirección completa ni la descripción aduanera. Por eso usa la
    ciudad/CP de referencia de cada país y devuelve una *estimación*; el wizard
    vuelve a cotizar con todos los datos reales antes de crear la solicitud.

    Se apoya en la misma capa multi-courier que usa Nuevo envío, pero adapta
    su respuesta al contrato pequeño de la pantalla. No persiste en
    ``cotizaciones``: esa tabla histórica tiene columnas FedEx-específicas y
    guardar ahí un costo DHL falsearía la auditoría.
    """
    from servicios.carriers import cotizar_carriers_cliente
    from servicios.configuracion_couriers_cliente import configuracion_cotizacion
    from servicios.paises import normalizar_iso2, referencia

    origen_iso = normalizar_iso2(origen_pais)
    destino_iso = normalizar_iso2(destino_pais)
    if not origen_iso or not destino_iso:
        raise ValueError("Elegí países válidos para origen y destino.")
    if origen_iso == "AR" and destino_iso == "AR":
        raise ValueError(
            "Los envíos nacionales se habilitarán cuando conectemos "
            "Andreani y OCA directamente. Todavía no se puede cotizar ni emitir."
        )

    try:
        valor_declarado = float(valor_declarado_usd)
    except (TypeError, ValueError):
        raise ValueError("El valor declarado debe ser válido.") from None
    if not math.isfinite(valor_declarado) or valor_declarado <= 0:
        raise ValueError("El valor declarado debe ser mayor a cero.")

    filas = paquetes or [{
        "cantidad": 1,
        "peso_kg": peso_kg,
        "largo_cm": largo_cm,
        "ancho_cm": ancho_cm,
        "alto_cm": alto_cm,
    }]
    piezas: list[dict] = []
    for indice, fila in enumerate(filas, start=1):
        if not isinstance(fila, dict):
            raise ValueError(f"Caja {indice}: el formato no es válido.")
        try:
            cantidad = parse_entero_formulario(
                fila.get("cantidad", 1),
                f"Caja {indice}: cantidad",
                minimo=1,
                maximo=20,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        try:
            medidas = {
                "peso_kg": float(fila.get("peso_kg")),
                "largo": float(fila.get("largo_cm", fila.get("largo"))),
                "ancho": float(fila.get("ancho_cm", fila.get("ancho"))),
                "alto": float(fila.get("alto_cm", fila.get("alto"))),
            }
        except (TypeError, ValueError):
            raise ValueError(
                f"Caja {indice}: el peso, las medidas y la cantidad deben ser válidos."
            ) from None
        if any(not math.isfinite(valor) for valor in medidas.values()):
            raise ValueError(f"Caja {indice}: el peso y las medidas deben ser finitos.")
        if any(valor <= 0 for valor in medidas.values()):
            raise ValueError(
                f"Caja {indice}: el peso y las tres medidas deben ser mayores a cero."
            )
        if medidas["peso_kg"] > 70:
            raise ValueError(f"Caja {indice}: el peso máximo por caja es 70 kg.")
        if medidas["largo"] + medidas["ancho"] + medidas["alto"] > 330:
            raise ValueError(
                f"Caja {indice}: la suma de las tres medidas no puede superar 330 cm."
            )
        for _ in range(cantidad):
            piezas.append({**medidas, "unidades": 1})
        if len(piezas) > 20:
            raise ValueError("DHL admite como máximo 20 cajas por envío.")

    if not piezas:
        raise ValueError("Agregá al menos una caja para cotizar.")

    valor_por_pieza = round(valor_declarado / len(piezas), 2)
    for pieza in piezas:
        pieza.update({
            "valor_declarado_usd": valor_por_pieza,
            "valor_unitario_usd": valor_por_pieza,
            "descripcion_en": "Merchandise",
        })

    peso_real = round(sum(p["peso_kg"] for p in piezas), 3)
    pesos_volumetricos = [
        calcular_peso_volumetrico(p["largo"], p["ancho"], p["alto"])
        for p in piezas
    ]
    peso_volumetrico = round(sum(pesos_volumetricos), 3)
    peso_usado = round(sum(
        max(p["peso_kg"], peso_vol)
        for p, peso_vol in zip(piezas, pesos_volumetricos)
    ), 3)

    origen = referencia(origen_iso)
    destino = referencia(destino_iso)
    origen["state"] = ciudad_a_state(origen.get("city", ""))
    destino["state"] = ciudad_a_state(destino.get("city", ""))

    acceso_couriers = configuracion_cotizacion(cliente)
    tarjetas = cotizar_carriers_cliente(
        origen=origen,
        destino=destino,
        paquete=piezas[0],
        paquetes=piezas if len(piezas) > 1 else None,
        dolar=_get_dolar_ars(),
        pricing_cliente=acceso_couriers["pricing_general"],
        pricing_por_courier=acceso_couriers["pricing_por_courier"],
        couriers_habilitados=acceso_couriers["couriers_habilitados"],
    )

    opciones = []
    no_disponibles = []
    for tarjeta in tarjetas:
        if tarjeta.get("estado") != "cotizado":
            # No exponer el error crudo: puede contener nombres de cuentas o
            # variables internas. Sí distinguir un problema de autenticación
            # productiva para que el cliente sepa que TAURO debe resolverlo.
            error_interno = str(tarjeta.get("error") or "").lower()
            if "http 401" in error_interno or "credenciales productivas" in error_interno:
                motivo = "La conexión productiva necesita revisión de TAURO."
            elif tarjeta.get("estado") == "no_habilitado":
                motivo = "No está habilitado para tu cuenta."
            elif tarjeta.get("estado") == "sin_pricing":
                motivo = "TAURO debe configurar el precio de tu cuenta para este operador."
            elif tarjeta.get("estado") == "proximamente":
                motivo = "La integración todavía no está disponible."
            elif tarjeta.get("estado") == "sin_multibulto":
                motivo = "No cotiza esta cantidad de bultos."
            else:
                motivo = "No devolvió tarifa para esta referencia."
            no_disponibles.append({
                "id": tarjeta["id"],
                "nombre": tarjeta["nombre"],
                "estado": tarjeta.get("estado") or "sin_tarifa",
                "motivo": motivo,
            })
            continue

        servicio = tarjeta.get("servicio") or "Servicio internacional"
        opciones.append({
            "carrier_id": tarjeta["id"],
            "carrier_nombre": tarjeta["nombre"],
            "carrier_logo": tarjeta.get("logo"),
            "servicio": servicio,
            "servicio_nombre": f"{tarjeta['nombre']} · {servicio}",
            "precio_final_ars": tarjeta["precio_ars"],
            "precio_final_usd": tarjeta["precio_usd"],
            "dias_estimados": tarjeta.get("dias_estimados") or "A confirmar",
            "tarifa_lista_ars": None,
            "peso_usado_kg": peso_usado,
            "peso_real_kg": peso_real,
            "peso_volumetrico_kg": peso_volumetrico,
            "ruta": f"{origen_iso} → {destino_iso}",
        })

    opciones.sort(key=lambda opcion: opcion["precio_final_ars"])
    return {
        "encontrado": bool(opciones),
        "opciones": opciones,
        "no_disponibles": no_disponibles,
        "resumen": {
            "ruta": f"{origen_iso} → {destino_iso}",
            "peso_usado_kg": peso_usado,
            "peso_real_kg": peso_real,
            "peso_volumetrico_kg": peso_volumetrico,
            "cobra_por_volumen": peso_usado > peso_real,
            "valor_declarado_usd": valor_declarado,
            "cantidad_bultos": len(piezas),
            "couriers_consultados": len(tarjetas),
        },
    }


def _destino_para_cotizar(ruta, destino_real: dict = None) -> dict:
    """
    A qué dirección se le cotiza.

    La ruta trae una ciudad y un CP de REFERENCIA (US → MIAMI 33101): sirve
    para una estimación sin destinatario, no para cobrar. Los couriers cobran
    recargos que dependen del CP exacto —zona remota, DHL USD 38 o 0,70/kg el
    que sea mayor; residencial USD 9,50— y esos recargos vienen ADENTRO del
    precio si les pasás la dirección real.

    Cotizando contra el CP de referencia: el courier contesta barato, le
    cobramos eso al cliente, después despachamos a la dirección REAL y el
    courier nos factura el recargo. La diferencia la come TAURO, y como nadie
    concilia, no aparece en ningún lado. La rama nacional ya lo hacía bien
    (portal_cliente.py:1051); la internacional tiraba la dirección.

    Si `destino_real` no trae CP, se usa la referencia: mejor una estimación
    que un error.
    """
    if destino_real and (destino_real.get("cp") or destino_real.get("postal_code")):
        return {
            "city": destino_real.get("ciudad") or destino_real.get("city") or ruta.destino_ciudad,
            "state": destino_real.get("estado") or destino_real.get("state") or "",
            "postal_code": destino_real.get("cp") or destino_real.get("postal_code"),
            "country": pais_a_iso2(ruta.destino_pais),
        }
    return {
        "city": ruta.destino_ciudad,
        "state": ciudad_a_state(ruta.destino_ciudad),
        "postal_code": ruta.destino_zip,
        "country": pais_a_iso2(ruta.destino_pais),
    }


def cotizar_bultos(
    cliente: str,
    markup_pct: float,
    ruta_id: str,
    bultos: list,
    destino_real: dict = None,
) -> dict:
    """
    Cotiza un envío MULTI-BULTO: N cajas (posiblemente de productos distintos)
    en una sola guía FedEx. Cada bulto:
      {peso_kg (por caja), largo_cm, ancho_cm, alto_cm,
       valor_unitario_usd, unidades (cajas idénticas), hs_code, descripcion_en}

    El peso facturable se calcula POR CAJA (máx entre real y volumétrico de
    cada una) y FedEx tarifa el conjunto. Devuelve un dict estilo
    CotizacionOutput + piezas_total/peso_total_kg. Lanza ValueError si la
    ruta no existe o FedEx no tarifa.
    """
    pricing = _pricing_courier_cliente(cliente, "fedex")
    ruta = get_ruta(ruta_id)
    if not ruta:
        raise ValueError(f"Ruta '{ruta_id}' no existe o está inactiva")

    if not isinstance(bultos, list) or not bultos:
        raise ValueError("Completá al menos un bulto antes de cotizar.")

    piezas_fedex = []
    peso_real_total = 0.0
    peso_facturable_total = 0.0
    piezas_total = 0
    for indice, b in enumerate(bultos, start=1):
        if not isinstance(b, dict):
            raise ValueError(f"Bulto {indice}: los datos no tienen un formato válido.")
        unidades = parse_entero_formulario(
            b.get("unidades"), f"Bulto {indice}, cantidad", minimo=1, maximo=20
        )
        peso_caja = parse_float_formulario(
            b.get("peso_kg"), f"Bulto {indice}, peso", minimo=0.001, maximo=70
        )
        largo = parse_float_formulario(
            b.get("largo_cm"), f"Bulto {indice}, largo", minimo=0.001, maximo=330
        )
        ancho = parse_float_formulario(
            b.get("ancho_cm"), f"Bulto {indice}, ancho", minimo=0.001, maximo=330
        )
        alto = parse_float_formulario(
            b.get("alto_cm"), f"Bulto {indice}, alto", minimo=0.001, maximo=330
        )
        if largo + ancho + alto > 330:
            raise ValueError(f"Bulto {indice}: la suma de las medidas no puede superar 330 cm.")
        valor_unitario = parse_float_formulario(
            b.get("valor_unitario_usd"),
            f"Bulto {indice}, valor unitario",
            importe=True,
            minimo=0.001,
        )
        vol = calcular_peso_volumetrico(
            largo, ancho, alto
        )
        peso_usado_caja = max(peso_caja, vol)
        piezas_total += unidades
        peso_real_total += peso_caja * unidades
        peso_facturable_total += peso_usado_caja * unidades
        piezas_fedex.append({
            "peso_kg": peso_usado_caja,
            "largo": largo,
            "ancho": ancho,
            "alto": alto,
            "valor_unitario_usd": valor_unitario,
            "unidades": unidades,
            "hs_code": b.get("hs_code", ""),
            "descripcion_en": b.get("descripcion_en", "Merchandise"),
        })

    fedex = FedExClient()
    rate_resp = fedex.get_rates(
        origen={
            "city": ruta.origen_ciudad,
            "state": ciudad_a_state(ruta.origen_ciudad),
            "postal_code": ruta.origen_zip,
            "country": pais_a_iso2(ruta.origen_pais),
        },
        destino=_destino_para_cotizar(ruta, destino_real),
        paquetes=piezas_fedex,
    )
    if not rate_resp.get("encontrado"):
        raise ValueError(
            f"FedEx no devolvió tarifa: {rate_resp.get('error', 'sin detalles')}"
        )

    dolar = _get_dolar_ars()
    costo = float(rate_resp.get("costo", 0))
    moneda = str(rate_resp.get("moneda", "USD")).upper()
    if moneda == "USD":
        costo_fedex_usd = round(costo, 2)
        costo_ars = round(costo * dolar, 2)
    else:
        costo_ars = costo
        costo_fedex_usd = round(costo_ars / dolar, 2) if dolar else 0.0

    tarifa_lista_ars = None
    if rate_resp.get("costo_lista"):
        lista = float(rate_resp["costo_lista"])
        tarifa_lista_ars = round(lista * dolar, 2) if moneda == "USD" else round(lista, 2)

    precio = aplicar_pricing(
        costo_usd=costo_fedex_usd, costo_ars=costo_ars, dolar=dolar, pricing=pricing,
    )

    coti_id = uuid.uuid4().hex[:16]
    valida_hasta = (
        datetime.now(tz=timezone.utc) + timedelta(hours=COTIZACION_VALIDA_HORAS)
    ).isoformat(timespec="seconds")

    try:
        dimensiones = " + ".join(
            f"{b.get('unidades', 1)}x({b.get('largo_cm')}x{b.get('ancho_cm')}x{b.get('alto_cm')})"
            for b in bultos
        )
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cotizaciones
                        (coti_id, cliente_id, ruta_id, peso_kg, dimensiones, peso_usado_kg,
                         costo_fedex_usd, markup_pct, markup_tipo, markup_valor,
                         precio_final_usd, precio_final_ars, dias_estimados, valida_hasta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coti_id, cliente, ruta.ruta_id, round(peso_real_total, 2),
                        dimensiones[:200], round(peso_facturable_total, 2),
                        costo_fedex_usd, precio["markup_pct_equivalente"],
                        precio["markup_tipo"], precio["markup_valor"],
                        precio["precio_final_usd"], precio["precio_final_ars"],
                        ruta.dias_estimados, valida_hasta,
                    ),
                )
    except Exception as e:
        print(f"[cotizador] No se pudo loguear cotización multi-bulto: {e}")

    if tarifa_lista_ars and tarifa_lista_ars <= precio["precio_final_ars"]:
        tarifa_lista_ars = None

    return {
        "coti_id": coti_id,
        "ruta_id": ruta.ruta_id,
        "piezas_total": piezas_total,
        "peso_total_kg": round(peso_real_total, 2),
        "peso_facturable_kg": round(peso_facturable_total, 2),
        "costo_fedex_usd": costo_fedex_usd,
        "precio_final_usd": precio["precio_final_usd"],
        "precio_final_ars": precio["precio_final_ars"],
        "tarifa_lista_ars": tarifa_lista_ars,
        "markup_tipo": precio["markup_tipo"],
        "markup_valor": precio["markup_valor"],
        "markup_pct": precio["markup_pct_equivalente"],
        "dias_estimados": ruta.dias_estimados,
        "valida_hasta": valida_hasta,
    }


def cotizar(
    cliente: str,
    markup_pct: float,
    input_data: CotizacionInput,
) -> CotizacionOutput:
    """Cotiza un envío. Lanza ValueError si la ruta no existe."""

    # 0. Permiso y regla efectiva ANTES de llamar a FedEx.
    pricing = _pricing_courier_cliente(cliente, "fedex")

    # 1. Resolver ruta
    ruta = get_ruta(input_data.ruta_id)
    if not ruta:
        raise ValueError(f"Ruta '{input_data.ruta_id}' no existe o está inactiva")

    # 2. Pesos
    peso_volumetrico = calcular_peso_volumetrico(
        input_data.largo_cm, input_data.ancho_cm, input_data.alto_cm
    )
    peso_usado = max(input_data.peso_kg, peso_volumetrico)

    # 3. Llamar FedEx
    fedex = FedExClient()
    rate_resp = fedex.get_rates(
        origen={
            "city": ruta.origen_ciudad,
            "state": ciudad_a_state(ruta.origen_ciudad),
            "postal_code": ruta.origen_zip,
            "country": pais_a_iso2(ruta.origen_pais),
        },
        destino={
            "city": ruta.destino_ciudad,
            "state": ciudad_a_state(ruta.destino_ciudad),
            "postal_code": ruta.destino_zip,
            "country": pais_a_iso2(ruta.destino_pais),
        },
        paquete={
            "peso_kg": peso_usado,
            "largo": input_data.largo_cm,
            "ancho": input_data.ancho_cm,
            "alto": input_data.alto_cm,
            # Valuación aduanera: usa el valor real del producto si vino;
            # si no, cae al default histórico (evita subdeclarar envíos caros).
            "valor_declarado_usd": input_data.valor_declarado_usd,
            "hs_code": input_data.hs_code or "",
            "descripcion_en": input_data.descripcion_en or "Merchandise",
            "unidades": input_data.unidades or 1,
        },
    )

    if not rate_resp.get("encontrado"):
        raise ValueError(
            f"FedEx no devolvió tarifa: {rate_resp.get('error', 'sin detalles')}"
        )

    # 4. Convertir a USD/ARS. FedEx sandbox suele devolver USD; producción ARS.
    dolar = _get_dolar_ars()
    costo = float(rate_resp.get("costo", 0))
    moneda = str(rate_resp.get("moneda", "USD")).upper()
    if moneda == "USD":
        costo_fedex_usd = round(costo, 2)
        costo_ars = round(costo * dolar, 2)
    else:
        costo_ars = costo
        costo_fedex_usd = round(costo_ars / dolar, 2) if dolar else 0.0

    # 4b. Tarifa pública (LIST) de FedEx en ARS — para mostrar el ahorro real.
    # Solo se expone si supera el precio final (si no, no hay ahorro que mostrar).
    tarifa_lista_ars = None
    costo_lista = rate_resp.get("costo_lista")
    if costo_lista:
        lista = float(costo_lista)
        tarifa_lista_ars = round(lista * dolar, 2) if moneda == "USD" else round(lista, 2)

    # 5. Aplicar regla de pricing del cliente.
    precio = aplicar_pricing(
        costo_usd=costo_fedex_usd,
        costo_ars=costo_ars,
        dolar=dolar,
        pricing=pricing,
    )
    precio_final_usd = precio["precio_final_usd"]
    precio_final_ars = precio["precio_final_ars"]
    markup_pct_equivalente = precio["markup_pct_equivalente"]

    # 6. UUID + validez
    coti_id = uuid.uuid4().hex[:16]
    valida_hasta = (
        datetime.now(tz=timezone.utc) + timedelta(hours=COTIZACION_VALIDA_HORAS)
    ).isoformat(timespec="seconds")

    # 7. Loguear en cotizaciones
    try:
        dimensiones = f"{input_data.largo_cm}x{input_data.ancho_cm}x{input_data.alto_cm}"
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cotizaciones
                        (coti_id, cliente_id, ruta_id, peso_kg, dimensiones, peso_usado_kg,
                         costo_fedex_usd, markup_pct, markup_tipo, markup_valor, precio_final_usd, precio_final_ars,
                         dias_estimados, valida_hasta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coti_id, cliente, ruta.ruta_id, input_data.peso_kg, dimensiones,
                        peso_usado, costo_fedex_usd, markup_pct_equivalente,
                        precio["markup_tipo"], precio["markup_valor"],
                        precio_final_usd, precio_final_ars,
                        ruta.dias_estimados, valida_hasta,
                    ),
                )
    except Exception as e:
        print(f"[cotizador] No se pudo loguear cotización: {e}")

    return CotizacionOutput(
        coti_id=coti_id,
        ruta=ruta.ruta_id,
        peso_real_kg=input_data.peso_kg,
        peso_volumetrico_kg=peso_volumetrico,
        peso_usado_kg=peso_usado,
        costo_fedex_usd=costo_fedex_usd,
        markup_pct=markup_pct_equivalente,
        markup_tipo=precio["markup_tipo"],
        markup_valor=precio["markup_valor"],
        precio_final_usd=precio_final_usd,
        precio_final_ars=precio_final_ars,
        tarifa_lista_ars=(
            tarifa_lista_ars
            if tarifa_lista_ars and tarifa_lista_ars > precio_final_ars
            else None
        ),
        dias_estimados=ruta.dias_estimados,
        valida_hasta=valida_hasta,
    )
