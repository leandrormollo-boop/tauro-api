from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from core.fedex_client import CarrierBase

load_dotenv()

# Argentina es UTC-3 fijo y no tiene horario de verano, así que el offset va
# a mano en vez de con ZoneInfo: evita depender del paquete tzdata, que en
# las imágenes slim de Railway puede no estar y tiraría ZoneInfoNotFoundError
# en producción justo al cotizar.
TZ_AR = timezone(timedelta(hours=-3))

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
        self.environment    = os.getenv("DHL_ENVIRONMENT", "sandbox").lower()
        self.base_url       = self.SANDBOX_URL if self.environment == "sandbox" else self.PROD_URL
        self.product_code   = os.getenv("DHL_PRODUCT_CODE", self.PRODUCTO_DEFAULT)

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
        origen_ar  = (origen.get("country")  or "AR").upper() == "AR"
        destino_ar = (destino.get("country") or "").upper()   == "AR"

        # Importación: entra a Argentina desde afuera.
        if destino_ar and not origen_ar:
            if not self.account_import:
                return None, (
                    "Falta DHL_ACCOUNT_NUMBER_IMPORT: es una importación y no "
                    "se puede cotizar con la cuenta de exportación"
                )
            return self.account_import, None

        # Todo lo demás (incluido AR→AR) va por la de exportación.
        return self.account_number, None

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

        return {
            "encontrado": True,
            "costo": float(precios[0].get("price", 0)),
            "moneda": precios[0].get("priceCurrency", "USD"),
            "servicio": prod.get("productName", "DHL Express Worldwide"),
            "dias_estimados": str(
                prod.get("deliveryCapabilities", {}).get("totalTransitDays", "2-4")
            ),
        }

    def _fecha_envio(self) -> str:
        """
        plannedShippingDateAndTime para el POST: YYYY-MM-DDTHH:MM:SSGMT-03:00.

        SIN espacio antes de GMT. La spec se contradice consigo misma —los
        ejemplos de /rates van sin espacio, los mensajes de error de la misma
        spec lo muestran con espacio— así que se arranca con la forma del
        ejemplo de /rates, que es el endpoint que estamos llamando. Si DHL
        devuelve "not well formatted", probar con espacio antes de GMT.
        """
        d = datetime.now(TZ_AR) + timedelta(days=1)
        return d.strftime("%Y-%m-%dT13:00:00GMT-03:00")

    @staticmethod
    def _direccion(d: dict, por_defecto_ciudad: str = "") -> dict:
        """
        Bloque de dirección del POST. `cityName` tiene minLength 1: mandarlo
        vacío —como hacía el camino del GET con destino.get("city", "")— es
        un rechazo garantizado. Y los objetos van con additionalProperties
        false, así que sólo pueden ir estas claves.
        """
        ciudad = (d.get("city") or por_defecto_ciudad or "").strip()
        bloque = {
            "postalCode": (d.get("postal_code") or "").strip(),
            "cityName": ciudad,
            "countryCode": (d.get("country") or "").strip().upper(),
        }
        if d.get("state"):
            bloque["provinceCode"] = str(d["state"]).strip()
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
        if not self.api_key or not self.api_secret:
            return {"encontrado": False, "error": "Credenciales DHL no configuradas"}
        if not paquetes:
            return {"encontrado": False, "error": "Sin bultos para cotizar"}

        cuenta, error_cuenta = self._cuenta_para(origen, destino)
        if error_cuenta:
            print(f"[dhl] {error_cuenta}")
            return {"encontrado": False, "error": error_cuenta}

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
            "plannedShippingDateAndTime": self._fecha_envio(),
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
                print(f"[dhl] POST /rates error {resp.status_code} (ref {msg_ref}): "
                      f"{resp.text[:300]}")
                return {"encontrado": False, "error": resp.text}
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
        # Varias cajas → POST /rates, que es el único que las acepta.
        if paquetes is not None and len(paquetes) > 1:
            return self.get_rates_multibulto(origen, destino, paquetes)
        if paquetes:
            paquete = paquetes[0]

        if not self.api_key or not self.api_secret:
            return {"encontrado": False, "error": "Credenciales DHL no configuradas"}

        # Identificador propio de esta llamada. Cuando le reclames un error a
        # soporte de DHL te lo van a pedir para buscar el request de su lado,
        # así que viaja en el header y queda en nuestro log.
        msg_ref = str(uuid.uuid4())

        cuenta, error_cuenta = self._cuenta_para(origen, destino)
        if error_cuenta:
            print(f"[dhl] {error_cuenta}")
            return {"encontrado": False, "error": error_cuenta}

        try:
            url = f"{self.base_url}/rates"
            params = {
                "accountNumber": cuenta,
                "originCountryCode": origen.get("country", "AR"),
                "originCityName": origen.get("city", "BUENOS AIRES"),
                "originPostalCode": origen.get("postal_code", "1043"),
                "destinationCountryCode": destino.get("country", "US"),
                "destinationCityName": destino.get("city", ""),
                "destinationPostalCode": destino.get("postal_code", ""),
                "weight": paquete.get("peso_kg", 0.5),
                "length": int(paquete.get("largo", 30)),
                "width":  int(paquete.get("ancho", 20)),
                "height": int(paquete.get("alto", 10)),
                # OBLIGATORIO según el OpenAPI oficial (required: true). Antes
                # iba en None y el filtro de abajo lo borraba, con el comentario
                # "DHL usa la fecha del día si se omite" — la spec dice lo
                # contrario y sin este parámetro DHL contesta 400 SIEMPRE.
                # Formato YYYY-MM-DD, no ISO con hora.
                "plannedShippingDate": (
                    datetime.now(TZ_AR).date() + timedelta(days=1)
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
                print(f"[dhl] get_rates error {resp.status_code} (ref {msg_ref}): {resp.text[:300]}")
                return {"encontrado": False, "error": resp.text}

            return self._parsear_rates(resp.json())

        except Exception as e:
            print(f"[dhl] Excepción en get_rates: {e}")
            return {"encontrado": False, "error": str(e)}

    def create_shipment(self, datos: dict) -> dict:
        raise NotImplementedError("create_shipment DHL — Fase 2")

    def track(self, tracking_number: str) -> dict:
        raise NotImplementedError("track DHL — Fase 2")
