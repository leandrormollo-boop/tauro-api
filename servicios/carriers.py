"""
Motor internacional legado de cotización multi-courier.

La presencia de credenciales indica solamente que el cliente técnico puede
intentarse. No acredita certificación, contrato comercial, permisos por cliente
ni disponibilidad productiva. Esos controles son independientes y se resuelven
con la matriz administrativa y el contrato de ``carrier_contract``.

No se debe considerar operativo a FedEx, UPS o DHL por existir una clase Python
o variables de entorno. Cada integración necesita sandbox, UAT productivo y
habilitación explícita antes de mostrarse como disponible.
"""
from __future__ import annotations

import math
import os

from core.database import get_conn
from core.fedex_client import FedExClient
from core.ups_client import UPSClient
from core.dhl_client import DHLClient
from servicios.numeros_humanos import parse_configuracion_numerica

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
        # La presencia de una key de sandbox no puede encender el cotizador
        # público ni publicar precios ficticios como si fueran contractuales.
        # Los registros falsos de tests no traen esta clave y conservan el
        # contrato genérico de carrier_activo().
        "entorno_requerido": ("DHL_ENVIRONMENT", "production"),
        "cliente": DHLClient,
    },
]


def _numero_config(parametro: str, valor) -> float | None:
    """Lee DB/env con la misma semántica numérica ES/EN que usa el admin."""
    try:
        numero = parse_configuracion_numerica(parametro, valor)
        if numero is None:
            return None
        resultado = float(numero)
        return resultado if math.isfinite(resultado) else None
    except (TypeError, ValueError):
        return None


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

    if not all(presente(r) for r in carrier["requisitos"]):
        return False

    entorno_requerido = carrier.get("entorno_requerido")
    if entorno_requerido:
        variable, esperado = entorno_requerido
        if (os.getenv(variable) or "").strip().lower() != esperado:
            return False
    return True


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
                    "   OR parametro LIKE 'WEB_MARGEN_FIJO_%%' "
                    "   OR parametro LIKE 'WEB_ADICIONAL_%%' "
                    "   OR parametro = 'WEB_DESC_FEDEX_PCT'"
                )
                filas = cur.fetchall()
    except Exception as e:
        print(f"[carriers] no pude leer las perillas de precio de config: {e}")
        return {}

    valores = {}
    for f in filas:
        numero = _numero_config(f["parametro"], f["valor"])
        if numero is None:
            print(f"[carriers] {f['parametro']}={f['valor']!r} no es un número; se ignora")
        else:
            valores[f["parametro"]] = numero
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
        numero = _numero_config(clave, crudo)
        if numero is not None:
            return numero
        else:
            print(f"[carriers] {clave}={crudo!r} no es un número; sigo con el general")

    return config.get("WEB_MARKUP_PCT", default_pct)


def _margen_fijo_de(carrier_id: str, config: dict = None) -> float:
    """
    Ganancia FIJA en ARS para UN carrier de la web (mismo modelo que el monto
    fijo por cliente del portal). Cuando está seteada, el precio de ese courier
    es COSTO + este monto, ignorando el markup %: la ganancia por envío es
    exactamente esta plata, no un porcentaje.

    Decisión de Leandro (04/08): DHL va con ganancia fija de $135.000. Se edita
    en /admin/config como WEB_MARGEN_FIJO_DHL_ARS (o por env var), sin deploy.
    0 / ausente = ese carrier sigue con markup %.
    """
    config = config or {}
    clave = f"WEB_MARGEN_FIJO_{carrier_id.upper()}_ARS"
    if clave in config:
        return config[clave]
    crudo = os.getenv(clave)
    if crudo is not None:
        numero = _numero_config(clave, crudo)
        if numero is not None:
            return numero
        else:
            print(f"[carriers] {clave}={crudo!r} no es un número; se ignora")
    return 0.0


def _desc_fedex(config: dict) -> float:
    """
    Descuento de FedEx: primero la tabla config (editable desde el admin),
    después la variable de entorno, después el 90 que definió Leandro (06/08).
    """
    if "WEB_DESC_FEDEX_PCT" in config:
        return config["WEB_DESC_FEDEX_PCT"]
    numero = _numero_config(
        "WEB_DESC_FEDEX_PCT", os.getenv("WEB_DESC_FEDEX_PCT", "90")
    )
    if numero is None:
        print("[carriers] WEB_DESC_FEDEX_PCT no es un número; uso 90")
        return 90.0
    return numero


def _adicional_de(carrier_id: str, config: dict = None) -> float:
    """
    Monto FIJO en ARS que se suma DESPUÉS del descuento, sólo en la web.

    Regla de Leandro (06/08/2026) para FedEx: "aplicarle un 90% de descuento y
    sumarle un markup de $10.000". O sea el precio de vidriera es
    `lista − 90% + $10.000`, no `lista − 90%` a secas.

    Es distinto de WEB_MARGEN_FIJO_<CARRIER>_ARS: aquél REEMPLAZA el cálculo
    (precio = costo + monto, caso DHL) y éste se APILA sobre el precio ya
    descontado. Los dos no se pisan porque la ganancia fija corta antes.

    Se edita en /admin/config o por env var como WEB_ADICIONAL_FEDEX_ARS.
    0 / ausente = no se suma nada.
    """
    config = config or {}
    clave = f"WEB_ADICIONAL_{carrier_id.upper()}_ARS"
    if clave in config:
        return config[clave]
    crudo = os.getenv(clave)
    if crudo is not None:
        numero = _numero_config(clave, crudo)
        if numero is not None:
            return numero
        else:
            print(f"[carriers] {clave}={crudo!r} no es un número; se ignora")
    # Default sólo para FedEx: es el único que va con el modelo de descuento.
    return 10000.0 if carrier_id.lower() == "fedex" else 0.0


def courier_default_cliente(cliente_id: str) -> str:
    """
    El courier que el cliente dejó configurado ("mis envíos van por DHL").
    Devuelve 'fedex' | 'dhl' | 'ups' o '' si elige en cada envío. Ante
    cualquier problema, '': que no se pueda leer la preferencia no puede
    frenar el wizard.
    """
    cliente_id = (cliente_id or "").strip().upper()
    if not cliente_id:
        return ""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT courier_default FROM clientes WHERE cliente_id = %s",
                            (cliente_id,))
                fila = cur.fetchone()
        valor = (fila["courier_default"] if fila else "").strip().lower()
        if valor not in {c["id"] for c in CARRIERS}:
            return ""
        # Una preferencia vieja de FedEx/UPS no puede preseleccionar una API
        # que hoy TAURO todavía no declaró operativa.
        from servicios.configuracion_couriers_cliente import permiso_courier
        return valor if permiso_courier(cliente_id, valor, "cotizar") else ""
    except Exception as e:
        print(f"[carriers] no pude leer el courier default de {cliente_id}: {e}")
        return ""


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
             descuento_pct: float = 0.0, margen_fijo_ars: float = 0.0,
             adicional_ars: float = 0.0) -> dict:
    """
    Convierte el costo crudo del carrier a precio final (ARS + USD).

    Prioridad:
      1. margen_fijo_ars > 0  → precio = COSTO + ese monto (ganancia fija en
         ARS, ej. DHL +$135.000). La ganancia por envío es esa plata exacta.
      2. descuento_pct > 0    → tarifa de lista con descuento MÁS
         `adicional_ars` (caso FedEx: lista − 90% + $10.000).
      3. si no                → tarifa × (1 + markup web).
    """
    # `costo` es lo que el carrier nos cobra a NOSOTROS (tarifa ACCOUNT en
    # FedEx). `costo_lista` es el precio público (LIST) cuando el courier
    # lo informa — es el correcto para mostrar tachado.
    try:
        costo_resultado = float(resultado.get("costo"))
    except (TypeError, ValueError):
        costo_resultado = 0.0
    if not math.isfinite(costo_resultado) or costo_resultado <= 0:
        raise ValueError("Costo de carrier inválido")
    moneda = str(resultado.get("moneda") or "USD").strip().upper()
    if moneda not in {"USD", "ARS"}:
        # Nunca asumir que EUR/GBP/etc. son pesos. Sin una cotización FX
        # explícita y auditada, fallar cerrado es la única forma de no vender
        # un flete internacional casi gratis.
        raise ValueError(f"Moneda de carrier no soportada: {moneda}")
    es_usd = moneda == "USD"
    costo_real_ars = round(costo_resultado * dolar) if es_usd else round(costo_resultado)

    # Ganancia FIJA: costo + monto. Gana la prioridad porque es una decisión
    # comercial explícita por courier (no se le encima el markup %).
    if margen_fijo_ars and margen_fijo_ars > 0:
        precio_ars = round(costo_real_ars + margen_fijo_ars)
        return {"precio_ars": precio_ars, "precio_usd": round(precio_ars / dolar, 2)}

    base = resultado.get("costo_lista") or costo_resultado
    if es_usd:
        lista_usd = base
        lista_ars = round(lista_usd * dolar)
    else:
        lista_ars = round(base)
        lista_usd = round(lista_ars / dolar, 2)

    if descuento_pct > 0:
        # Regla de Leandro (06/08): "aplicarle un 90% de descuento y sumarle un
        # markup de $10.000". El adicional va DESPUÉS del descuento y ANTES del
        # piso, así el piso ve el precio que el cliente realmente va a pagar.
        precio_ars = round(lista_ars * (1 - descuento_pct / 100)) + round(adicional_ars)

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


def costos_carriers(origen: dict, destino: dict, paquete: dict,
                    paquetes: list = None,
                    couriers_habilitados: set[str] | None = None) -> list[dict]:
    """
    ⚠️ CAPA INTERNA: devuelve LO QUE CADA COURIER NOS COBRA A NOSOTROS.
    NUNCA mandar esto tal cual a un cliente — de acá sale nuestro margen.

    Es la única capa que le habla a los couriers. Encima van dos calculadoras
    de precio distintas, porque son dos negocios distintos:

      cotizar_carriers()         → web pública: costo + margen de vidriera
      cotizar_carriers_cliente() → portal: costo + el monto fijo de ESE cliente
                                   (Prete Rosso $11.000, Melcior $14.000,
                                    WAIMAO $95.000)

    Antes había una sola función con el precio de la web cableado adentro.
    Enchufarla al portal le habría cobrado a WAIMAO el precio de vidriera en
    vez de su regla.

    Cada item: {id, nombre, logo, servicio, estado} y, si estado=="cotizado",
    además {costo, moneda, dias_estimados}.
    """
    salida: list[dict] = []
    habilitados = (
        {str(c).strip().lower() for c in couriers_habilitados}
        if couriers_habilitados is not None else None
    )

    for c in CARRIERS:
        base = {
            "id": c["id"],
            "nombre": c["nombre"],
            "logo": c["logo"],
            "servicio": c["servicio"],
        }

        # El permiso se controla ANTES de construir el cliente o tocar la API.
        # Ocultarlo sólo en HTML permitiría saltarlo con un POST manual.
        if habilitados is not None and c["id"] not in habilitados:
            salida.append({**base, "estado": "no_habilitado"})
            continue

        if not carrier_activo(c):
            salida.append({**base, "estado": "proximamente"})
            continue

        # Envío de VARIAS cajas distintas: sólo cotizan los couriers que
        # saben tarifar N piezas. Al que no puede se lo marca y se lo saca
        # del comparador — NO se le manda la suma de los pesos, porque cada
        # caja paga por su propio peso volumétrico y sumarlas cotiza de
        # menos. Un precio de menos hoy es una pérdida al facturar.
        multi = paquetes is not None and len(paquetes) > 1
        cliente = c["cliente"]()
        if multi and not getattr(cliente, "MULTIBULTO", False):
            salida.append({
                **base,
                "estado": "sin_multibulto",
                "error": f"{c['nombre']} todavía no cotiza envíos de varias cajas",
            })
            continue

        try:
            if paquetes is not None:
                resultado = cliente.get_rates(origen, destino, paquetes=paquetes)
            else:
                resultado = cliente.get_rates(origen, destino, paquete)
        except Exception as e:  # una caída de un carrier no tumba a los otros
            print(f"[carriers] {c['id']} get_rates excepción: {e}")
            resultado = {"encontrado": False}

        if not resultado.get("encontrado"):
            salida.append({**base, "estado": "sin_tarifa", "error": resultado.get("error")})
            continue

        moneda = str(resultado.get("moneda") or "USD").strip().upper()
        if moneda not in {"USD", "ARS"}:
            print(f"[carriers] {c['id']} devolvió moneda no soportada {moneda}; "
                  "se descarta la tarifa")
            salida.append({
                **base, "estado": "sin_tarifa",
                "error": "El courier devolvió una moneda que TAURO todavía no convierte.",
            })
            continue

        try:
            costo = float(resultado.get("costo"))
        except (TypeError, ValueError):
            costo = 0.0
        if not math.isfinite(costo) or costo <= 0:
            print(f"[carriers] {c['id']} devolvió un costo inválido; se descarta la tarifa")
            salida.append({
                **base, "estado": "sin_tarifa",
                "error": "El courier devolvió un importe inválido.",
            })
            continue
        costo_lista = resultado.get("costo_lista")
        try:
            costo_lista = float(costo_lista) if costo_lista is not None else None
        except (TypeError, ValueError):
            costo_lista = None
        if costo_lista is not None and (
            not math.isfinite(costo_lista) or costo_lista <= 0
        ):
            costo_lista = None

        # "INTERNATIONAL_PRIORITY" → "International Priority" (prolijo para la web)
        salida.append({
            **base,
            "estado": "cotizado",
            "servicio": (resultado.get("servicio") or c["servicio"]).replace("_", " ").title(),
            "dias_estimados": str(resultado.get("dias_estimados", "3-5")),
            "costo": costo,
            "moneda": moneda,
            "costo_lista": costo_lista,
        })

    return salida


def cotizar_carriers(origen: dict, destino: dict, paquete: dict,
                     dolar: float, markup_pct: float,
                     paquetes: list = None) -> list[dict]:
    """
    PRECIO DE VIDRIERA (web pública taurosolutions.ar).

    estado:
      - "cotizado"     → tarifa real (precio_ars/precio_usd/dias_estimados)
      - "proximamente" → carrier sin credenciales todavía (se muestra con logo)
      - "sin_tarifa"   → activo pero sin cobertura para esa ruta
    """
    salida: list[dict] = []

    # Una sola lectura de config por cotización, no una por carrier.
    pricing = _pricing_configurado()

    for crudo in costos_carriers(origen, destino, paquete, paquetes):
        base = {k: crudo[k] for k in ("id", "nombre", "logo", "servicio")}

        if crudo["estado"] != "cotizado":
            salida.append({**base, "estado": crudo["estado"]})
            continue

        c = {"id": crudo["id"]}
        resultado = {
            "costo": crudo["costo"],
            "moneda": crudo["moneda"],
            "costo_lista": crudo.get("costo_lista"),
        }
        servicio = crudo["servicio"]

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

        # Ganancia fija por courier (DHL +$135.000, decisión de Leandro 04/08):
        # si está seteada, manda sobre el markup % y sobre el descuento.
        margen_fijo = _margen_fijo_de(c["id"], pricing)

        # Monto fijo que se SUMA al precio ya descontado (FedEx +$10.000,
        # decisión de Leandro 06/08). Sólo aplica en la rama del descuento.
        adicional = _adicional_de(c["id"], pricing) if descuento > 0 else 0.0

        salida.append({
            **base,
            "estado": "cotizado",
            "servicio": servicio,
            "dias_estimados": crudo["dias_estimados"],
            **_precios(resultado, dolar, markup_carrier,
                       descuento_pct=descuento, margen_fijo_ars=margen_fijo,
                       adicional_ars=adicional),
        })

    return salida


def cotizar_carriers_cliente(origen: dict, destino: dict, paquete: dict,
                             dolar: float, pricing_cliente: dict,
                             paquetes: list = None,
                             pricing_por_courier: dict[str, dict] | None = None,
                             couriers_habilitados: set[str] | None = None) -> list[dict]:
    """
    PRECIO DEL PORTAL: los 3 couriers con la regla de ESE cliente.

    `pricing_cliente` es lo que devuelve pricing.get_pricing_config(cliente):
    {"tipo": "FIJO_ARS", "valor": 95000.0} para WAIMAO, por ejemplo.

    Devuelve SÓLO precios finales. Ninguna clave con el costo ni el margen:
    el cliente ve lo que le cobramos y nada más (regla de Leandro, 01/08/2026).
    Lo vigila tests/test_no_fuga_costo.py.
    """
    from servicios.pricing import aplicar_pricing

    salida: list[dict] = []

    for crudo in costos_carriers(
        origen, destino, paquete, paquetes,
        couriers_habilitados=couriers_habilitados,
    ):
        base = {k: crudo[k] for k in ("id", "nombre", "logo", "servicio")}

        if crudo["estado"] != "cotizado":
            salida.append({**base, "estado": crudo["estado"], "error": crudo.get("error")})
            continue

        moneda = str(crudo.get("moneda") or "USD").strip().upper()
        if moneda not in {"USD", "ARS"}:
            salida.append({**base, "estado": "sin_tarifa",
                           "error": "Moneda del courier no soportada."})
            continue

        es_usd = moneda == "USD"
        costo_ars = round(crudo["costo"] * dolar) if es_usd else round(crudo["costo"])
        costo_usd = crudo["costo"] if es_usd else round(crudo["costo"] / dolar, 2)

        pricing_efectivo = (
            (pricing_por_courier or {}).get(crudo["id"])
            or pricing_cliente
        )
        precio = aplicar_pricing(
            costo_usd=costo_usd, costo_ars=costo_ars,
            dolar=dolar, pricing=pricing_efectivo,
        )

        # Las claves se eligen a mano: aplicar_pricing devuelve además
        # markup_valor y markup_pct_equivalente, y con eso el cliente despeja
        # nuestro costo con una resta.
        salida.append({
            **base,
            "estado": "cotizado",
            "servicio": crudo["servicio"],
            "dias_estimados": crudo["dias_estimados"],
            "precio_ars": precio["precio_final_ars"],
            "precio_usd": precio["precio_final_usd"],
        })

    return salida
