from __future__ import annotations

import base64
import os
import time
from datetime import date
import requests
from abc import ABC, abstractmethod

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(path: str | None = None):
        env_path = path or ".env"
        if not os.path.exists(env_path):
            return False
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return True

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
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(
                f"[fedex] No se pudo obtener token OAuth en ambiente {self.environment}. "
                f"Status {resp.status_code}: {resp.text[:500]}"
            )

        self._token = access_token
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
                            # Total del bulto: unitario × cantidad
                            "amount": round(
                                float(paquete.get("valor_declarado_usd", 100) or 100)
                                * max(int(paquete.get("unidades", 1) or 1), 1),
                                2,
                            ),
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
                                # Valor aduanero total: unitario × cantidad
                                "amount": round(
                                    float(paquete.get("valor_declarado_usd", 100) or 100)
                                    * max(int(paquete.get("unidades", 1) or 1), 1),
                                    2,
                                ),
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
            reply_details = data.get("output", {}).get("rateReplyDetails", [])
            if not reply_details:
                return {"encontrado": False, "error": "Sin tarifas en respuesta FedEx"}

            rated = reply_details[0].get("ratedShipmentDetails", []) or []
            if not rated:
                return {"encontrado": False, "error": "Sin tarifas en respuesta FedEx"}

            # FedEx devuelve tarifa LIST (precio de lista) y ACCOUNT (tu tarifa
            # negociada) en el mismo array, sin orden garantizado. Hay que elegir
            # ACCOUNT: es el costo real de Tauro y la base correcta del markup.
            rate_detail = next(
                (d for d in rated if "ACCOUNT" in (d.get("rateType") or "").upper()),
                rated[0],
            )
            # LIST = lo que pagaría un particular sin cuenta. Sirve para mostrarle
            # al cliente el ahorro real vs la tarifa pública de FedEx.
            list_detail = next(
                (d for d in rated if "LIST" in (d.get("rateType") or "").upper()
                 and d is not rate_detail),
                None,
            )

            total_net_charge = rate_detail.get("totalNetCharge")
            if total_net_charge is None:
                return {"encontrado": False, "error": "Sin tarifas en respuesta FedEx"}

            # totalNetCharge es un float directo (USD en sandbox, ARS en producción AR)
            costo = float(total_net_charge)
            moneda = rate_detail.get("currency", "USD")

            costo_lista = None
            if list_detail and list_detail.get("totalNetCharge") is not None:
                lista = float(list_detail["totalNetCharge"])
                if lista > 0:
                    costo_lista = lista

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
                "costo_lista": costo_lista,
                "moneda": moneda,
                "servicio": "INTERNATIONAL_PRIORITY",
                "dias_estimados": transit,
            }

        except Exception as e:
            print(f"[fedex] Excepción en get_rates: {e}")
            return {"encontrado": False, "error": str(e)}

    def create_shipment(self, datos: dict) -> dict:
        """
        Emite una guía real en FedEx (Ship API) y devuelve el tracking + el label
        en PDF (base64 y bytes). En sandbox el label sale con marca de agua TEST.

        datos esperado:
          {
            "shipper":   {nombre, empresa, telefono, calle, ciudad, estado, zip, pais},
            "recipient": {nombre, telefono, calle, ciudad, estado, zip, pais},
            "package":   {peso_kg, largo, ancho, alto},
            "commodity": {descripcion, hs_code, cantidad, valor_unitario_usd, pais_origen},
            "servicio":  "INTERNATIONAL_PRIORITY"   # opcional
          }

        Retorna {encontrado, tracking, servicio, label_pdf(bytes), label_b64, error}.
        """
        shipper = datos.get("shipper", {}) or {}
        recipient = datos.get("recipient", {}) or {}
        package = datos.get("package", {}) or {}
        commodity = datos.get("commodity", {}) or {}
        servicio = datos.get("servicio") or "INTERNATIONAL_PRIORITY"

        cantidad = max(int(commodity.get("cantidad", 1) or 1), 1)
        valor_unitario = float(commodity.get("valor_unitario_usd", 100) or 100)
        valor_total = round(valor_unitario * cantidad, 2)
        peso = float(package.get("peso_kg", 0.5) or 0.5)

        def _tel(v):
            return (str(v or "").strip() or "0000000000")

        payload = {
            "labelResponseOptions": "LABEL",
            "accountNumber": {"value": self.account_number},
            "requestedShipment": {
                "shipper": {
                    "contact": {
                        "personName": shipper.get("nombre", ""),
                        "phoneNumber": _tel(shipper.get("telefono")),
                        "companyName": shipper.get("empresa") or shipper.get("nombre", ""),
                    },
                    "address": {
                        "streetLines": [shipper.get("calle", "")],
                        "city": shipper.get("ciudad", ""),
                        "stateOrProvinceCode": shipper.get("estado", ""),
                        "postalCode": shipper.get("zip", ""),
                        "countryCode": shipper.get("pais", "AR"),
                    },
                },
                "recipients": [{
                    "contact": {
                        "personName": recipient.get("nombre", ""),
                        "phoneNumber": _tel(recipient.get("telefono")),
                    },
                    "address": {
                        "streetLines": [recipient.get("calle", "")],
                        "city": recipient.get("ciudad", ""),
                        "stateOrProvinceCode": recipient.get("estado", ""),
                        "postalCode": recipient.get("zip", ""),
                        "countryCode": recipient.get("pais", "US"),
                    },
                }],
                "shipDatestamp": date.today().isoformat(),
                "serviceType": servicio,
                "packagingType": "YOUR_PACKAGING",
                "pickupType": "DROPOFF_AT_FEDEX_LOCATION",
                "blockInsightVisibility": False,
                "shippingChargesPayment": {
                    "paymentType": "SENDER",
                    "payor": {"responsibleParty": {"accountNumber": {"value": self.account_number}}},
                },
                "labelSpecification": {
                    "imageType": "PDF",
                    "labelStockType": "PAPER_4X6",
                },
                "customsClearanceDetail": {
                    "dutiesPayment": {
                        "paymentType": "SENDER",
                        "payor": {"responsibleParty": {"accountNumber": {"value": self.account_number}}},
                    },
                    "commodities": [{
                        "description": commodity.get("descripcion", "Merchandise"),
                        "countryOfManufacture": commodity.get("pais_origen", "AR"),
                        "quantity": cantidad,
                        "quantityUnits": "PCS",
                        "unitPrice": {"amount": valor_unitario, "currency": "USD"},
                        "customsValue": {"amount": valor_total, "currency": "USD"},
                        "weight": {"units": "KG", "value": peso},
                        "harmonizedCode": commodity.get("hs_code", ""),
                    }],
                },
                "requestedPackageLineItems": [{
                    "weight": {"units": "KG", "value": peso},
                    "dimensions": {
                        "length": int(package.get("largo", 30)),
                        "width": int(package.get("ancho", 20)),
                        "height": int(package.get("alto", 10)),
                        "units": "CM",
                    },
                    "declaredValue": {"amount": valor_total, "currency": "USD"},
                }],
            },
        }

        url = f"{self.base_url}/ship/v1/shipments"
        try:
            resp = self._request_with_retry("POST", url, json=payload)
            data = resp.json() if resp.content else {}

            if resp.status_code not in (200, 201):
                msg = self._extraer_errores_fedex(data) or resp.text[:400]
                print(f"[fedex] create_shipment error {resp.status_code}: {msg}")
                return {"encontrado": False, "error": msg}

            transaction = (data.get("output", {}).get("transactionShipments") or [{}])[0]
            tracking = transaction.get("masterTrackingNumber", "")

            label_b64 = None
            for piece in transaction.get("pieceResponses", []) or []:
                for doc in piece.get("packageDocuments", []) or []:
                    if doc.get("encodedLabel"):
                        label_b64 = doc["encodedLabel"]
                        break
                if label_b64:
                    break

            if not tracking:
                return {"encontrado": False, "error": "FedEx no devolvió número de guía"}

            return {
                "encontrado": True,
                "tracking": tracking,
                "servicio": transaction.get("serviceName", servicio),
                "label_pdf": base64.b64decode(label_b64) if label_b64 else None,
                "label_b64": label_b64,
            }

        except Exception as e:
            print(f"[fedex] Excepción en create_shipment: {e}")
            return {"encontrado": False, "error": str(e)}

    @staticmethod
    def _extraer_errores_fedex(data: dict) -> str:
        """Junta los mensajes de error que devuelve la API de FedEx."""
        errores = (data or {}).get("errors") or []
        partes = []
        for err in errores:
            code = err.get("code", "")
            message = err.get("message", "")
            partes.append(f"{code}: {message}".strip(": "))
        return " | ".join(p for p in partes if p)

    def track_many(self, tracking_numbers: list[str]) -> dict:
        """
        Consulta el estado FedEx de varios trackings.

        Retorna un diccionario por tracking con el primer trackResult crudo de FedEx.
        La normalización del estado de negocio la hace servicios/tracking_fedex_tauro.py.
        """
        clean_numbers = []
        for number in tracking_numbers:
            text = str(number or "").strip()
            if text and text not in clean_numbers:
                clean_numbers.append(text)

        if not clean_numbers:
            return {}

        url = f"{self.base_url}/track/v1/trackingnumbers"
        payload = {
            "includeDetailedScans": True,
            "trackingInfo": [
                {"trackingNumberInfo": {"trackingNumber": number}}
                for number in clean_numbers
            ],
        }

        resp = self._request_with_retry(
            "POST",
            url,
            json=payload,
            headers={"Accept": "application/json", "X-locale": "es_AR"},
        )

        if resp.status_code != 200:
            raise RuntimeError(f"[fedex] track error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        output: dict[str, dict] = {}
        complete_results = data.get("output", {}).get("completeTrackResults", [])

        for complete in complete_results:
            tracking = str(complete.get("trackingNumber") or "").strip()
            track_results = complete.get("trackResults") or []
            if not tracking and track_results:
                tracking_info = track_results[0].get("trackingNumberInfo") or {}
                tracking = str(tracking_info.get("trackingNumber") or "").strip()
            if tracking:
                output[tracking] = track_results[0] if track_results else {
                    "error": {"message": "FedEx no devolvió trackResults"},
                }

        for tracking in clean_numbers:
            output.setdefault(
                tracking,
                {"error": {"message": "FedEx no devolvió resultado para este tracking"}},
            )

        return output

    def track(self, tracking_number: str) -> dict:
        """
        Consulta un único tracking FedEx.
        """
        tracking_number = str(tracking_number or "").strip()
        if not tracking_number:
            return {"error": "tracking_number vacío"}
        return self.track_many([tracking_number]).get(tracking_number, {})
