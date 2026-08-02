"""
Registro multi-courier de TAURO.

Cada carrier declara sus REQUISITOS (las variables de entorno que necesita para
operar). Mientras falten, el cotizador lo muestra igual —con su logo— en estado
"próximamente". El día que se cargan las credenciales en Railway → Variables, el
carrier se enciende solo y empieza a cotizar en vivo. Cero cambios de código.

FedEx ya opera. UPS y DHL tienen su cliente escrito y listo (core/ups_client.py,
core/dhl_client.py); esperan credenciales.
"""
from __future__ import annotations

import os

from core.database import get_conn
from core.fedex_client import FedExClient
from core.ups_client import UPSClient
from core.dhl_client import DHLClient

# Orden = orden de aparición en la web.
CARRIERS = [
    {
        "id": "fedex",
        "nombre": "FedEx",
        "servicio": "International Priority",
        "logo": "/static/img/carriers/fedex.svg",
        "requisitos": ("FEDEX_API_KEY", "FEDEX_SECRET_KEY", "FEDEX_ACCOUNT_NUMBER"),
        "cliente": FedExClient,
    },
    {
        "id": "ups",
        "nombre": "UPS",
        "servicio": "Worldwide Express",
        "logo": "/static/img/carriers/ups.svg",
        "requisitos": ("UPS_CLIENT_ID", "UPS_CLIENT_SECRET", "UPS_ACCOUNT_NUMBER"),
        "cliente": UPSClient,
    },
    {
        "id": "dhl",
        "nombre": "DHL Express",
        "servicio": "Express Worldwide",
        "logo": "/static/img/carriers/dhl.svg",
        # La cuenta acepta dos nombres (ver dhl_client): alcanza con uno.
        "requisitos": (
            "DHL_API_KEY",
            "DHL_API_SECRET",
            ("DHL_ACCOUNT_NUMBER_EXPO", "DHL_ACCOUNT_NUMBER"),
        ),
        "cliente": DHLClient,
    },
]


def carrier_activo(carrier: dict) -> bool:
    """
    Un carrier está activo cuando TODAS sus variables de entorno están cargadas.

    Un requisito puede ser el nombre de una variable, o una tupla de nombres
    alternativos para el MISMO dato (por ejemplo la cuenta de DHL, que acepta
    DHL_ACCOUNT_NUMBER_EXPO o el nombre viejo DHL_ACCOUNT_NUMBER): en ese caso
    alcanza con que esté cargada una.
    """
    def presente(req) -> bool:
        nombres = (req,) if isinstance(req, str) else req
        return any(os.getenv(n) for n in nombres)

    return all(presente(r) for r in carrier["requisitos"])


def _pricing_configurado() -> dict:
    """
    Las perillas de precio cargadas en la tabla `config` — las que Leandro
    edita desde /admin/config, sin deploy ni tocar Railway: los márgenes por
    courier y el descuento de FedEx.

    Van todas juntas a propósito. Si el margen de FedEx viviera en el admin y
    su descuento en una variable de Railway, la pantalla mostraría una perilla
    que no hace nada — que es exactamente el problema que tenía WEB_MARKUP_PCT
    hasta hoy: estaba en la tabla y el código leía el entorno.

    Ante cualquier problema de base devuelve {} y todo cae a las variables de
    entorno: que no se pueda leer un margen no puede dejar la web sin cotizar.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT parametro, valor FROM config "
                    "WHERE parametro LIKE 'WEB_MARKUP_PCT%%' "
                    "   OR parametro = 'WEB_DESC_FEDEX_PCT'"
                )
                filas = cur.fetchall()
    except Exception as e:
        print(f"[carriers] no pude leer las perillas de precio de config: {e}")
        return {}

    valores = {}
    for f in filas:
        try:
            valores[f["parametro"]] = float(str(f["valor"]).strip())
        except (TypeError, ValueError):
            print(f"[carriers] {f['parametro']}={f['valor']!r} no es un número; se ignora")
    return valores


def _markup_de(carrier_id: str, default_pct: float, config: dict = None) -> float:
    """
    Margen de la web para UN carrier, en orden de prioridad:

      1. WEB_MARKUP_PCT_<CARRIER> en la tabla config  ← lo edita el admin
      2. WEB_MARKUP_PCT_<CARRIER> como variable de entorno
      3. WEB_MARKUP_PCT general de la tabla config
      4. El general que venga por parámetro

    Cada courier tiene su propio margen (decisión de Leandro, 01/08/2026): no
    es lo mismo vender un DHL que llega en 2 días que un FedEx que llega en 5,
    ni son iguales las tarifas que negociamos con cada uno.
    """
    config = config or {}
    clave = f"WEB_MARKUP_PCT_{carrier_id.upper()}"

    if clave in config:
        return config[clave]

    crudo = os.getenv(clave)
    if crudo is not None:
        try:
            return float(crudo)
        except ValueError:
            print(f"[carriers] {clave}={crudo!r} no es un número; sigo con el general")

    return config.get("WEB_MARKUP_PCT", default_pct)


def _desc_fedex(config: dict) -> float:
    """
    Descuento de FedEx: primero la tabla config (editable desde el admin),
    después la variable de entorno, después el 88 que definió Leandro.
    """
    if "WEB_DESC_FEDEX_PCT" in config:
        return config["WEB_DESC_FEDEX_PCT"]
    try:
        return float(os.getenv("WEB_DESC_FEDEX_PCT", "88"))
    except ValueError:
        print("[carriers] WEB_DESC_FEDEX_PCT no es un número; uso 88")
        return 88.0


def carriers_activos() -> list:
    """
    Los couriers que HOY pueden cotizar, para mostrarlos como partners en la
    web. Se calcula de las credenciales cargadas, no de una lista escrita a
    mano: el día que se encienda UPS aparece solo, y si a un courier se le
    caen las credenciales deja de figurar en vez de quedar prometido.
    """
    return [
        {"id": c["id"], "nombre": c["nombre"], "logo": c["logo"]}
        for c in CARRIERS if carrier_activo(c)
    ]


def _precios(resultado: dict, dolar: float, markup_pct: float,
             descuento_pct: float = 0.0) -> dict:
    """
    Convierte el costo crudo del carrier a precio final (ARS + USD).

    Sin descuento: precio = tarifa × (1 + markup web).
    Con descuento (pedido de Leandro para FedEx): el precio final es la tarifa
    del carrier CON el descuento aplicado (sin markup encima), y se devuelve
    también la tarifa de lista para mostrarla tachada en la web.
    """
    # `costo` es lo que el carrier nos cobra a NOSOTROS (tarifa ACCOUNT en
    # FedEx). `costo_lista` es el precio público (LIST) cuando el courier
    # lo informa — es el correcto para mostrar tachado.
    es_usd = resultado.get("moneda", "USD") == "USD"
    costo_real_ars = round(resultado["costo"] * dolar) if es_usd else round(resultado["costo"])

    base = resultado.get("costo_lista") or resultado["costo"]
    if es_usd:
        lista_usd = base
        lista_ars = round(lista_usd * dolar)
    else:
        lista_ars = round(base)
        lista_usd = round(lista_ars / dolar, 2)

    if descuento_pct > 0:
        precio_ars = round(lista_ars * (1 - descuento_pct / 100))

        # ── PISO DE SEGURIDAD ──────────────────────────────────────────
        # OJO con lo que representa `resultado["costo"]`: para FedEx es la
        # tarifa ACCOUNT, o sea LO QUE TAURO LE PAGA al courier, no el
        # precio público. Aplicarle un descuento grande vende POR DEBAJO
        # DEL COSTO — hoy no se nota porque la cuenta está en sandbox y
        # ACCOUNT ≈ LIST, pero el día que entre la tarifa negociada de
        # producción cada envío pasaría a perder plata en silencio.
        #
        # El piso corre SÓLO con cuenta de producción: en sandbox FedEx
        # devuelve tarifas ficticias infladas (USD 415 por 1,2 kg a Miami),
        # así que aplicarlo ahí rompería el precio de vidriera sin proteger
        # nada real. Se enciende solo el día que entre la cuenta productiva
        # — que es exactamente cuando el costo pasa a ser plata de verdad.
        en_produccion = os.getenv("FEDEX_ENVIRONMENT", "sandbox").lower() == "production"
        margen_min = float(os.getenv("WEB_MARGEN_MINIMO_PCT", "15"))
        if en_produccion and margen_min > 0:
            piso_ars = round(costo_real_ars * (1 + margen_min / 100))
            if precio_ars < piso_ars:
                print(f"[carriers] PISO DE SEGURIDAD: el descuento daba ARS {precio_ars} "
                      f"pero el costo es ARS {costo_real_ars} → se cobra ARS {piso_ars} "
                      f"(costo + {margen_min:.0f}%). Revisá WEB_DESC_FEDEX_PCT.")
                precio_ars = piso_ars

        precio_usd = round(precio_ars / dolar, 2)
        return {
            "precio_ars": precio_ars,
            "precio_usd": precio_usd,
            "precio_lista_ars": lista_ars,
            "precio_lista_usd": round(lista_usd, 2),
            "descuento_pct": round(descuento_pct),
        }

    precio_ars = round(lista_ars * (1 + markup_pct / 100))
    precio_usd = round(precio_ars / dolar, 2)
    return {"precio_ars": precio_ars, "precio_usd": precio_usd}


def cotizar_carriers(origen: dict, destino: dict, paquete: dict,
                     dolar: float, markup_pct: float) -> list[dict]:
    """
    Cotiza los 3 carriers y devuelve una tarjeta por cada uno.

    estado:
      - "cotizado"     → tarifa real (precio_ars/precio_usd/dias_estimados)
      - "proximamente" → carrier sin credenciales todavía (se muestra con logo)
      - "sin_tarifa"   → activo pero sin cobertura para esa ruta
    """
    salida: list[dict] = []

    # Una sola lectura de config por cotización, no una por carrier.
    pricing = _pricing_configurado()

    for c in CARRIERS:
        base = {
            "id": c["id"],
            "nombre": c["nombre"],
            "logo": c["logo"],
            "servicio": c["servicio"],
        }

        if not carrier_activo(c):
            salida.append({**base, "estado": "proximamente"})
            continue

        try:
            resultado = c["cliente"]().get_rates(origen, destino, paquete)
        except Exception as e:  # una caída de un carrier no tumba a los otros
            print(f"[carriers] {c['id']} get_rates excepción: {e}")
            resultado = {"encontrado": False}

        if not resultado.get("encontrado"):
            salida.append({**base, "estado": "sin_tarifa"})
            continue

        # "INTERNATIONAL_PRIORITY" → "International Priority" (prolijo para la web)
        servicio = (resultado.get("servicio") or c["servicio"]).replace("_", " ").title()

        # FedEx sale con descuento sobre su tarifa de lista (WEB_DESC_FEDEX_PCT,
        # tunable en Railway → Variables sin tocar código; 0 = sin descuento).
        #
        # POR QUÉ FEDEX VA CON DESCUENTO Y DHL CON MARGEN (Leandro, 01/08/2026):
        # no es una inconsistencia, es que están en momentos distintos.
        # Con FedEx TODAVÍA NO tenemos tarifa negociada, así que el descuento
        # SIMULA el precio al que queremos vender. Con DHL sí la tenemos, así
        # que su tarifa se toma como costo real y se le suma el 20% de ganancia
        # (WEB_MARKUP_PCT / WEB_MARKUP_PCT_DHL) como corresponde.
        # El día que entre la cuenta negociada de FedEx, este descuento tiene
        # que morir y FedEx pasa a margen igual que DHL.
        #
        # OJO CON LA CALIBRACIÓN (corregido 28/07): este comentario decía que el
        # 90% dejaba el paquete de 1,2 kg a US en ~USD 40. Está MAL: esos USD 40
        # eran del paquete de 5 kg (el default del widget de la web, no el de
        # referencia). Medido contra producción, con 90% el paquete real de
        # 1,2 kg sale USD 24-27 — entre 30% y 40% POR DEBAJO del objetivo que
        # definió Leandro. No recalibrar este número contra las tarifas de
        # sandbox, que son ficticias: hacerlo recién con la cuenta de producción.
        descuento = _desc_fedex(pricing) if c["id"] == "fedex" else 0.0

        # Markup POR CARRIER: cada courier tiene su propio margen. Se setea con
        # WEB_MARKUP_PCT_DHL / _FEDEX / _UPS en Railway y, si no está, cae al
        # WEB_MARKUP_PCT general. Antes el margen era uno solo para todos, así
        # que no se podía cobrar distinto un DHL que llega en 2 días que un
        # FedEx que llega en 5.
        markup_carrier = _markup_de(c["id"], markup_pct, pricing)

        salida.append({
            **base,
            "estado": "cotizado",
            "servicio": servicio,
            "dias_estimados": str(resultado.get("dias_estimados", "3-5")),
            **_precios(resultado, dolar, markup_carrier, descuento_pct=descuento),
        })

    return salida
