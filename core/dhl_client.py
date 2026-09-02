from __future__ import annotations

import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from core.fedex_client import CarrierBase
from servicios.impuestos import incoterm as incoterm_de

load_dotenv()

# Argentina es UTC-3 fijo y no tiene horario de verano, así que el offset va
# a mano en vez de con ZoneInfo: evita depender del paquete tzdata, que en
# las imágenes slim de Railway puede no estar y tiraría ZoneInfoNotFoundError
# en producción justo al cotizar.
TZ_AR = timezone(timedelta(hours=-3))

# Guardas operativas TAURO. Se validan otra vez en el cliente del carrier:
# así un caller interno o una solicitud histórica tampoco puede expandir una
# cantidad descontrolada de paquetes antes de llamar a MyDHL.
MAX_DHL_PACKAGES = 20
MAX_DHL_PACKAGE_KG = 70.0
MAX_DHL_PICKUP_KG = MAX_DHL_PACKAGES * MAX_DHL_PACKAGE_KG
MAX_DHL_CUSTOMS_UNITS = 9999

# MyDHL exige que la hora de retiro/emisión lleve el huso DEL ORIGEN. El
# contenedor de producción instala tzdata (Dockerfile), por eso usamos zonas
# IANA y no offsets fijos: Londres/Madrid cambian por horario de verano. Para
# países con más de un huso, la zona es la de la ciudad de referencia del
# catálogo; los remitentes de WAIMAO (CN/AR/IN) no tienen esa ambigüedad.
TZ_POR_PAIS = {
    "AR": "America/Argentina/Buenos_Aires", "US": "America/New_York",
    "CN": "Asia/Shanghai", "IN": "Asia/Kolkata", "BD": "Asia/Dhaka",
    "VN": "Asia/Ho_Chi_Minh", "PK": "Asia/Karachi", "TH": "Asia/Bangkok",
    "ID": "Asia/Jakarta", "TR": "Europe/Istanbul", "KR": "Asia/Seoul",
    "JP": "Asia/Tokyo", "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei",
    "AE": "Asia/Dubai", "ES": "Europe/Madrid", "IT": "Europe/Rome",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "PT": "Europe/Lisbon",
    "NL": "Europe/Amsterdam", "GB": "Europe/London", "BR": "America/Sao_Paulo",
    "CL": "America/Santiago", "UY": "America/Montevideo", "PY": "America/Asuncion",
    "BO": "America/La_Paz", "PE": "America/Lima", "EC": "America/Guayaquil",
    "CO": "America/Bogota", "MX": "America/Mexico_City", "CA": "America/Toronto",
    "AU": "Australia/Sydney", "IL": "Asia/Jerusalem", "ZA": "Africa/Johannesburg",
}

# ─────────────────────────────────────────────
# DHL EXPRESS CLIENT (MyDHL API)
# Documentación: https://developer.dhl.com/api-reference/dhl-express-mydhl-api
# Auth: Basic (API key + secret). Se activa cuando están las credenciales
# en Railway → Variables (DHL_API_KEY / DHL_API_SECRET / DHL_ACCOUNT_NUMBER).
# Mientras falten, el cotizador lo muestra con el logo en "próximamente".
# ─────────────────────────────────────────────

class DHLClient(CarrierBase):

    # POST /rates acepta la lista de bultos, cada uno con su peso y medidas.
    MULTIBULTO = True

    SANDBOX_URL = "https://express.api.dhl.com/mydhlapi/test"
    PROD_URL    = "https://express.api.dhl.com/mydhlapi"

    # Versión del contrato contra la que validamos. Pineada a propósito.
    API_VERSION = "3.3.1"

    # "P" = EXPRESS WORLDWIDE NONDOC, el producto que corresponde a TAURO:
    # nuestros envíos llevan mercadería con valor declarado, no documentos.
    PRODUCTO_DEFAULT = "P"

    # Requisito de certificación confirmado por DHL Sistemas (11/08/2026):
    # Data Staging 12 se representa en el contrato JSON como el servicio PV.
    # El código XML histórico PT no es equivalente y no debe usarse aquí.
    DATA_STAGING_SERVICE_CODE = "PV"

    def __init__(self):
        self.api_key        = os.getenv("DHL_API_KEY")
        self.api_secret     = os.getenv("DHL_API_SECRET")
        # DHL da UNA CUENTA POR SENTIDO y cada una tiene su propia grilla
        # negociada. Se aceptan los dos nombres a propósito: son dos números
        # de 9 dígitos parecidísimos (741622792 / 730089966) y un nombre que
        # no dice el sentido invita a cargarlos cruzados. _EXPO gana si están
        # las dos, y el nombre viejo sigue andando para no romper nada.
        self.account_number = (
            os.getenv("DHL_ACCOUNT_NUMBER_EXPO")
            or os.getenv("DHL_ACCOUNT_NUMBER")
        )
        self.account_import = (
            os.getenv("DHL_ACCOUNT_NUMBER_IMPO")
            or os.getenv("DHL_ACCOUNT_NUMBER_IMPORT")
        )
        entorno = os.getenv("DHL_ENVIRONMENT", "sandbox").strip().lower()
        # Fallar CERRADO: antes un typo como `sandox` seleccionaba producción.
        # Nunca una variable mal escrita puede convertir una prueba en costo real.
        self.environment = entorno if entorno in {"sandbox", "production"} else "invalid"
        self.configuration_error = (
            None if self.environment != "invalid" else
            "DHL_ENVIRONMENT debe ser 'sandbox' o 'production'. No se llamó a DHL."
        )
        self.base_url = (
            self.PROD_URL if self.environment == "production" else self.SANDBOX_URL
        )
        self.product_code   = os.getenv("DHL_PRODUCT_CODE", self.PRODUCTO_DEFAULT)

    def _error_configuracion(self) -> str | None:
        if self.configuration_error:
            return self.configuration_error
        if not (self.api_key and self.api_secret):
            return "Faltan credenciales de DHL."
        return None

    @staticmethod
    def _zona_origen(pais: str) -> tuple[ZoneInfo | None, str | None]:
        iso = (pais or "AR").strip().upper()[:2]
        nombre = TZ_POR_PAIS.get(iso)
        if not nombre:
            return None, (
                f"Todavía no está configurada la zona horaria de {iso} para DHL. "
                "Pedinos que la validemos antes de agendar."
            )
        try:
            return ZoneInfo(nombre), None
        except ZoneInfoNotFoundError:
            return None, "El servidor no tiene disponible la base de zonas horarias."

    def _cuenta_para(self, origen: dict, destino: dict) -> tuple:
        """
        Elige la cuenta según el sentido del envío.

        Devuelve (numero_de_cuenta, error). Mandar la cuenta del sentido
        equivocado NO da error en DHL: contesta con la grilla del otro
        sentido, o sea un precio que después no es el que facturan. Por eso,
        si es una importación y no está cargada la cuenta de impo, se
        devuelve error explícito en vez de caer a la de expo — mejor que el
        comparador diga "no disponible" a que publique un precio mentiroso.
        """
        origen_ar = (origen.get("country") or "AR").upper() == "AR"

        # Regla de Leandro (05/08): la cuenta de EXPO es sólo para lo que
        # SALE de Argentina. Todo lo demás —importaciones a AR Y envíos
        # entre terceros países (China → México)— va por la cuenta de IMPO.
        if origen_ar:
            return self.account_number, None

        if not self.account_import:
            return None, (
                "Falta DHL_ACCOUNT_NUMBER_IMPO: los envíos que no salen de "
                "Argentina (importaciones y tercer país) van por la cuenta "
                "de importación y no se pueden cotizar con la de expo"
            )
        return self.account_import, None

    @staticmethod
    def _error_legible(resp) -> str:
        """
        Traduce el cuerpo de error de DHL a algo accionable.

        MyDHL API contesta los rechazos de validación con un `detail` genérico
        —"Multiple problems found, see Additional Details"— y mete los problemas
        REALES en `additionalDetails`. Leer sólo `detail` deja al admin con un
        cartel rojo que no dice nada: pasó exactamente eso emitiendo la guía #4
        el 06/08. Es la diferencia entre "no anda" y "el CP de destino no existe".
        """
        # Un 401 no es una ruta sin cobertura: MyDHL rechazó la
        # autenticación productiva. Devolver una causa segura y accionable
        # permite que el portal avise a TAURO sin exponer la respuesta cruda,
        # la cuenta ni ningún secreto.
        if getattr(resp, "status_code", None) in (401, 403):
            return (
                "DHL rechazó las credenciales o el acceso productivo "
                f"(HTTP {resp.status_code})."
            )

        try:
            j = resp.json()
        except Exception:
            return f"DHL rechazó la solicitud (HTTP {getattr(resp, 'status_code', '?')})."

        if not isinstance(j, dict):
            return f"DHL rechazó la solicitud (HTTP {getattr(resp, 'status_code', '?')})."

        base = j.get("detail") or j.get("message") or j.get("title") or ""

        detalles = j.get("additionalDetails")
        if isinstance(detalles, str):
            detalles = [detalles]
        if isinstance(detalles, (list, tuple)):
            partes = [str(d).strip() for d in detalles if str(d or "").strip()]
            if partes:
                # Sin el " · " los problemas se leen como una sola frase larga.
                return " · ".join(partes)[:600]

        return (base or f"DHL rechazó la solicitud (HTTP {resp.status_code}).")[:300]

    @staticmethod
    def _codigos_error(resp) -> str:
        """Extrae sólo códigos aptos para observabilidad, nunca textos del body."""
        try:
            data = resp.json()
        except Exception:
            return "sin_codigo"
        if not isinstance(data, dict):
            return "sin_codigo"
        codigos = []
        for clave in ("code", "errorCode", "status"):
            valor = data.get(clave)
            if valor not in (None, ""):
                codigos.append(str(valor).strip())
        detalles = data.get("additionalDetails") or []
        if isinstance(detalles, dict):
            detalles = [detalles]
        if isinstance(detalles, (list, tuple)):
            for detalle in detalles:
                if isinstance(detalle, dict):
                    valor = detalle.get("code") or detalle.get("errorCode")
                    if valor not in (None, ""):
                        codigos.append(str(valor).strip())
        return ",".join(c for c in codigos[:5] if c) or "sin_codigo"

    def _parsear_rates(self, data: dict) -> dict:
        """
        Lee la respuesta de /rates. GET y POST devuelven el MISMO schema, así
        que el parseo es uno solo.
        """
        productos = data.get("products", [])
        if not productos:
            return {"encontrado": False, "error": "Sin tarifas en respuesta DHL"}

        # Elegir el producto POR CÓDIGO, nunca el primero de la lista: DHL
        # devuelve varios (Worldwide, 12:00, 9:00) sin garantizar el orden.
        # Se descartan los isCustomerAgreement, que necesitan acuerdo previo.
        estandar = [p for p in productos if not p.get("isCustomerAgreement")]
        prod = next(
            (p for p in estandar if p.get("productCode") == self.product_code),
            None,
        )
        if prod is None:
            disponibles = [p.get("productCode") for p in estandar]
            return {"encontrado": False,
                    "error": f"DHL no ofrece {self.product_code} en esa ruta "
                             f"(disponibles: {disponibles})"}

        precios = prod.get("totalPrice", [])
        if not precios:
            return {"encontrado": False, "error": "Producto DHL sin precio"}

        # MyDHL puede devolver BASEC/PULCL/BILLC en cualquier orden. BILLC es
        # la moneda e importe de facturación de NUESTRA cuenta; usar [0]
        # podía tomar una base informativa distinta de lo que luego cobra DHL.
        precio_facturado = next(
            (p for p in precios
             if str(p.get("currencyType") or "").strip().upper() == "BILLC"),
            None,
        )
        # Respuestas antiguas/mocks traen un único totalPrice sin currencyType.
        # Ese caso es inequívoco; con varios precios y sin BILLC se falla cerrado.
        if precio_facturado is None and len(precios) == 1:
            precio_facturado = precios[0]
        if precio_facturado is None:
            return {"encontrado": False,
                    "error": "DHL no indicó el precio de facturación (BILLC)."}
        try:
            costo = float(precio_facturado.get("price"))
        except (TypeError, ValueError):
            costo = 0.0
        if not math.isfinite(costo) or costo <= 0:
            return {"encontrado": False,
                    "error": "DHL devolvió un precio de facturación inválido."}

        return {
            "encontrado": True,
            "costo": costo,
            "moneda": precio_facturado.get("priceCurrency", "USD"),
            "servicio": prod.get("productName", "DHL Express Worldwide"),
            "dias_estimados": str(
                prod.get("deliveryCapabilities", {}).get("totalTransitDays", "2-4")
            ),
        }

    def _fecha_envio(self, origen_pais: str = "AR") -> str:
        """
        plannedShippingDateAndTime para el POST: YYYY-MM-DDTHH:MM:SSGMT-03:00.

        SIN espacio antes de GMT. La spec se contradice consigo misma —los
        ejemplos de /rates van sin espacio, los mensajes de error de la misma
        spec lo muestran con espacio— así que se arranca con la forma del
        ejemplo de /rates, que es el endpoint que estamos llamando. Si DHL
        devuelve "not well formatted", probar con espacio antes de GMT.
        """
        zona, error = self._zona_origen(origen_pais)
        # El catálogo de países y TZ_POR_PAIS se actualizan juntos. Si por un
        # despliegue incompleto faltara una zona, no inventamos el huso: esta
        # excepción se convierte en un error legible antes de llamar a DHL.
        if error:
            raise ValueError(error)
        d = datetime.now(zona) + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        offset = d.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}"
        return d.strftime("%Y-%m-%dT13:00:00GMT") + offset

    @staticmethod
    def _codigo_postal(pais: str, valor) -> str:
        """Normaliza el CPA argentino al formato de 4 dígitos de MyDHL.

        El portal acepta tanto `1043` como el CPA completo `C1043ABC`, pero
        MyDHL Rates/Shipments rechaza la letra y exige `9999(4)`. Para otros
        países se conserva el valor sin reinterpretarlo.
        """
        codigo = str(valor or "").strip().upper()
        if str(pais or "").strip().upper()[:2] == "AR":
            digitos = "".join(ch for ch in codigo if ch.isdigit())
            if len(digitos) == 4:
                return digitos
        return codigo

    @staticmethod
    def _direccion(d: dict, por_defecto_ciudad: str = "") -> dict:
        """
        Bloque de dirección del POST. `cityName` tiene minLength 1: mandarlo
        vacío —como hacía el camino del GET con destino.get("city", "")— es
        un rechazo garantizado. Y los objetos van con additionalProperties
        false, así que sólo pueden ir estas claves.

        Acepta las claves EN LOS DOS IDIOMAS, y no es gusto: el contrato de
        create_shipment está documentado en castellano ({ciudad, zip, pais},
        ver FedExClient.create_shipment) y así arma el despachador de
        emisión, mientras que el camino de cotización pasa {city,
        postal_code, country}. Esta función leía SOLO inglés, así que toda
        emisión llegaba a DHL con ciudad y país vacíos — el rechazo
        "cityName: expected minLength 1, actual 0" de la guía #4 (06/08).
        """
        ciudad = (d.get("ciudad") or d.get("city") or por_defecto_ciudad or "").strip()
        pais = str(d.get("pais") or d.get("country") or "").strip().upper()
        bloque = {
            "postalCode": DHLClient._codigo_postal(
                pais, d.get("zip") or d.get("postal_code") or ""
            ),
            "cityName": ciudad,
            "countryCode": pais,
        }
        estado = d.get("estado") or d.get("state")
        # MyDHL exige minLength 2. Argentina suele venir como la letra única
        # del CPA (C/B/X...), que no es un provinceCode válido; al ser opcional
        # se omite en vez de provocar un rechazo de toda la guía.
        codigo_estado = str(estado or "").strip().upper()
        if len(codigo_estado) == 2 and codigo_estado.isalpha():
            bloque["provinceCode"] = codigo_estado
        return bloque

    def get_rates_multibulto(self, origen: dict, destino: dict, paquetes: list) -> dict:
        """
        POST /rates — cotiza N cajas DISTINTAS en un mismo envío.

        Cada caja va como un elemento de `packages` con SU peso y SUS medidas:
        no hay campo de cantidad, tres cajas iguales son tres elementos. Es
        justamente el punto — cada una paga por su propio peso volumétrico, y
        sumarlas cotizaría de menos.

        Devuelve el MISMO contrato que get_rates(): la respuesta del POST usa
        el mismo schema que el GET, así que el parseo se comparte.
        """
        if error_config := self._error_configuracion():
            return {"encontrado": False, "error": error_config}
        if not paquetes:
            return {"encontrado": False, "error": "Sin bultos para cotizar"}
        if len(paquetes) > MAX_DHL_PACKAGES:
            return {"encontrado": False,
                    "error": f"DHL admite como máximo {MAX_DHL_PACKAGES} bultos por envío."}

        cuenta, error_cuenta = self._cuenta_para(origen, destino)
        if error_cuenta:
            print(f"[dhl] {error_cuenta}")
            return {"encontrado": False, "error": error_cuenta}

        zona_origen, error_zona = self._zona_origen(origen.get("country", "AR"))
        if error_zona:
            return {"encontrado": False, "error": error_zona}

        ciudad_destino = (destino.get("city") or "").strip()
        if not ciudad_destino:
            # minLength 1: sin ciudad el POST vuelve 400. Mejor decirlo.
            return {"encontrado": False,
                    "error": "DHL necesita la ciudad de destino para cotizar"}

        msg_ref = str(uuid.uuid4())
        cuerpo = {
            "customerDetails": {
                "shipperDetails": self._direccion(origen, "BUENOS AIRES"),
                "receiverDetails": self._direccion(destino),
            },
            "accounts": [{"typeCode": "shipper", "number": str(cuenta)}],
            "plannedShippingDateAndTime": self._fecha_envio(origen.get("country", "AR")),
            "unitOfMeasurement": "metric",
            # boolean de verdad: en el GET viajaba como el string "true".
            "isCustomsDeclarable": True,
            "packages": [
                {
                    "weight": round(float(p.get("peso_kg") or 0.5), 3),
                    "dimensions": {
                        # Las tres son obligatorias si mandás dimensions.
                        "length": round(float(p.get("largo_cm") or p.get("largo") or 30), 3),
                        "width": round(float(p.get("ancho_cm") or p.get("ancho") or 20), 3),
                        "height": round(float(p.get("alto_cm") or p.get("alto") or 10), 3),
                    },
                }
                for p in paquetes
            ],
        }
        asegurar_carga = any(
            p.get("asegurar_carga") is True
            or str(p.get("asegurar_carga") or "").strip().upper() in {"SI", "SÍ", "1", "TRUE"}
            for p in paquetes
        )
        if asegurar_carga:
            try:
                valor_asegurado = sum(
                    float(p.get("valor_declarado_caja_usd")) for p in paquetes
                )
            except (TypeError, ValueError):
                return {"encontrado": False,
                        "error": "Cada caja asegurada necesita un valor declarado válido."}
            if not math.isfinite(valor_asegurado) or valor_asegurado <= 0:
                return {"encontrado": False,
                        "error": "El valor total asegurado debe ser mayor a cero."}
            cuerpo["valueAddedServices"] = [{
                "serviceCode": "II",
                "value": round(valor_asegurado, 2),
                "currency": "USD",
            }]

        try:
            resp = requests.post(
                f"{self.base_url}/rates",
                json=cuerpo,
                auth=(self.api_key, self.api_secret),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-version": self.API_VERSION,
                    "Message-Reference": msg_ref,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                codigo = self._codigos_error(resp)
                print(f"[dhl] POST /rates error HTTP {resp.status_code}; "
                      f"código={codigo}; ref={msg_ref}")
                return {"encontrado": False, "error": self._error_legible(resp)}
            return self._parsear_rates(resp.json())
        except Exception as e:
            print(f"[dhl] Excepción en get_rates_multibulto (ref {msg_ref}): {e}")
            return {"encontrado": False, "error": str(e)}

    def get_rates(self, origen: dict, destino: dict, paquete: dict = None,
                  paquetes: list = None) -> dict:
        """
        Consulta tarifas DHL Express Worldwide.

        Retorna el mismo contrato que FedExClient/UPSClient.get_rates():
        {
            "encontrado": True/False,
            "costo": float,
            "moneda": str,          ← "USD" o "ARS"
            "servicio": str,
            "dias_estimados": str,
        }
        """
        # Varias cajas o seguro → POST /rates. El GET no permite enviar el
        # valor monetario que exige el servicio de protección II.
        seguro_solicitado = bool(paquetes) and any(
            p.get("asegurar_carga") is True
            or str(p.get("asegurar_carga") or "").strip().upper() in {"SI", "SÍ", "1", "TRUE"}
            for p in paquetes
        )
        if paquetes is not None and (len(paquetes) > 1 or seguro_solicitado):
            return self.get_rates_multibulto(origen, destino, paquetes)
        if paquetes:
            paquete = paquetes[0]

        if error_config := self._error_configuracion():
            return {"encontrado": False, "error": error_config}

        # Identificador propio de esta llamada. Cuando le reclames un error a
        # soporte de DHL te lo van a pedir para buscar el request de su lado,
        # así que viaja en el header y queda en nuestro log.
        msg_ref = str(uuid.uuid4())

        cuenta, error_cuenta = self._cuenta_para(origen, destino)
        if error_cuenta:
            print(f"[dhl] {error_cuenta}")
            return {"encontrado": False, "error": error_cuenta}

        zona_origen, error_zona = self._zona_origen(origen.get("country", "AR"))
        if error_zona:
            return {"encontrado": False, "error": error_zona}

        try:
            url = f"{self.base_url}/rates"
            params = {
                "accountNumber": cuenta,
                "originCountryCode": origen.get("country", "AR"),
                "originCityName": origen.get("city", "BUENOS AIRES"),
                "originPostalCode": self._codigo_postal(
                    origen.get("country", "AR"), origen.get("postal_code", "1043")
                ),
                "destinationCountryCode": destino.get("country", "US"),
                "destinationCityName": destino.get("city", ""),
                "destinationPostalCode": self._codigo_postal(
                    destino.get("country", "US"), destino.get("postal_code", "")
                ),
                "weight": paquete.get("peso_kg", 0.5),
                # El wizard usa *_cm; el cotizador rápido usa las claves
                # cortas. Aceptar ambos evita cotizar 30×20×10 y emitir luego
                # las medidas reales, una diferencia directa de facturación.
                # Nunca truncar hacia abajo: 48,9 cm no puede cotizarse como
                # 48 y emitirse después con 48,9. Redondear hacia arriba evita
                # subcotizar peso volumétrico.
                "length": math.ceil(float(paquete.get("largo_cm") or paquete.get("largo") or 30)),
                "width":  math.ceil(float(paquete.get("ancho_cm") or paquete.get("ancho") or 20)),
                "height": math.ceil(float(paquete.get("alto_cm") or paquete.get("alto") or 10)),
                # OBLIGATORIO según el OpenAPI oficial (required: true). Antes
                # iba en None y el filtro de abajo lo borraba, con el comentario
                # "DHL usa la fecha del día si se omite" — la spec dice lo
                # contrario y sin este parámetro DHL contesta 400 SIEMPRE.
                # Formato YYYY-MM-DD, no ISO con hora.
                "plannedShippingDate": (
                    datetime.now(zona_origen).date() + timedelta(days=1)
                ).isoformat(),
                # Si la fecha cae domingo o feriado, DHL devuelve los productos
                # del próximo día hábil en vez de una lista vacía.
                "nextBusinessDay": "true",
                "isCustomsDeclarable": "true",
                "unitOfMeasurement": "metric",
            }
            # Limpiar los None para no romper el querystring
            params = {k: v for k, v in params.items() if v is not None}

            resp = requests.get(
                url,
                params=params,
                auth=(self.api_key, self.api_secret),
                headers={
                    "Accept": "application/json",
                    # Único header obligatorio de MyDHL API v2. Su schema tiene
                    # default 3.3.1, así que hoy pasaría igual — pero pinearlo
                    # evita que el día que DHL publique 3.4 la cuenta se mueva
                    # sola de contrato y cambie la forma de la respuesta en
                    # producción sin que nadie haya tocado una línea.
                    "x-version": self.API_VERSION,
                    "Message-Reference": msg_ref,
                },
                timeout=30,
            )

            if resp.status_code != 200:
                codigo = self._codigos_error(resp)
                print(f"[dhl] get_rates error HTTP {resp.status_code}; "
                      f"código={codigo}; ref={msg_ref}")
                return {"encontrado": False, "error": self._error_legible(resp)}

            return self._parsear_rates(resp.json())

        except Exception as e:
            print(f"[dhl] Excepción en get_rates: {e}")
            return {"encontrado": False, "error": str(e)}

    @staticmethod
    def _contacto(d: dict, por_defecto_nombre: str = "") -> dict:
        """
        contactInformation del POST /shipments. El caller valida teléfono y
        nombre antes de emitir: nunca se inventa un dato de contacto.
        """
        return {
            "phone": (d.get("telefono") or "").strip()[:25],
            "companyName": (d.get("empresa") or d.get("nombre")
                            or por_defecto_nombre or "N/A").strip()[:60],
            "fullName": (d.get("nombre") or por_defecto_nombre or "N/A").strip()[:45],
            **({"email": d["email"].strip()[:50]} if (d.get("email") or "").strip() else {}),
        }

    @staticmethod
    def _registro_exportador(shipper: dict) -> dict:
        """
        registrationNumbers con el CUIT del cliente. Sin documento cargado
        se omite la clave entera: mandarla vacía hace que DHL rechace el
        envío, y un bloque ausente es válido.
        """
        # Alfanumérico A PROPÓSITO: el CUIT argentino es numérico, pero el
        # USCC chino (91330782MA2DCHET04) y el RFC mexicano (CEN040218L96)
        # llevan letras. Quedarse sólo con dígitos MUTILABA el Tax ID del
        # shipper de la primera guía real (HAILU) — lo cazó el test con
        # datos reales. Se sacan sólo separadores: espacios, puntos, guiones.
        doc = "".join(ch for ch in str(shipper.get("documento") or "")
                      if ch.isalnum()).upper()
        if not doc:
            return {}
        return {
            "registrationNumbers": [{
                "number": doc[:35],
                "issuerCountryCode": (shipper.get("pais") or "AR").upper()[:2],
                # VAT es el typeCode que DHL usa para el número de
                # contribuyente del exportador en los países sin figura
                # propia. Si DHL lo rechaza, el error lo va a decir.
                "typeCode": "VAT",
            }],
        }

    @staticmethod
    def _cuit_argentino_valido(documento: object) -> bool:
        """Valida longitud y dígito verificador de un CUIT argentino."""
        cuit = "".join(ch for ch in str(documento or "") if ch.isdigit())
        if len(cuit) != 11:
            return False
        pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
        verificador = 11 - sum(
            int(numero) * peso for numero, peso in zip(cuit[:10], pesos)
        ) % 11
        if verificador == 11:
            verificador = 0
        if verificador == 10:
            return False
        return verificador == int(cuit[-1])

    def _direccion_envio(self, d: dict) -> dict:
        """
        postalAddress del POST. Igual que en /rates pero con la calle, que
        para cotizar no hace falta y para emitir es obligatoria.
        """
        bloque = self._direccion(d)
        bloque["addressLine1"] = (d.get("calle") or d.get("direccion") or "")[:45]
        if d.get("direccion2"):
            bloque["addressLine2"] = str(d["direccion2"])[:45]
        return bloque

    def create_shipment(self, datos: dict) -> dict:
        """
        Emite una guía real en DHL (MyDHL API POST /shipments) y devuelve el
        tracking + el label PDF.

        Mismo contrato de entrada que FedExClient.create_shipment para que el
        despachador de emisión los trate igual:
          {"shipper": {...}, "recipient": {...}, "bultos": [...]} o
          {"package": {...}, "commodity": {...}}

        NO es idempotente: dos llamadas emiten dos guías facturadas. Quien
        llame tiene que traer su propia reserva (igual que FedEx).
        """
        if error_config := self._error_configuracion():
            return {"encontrado": False, "error": error_config}

        shipper = datos.get("shipper") or {}
        recipient = datos.get("recipient") or {}
        for etiqueta, parte in (("remitente", shipper),
                                ("destinatario", recipient)):
            requeridos = {
                "nombre": parte.get("nombre"),
                "teléfono": parte.get("telefono"),
                "dirección": parte.get("calle") or parte.get("direccion"),
                "ciudad": parte.get("ciudad") or parte.get("city"),
                "país": parte.get("pais") or parte.get("country"),
            }
            faltan = [campo for campo, valor in requeridos.items()
                      if not str(valor or "").strip()]
            if faltan:
                return {"encontrado": False, "error":
                        f"Completá {', '.join(faltan)} del {etiqueta} "
                        "antes de emitir con DHL."}

        # DHL Sistemas exige identificar al exportador argentino en
        # shipperDetails.registrationNumbers. Bloquear antes del POST evita
        # emitir una guía y una factura comercial fiscalmente incompletas.
        pais_shipper = (
            shipper.get("pais") or shipper.get("country") or "AR"
        ).upper()[:2]
        if pais_shipper == "AR" and not self._cuit_argentino_valido(
            shipper.get("documento")
        ):
            return {
                "encontrado": False,
                "error": (
                    "Completá un CUIT argentino válido de 11 dígitos "
                    "para el remitente antes de emitir con DHL."
                ),
            }
        bultos = datos.get("bultos") or []
        if not bultos:
            package = datos.get("package") or {}
            commodity = datos.get("commodity") or {}
            bultos = [{
                "peso_kg": package.get("peso_kg", 0.5),
                "largo": package.get("largo", 30), "ancho": package.get("ancho", 20),
                "alto": package.get("alto", 10),
                "unidades": commodity.get("cantidad", 1),
                # Contrato legado package+commodity: hasta ahora la cantidad
                # servía a la vez para cajas y unidades comerciales. Se deja
                # ese comportamiento como fallback; los callers nuevos deben
                # enviar unidades_aduana de forma explícita cuando difieran.
                "unidades_aduana": commodity.get(
                    "unidades_aduana", commodity.get("cantidad", 1)
                ),
                "valor_unitario_usd": commodity.get("valor_unitario_usd", 100),
                "descripcion_en": commodity.get("descripcion", "Merchandise"),
                "hs_code": commodity.get("hs_code", ""),
                "pais_origen": commodity.get("pais_origen", "AR"),
            }]

        cuenta, error = self._cuenta_para(
            {"country": shipper.get("pais", "AR")},
            {"country": recipient.get("pais", "")})
        if error:
            return {"encontrado": False, "error": error}

        # El país de fabricación por defecto es el del ORIGEN DEL ENVÍO
        # (regla de Leandro 01/08): sale de China → CN, de Argentina → AR.
        # Un "AR" fijo es una declaración falsa ante la aduana en cualquier
        # importación, que es justo lo que va a hacer WAIMAO.
        pais_origen_envio = (shipper.get("pais") or "AR").upper()[:2]

        # Las cajas físicas y las unidades comerciales NO son equivalentes:
        # una caja puede contener 8 camisas. `packages` representa cajas;
        # exportDeclaration representa las unidades que declara la factura.
        # Para solicitudes históricas sin `unidades_aduana`, ambas cantidades
        # siguen coincidiendo y el payload conserva el comportamiento anterior.
        piezas, line_items, valor_total = [], [], 0.0
        valor_total_cajas = 0.0
        total_cajas = 0
        for i, b in enumerate(bultos, start=1):
            try:
                cajas = int(b.get("unidades") or b.get("cantidad") or 1)
                unidades_aduana_raw = b.get("unidades_aduana")
                unidades_aduana = int(
                    cajas if unidades_aduana_raw in (None, "") else unidades_aduana_raw
                )
                valor_u = float(b.get("valor_unitario_usd") or 0)
                peso_caja = float(b.get("peso_kg") or 0.5)
            except (TypeError, ValueError):
                return {"encontrado": False,
                        "error": "Revisá cajas, peso, valor y unidades aduaneras."}
            if cajas < 1 or total_cajas + cajas > MAX_DHL_PACKAGES:
                return {"encontrado": False,
                        "error": f"DHL admite como máximo {MAX_DHL_PACKAGES} bultos por envío."}
            if not 1 <= unidades_aduana <= MAX_DHL_CUSTOMS_UNITS:
                return {"encontrado": False,
                        "error": ("Las unidades aduaneras deben estar entre 1 y "
                                  f"{MAX_DHL_CUSTOMS_UNITS} por renglón.")}
            if not math.isfinite(peso_caja) or not 0 < peso_caja <= MAX_DHL_PACKAGE_KG:
                return {"encontrado": False,
                        "error": f"Cada bulto DHL debe pesar hasta {MAX_DHL_PACKAGE_KG:g} kg."}
            if not math.isfinite(valor_u) or valor_u <= 0:
                return {"encontrado": False,
                        "error": "El valor unitario de cada ítem debe ser mayor a cero."}
            total_invoice_linea = round(valor_u * unidades_aduana, 2)
            valor_caja_crudo = b.get("valor_declarado_caja_usd")
            try:
                valor_caja = (
                    total_invoice_linea / cajas
                    if valor_caja_crudo in (None, "")
                    else float(valor_caja_crudo)
                )
            except (TypeError, ValueError):
                return {"encontrado": False,
                        "error": "Revisá el valor declarado por caja."}
            if not math.isfinite(valor_caja) or valor_caja <= 0:
                return {"encontrado": False,
                        "error": "El valor declarado por caja debe ser mayor a cero."}
            total_cajas_linea = round(valor_caja * cajas, 2)
            if (valor_caja_crudo not in (None, "")
                    and abs(total_cajas_linea - total_invoice_linea) > 0.02):
                return {"encontrado": False, "error": (
                    f"Caja {i}: el valor declarado de las cajas (USD "
                    f"{total_cajas_linea:.2f}) no coincide con la invoice "
                    f"(USD {total_invoice_linea:.2f})."
                )}
            total_cajas += cajas
            peso_linea = round(peso_caja * cajas, 3)
            for _ in range(cajas):
                piezas.append({
                    "weight": round(peso_caja, 3),
                    "dimensions": {
                        "length": round(float(b.get("largo_cm") or b.get("largo") or 30), 3),
                        "width": round(float(b.get("ancho_cm") or b.get("ancho") or 20), 3),
                        "height": round(float(b.get("alto_cm") or b.get("alto") or 10), 3),
                    },
                })
            valor_total += total_invoice_linea
            valor_total_cajas += total_cajas_linea
            line_items.append({
                "number": i,
                "description": (b.get("descripcion_en") or b.get("producto_alias")
                                or "Merchandise")[:75],
                "price": round(valor_u, 2),
                "quantity": {
                    "value": unidades_aduana,
                    "unitOfMeasurement": "PCS",
                },
                "commodityCodes": ([{"typeCode": "outbound",
                                     "value": str(b["hs_code"]).replace(".", "")[:18]}]
                                   if b.get("hs_code") else []),
                "exportReasonType": "permanent",
                # Regla de Leandro (01/08): el país de fabricación es el del
                # ORIGEN DEL ENVÍO. Sale de China → CN. El "AR" fijo era falso
                # para cualquier importación y es una declaración ante aduana.
                "manufacturerCountry": (
                    b.get("pais_origen") or pais_origen_envio
                ).upper()[:2],
                "weight": {
                    # Peso de todo el renglon comercial. Las dimensiones y el
                    # peso POR CAJA ya viven, por separado, en `packages`.
                    "netValue": peso_linea,
                    "grossValue": peso_linea,
                },
            })

        # El caller productivo persiste esta referencia ANTES del POST. Si el
        # proceso muere sin leer la respuesta, TAURO puede buscar exactamente
        # la operación en MyDHL sin habilitar un reintento a ciegas.
        msg_ref = str(
            datos.get("message_reference") or f"tauro-ship-{uuid.uuid4().hex[:20]}"
        )[:36]
        servicios_adicionales = [{
            "serviceCode": self.DATA_STAGING_SERVICE_CODE,
        }]
        asegurar_carga = (
            datos.get("asegurar_carga") is True
            or str(datos.get("asegurar_carga") or "").strip().upper()
            in {"SI", "SÍ", "1", "TRUE"}
        )
        if asegurar_carga:
            servicios_adicionales.append({
                "serviceCode": "II",
                "value": round(valor_total_cajas, 2),
                "currency": "USD",
            })

        cuerpo = {
            "plannedShippingDateAndTime": self._fecha_envio(shipper.get("pais", "AR")),
            "pickup": {"isRequested": False},
            "productCode": self.product_code,
            "accounts": [{"typeCode": "shipper", "number": cuenta}],
            "valueAddedServices": servicios_adicionales,
            "customerDetails": {
                "shipperDetails": {
                    "postalAddress": self._direccion_envio(shipper),
                    "contactInformation": self._contacto(shipper, "TAURO Solutions"),
                    # CUIT del cliente como EXPORTADOR (Leandro, 01/08: "el
                    # cuit es el del cliente como exportador siempre"). Viaja
                    # en la solicitud como remitente_documento y hasta hoy
                    # nunca se le mandaba al courier.
                    **self._registro_exportador(shipper),
                },
                "receiverDetails": {
                    "postalAddress": self._direccion_envio(recipient),
                    "contactInformation": self._contacto(recipient),
                    # MyDHL admite números regulatorios tanto del shipper como
                    # del recipient. Para importaciones de WAIMAO, no perder el
                    # identificador fiscal que el cliente completó en el portal.
                    **self._registro_exportador(recipient),
                },
            },
            "content": {
                "packages": piezas,
                "isCustomsDeclarable": True,
                "declaredValue": round(valor_total or 1, 2),
                "declaredValueCurrency": "USD",
                "description": (line_items[0]["description"] if line_items else "Merchandise")[:70],
                # DDP si el cliente eligió hacerse cargo de los impuestos,
                # DAP si los paga quien recibe. Estaba fijo en DAP, así que
                # la elección del cliente no llegaba a la guía.
                "incoterm": incoterm_de(datos.get("tax_paga")),
                "unitOfMeasurement": "metric",
                "exportDeclaration": {
                    "lineItems": line_items,
                    "invoice": {
                        # Leandro: "nro de factura: siempre el número de la
                        # fecha que se está realizando el envío".
                        "number": datetime.now(TZ_AR).strftime("%Y%m%d"),
                        "date": datetime.now(TZ_AR).strftime("%Y-%m-%d"),
                    },
                    "exportReason": "permanent",
                },
            },
            "outputImageProperties": {
                "encodingFormat": "pdf",
                "imageOptions": [
                    {"typeCode": "label", "templateName": "ECOM26_84_A4_001"},
                    {
                        "typeCode": "invoice",
                        "templateName": "COMMERCIAL_INVOICE_P_10",
                        "isRequested": True,
                        "invoiceType": "commercial",
                    },
                ],
            },
        }

        try:
            resp = requests.post(
                f"{self.base_url}/shipments",
                json=cuerpo,
                auth=(self.api_key, self.api_secret),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-version": self.API_VERSION,
                    "Message-Reference": msg_ref,
                },
                timeout=60,   # emitir tarda más que cotizar
            )
        except Exception as e:
            # Sin respuesta NO sabemos si DHL emitió: se avisa para verificar
            # antes de reintentar, igual que en el camino nacional.
            print(f"[dhl] EXCEPCIÓN emitiendo (ref {msg_ref}): {e}. "
                  f"VERIFICAR en MyDHL si la guía salió antes de reintentar.")
            return {"encontrado": False,
                    "error": "Error de comunicación con DHL. Verificá en MyDHL "
                             "antes de reintentar.",
                    "incierto": True, "message_reference": msg_ref}

        if resp.status_code not in (200, 201):
            codigo = self._codigos_error(resp)
            print(f"[dhl] POST /shipments error HTTP {resp.status_code}; "
                  f"código={codigo}; ref={msg_ref}")
            # Un timeout HTTP o un error del servidor puede llegar después de
            # que DHL haya persistido la guía. No es seguro liberar la reserva
            # ni reintentar: la referencia permite reconciliar en MyDHL.
            if resp.status_code == 408 or 500 <= resp.status_code <= 599:
                return {
                    "encontrado": False,
                    "error": "DHL no confirmó la emisión. Verificá en MyDHL "
                             "antes de reintentar.",
                    "incierto": True,
                    "message_reference": msg_ref,
                }
            return {"encontrado": False, "error": self._error_legible(resp)}

        try:
            data = resp.json()
            tracking = str(data.get("shipmentTrackingNumber") or "")
            if not tracking:
                return {"encontrado": False, "error": "DHL no devolvió tracking.",
                        "incierto": True, "message_reference": msg_ref}

            # La guía y la factura comercial son documentos independientes.
            # Nunca permitir que una invoice reemplace al label por orden de
            # respuesta o por un typeCode con distinto uso de guiones bajos.
            label_b64 = ""
            invoice_b64 = ""
            for doc in (data.get("documents") or []):
                tipo = (doc.get("typeCode") or "").replace("_", "").lower()
                if tipo == "label":
                    label_b64 = doc.get("content") or ""
                elif tipo in {"invoice", "commercialinvoice"}:
                    invoice_b64 = doc.get("content") or ""

            import base64

            def _decodificar_pdf(contenido_b64: str, nombre: str):
                if not contenido_b64:
                    return None
                try:
                    pdf = base64.b64decode(contenido_b64, validate=True)
                    if not pdf.startswith(b"%PDF"):
                        print(f"[dhl] guía {tracking} emitida pero {nombre} "
                              "no es un PDF válido")
                        return None
                    return pdf
                except Exception as e:
                    print(f"[dhl] guía {tracking} emitida pero {nombre} "
                          f"no decodifica: {e}")
                    return None

            label_pdf = _decodificar_pdf(label_b64, "el label")
            invoice_pdf = _decodificar_pdf(invoice_b64, "la factura comercial")

            print(f"[dhl] guía emitida: {tracking} (ref {msg_ref})")
            return {
                "encontrado": True,
                "tracking": tracking,
                "servicio": "DHL Express Worldwide",
                "label_pdf": label_pdf,
                "label_b64": label_b64,
                "invoice_pdf": invoice_pdf,
                "invoice_b64": invoice_b64,
                "message_reference": msg_ref,
            }
        except Exception as e:
            # La guía PUEDE existir aunque no podamos leer la respuesta.
            print(f"[dhl] respuesta ilegible tras emitir (ref {msg_ref}): {e}")
            return {"encontrado": False,
                    "error": "DHL respondió algo inesperado. Verificá en MyDHL "
                             "si la guía se emitió.",
                    "incierto": True, "message_reference": msg_ref}

    def track(self, tracking_number: str) -> dict:
        """Estado de un envío DHL. Devuelve {encontrado, estado, descripcion, eventos}."""
        tracking_number = str(tracking_number or "").strip()
        if not tracking_number:
            return {"encontrado": False, "error": "tracking vacío"}
        if error_config := self._error_configuracion():
            return {"encontrado": False, "error": error_config}
        try:
            resp = requests.get(
                f"{self.base_url}/shipments/{tracking_number}/tracking",
                params={"requestGMTOffsetPerEvent": "true"},
                auth=(self.api_key, self.api_secret),
                headers={"Accept": "application/json", "x-version": self.API_VERSION},
                timeout=30,
            )
            if resp.status_code != 200:
                return {
                    "encontrado": False,
                    "error": f"DHL {resp.status_code}",
                    "http_status": int(resp.status_code),
                }
            envios = (resp.json().get("shipments") or [])
            if not envios:
                return {"encontrado": False, "error": "Sin datos de tracking"}
            env = envios[0]
            eventos = env.get("events") or []
            # `shipments[].status` informa si la consulta fue exitosa; no es
            # el estado logístico. El avance real vive en el evento más
            # reciente. Elegimos por fecha/hora para no depender del orden de
            # la respuesta de DHL.
            def _clave_evento(par):
                indice, evento = par
                fecha = str(evento.get("date") or "")
                hora = str(evento.get("time") or "")
                timestamp = str(
                    evento.get("timestamp")
                    or evento.get("dateTime")
                    or evento.get("gmtDateTime")
                    or ""
                )
                if not timestamp:
                    offset = str(evento.get("GMTOffset") or "")
                    timestamp = f"{fecha}T{hora}{offset}"
                try:
                    instante = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                    ).timestamp()
                except (TypeError, ValueError):
                    instante = float("-inf")
                return (instante, timestamp, indice)

            ultimo_evento = None
            eventos_validos = [e for e in eventos if isinstance(e, dict)]
            if eventos_validos:
                ultimo_evento = max(
                    enumerate(eventos_validos), key=_clave_evento
                )[1]
            estado_evento = ""
            descripcion_evento = ""
            if ultimo_evento:
                estado_evento = str(
                    ultimo_evento.get("typeCode")
                    or ultimo_evento.get("statusCode")
                    or ultimo_evento.get("eventType")
                    or ""
                ).strip().upper()
                descripcion_evento = str(
                    ultimo_evento.get("description") or ""
                ).strip()
            return {
                "encontrado": True,
                "estado": estado_evento,
                "estado_consulta": str(env.get("status") or "").upper(),
                "descripcion": descripcion_evento,
                "eventos": eventos_validos,
                "ultimo_evento": ultimo_evento,
            }
        except Exception as e:
            return {"encontrado": False, "error": str(e)}

    # ── Recolecciones (MyDHL API POST /pickups) ──────────────────────────
    def create_pickup(self, datos: dict) -> dict:
        """
        Agenda una recolección en DHL. Mismo contrato de entrada/salida que
        FedExClient.create_pickup para que servicios/recolecciones.py los
        trate igual: {origen, fecha, ready_time, close_time, peso_kg, bultos,
        instrucciones} → {encontrado, confirmation_code, ubicacion, error}.

        El payload está calcado del ejemplo oficial que mandó DHL
        (Pickup.txt, 09/2025) — con NUESTRA cuenta, no la 741582719 del
        ejemplo, que es de un tercero. Ojo con la fecha: acá va con el offset
        local del ORIGEN (por ejemplo -03:00 AR, +08:00 CN), sin el literal
        "GMT" que usa /shipments. Son contratos distintos de DHL.
        """
        if error_config := self._error_configuracion():
            return {"encontrado": False, "error": error_config}

        origen = datos.get("origen") or {}
        requeridos_origen = {
            "nombre": origen.get("nombre"),
            "teléfono": origen.get("telefono"),
            "dirección": origen.get("calle"),
            "ciudad": origen.get("ciudad"),
            "país": origen.get("pais"),
        }
        faltan_origen = [campo for campo, valor in requeridos_origen.items()
                         if not str(valor or "").strip()]
        if faltan_origen:
            return {"encontrado": False, "error":
                    "Completá " + ", ".join(faltan_origen) +
                    " de la dirección de retiro antes de pedir DHL."}
        cuenta, error = self._cuenta_para(
            {"country": origen.get("pais", "AR")}, {"country": ""})
        if error:
            return {"encontrado": False, "error": error}

        fecha = (datos.get("fecha") or "").strip()
        ready = (datos.get("ready_time") or "09:00").strip()
        close = (datos.get("close_time") or "17:00").strip()

        zona, error_zona = self._zona_origen(origen.get("pais", "AR"))
        if error_zona:
            return {"encontrado": False, "error": error_zona}
        try:
            hora_local = datetime.strptime(
                f"{fecha} {ready}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=zona)
        except ValueError:
            return {"encontrado": False, "error": "Fecha u horario de retiro inválido."}
        offset = hora_local.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}"

        # Un retiro DHL siempre nace de una guía emitida. Ahí están las piezas,
        # medidas y declaración reales; inventarlas acá podría mandar a DHL un
        # retiro distinto del shipment que ya aceptó.
        paquetes_entrada = datos.get("paquetes")
        if not isinstance(paquetes_entrada, list) or not paquetes_entrada:
            return {"encontrado": False, "error":
                    "Para pedir un retiro DHL elegí una guía emitida con sus "
                    "cajas, medidas y valor declarado."}
        paquetes = []
        peso_total = 0.0
        for p in paquetes_entrada:
            if not isinstance(p, dict):
                return {"encontrado": False,
                        "error": "Revisá los datos de los bultos del retiro."}
            try:
                unidades = int(p.get("unidades") or p.get("cantidad"))
                peso_pieza = float(p.get("peso_kg") or p.get("weight"))
                largo = float(p.get("largo_cm") or p.get("largo"))
                ancho = float(p.get("ancho_cm") or p.get("ancho"))
                alto = float(p.get("alto_cm") or p.get("alto"))
            except (TypeError, ValueError):
                return {"encontrado": False,
                        "error": "Revisá la cantidad, el peso y las medidas de los bultos del retiro."}
            if unidades < 1 or len(paquetes) + unidades > MAX_DHL_PACKAGES:
                return {"encontrado": False,
                        "error": f"DHL admite como máximo {MAX_DHL_PACKAGES} bultos por retiro."}
            if not math.isfinite(peso_pieza) or not 0 < peso_pieza <= MAX_DHL_PACKAGE_KG:
                return {"encontrado": False,
                        "error": f"Cada bulto DHL debe pesar hasta {MAX_DHL_PACKAGE_KG:g} kg."}
            if any(not math.isfinite(v) or v <= 0 for v in (largo, ancho, alto)):
                return {"encontrado": False,
                        "error": "Las tres medidas de cada bulto DHL deben ser mayores a cero."}
            peso_total += peso_pieza * unidades
            if peso_total > MAX_DHL_PICKUP_KG:
                return {"encontrado": False,
                        "error": f"El retiro supera {MAX_DHL_PICKUP_KG:g} kg totales."}
            for _ in range(unidades):
                paquetes.append({
                    "weight": round(peso_pieza, 3),
                    "dimensions": {
                        "length": round(largo, 3),
                        "width": round(ancho, 3),
                        "height": round(alto, 3),
                    },
                })

        valor_declarado = datos.get("valor_declarado_usd")
        if valor_declarado in (None, "") and paquetes_entrada:
            try:
                valor_declarado = 0.0
                for p in paquetes_entrada:
                    unidades_aduana = int(p.get("unidades_aduana"))
                    valor_unitario = float(p.get("valor_unitario_usd"))
                    if not 1 <= unidades_aduana <= MAX_DHL_CUSTOMS_UNITS:
                        raise ValueError("unidades aduaneras fuera de rango")
                    if not math.isfinite(valor_unitario) or valor_unitario <= 0:
                        raise ValueError("valor unitario inválido")
                    valor_declarado += valor_unitario * unidades_aduana
            except (TypeError, ValueError):
                return {"encontrado": False,
                        "error": "Revisá el valor y las unidades aduaneras del retiro."}
        try:
            valor_declarado = float(valor_declarado)
        except (TypeError, ValueError):
            return {"encontrado": False,
                    "error": "Revisá el valor declarado de los bultos del retiro."}
        if not math.isfinite(valor_declarado) or valor_declarado <= 0:
            return {"encontrado": False,
                    "error": "El valor declarado del retiro debe ser mayor a cero."}

        cuerpo = {
            "plannedPickupDateAndTime": f"{fecha}T{ready}:00{offset}",
            "closeTime": close,
            "accounts": [{"typeCode": "shipper", "number": cuenta}],
            "customerDetails": {
                "shipperDetails": {
                    "postalAddress": {
                        "postalCode": self._codigo_postal(
                            origen.get("pais", "AR"), origen.get("zip") or ""
                        ),
                        "cityName": (origen.get("ciudad") or "").strip(),
                        "countryCode": (origen.get("pais") or "AR").upper()[:2],
                        "addressLine1": (origen.get("calle") or "")[:45],
                    },
                    "contactInformation": {
                        "phone": (origen.get("telefono") or "")[:25],
                        "companyName": (origen.get("empresa")
                                        or origen.get("nombre") or "N/A")[:60],
                        "fullName": (origen.get("nombre") or "N/A")[:45],
                    },
                },
            },
            "shipmentDetails": [{
                "productCode": self.product_code,
                "isCustomsDeclarable": True,
                # Obligatorios cuando isCustomsDeclarable=true. Son los mismos
                # datos reales de la guía: nunca un mínimo técnico inventado.
                "declaredValue": round(valor_declarado, 2),
                "declaredValueCurrency": "USD",
                "unitOfMeasurement": "metric",
                "packages": paquetes,
            }],
        }
        if (datos.get("instrucciones") or "").strip():
            cuerpo["specialInstructions"] = [
                {"value": str(datos["instrucciones"])[:75]}]

        msg_ref = str(
            datos.get("message_reference") or f"tauro-pickup-{uuid.uuid4().hex[:18]}"
        )[:36]
        try:
            resp = requests.post(
                f"{self.base_url}/pickups",
                json=cuerpo,
                auth=(self.api_key, self.api_secret),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-version": self.API_VERSION,
                    "Message-Reference": msg_ref,
                },
                timeout=30,
            )
        except Exception as e:
            print(f"[dhl] EXCEPCIÓN agendando pickup (ref {msg_ref}): {e}")
            return {"encontrado": False,
                    "error": "Error de comunicación con DHL al agendar. "
                             "Verificá en MyDHL antes de volver a pedirla.",
                    "incierto": True, "message_reference": msg_ref}

        if resp.status_code not in (200, 201):
            codigo = self._codigos_error(resp)
            print(f"[dhl] POST /pickups error HTTP {resp.status_code}; "
                  f"código={codigo}; ref={msg_ref}")
            # Igual que una guía, un retiro puede haberse creado aunque el
            # gateway termine respondiendo 408/5xx. Marcarlo como incierto
            # evita agendar dos recolecciones por un reintento automático.
            if resp.status_code == 408 or 500 <= resp.status_code <= 599:
                return {
                    "encontrado": False,
                    "error": "DHL no confirmó la recolección. Verificá en MyDHL "
                             "antes de volver a pedirla.",
                    "incierto": True,
                    "message_reference": msg_ref,
                }
            return {"encontrado": False, "error": self._error_legible(resp)}

        try:
            data = resp.json()
            codigos = data.get("dispatchConfirmationNumbers") or []
            if not codigos:
                return {"encontrado": False,
                        "error": "DHL no devolvió número de confirmación.",
                        "incierto": True, "message_reference": msg_ref}
            return {
                "encontrado": True,
                "confirmation_code": str(codigos[0]),
                # DHL no informa estación en la respuesta; se guarda vacío y
                # cancel_pickup no la necesita (usa sólo el código).
                "ubicacion": "",
                "message_reference": msg_ref,
            }
        except Exception as e:
            return {"encontrado": False,
                    "error": f"Respuesta de DHL ilegible: {e}. Verificá en MyDHL.",
                    "incierto": True, "message_reference": msg_ref}

    def cancel_pickup(self, confirmation_code: str, fecha: str = "",
                      ubicacion: str = "") -> dict:
        """
        Cancela una recolección. `fecha` y `ubicacion` existen para calzar la
        firma de FedEx; DHL cancela sólo con el código.

        Escrita contra la doc (DELETE /pickups/{código} + requestorName) y
        NUNCA ejecutada en vivo — la primera cancelación real hay que mirarla,
        mismo criterio que la primera guía.
        """
        if error_config := self._error_configuracion():
            return {"ok": False, "error": error_config}
        codigo = (confirmation_code or "").strip()
        if not codigo:
            return {"ok": False, "error": "Sin código de confirmación."}
        try:
            resp = requests.delete(
                f"{self.base_url}/pickups/{codigo}",
                params={"requestorName": "TAURO Solutions",
                        "reason": "001"},
                auth=(self.api_key, self.api_secret),
                headers={"Accept": "application/json",
                         "x-version": self.API_VERSION},
                timeout=30,
            )
        except Exception as e:
            return {"ok": False, "error": f"Error de comunicación con DHL: {e}"}

        if resp.status_code in (200, 202, 204):
            return {"ok": True}
        error_code = self._codigos_error(resp)
        print(f"[dhl] DELETE /pickups error HTTP {resp.status_code}; "
              f"código={error_code}")
        return {"ok": False, "error": self._error_legible(resp)}
