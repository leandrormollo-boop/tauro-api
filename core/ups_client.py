from __future__ import annotations

import os
import time
from datetime import datetime

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

    @staticmethod
    def _errores_respuesta(resp) -> tuple[str, str]:
        """Devuelve (detalle para UI, códigos seguros para logs)."""
        try:
            data = resp.json()
        except Exception:
            return "", "sin_codigo"
        errores = (data or {}).get("response", {}).get("errors") or []
        if isinstance(errores, dict):
            errores = [errores]
        mensajes = []
        codigos = []
        for error in errores:
            if not isinstance(error, dict):
                continue
            mensaje = str(error.get("message") or "").strip()
            codigo = str(error.get("code") or "").strip()
            if mensaje:
                mensajes.append(mensaje)
            if codigo:
                codigos.append(codigo)
        return "; ".join(mensajes)[:300], ",".join(codigos[:5]) or "sin_codigo"

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
                detalle, codigo = self._errores_respuesta(resp)
                print(f"[ups] get_rates error HTTP {resp.status_code}; código={codigo}")
                return {
                    "encontrado": False,
                    "error": detalle or f"UPS rechazó la cotización (HTTP {resp.status_code}).",
                }

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

    @staticmethod
    def _direccion_ups(d: dict) -> dict:
        """Bloque Address de la Shipping API (mismas claves que en Rate)."""
        return {
            "AddressLine": [(d.get("calle") or d.get("street") or "")[:35]],
            "City": (d.get("ciudad") or d.get("city") or "")[:30],
            "StateProvinceCode": (d.get("estado") or d.get("state") or "")[:5],
            "PostalCode": (d.get("zip") or d.get("postal_code") or "")[:9],
            "CountryCode": (d.get("pais") or d.get("country") or "AR")[:2].upper(),
        }

    def create_shipment(self, datos: dict) -> dict:
        """
        Emite una guía real en UPS (Shipping API) y devuelve tracking + label.

        Mismo contrato de entrada que FedEx y DHL — el despachador de emisión
        los trata igual:
          {"shipper": {...}, "recipient": {...}, "bultos": [...]} o
          {"package": {...}, "commodity": {...}}

        NO es idempotente: dos llamadas emiten dos guías facturadas.
        """
        if not (self.client_id and self.client_secret):
            return {"encontrado": False, "error": "Credenciales UPS no configuradas"}
        if not self.account_number:
            return {"encontrado": False, "error": "Falta UPS_ACCOUNT_NUMBER."}

        shipper = datos.get("shipper") or {}
        recipient = datos.get("recipient") or {}
        bultos = datos.get("bultos") or []
        if not bultos:
            package = datos.get("package") or {}
            commodity = datos.get("commodity") or {}
            bultos = [{
                "peso_kg": package.get("peso_kg", 0.5),
                "largo": package.get("largo", 30), "ancho": package.get("ancho", 20),
                "alto": package.get("alto", 10),
                "unidades": commodity.get("cantidad", 1),
                "valor_unitario_usd": commodity.get("valor_unitario_usd", 100),
                "descripcion_en": commodity.get("descripcion", "Merchandise"),
                "hs_code": commodity.get("hs_code", ""),
                "pais_origen": commodity.get("pais_origen", "AR"),
            }]

        # Una pieza por unidad, igual que FedEx y DHL.
        piezas, productos, valor_total = [], [], 0.0
        for b in bultos:
            unidades = max(int(b.get("unidades") or b.get("cantidad") or 1), 1)
            valor_u = float(b.get("valor_unitario_usd") or 0)
            for _ in range(unidades):
                piezas.append({
                    "Packaging": {"Code": "02"},
                    "Dimensions": {
                        "UnitOfMeasurement": {"Code": "CM"},
                        "Length": str(int(b.get("largo_cm") or b.get("largo") or 30)),
                        "Width": str(int(b.get("ancho_cm") or b.get("ancho") or 20)),
                        "Height": str(int(b.get("alto_cm") or b.get("alto") or 10)),
                    },
                    "PackageWeight": {
                        "UnitOfMeasurement": {"Code": "KGS"},
                        "Weight": str(round(float(b.get("peso_kg") or 0.5), 2)),
                    },
                })
            valor_total += valor_u * unidades
            productos.append({
                "Description": (b.get("descripcion_en") or "Merchandise")[:35],
                "Unit": {
                    "Number": str(unidades),
                    "UnitOfMeasurement": {"Code": "PCS"},
                    "Value": str(round(valor_u, 2)),
                },
                "CommodityCode": str(b.get("hs_code") or "").replace(".", "")[:15],
                "OriginCountryCode": (b.get("pais_origen") or "AR")[:2].upper(),
            })

        payload = {
            "ShipmentRequest": {
                "Request": {"SubVersion": "2403",
                            "RequestOption": "nonvalidate"},
                "Shipment": {
                    "Description": (productos[0]["Description"] if productos
                                    else "Merchandise")[:50],
                    "Shipper": {
                        "Name": (shipper.get("empresa") or shipper.get("nombre") or "TAURO")[:35],
                        "AttentionName": (shipper.get("nombre") or "TAURO")[:35],
                        "Phone": {"Number": (shipper.get("telefono") or "0000000000")[:15]},
                        "ShipperNumber": self.account_number,
                        "Address": self._direccion_ups(shipper),
                    },
                    "ShipFrom": {
                        "Name": (shipper.get("empresa") or shipper.get("nombre") or "TAURO")[:35],
                        "AttentionName": (shipper.get("nombre") or "TAURO")[:35],
                        "Phone": {"Number": (shipper.get("telefono") or "0000000000")[:15]},
                        "Address": self._direccion_ups(shipper),
                    },
                    "ShipTo": {
                        "Name": (recipient.get("empresa") or recipient.get("nombre") or "")[:35],
                        "AttentionName": (recipient.get("nombre") or "")[:35],
                        "Phone": {"Number": (recipient.get("telefono") or "0000000000")[:15]},
                        "Address": self._direccion_ups(recipient),
                    },
                    "PaymentInformation": {
                        "ShipmentCharge": {
                            "Type": "01",   # transporte
                            "BillShipper": {"AccountNumber": self.account_number},
                        }
                    },
                    "Service": {"Code": "07", "Description": "UPS Worldwide Express"},
                    "Package": piezas,
                    # Factura de aduana: sin esto un envío internacional con
                    # mercadería no despacha.
                    "ShipmentServiceOptions": {
                        "InternationalForms": {
                            "FormType": "01",   # invoice
                            "InvoiceDate": datetime.now().strftime("%Y%m%d"),
                            "ReasonForExport": "SALE",
                            "CurrencyCode": "USD",
                            "Product": productos,
                            "Contacts": {
                                "SoldTo": {
                                    "Name": (recipient.get("nombre") or "")[:35],
                                    "AttentionName": (recipient.get("nombre") or "")[:35],
                                    "Phone": {"Number": (recipient.get("telefono") or "0000000000")[:15]},
                                    "Address": self._direccion_ups(recipient),
                                }
                            },
                        }
                    },
                },
                "LabelSpecification": {
                    "LabelImageFormat": {"Code": "GIF"},
                    "HTTPUserAgent": "Mozilla/4.5",
                },
            }
        }

        try:
            token = self._get_token()
            resp = requests.post(
                f"{self.base_url}/api/shipments/v2403/ship",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "transId": "tauro-ship",
                    "transactionSrc": "tauro",
                },
                timeout=60,
            )
        except Exception as e:
            print(f"[ups] EXCEPCIÓN emitiendo: {e}. VERIFICAR en UPS si la guía "
                  f"salió antes de reintentar.")
            return {"encontrado": False,
                    "error": "Error de comunicación con UPS. Verificá en UPS "
                             "antes de reintentar."}

        if resp.status_code not in (200, 201):
            detalle, codigo = self._errores_respuesta(resp)
            print(f"[ups] create_shipment error HTTP {resp.status_code}; código={codigo}")
            return {
                "encontrado": False,
                "error": detalle or f"UPS rechazó la emisión (HTTP {resp.status_code}).",
            }

        try:
            res = resp.json()["ShipmentResponse"]["ShipmentResults"]
            tracking = str(res.get("ShipmentIdentificationNumber") or "")
            if not tracking:
                return {"encontrado": False, "error": "UPS no devolvió tracking."}

            # El label viene en base64 por paquete. UPS lo da en GIF: se
            # guarda tal cual — el sistema muestra el archivo, no exige PDF.
            paquetes = res.get("PackageResults") or []
            if isinstance(paquetes, dict):
                paquetes = [paquetes]
            label_b64 = ""
            if paquetes:
                label_b64 = (paquetes[0].get("ShippingLabel") or {}).get("GraphicImage") or ""
            label_bytes = None
            if label_b64:
                import base64
                try:
                    label_bytes = base64.b64decode(label_b64)
                except Exception as e:
                    print(f"[ups] guía {tracking} emitida pero el label no decodifica: {e}")

            print(f"[ups] guía emitida: {tracking}")
            return {
                "encontrado": True,
                "tracking": tracking,
                "servicio": "UPS Worldwide Express",
                "label_pdf": label_bytes,
                "label_b64": label_b64,
            }
        except Exception as e:
            print(f"[ups] respuesta ilegible tras emitir: {e}")
            return {"encontrado": False,
                    "error": "UPS respondió algo inesperado. Verificá en UPS si "
                             "la guía se emitió."}

    def track(self, tracking_number: str) -> dict:
        """Estado de un envío UPS. Devuelve {encontrado, estado, descripcion, eventos}."""
        tracking_number = str(tracking_number or "").strip()
        if not tracking_number:
            return {"encontrado": False, "error": "tracking vacío"}
        if not (self.client_id and self.client_secret):
            return {"encontrado": False, "error": "Credenciales UPS no configuradas"}
        try:
            token = self._get_token()
            resp = requests.get(
                f"{self.base_url}/api/track/v1/details/{tracking_number}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "transId": "tauro-track",
                    "transactionSrc": "tauro",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                return {"encontrado": False, "error": f"UPS {resp.status_code}"}
            paquetes = (resp.json().get("trackResponse", {})
                        .get("shipment", [{}])[0].get("package") or [])
            if not paquetes:
                return {"encontrado": False, "error": "Sin datos de tracking"}
            actividad = paquetes[0].get("activity") or []
            ultimo = actividad[0] if actividad else {}
            return {
                "encontrado": True,
                "estado": ((ultimo.get("status") or {}).get("type") or "").upper(),
                "descripcion": (ultimo.get("status") or {}).get("description") or "",
                "eventos": actividad,
                "ultimo_evento": ultimo or None,
            }
        except Exception as e:
            return {"encontrado": False, "error": str(e)}
