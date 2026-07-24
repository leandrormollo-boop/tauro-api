from __future__ import annotations

import os
import requests
from dotenv import load_dotenv
from core.fedex_client import CarrierBase

load_dotenv()

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

    def __init__(self):
        self.api_key        = os.getenv("DHL_API_KEY")
        self.api_secret     = os.getenv("DHL_API_SECRET")
        self.account_number = os.getenv("DHL_ACCOUNT_NUMBER")
        self.environment    = os.getenv("DHL_ENVIRONMENT", "sandbox").lower()
        self.base_url       = self.SANDBOX_URL if self.environment == "sandbox" else self.PROD_URL

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

        try:
            url = f"{self.base_url}/rates"
            params = {
                "accountNumber": self.account_number,
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
                "plannedShippingDate": None,   # DHL usa fecha del día si se omite en algunos entornos
                "isCustomsDeclarable": "true",
                "unitOfMeasurement": "metric",
            }
            # Limpiar los None para no romper el querystring
            params = {k: v for k, v in params.items() if v is not None}

            resp = requests.get(
                url,
                params=params,
                auth=(self.api_key, self.api_secret),
                headers={"Accept": "application/json"},
                timeout=30,
            )

            if resp.status_code != 200:
                print(f"[dhl] get_rates error {resp.status_code}: {resp.text[:300]}")
                return {"encontrado": False, "error": resp.text}

            data = resp.json()
            productos = data.get("products", [])
            if not productos:
                return {"encontrado": False, "error": "Sin tarifas en respuesta DHL"}

            # Primer producto (típicamente EXPRESS WORLDWIDE). Tomamos el total facturado.
            prod = productos[0]
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
