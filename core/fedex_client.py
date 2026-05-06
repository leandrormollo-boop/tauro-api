import os
import time
import requests
from abc import ABC, abstractmethod
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CLASE BASE — permite agregar DHL u otros carriers en Fase 2
# ─────────────────────────────────────────────

class CarrierBase(ABC):
    @abstractmethod
    def get_rates(self, origen: dict, destino: dict, paquete: dict) -> dict:
        pass

    @abstractmethod
    def create_shipment(self, datos: dict) -> dict:
        pass

    @abstractmethod
    def track(self, tracking_number: str) -> dict:
        pass


# ─────────────────────────────────────────────
# FEDEX CLIENT
# ─────────────────────────────────────────────

class FedExClient(CarrierBase):

    SANDBOX_URL = "https://apis-sandbox.fedex.com"
    PROD_URL = "https://apis.fedex.com"

    def __init__(self):
        self.api_key = os.getenv("FEDEX_API_KEY")
        self.secret_key = os.getenv("FEDEX_SECRET_KEY")
        self.account_number = os.getenv("FEDEX_ACCOUNT_NUMBER")
        self.environment = os.getenv("FEDEX_ENVIRONMENT", "sandbox").lower()
        self.base_url = self.SANDBOX_URL if self.environment == "sandbox" else self.PROD_URL

        # Cache del token OAuth2
        self._token: str | None = None
        self._token_expires_at: float = 0

    def _get_token(self) -> str:
        """
        Obtiene token OAuth2 de FedEx con caché.
        No hace una llamada nueva si el token sigue vigente.
        """
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        url = f"{self.base_url}/oauth/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        resp = self._request_with_retry("POST", url, data=payload, headers=headers, auth_call=True)
        data = resp.json()

        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        return self._token

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        auth_call: bool = False,
        **kwargs,
    ) -> requests.Response:
        """
        Ejecuta un request HTTP con retry exponencial.
        Reinicia en errores 5xx o de red.
        """
        for intento in range(max_retries):
            try:
                if not auth_call:
                    token = self._get_token()
                    headers = kwargs.pop("headers", {})
                    headers["Authorization"] = f"Bearer {token}"
                    headers["Content-Type"] = "application/json"
                    kwargs["headers"] = headers

                resp = requests.request(method, url, timeout=30, **kwargs)

                if resp.status_code < 500:
                    return resp

                print(f"[fedex] Error {resp.status_code} en intento {intento + 1}. Reintentando...")
                time.sleep(2 ** intento)

            except requests.exceptions.RequestException as e:
                print(f"[fedex] Error de red en intento {intento + 1}: {e}")
                if intento < max_retries - 1:
                    time.sleep(2 ** intento)
                else:
                    raise

        raise RuntimeError(f"[fedex] Máximo de reintentos alcanzado para {url}")

    def get_rates(self, origen: dict, destino: dict, paquete: dict) -> dict:
        """
        Consulta las tarifas de FedEx International Priority.

        origen: {
            "street": "Av. Corrientes 1234",
            "city": "BUENOS AIRES",
            "state": "B",
            "postal_code": "1043",
            "country": "AR"
        }

        destino: {
            "street": "123 Main St",
            "city": "New York",
            "state": "NY",
            "postal_code": "10001",
            "country": "US"
        }

        paquete: {
            "peso_kg": 0.5,
            "largo": 30,
            "ancho": 20,
            "alto": 10,
            "valor_declarado_usd": 150.0
        }

        Retorna: {
            "encontrado": True/False,
            "costo_ars": float,   ← FedEx Argentina cotiza en ARS
            "servicio": str,
            "dias_estimados": int
        }
        """
        url = f"{self.base_url}/rate/v1/rates/quotes"

        payload = {
            "accountNumber": {"value": self.account_number},
            "requestedShipment": {
                "shipper": {
                    "address": {
                        "streetLines": [origen.get("street", "")],
                        "city": origen.get("city", "BUENOS AIRES"),
                        "stateOrProvinceCode": origen.get("state", "B"),
                        "postalCode": origen.get("postal_code", "1043"),
                        "countryCode": origen.get("country", "AR"),
                    }
                },
                "recipient": {
                    "address": {
                        "streetLines": [destino.get("street", "")],
                        "city": destino.get("city", ""),
                        "stateOrProvinceCode": destino.get("state", ""),
                        "postalCode": destino.get("postal_code", ""),
                        "countryCode": destino.get("country", "US"),
                    }
                },
                "pickupType": "DROPOFF_AT_FEDEX_LOCATION",
                "serviceType": "INTERNATIONAL_PRIORITY",
                "packagingType": "YOUR_PACKAGING",
                "shippingChargesPayment": {
                    "paymentType": "SENDER",
                    "payor": {
                        "responsibleParty": {
                            "accountNumber": {"value": self.account_number}
                        }
                    },
                },
                "requestedPackageLineItems": [
                    {
                        "weight": {
                            "units": "KG",
                            "value": paquete.get("peso_kg", 0.5),
                        },
                        "dimensions": {
                            "length": int(paquete.get("largo", 30)),
                            "width": int(paquete.get("ancho", 20)),
                            "height": int(paquete.get("alto", 10)),
                            "units": "CM",
                        },
                        "declaredValue": {
                            "amount": paquete.get("valor_declarado_usd", 100),
                            "currency": "USD",
                        },
                    }
                ],
                "customsClearanceDetail": {
                    "dutiesPayment": {
                        "paymentType": "SENDER",
                        "payor": {"responsibleParty": {"accountNumber": {"value": self.account_number}}},
                    },
                    "commodities": [
                        {
                            "numberOfPieces": 1,
                            "description": paquete.get("descripcion_en", "Merchandise"),
                            "countryOfManufacture": "AR",
                            "harmonizedCode": paquete.get("hs_code", ""),
                            "quantity": paquete.get("unidades", 1),
                            "quantityUnits": "PCS",
                            "unitPrice": {
                                "amount": paquete.get("valor_declarado_usd", 100),
                                "currency": "USD",
                            },
                            "customsValue": {
                                "amount": paquete.get("valor_declarado_usd", 100),
                                "currency": "USD",
                            },
                            "weight": {"units": "KG", "value": paquete.get("peso_kg", 0.5)},
                        }
                    ],
                },
                "rateRequestType": ["LIST", "ACCOUNT"],
            },
        }

        try:
            resp = self._request_with_retry("POST", url, json=payload)

            if resp.status_code != 200:
                print(f"[fedex] get_rates error {resp.status_code}: {resp.text[:300]}")
                return {"encontrado": False, "error": resp.text}

            data = resp.json()
            rate_detail = (
                data.get("output", {})
                .get("rateReplyDetails", [{}])[0]
                .get("ratedShipmentDetails", [{}])[0]
            )

            if not rate_detail:
                return {"encontrado": False, "error": "Sin tarifas en respuesta FedEx"}

            total_net_charge = rate_detail.get("totalNetCharge")
            if total_net_charge is None:
                return {"encontrado": False, "error": "Sin tarifas en respuesta FedEx"}

            # totalNetCharge es un float directo (USD en sandbox, ARS en producción AR)
            costo = float(total_net_charge)
            moneda = rate_detail.get("currency", "USD")

            transit = (
                data.get("output", {})
                .get("rateReplyDetails", [{}])[0]
                .get("commit", {})
                .get("transitDays", {})
                .get("value", "3-5")
            )

            return {
                "encontrado": True,
                "costo": costo,
                "moneda": moneda,
                "servicio": "INTERNATIONAL_PRIORITY",
                "dias_estimados": transit,
            }

        except Exception as e:
            print(f"[fedex] Excepción en get_rates: {e}")
            return {"encontrado": False, "error": str(e)}

    def create_shipment(self, datos: dict) -> dict:
        """
        FASE 2 — No se usa en el MVP.
        En el MVP Tauro genera las guías manualmente con el PDF de pedido.
        """
        raise NotImplementedError(
            "create_shipment está reservado para Fase 2. "
            "En el MVP las guías se generan manualmente desde el portal FedEx."
        )

    def track(self, tracking_number: str) -> dict:
        """
        FASE 2 — No activo en MVP.
        """
        raise NotImplementedError("track está reservado para Fase 2.")
