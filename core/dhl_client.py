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
        # negociada. DHL_ACCOUNT_NUMBER es la de exportación (lo que sale de
        # Argentina) y DHL_ACCOUNT_NUMBER_IMPORT la de importación.
        self.account_number = os.getenv("DHL_ACCOUNT_NUMBER")
        self.account_import = os.getenv("DHL_ACCOUNT_NUMBER_IMPORT")
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

    def get_rates(self, origen: dict, destino: dict, paquete: dict) -> dict:
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

            data = resp.json()
            productos = data.get("products", [])
            if not productos:
                return {"encontrado": False, "error": "Sin tarifas en respuesta DHL"}

            # Elegir el producto POR CÓDIGO, nunca el primero de la lista.
            # GET /rates devuelve varios (Express Worldwide, 12:00, 9:00…) y no
            # garantiza el orden: agarrar el [0] podía cotizar un premium y
            # perder la venta, o —peor— cotizar uno más barato y despachar
            # después como Worldwide, con la factura de DHL más alta que lo
            # que ya le cobramos al cliente.
            # Se descartan los isCustomerAgreement: DHL sólo los da con un
            # acuerdo previo y no son los que vendemos.
            estandar = [p for p in productos if not p.get("isCustomerAgreement")]
            prod = next(
                (p for p in estandar if p.get("productCode") == self.product_code),
                None,
            )
            if prod is None:
                disponibles = [p.get("productCode") for p in estandar]
                return {
                    "encontrado": False,
                    "error": (
                        f"DHL no ofrece {self.product_code} en esa ruta "
                        f"(disponibles: {disponibles})"
                    ),
                }

            precios = prod.get("totalPrice", [])
            if not precios:
                return {"encontrado": False, "error": "Producto DHL sin precio"}

            costo  = float(precios[0].get("price", 0))
            moneda = precios[0].get("priceCurrency", "USD")
            dias   = str(prod.get("deliveryCapabilities", {}).get("totalTransitDays", "2-4"))

            return {
                "encontrado": True,
                "costo": costo,
                "moneda": moneda,
                "servicio": prod.get("productName", "DHL Express Worldwide"),
                "dias_estimados": dias,
            }

        except Exception as e:
            print(f"[dhl] Excepción en get_rates: {e}")
            return {"encontrado": False, "error": str(e)}

    def create_shipment(self, datos: dict) -> dict:
        raise NotImplementedError("create_shipment DHL — Fase 2")

    def track(self, tracking_number: str) -> dict:
        raise NotImplementedError("track DHL — Fase 2")
