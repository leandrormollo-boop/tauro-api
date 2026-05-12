import os
import time
import requests
from dotenv import load_dotenv
from core.fedex_client import CarrierBase

load_dotenv()

# ─────────────────────────────────────────────
# UPS CLIENT
# Documentación: https://developer.ups.com/api/reference
# ─────────────────────────────────────────────

class UPSClient(CarrierBase):

    SANDBOX_URL = "https://wwwcie.ups.com"
    PROD_URL    = "https://onlinetools.ups.com"

    def __init__(self):
        self.client_id     = os.getenv("UPS_CLIENT_ID")
        self.client_secret = os.getenv("UPS_CLIENT_SECRET")
        self.account_number = os.getenv("UPS_ACCOUNT_NUMBER")
        self.environment   = os.getenv("UPS_ENVIRONMENT", "sandbox").lower()
        self.base_url      = self.SANDBOX_URL if self.environment == "sandbox" else self.PROD_URL

        self._token: str | None = None
        self._token_expires_at: float = 0

    def _get_token(self) -> str:
        """OAuth2 token con caché."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        url = f"{self.base_url}/security/v1/oauth/token"
        resp = requests.post(
            url,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def get_rates(self, origen: dict, destino: dict, paquete: dict) -> dict:
        """
        Consulta tarifas UPS Worldwide Express.

        Retorna el mismo contrato que FedExClient.get_rates():
        {
            "encontrado": True/False,
            "costo": float,
            "moneda": str,          ← "USD" o "ARS"
            "servicio": str,
            "dias_estimados": str,
        }
        """
        if not self.client_id or not self.client_secret:
            return {"encontrado": False, "error": "Credenciales UPS no configuradas"}

        try:
            token = self._get_token()
            url   = f"{self.base_url}/api/rating/v2403/Rate"

            payload = {
                "RateRequest": {
                    "Request": {"SubVersion": "2403"},
                    "Shipment": {
                        "Shipper": {
                            "ShipperNumber": self.account_number,
                            "Address": {
                                "AddressLine": [origen.get("street", "Av. Corrientes 1234")],
                                "City": origen.get("city", "BUENOS AIRES"),
                                "StateProvinceCode": origen.get("state", "B"),
                                "PostalCode": origen.get("postal_code", "1043"),
                                "CountryCode": origen.get("country", "AR"),
                            },
                        },
                        "ShipTo": {
                            "Address": {
                                "AddressLine": [destino.get("street", "")],
                                "City": destino.get("city", ""),
                                "StateProvinceCode": destino.get("state", ""),
                                "PostalCode": destino.get("postal_code", ""),
                                "CountryCode": destino.get("country", "US"),
                                "ResidentialAddressIndicator": "",
                            }
                        },
                        "ShipFrom": {
                            "Address": {
                                "AddressLine": [origen.get("street", "Av. Corrientes 1234")],
                                "City": origen.get("city", "BUENOS AIRES"),
                                "StateProvinceCode": origen.get("state", "B"),
                                "PostalCode": origen.get("postal_code", "1043"),
                                "CountryCode": origen.get("country", "AR"),
                            }
                        },
                        "Service": {"Code": "07", "Description": "UPS Worldwide Express"},
                        "Package": {
                            "PackagingType": {"Code": "02"},
                            "Dimensions": {
                                "UnitOfMeasurement": {"Code": "CM"},
                                "Length": str(int(paquete.get("largo", 30))),
                                "Width":  str(int(paquete.get("ancho", 20))),
                                "Height": str(int(paquete.get("alto", 10))),
                            },
                            "PackageWeight": {
                                "UnitOfMeasurement": {"Code": "KGS"},
                                "Weight": str(paquete.get("peso_kg", 0.5)),
                            },
                        },
                        "InvoiceLineTotal": {
                            "CurrencyCode": "USD",
                            "MonetaryValue": str(paquete.get("valor_declarado_usd", 100)),
                        },
                    },
                }
            }

            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "transId": "tauro-api",
                    "transactionSrc": "testing",
                },
                timeout=30,
            )

            if resp.status_code != 200:
                print(f"[ups] get_rates error {resp.status_code}: {resp.text[:300]}")
                return {"encontrado": False, "error": resp.text}

            data = resp.json()
            rated = data.get("RateResponse", {}).get("RatedShipment", {})
            if not rated:
                return {"encontrado": False, "error": "Sin tarifas en respuesta UPS"}

            total    = rated.get("TotalCharges", {})
            costo    = float(total.get("MonetaryValue", 0))
            moneda   = total.get("CurrencyCode", "USD")
            dias     = rated.get("GuaranteedDelivery", {}).get("BusinessDaysInTransit", "3-5")

            return {
                "encontrado": True,
                "costo": costo,
                "moneda": moneda,
                "servicio": "UPS_WORLDWIDE_EXPRESS",
                "dias_estimados": dias,
            }

        except Exception as e:
            print(f"[ups] Excepción en get_rates: {e}")
            return {"encontrado": False, "error": str(e)}

    def create_shipment(self, datos: dict) -> dict:
        raise NotImplementedError("create_shipment UPS — Fase 2")

    def track(self, tracking_number: str) -> dict:
        raise NotImplementedError("track UPS — Fase 2")
