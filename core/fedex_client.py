from __future__ import annotations

import base64
import hashlib
import math
import os
import threading
import time
import weakref
from collections import OrderedDict
from datetime import date
import requests
from abc import ABC, abstractmethod

from core.carrier_http import (
    QuoteBudget, QuoteDeadlineExceeded, quote_session,
)
from servicios.impuestos import paga_el_remitente

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
    # ¿Este courier sabe cotizar N cajas DISTINTAS en un mismo envío?
    #
    # Importa para plata: cada caja paga por su propio peso volumétrico
    # (L×A×H/5000), así que sumar los pesos y mandar un bulto equivalente
    # cotiza de MENOS — tres cajas grandes y livianas se convierten en una
    # chica y pesada. Los couriers que no lo soportan quedan fuera del
    # comparador cuando el envío tiene más de una caja, en vez de dar un
    # precio que después no es el que facturan.
    MULTIBULTO = False

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

    # get_rates acepta paquetes=[...] y FedEx tarifa las N piezas juntas.
    MULTIBULTO = True

    SANDBOX_URL = "https://apis-sandbox.fedex.com"
    PROD_URL = "https://apis.fedex.com"

    # Caché de proceso: los clientes se crean por cotización, por eso un caché
    # de instancia desperdicia un OAuth por courier/request. La clave sólo
    # conserva un hash del ambiente y las credenciales, nunca los secretos.
    _TOKEN_CACHE_MAX = 32
    _TOKEN_EXPIRY_MARGIN_SECONDS = 60
    _token_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
    # Este lock protege únicamente los mapas en memoria. La red OAuth corre
    # bajo un single-flight por clave, para que una cuenta lenta no bloquee a
    # otra con credenciales diferentes.
    _token_cache_lock = threading.RLock()
    _token_key_locks = weakref.WeakValueDictionary()

    def __init__(self):
        self.api_key = os.getenv("FEDEX_API_KEY")
        self.secret_key = os.getenv("FEDEX_SECRET_KEY")
        self.account_number = os.getenv("FEDEX_ACCOUNT_NUMBER")
        self.environment = os.getenv("FEDEX_ENVIRONMENT", "sandbox").lower()
        self.base_url = self.SANDBOX_URL if self.environment == "sandbox" else self.PROD_URL

    def _token_cache_key(self) -> str:
        material = "\0".join((
            self.environment or "",
            self.base_url or "",
            self.api_key or "",
            self.secret_key or "",
        )).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @classmethod
    def _clear_token_cache_for_tests(cls) -> None:
        with cls._token_cache_lock:
            cls._token_cache.clear()
            cls._token_key_locks.clear()

    @classmethod
    def _token_lock_for_key(cls, cache_key: str):
        with cls._token_cache_lock:
            lock = cls._token_key_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                cls._token_key_locks[cache_key] = lock
            return lock

    @classmethod
    def _cached_token(
        cls, cache_key: str, *, stale_token: str | None = None,
    ) -> str | None:
        """Lee/LRU/invalida con una sección global corta y sin hacer red."""
        now = time.monotonic()
        with cls._token_cache_lock:
            cached = cls._token_cache.get(cache_key)
            if not cached:
                return None
            token, expires_at = cached
            vigente = now < expires_at - cls._TOKEN_EXPIRY_MARGIN_SECONDS
            if vigente and (stale_token is None or token != stale_token):
                cls._token_cache.move_to_end(cache_key)
                return token
            cls._token_cache.pop(cache_key, None)
            return None

    def _invalidate_cached_token(self, rejected_token: str) -> None:
        """Quita sólo el bearer rechazado; no pisa una renovación concurrente."""
        cache_key = self._token_cache_key()
        with self._token_cache_lock:
            cached = self._token_cache.get(cache_key)
            if cached and cached[0] == rejected_token:
                self._token_cache.pop(cache_key, None)

    def _get_token(
        self, *, quote_read: bool = False, stale_token: str | None = None,
        quote_budget: QuoteBudget | None = None,
    ) -> str:
        """
        Obtiene token OAuth2 de FedEx con caché.
        No hace una llamada nueva si el token sigue vigente.
        """
        cache_key = self._token_cache_key()
        if quote_budget:
            quote_budget.ensure_remaining()
        cached = self._cached_token(cache_key, stale_token=stale_token)
        if cached:
            return cached

        # Dos instancias con la misma clave comparten la renovación; cuentas
        # distintas usan locks distintos y pueden autenticar en paralelo.
        lock = self._token_lock_for_key(cache_key)
        if quote_budget:
            acquired = lock.acquire(timeout=quote_budget.ensure_remaining())
            if not acquired:
                raise QuoteDeadlineExceeded(
                    "Se agotó el tiempo total de consulta de tarifa."
                )
        else:
            lock.acquire()
            acquired = True
        try:
            # El lock pudo quedar libre justo al final del presupuesto.
            if quote_budget:
                quote_budget.ensure_remaining()
            cached = self._cached_token(cache_key, stale_token=stale_token)
            if cached:
                # Tras un 401, otro thread puede haber renovado mientras éste
                # esperaba el single-flight. En ese caso reutilizamos el nuevo.
                return cached

            url = f"{self.base_url}/oauth/token"
            payload = {
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.secret_key,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            resp = self._request_with_retry(
                "POST", url, data=payload, headers=headers, auth_call=True,
                quote_read=quote_read, quote_budget=quote_budget,
            )
            data = resp.json()
            access_token = data.get("access_token")
            if not access_token:
                codigo = self._codigos_errores_fedex(data)
                raise RuntimeError(
                    f"[fedex] No se pudo obtener token OAuth en ambiente {self.environment}. "
                    f"HTTP {resp.status_code}; código={codigo}"
                )

            try:
                expires_in = float(data.get("expires_in", 3600))
                if not math.isfinite(expires_in):
                    expires_in = 3600.0
                expires_in = max(expires_in, 0.0)
            except (TypeError, ValueError):
                expires_in = 3600.0
            with self._token_cache_lock:
                self._token_cache[cache_key] = (
                    str(access_token), time.monotonic() + expires_in,
                )
                self._token_cache.move_to_end(cache_key)
                while len(self._token_cache) > self._TOKEN_CACHE_MAX:
                    self._token_cache.popitem(last=False)
            return str(access_token)
        finally:
            if acquired:
                lock.release()

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        auth_call: bool = False,
        quote_read: bool = False,
        quote_budget: QuoteBudget | None = None,
        **kwargs,
    ) -> requests.Response:
        """
        Ejecuta un request HTTP con retry exponencial.
        Reinicia en errores 5xx o de red.
        """
        stale_token = None
        retried_401 = False
        if quote_read and quote_budget is None:
            quote_budget = QuoteBudget.start()
        for intento in range(max_retries):
            try:
                while True:
                    request_kwargs = dict(kwargs)
                    if not auth_call:
                        token = self._get_token(
                            quote_read=quote_read, stale_token=stale_token,
                            quote_budget=quote_budget,
                        )
                        stale_token = None
                        headers = dict(request_kwargs.pop("headers", {}))
                        headers["Authorization"] = f"Bearer {token}"
                        headers["Content-Type"] = "application/json"
                        request_kwargs["headers"] = headers

                    requester = quote_session() if quote_read else requests
                    timeout = quote_budget.timeout() if quote_budget else 30
                    resp = requester.request(
                        method, url, timeout=timeout, **request_kwargs,
                    )

                    # Sólo Rate API puede repetir de forma segura tras renovar
                    # el bearer. No consume un retry de red/5xx.
                    if (
                        quote_read and not auth_call and resp.status_code == 401
                        and not retried_401
                    ):
                        retried_401 = True
                        stale_token = token
                        continue
                    if not auth_call and resp.status_code == 401:
                        # Ship/pickup/cancel jamás se repiten. Invalidar el
                        # bearer hace que la próxima operación autentique de
                        # nuevo, sin borrar un token que otro thread ya renovó.
                        self._invalidate_cached_token(token)
                    break

                if resp.status_code < 500:
                    return resp

                print(f"[fedex] Error {resp.status_code} en intento {intento + 1}.")
                if intento < max_retries - 1:
                    if quote_budget:
                        quote_budget.sleep_before_retry(2 ** intento)
                    else:
                        time.sleep(2 ** intento)

            except requests.exceptions.RequestException as e:
                print(f"[fedex] Error de red en intento {intento + 1}: {e}")
                if isinstance(e, QuoteDeadlineExceeded):
                    raise
                if intento < max_retries - 1:
                    if quote_budget:
                        quote_budget.sleep_before_retry(2 ** intento)
                    else:
                        time.sleep(2 ** intento)
                else:
                    raise

        raise RuntimeError(f"[fedex] Máximo de reintentos alcanzado para {url}")

    @staticmethod
    def _piezas_fedex(paquetes: list[dict]) -> tuple[list[dict], list[dict], int]:
        """
        Convierte la lista de bultos en (requestedPackageLineItems, commodities,
        totalPackageCount) para Rate y Ship API.

        Cada bulto: {peso_kg (por caja), largo, ancho, alto,
                     valor_unitario_usd (por caja), unidades (cajas idénticas),
                     hs_code, descripcion_en, pais_origen}
        Una fila con unidades=N viaja como N piezas idénticas
        (groupPackageCount) — cada caja con su propio label.
        """
        line_items, commodities, total = [], [], 0
        for p in paquetes:
            unidades = max(int(p.get("unidades", 1) or 1), 1)
            peso = float(p.get("peso_kg", 0.5) or 0.5)
            valor_unitario = round(float(p.get("valor_unitario_usd", p.get("valor_declarado_usd", 100)) or 100), 2)
            total += unidades
            line_items.append({
                "groupPackageCount": unidades,
                "weight": {"units": "KG", "value": peso},
                "dimensions": {
                    "length": int(p.get("largo", 30)),
                    "width": int(p.get("ancho", 20)),
                    "height": int(p.get("alto", 10)),
                    "units": "CM",
                },
                # declaredValue es POR PIEZA (FedEx lo aplica a cada caja del grupo)
                "declaredValue": {"amount": valor_unitario, "currency": "USD"},
            })
            commodities.append({
                "numberOfPieces": unidades,
                "description": p.get("descripcion_en") or p.get("descripcion") or "Merchandise",
                "countryOfManufacture": p.get("pais_origen", "AR"),
                "harmonizedCode": p.get("hs_code", ""),
                "quantity": unidades,
                "quantityUnits": "PCS",
                "unitPrice": {"amount": valor_unitario, "currency": "USD"},
                "customsValue": {
                    "amount": round(valor_unitario * unidades, 2),
                    "currency": "USD",
                },
                "weight": {"units": "KG", "value": round(peso * unidades, 2)},
            })
        return line_items, commodities, total

    def get_rates(
        self, origen: dict, destino: dict, paquete: dict | None = None,
        todos_los_servicios: bool = False,
        paquetes: list[dict] | None = None,
    ) -> dict:
        """
        Consulta las tarifas de FedEx International Priority.
        Con todos_los_servicios=True omite el serviceType y FedEx devuelve
        TODAS las opciones disponibles para la ruta (Priority, Economy, etc.)
        en la clave extra "opciones": [{servicio, servicio_nombre, costo,
        costo_lista, moneda, dias_estimados}, ...]. Las claves de siempre
        (costo, moneda, ...) traen la opción más barata para retrocompat.

        Con paquetes=[{...}, ...] (multi-bulto) cotiza N piezas en un solo
        envío — ver _piezas_fedex para el formato de cada bulto. `paquete`
        (singular) mantiene el comportamiento histórico de un solo bulto.

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

        if paquetes:
            # Multi-bulto: N piezas en un solo envío.
            line_items, commodities, _total = self._piezas_fedex(paquetes)
        else:
            # Retrocompat: un solo bulto que contiene `unidades` unidades
            # (peso y valor ya vienen totalizados por el caller).
            paquete = paquete or {}
            valor_total = round(
                float(paquete.get("valor_declarado_usd", 100) or 100)
                * max(int(paquete.get("unidades", 1) or 1), 1),
                2,
            )
            line_items = [{
                "weight": {"units": "KG", "value": paquete.get("peso_kg", 0.5)},
                "dimensions": {
                    "length": int(paquete.get("largo", 30)),
                    "width": int(paquete.get("ancho", 20)),
                    "height": int(paquete.get("alto", 10)),
                    "units": "CM",
                },
                "declaredValue": {"amount": valor_total, "currency": "USD"},
            }]
            commodities = [{
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
                "customsValue": {"amount": valor_total, "currency": "USD"},
                "weight": {"units": "KG", "value": paquete.get("peso_kg", 0.5)},
            }]

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
                "packagingType": "YOUR_PACKAGING",
                "shippingChargesPayment": {
                    "paymentType": "SENDER",
                    "payor": {
                        "responsibleParty": {
                            "accountNumber": {"value": self.account_number}
                        }
                    },
                },
                "requestedPackageLineItems": line_items,
                "customsClearanceDetail": {
                    "dutiesPayment": {
                        "paymentType": "SENDER",
                        "payor": {"responsibleParty": {"accountNumber": {"value": self.account_number}}},
                    },
                    "commodities": commodities,
                },
                "rateRequestType": ["LIST", "ACCOUNT"],
            },
        }
        if not todos_los_servicios:
            payload["requestedShipment"]["serviceType"] = "INTERNATIONAL_PRIORITY"

        try:
            quote_budget = QuoteBudget.start()
            resp = self._request_with_retry(
                "POST", url, json=payload, quote_read=True,
                quote_budget=quote_budget,
            )

            if resp.status_code != 200:
                try:
                    error_data = resp.json()
                except Exception:
                    error_data = {}
                codigo = self._codigos_errores_fedex(error_data)
                print(f"[fedex] get_rates error HTTP {resp.status_code}; código={codigo}")
                detalle = self._extraer_errores_fedex(error_data)
                return {
                    "encontrado": False,
                    "error": detalle or f"FedEx rechazó la cotización (HTTP {resp.status_code}).",
                }

            data = resp.json()
            reply_details = data.get("output", {}).get("rateReplyDetails", [])
            if not reply_details:
                return {"encontrado": False, "error": "Sin tarifas en respuesta FedEx"}

            def _parse_reply(detail: dict):
                """Extrae costo ACCOUNT + LIST de un rateReplyDetail. None si no sirve."""
                rated = detail.get("ratedShipmentDetails", []) or []
                if not rated:
                    return None
                # FedEx devuelve LIST (precio de lista) y ACCOUNT (tarifa negociada)
                # sin orden garantizado: ACCOUNT es el costo real de Tauro.
                rate_detail = next(
                    (d for d in rated if "ACCOUNT" in (d.get("rateType") or "").upper()),
                    rated[0],
                )
                list_detail = next(
                    (d for d in rated if "LIST" in (d.get("rateType") or "").upper()
                     and d is not rate_detail),
                    None,
                )
                total = rate_detail.get("totalNetCharge")
                if total is None:
                    return None
                costo_lista = None
                if list_detail and list_detail.get("totalNetCharge") is not None:
                    lista = float(list_detail["totalNetCharge"])
                    if lista > 0:
                        costo_lista = lista
                transit = ((detail.get("commit") or {}).get("transitDays") or {}).get("value") or "A confirmar"
                return {
                    "servicio": detail.get("serviceType", "INTERNATIONAL_PRIORITY"),
                    "servicio_nombre": detail.get("serviceName")
                        or str(detail.get("serviceType", "")).replace("_", " ").title(),
                    "costo": float(total),
                    "costo_lista": costo_lista,
                    "moneda": rate_detail.get("currency", "USD"),
                    "dias_estimados": transit,
                }

            opciones = [o for o in (_parse_reply(d) for d in reply_details) if o]
            if not opciones:
                return {"encontrado": False, "error": "Sin tarifas en respuesta FedEx"}
            opciones.sort(key=lambda o: o["costo"])
            mejor = opciones[0]

            resultado = {
                "encontrado": True,
                "costo": mejor["costo"],
                "costo_lista": mejor["costo_lista"],
                "moneda": mejor["moneda"],
                "servicio": mejor["servicio"],
                "dias_estimados": mejor["dias_estimados"],
            }
            if todos_los_servicios:
                resultado["opciones"] = opciones
            return resultado

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

        MULTI-BULTO: en vez de package+commodity se puede pasar
            "bultos": [{peso_kg, largo, ancho, alto, valor_unitario_usd,
                        unidades, hs_code, descripcion_en, pais_origen}, ...]
        y el envío sale con N piezas (una caja por unidad, cada una con su
        label). El PDF devuelto une los labels de todas las piezas.

        Retorna {encontrado, tracking, servicio, label_pdf(bytes), label_b64, error}.
        """
        if not (self.api_key and self.secret_key and self.account_number):
            return {
                "encontrado": False,
                "rechazo_confirmado": True,
                "error": "Credenciales FedEx no configuradas.",
            }

        shipper = datos.get("shipper", {}) or {}
        recipient = datos.get("recipient", {}) or {}
        bultos = datos.get("bultos") or []
        package = datos.get("package", {}) or {}
        commodity = datos.get("commodity", {}) or {}
        servicio = datos.get("servicio") or "INTERNATIONAL_PRIORITY"

        if bultos:
            line_items, commodities, _total = self._piezas_fedex(bultos)
        else:
            cantidad = max(int(commodity.get("cantidad", 1) or 1), 1)
            valor_unitario = float(commodity.get("valor_unitario_usd", 100) or 100)
            valor_total = round(valor_unitario * cantidad, 2)
            peso = float(package.get("peso_kg", 0.5) or 0.5)
            line_items = [{
                "weight": {"units": "KG", "value": peso},
                "dimensions": {
                    "length": int(package.get("largo", 30)),
                    "width": int(package.get("ancho", 20)),
                    "height": int(package.get("alto", 10)),
                    "units": "CM",
                },
                "declaredValue": {"amount": valor_total, "currency": "USD"},
            }]
            commodities = [{
                "description": commodity.get("descripcion", "Merchandise"),
                "countryOfManufacture": commodity.get("pais_origen", "AR"),
                "quantity": cantidad,
                "quantityUnits": "PCS",
                "unitPrice": {"amount": valor_unitario, "currency": "USD"},
                "customsValue": {"amount": valor_total, "currency": "USD"},
                "weight": {"units": "KG", "value": peso},
                "harmonizedCode": commodity.get("hs_code", ""),
            }]

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
                # ── QUIÉN PAGA LOS IMPUESTOS DE DESTINO ─────────────
                # Lo elige el CLIENTE (regla de Leandro 01/08/2026), no es
                # una constante. Hasta hoy esto decía SENDER fijo con la
                # cuenta de TAURO: pagábamos los derechos de TODOS los
                # envíos, incluidas importaciones de mercadería ajena. Con
                # EE.UU. pesa fuerte — se eliminó la franquicia de USD 800
                # y hoy toda caja paga arancel.
                #   RECIPIENT = los abona quien recibe (DAP)
                #   SENDER    = los prepagamos y se los facturamos (DDP)
                "customsClearanceDetail": {
                    "dutiesPayment": (
                        {
                            "paymentType": "SENDER",
                            "payor": {"responsibleParty": {"accountNumber": {"value": self.account_number}}},
                        }
                        if paga_el_remitente(datos.get("tax_paga"))
                        else {"paymentType": "RECIPIENT"}
                    ),
                    "commodities": commodities,
                },
                "requestedPackageLineItems": line_items,
            },
        }

        url = f"{self.base_url}/ship/v1/shipments"
        try:
            # SIN REINTENTOS. Emitir una guía NO es idempotente: si FedEx
            # ya creó el envío y la respuesta se pierde (timeout, 502),
            # repetir el POST crea un SEGUNDO envío real, con su etiqueta
            # y su declaración de aduana. Preferimos fallar y que un humano
            # verifique en fedex.com antes que duplicar en silencio.
            resp = self._request_with_retry("POST", url, json=payload, max_retries=1)
            data = resp.json() if resp.content else {}

            if resp.status_code not in (200, 201):
                codigo = self._codigos_errores_fedex(data)
                msg = self._extraer_errores_fedex(data) or (
                    f"FedEx rechazó la emisión (HTTP {resp.status_code})."
                )
                print(f"[fedex] create_shipment error HTTP {resp.status_code}; código={codigo}")
                return {
                    "encontrado": False,
                    "error": msg,
                    # Un 5xx puede ocurrir después de que el proveedor haya
                    # aceptado el POST. 408/409 no prueban ausencia de emisión.
                    "incierto": resp.status_code not in {400, 401, 403, 404, 422, 429},
                    "rechazo_confirmado": resp.status_code in {400, 401, 403, 404, 422, 429},
                }

            transaction = (data.get("output", {}).get("transactionShipments") or [{}])[0]
            tracking = transaction.get("masterTrackingNumber", "")

            # Un label por pieza: los juntamos todos en un solo PDF
            # (multi-bulto → una hoja por caja para imprimir de una).
            labels_pdf = []
            for piece in transaction.get("pieceResponses", []) or []:
                for doc in piece.get("packageDocuments", []) or []:
                    if doc.get("encodedLabel"):
                        try:
                            labels_pdf.append(base64.b64decode(doc["encodedLabel"]))
                        except Exception:
                            pass
                        break

            if not tracking:
                return {
                    "encontrado": False,
                    "incierto": True,
                    "error": "FedEx respondió sin número de guía",
                }

            label_pdf = self._merge_pdfs(labels_pdf)
            return {
                "encontrado": True,
                "tracking": tracking,
                "servicio": transaction.get("serviceName", servicio),
                "label_pdf": label_pdf,
                "label_b64": base64.b64encode(label_pdf).decode() if label_pdf else None,
                "piezas": len(labels_pdf),
            }

        except Exception as e:
            print(f"[fedex] Excepción en create_shipment: {e}")
            return {"encontrado": False, "incierto": True, "error": str(e)}

    @staticmethod
    def _merge_pdfs(pdfs: list[bytes]) -> bytes | None:
        """Une varios PDFs (un label por pieza) en uno solo. Con un solo PDF
        lo devuelve tal cual; si pypdf no está disponible, devuelve el primero
        para no romper la emisión (mejor un label que ninguno)."""
        pdfs = [p for p in pdfs if p]
        if not pdfs:
            return None
        if len(pdfs) == 1:
            return pdfs[0]
        try:
            import io
            from pypdf import PdfReader, PdfWriter
            writer = PdfWriter()
            for blob in pdfs:
                for page in PdfReader(io.BytesIO(blob)).pages:
                    writer.add_page(page)
            out = io.BytesIO()
            writer.write(out)
            return out.getvalue()
        except Exception as e:
            print(f"[fedex] No se pudieron unir los labels ({e}); guardo el primero.")
            return pdfs[0]

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

    @staticmethod
    def _codigos_errores_fedex(data: dict) -> str:
        """Resumen seguro para logs: nunca incluye mensajes ni valores del body."""
        errores = (data or {}).get("errors") or []
        codigos = [str(e.get("code") or "").strip() for e in errores if isinstance(e, dict)]
        return ",".join(c for c in codigos[:5] if c) or "sin_codigo"

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
            try:
                error_data = resp.json()
            except Exception:
                error_data = {}
            codigo = self._codigos_errores_fedex(error_data)
            raise RuntimeError(
                f"[fedex] track error HTTP {resp.status_code}; código={codigo}"
            )

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

    # ── Recolecciones (Pickup API) ──────────────────────────────
    # El chofer pasa a buscar los paquetes en vez de que el cliente los
    # lleve a una sucursal. Reglas de FedEx que definen el diseño:
    #   - readyTime  = desde cuándo están listos (default 09:00)
    #   - closeTime  = hasta qué hora puede entrar el chofer (default 14:00)
    #   - closeTime tiene que ser POSTERIOR a readyTime y anterior al corte
    #     del código postal; entre ambos tiene que haber tiempo suficiente
    #     ("access time"), por eso se exige un mínimo de 2 horas.
    # Ref: https://developer.fedex.com/api/en-us/catalog/pickup.html

    PICKUP_MIN_HORAS = 2

    def _direccion_pickup(self, d: dict) -> dict:
        return {
            "address": {
                "streetLines": [d.get("calle", "")][:2],
                "city": d.get("ciudad", ""),
                "stateOrProvinceCode": d.get("estado", ""),
                "postalCode": d.get("zip", ""),
                "countryCode": d.get("pais", "AR"),
                "residential": False,
            },
            "contact": {
                "personName": d.get("nombre", ""),
                "companyName": d.get("empresa", "") or d.get("nombre", ""),
                "phoneNumber": d.get("telefono", ""),
            },
        }

    def create_pickup(self, datos: dict) -> dict:
        """
        Agenda una recolección. datos:
          {
            "origen": {nombre, empresa, telefono, calle, ciudad, estado, zip, pais},
            "fecha": "YYYY-MM-DD",
            "ready_time": "09:00", "close_time": "17:00",
            "peso_kg": 5.0, "bultos": 2,
            "instrucciones": "Timbre 3B",
          }
        Devuelve {encontrado, confirmation_code, ubicacion, error}.

        NO es idempotente: dos llamadas agendan dos visitas del chofer. Quien
        llame tiene que traer su propia reserva (igual que la emisión).
        """
        origen = datos.get("origen") or {}
        fecha = str(datos.get("fecha") or "").strip()
        ready = str(datos.get("ready_time") or "09:00").strip()
        close = str(datos.get("close_time") or "17:00").strip()

        if not fecha:
            return {"encontrado": False, "error": "Falta la fecha de la recolección."}
        try:
            h_ready = int(ready.split(":")[0]) * 60 + int(ready.split(":")[1])
            h_close = int(close.split(":")[0]) * 60 + int(close.split(":")[1])
        except (ValueError, IndexError):
            return {"encontrado": False, "error": "Horarios inválidos (formato HH:MM)."}
        if h_close - h_ready < self.PICKUP_MIN_HORAS * 60:
            return {"encontrado": False,
                    "error": f"Tiene que haber al menos {self.PICKUP_MIN_HORAS} horas "
                             f"entre que está listo y el cierre — el chofer necesita "
                             f"esa ventana para pasar."}

        payload = {
            "associatedAccountNumber": {"value": self.account_number},
            "originDetail": {
                "pickupLocation": self._direccion_pickup(origen),
                "readyDateTimestamp": f"{fecha}T{ready}:00Z",
                "customerCloseTime": f"{close}:00",
            },
            "totalWeight": {"units": "KG", "value": float(datos.get("peso_kg") or 1)},
            "packageCount": max(int(datos.get("bultos") or 1), 1),
            "carrierCode": "FDXE",
            "countryRelationship": "INTERNATIONAL",
        }
        if datos.get("instrucciones"):
            payload["remarks"] = str(datos["instrucciones"])[:255]

        try:
            resp = self._request_with_retry(
                "POST", f"{self.base_url}/pickup/v1/pickups",
                json=payload, max_retries=1,   # agendar NO es idempotente
            )
        except Exception as e:
            return {"encontrado": False, "error": f"Error de red con FedEx: {e}"}

        if resp.status_code not in (200, 201):
            detalle = ""
            try:
                errs = resp.json().get("errors") or []
                detalle = "; ".join(e.get("message", "") for e in errs)[:300]
            except Exception:
                detalle = ""
            return {"encontrado": False,
                    "error": detalle or f"FedEx rechazó la recolección ({resp.status_code})."}

        try:
            out = resp.json().get("output") or {}
            return {
                "encontrado": True,
                "confirmation_code": out.get("pickupConfirmationCode") or "",
                "ubicacion": out.get("location") or "",
            }
        except Exception as e:
            return {"encontrado": False, "error": f"Respuesta inesperada de FedEx: {e}"}

    def cancel_pickup(self, confirmation_code: str, fecha: str,
                      ubicacion: str = "") -> dict:
        """Cancela una recolección agendada. Devuelve {ok, error}."""
        payload = {
            "associatedAccountNumber": {"value": self.account_number},
            "pickupConfirmationCode": str(confirmation_code),
            "scheduledDate": fecha,
            "carrierCode": "FDXE",
        }
        if ubicacion:
            payload["location"] = ubicacion
        try:
            resp = self._request_with_retry(
                "PUT", f"{self.base_url}/pickup/v1/pickups/cancel",
                json=payload, max_retries=1,
            )
        except Exception as e:
            return {"ok": False, "error": f"Error de red con FedEx: {e}"}
        if resp.status_code in (200, 201):
            return {"ok": True}
        return {"ok": False, "error": f"FedEx no canceló la recolección ({resp.status_code})."}
