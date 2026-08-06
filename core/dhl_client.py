from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

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
        try:
            j = resp.json()
        except Exception:
            return (resp.text or "")[:300]

        if not isinstance(j, dict):
            return (resp.text or "")[:300]

        base = j.get("detail") or j.get("message") or j.get("title") or ""

        detalles = j.get("additionalDetails")
        if isinstance(detalles, str):
            detalles = [detalles]
        if isinstance(detalles, (list, tuple)):
            partes = [str(d).strip() for d in detalles if str(d or "").strip()]
            if partes:
                # Sin el " · " los problemas se leen como una sola frase larga.
                return " · ".join(partes)[:600]

        return (base or (resp.text or ""))[:300]

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
                return {"encontrado": False, "error": self._error_legible(resp)}

            return self._parsear_rates(resp.json())

        except Exception as e:
            print(f"[dhl] Excepción en get_rates: {e}")
            return {"encontrado": False, "error": str(e)}

    @staticmethod
    def _contacto(d: dict, por_defecto_nombre: str = "") -> dict:
        """
        contactInformation del POST /shipments. DHL exige phone y email con
        formato válido: si el cliente no cargó teléfono, mandar vacío hace
        que rechace el envío entero, así que hay fallback.
        """
        return {
            "phone": (d.get("telefono") or "0000000000").strip()[:25],
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
        if not (self.api_key and self.api_secret):
            return {"encontrado": False, "error": "Faltan credenciales de DHL."}

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

        # Una pieza por unidad, igual que en FedEx: N cajas idénticas viajan
        # como N piezas, cada una con su etiqueta.
        piezas, line_items, valor_total = [], [], 0.0
        for i, b in enumerate(bultos, start=1):
            unidades = max(int(b.get("unidades") or b.get("cantidad") or 1), 1)
            valor_u = float(b.get("valor_unitario_usd") or 0)
            for _ in range(unidades):
                piezas.append({
                    "weight": round(float(b.get("peso_kg") or 0.5), 3),
                    "dimensions": {
                        "length": round(float(b.get("largo_cm") or b.get("largo") or 30), 3),
                        "width": round(float(b.get("ancho_cm") or b.get("ancho") or 20), 3),
                        "height": round(float(b.get("alto_cm") or b.get("alto") or 10), 3),
                    },
                })
            valor_total += valor_u * unidades
            line_items.append({
                "number": i,
                "description": (b.get("descripcion_en") or b.get("producto_alias")
                                or "Merchandise")[:75],
                "price": round(valor_u, 2),
                "quantity": {"value": unidades, "unitOfMeasurement": "PCS"},
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
                    "netValue": round(float(b.get("peso_kg") or 0.5), 3),
                    "grossValue": round(float(b.get("peso_kg") or 0.5), 3),
                },
            })

        msg_ref = f"tauro-ship-{uuid.uuid4().hex[:20]}"
        cuerpo = {
            "plannedShippingDateAndTime": self._fecha_envio(),
            "pickup": {"isRequested": False},
            "productCode": self.product_code,
            "accounts": [{"typeCode": "shipper", "number": cuenta}],
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
                "imageOptions": [{"typeCode": "label", "templateName": "ECOM26_84_A4_001"}],
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
                             "antes de reintentar."}

        if resp.status_code not in (200, 201):
            print(f"[dhl] POST /shipments error {resp.status_code} (ref {msg_ref}): "
                  f"{resp.text[:400]}")
            return {"encontrado": False, "error": self._error_legible(resp)}

        try:
            data = resp.json()
            tracking = str(data.get("shipmentTrackingNumber") or "")
            if not tracking:
                return {"encontrado": False, "error": "DHL no devolvió tracking."}

            # El label viene en base64 dentro de documents (typeCode "label").
            label_b64 = ""
            for doc in (data.get("documents") or []):
                if (doc.get("typeCode") or "").lower() == "label":
                    label_b64 = doc.get("content") or ""
                    break
            label_pdf = None
            if label_b64:
                import base64
                try:
                    label_pdf = base64.b64decode(label_b64)
                except Exception as e:
                    print(f"[dhl] guía {tracking} emitida pero el label no decodifica: {e}")

            print(f"[dhl] guía emitida: {tracking} (ref {msg_ref})")
            return {
                "encontrado": True,
                "tracking": tracking,
                "servicio": "DHL Express Worldwide",
                "label_pdf": label_pdf,
                "label_b64": label_b64,
            }
        except Exception as e:
            # La guía PUEDE existir aunque no podamos leer la respuesta.
            print(f"[dhl] respuesta ilegible tras emitir (ref {msg_ref}): {e}")
            return {"encontrado": False,
                    "error": "DHL respondió algo inesperado. Verificá en MyDHL "
                             "si la guía se emitió."}

    def track(self, tracking_number: str) -> dict:
        """Estado de un envío DHL. Devuelve {encontrado, estado, descripcion, eventos}."""
        tracking_number = str(tracking_number or "").strip()
        if not tracking_number:
            return {"encontrado": False, "error": "tracking vacío"}
        if not (self.api_key and self.api_secret):
            return {"encontrado": False, "error": "Faltan credenciales de DHL."}
        try:
            resp = requests.get(
                f"{self.base_url}/shipments/{tracking_number}/tracking",
                auth=(self.api_key, self.api_secret),
                headers={"Accept": "application/json", "x-version": self.API_VERSION},
                timeout=30,
            )
            if resp.status_code != 200:
                return {"encontrado": False, "error": f"DHL {resp.status_code}"}
            envios = (resp.json().get("shipments") or [])
            if not envios:
                return {"encontrado": False, "error": "Sin datos de tracking"}
            env = envios[0]
            eventos = env.get("events") or []
            return {
                "encontrado": True,
                "estado": (env.get("status") or "").upper(),
                "descripcion": env.get("description") or "",
                "eventos": eventos,
                "ultimo_evento": eventos[0] if eventos else None,
            }
        except Exception as e:
            return {"encontrado": False, "error": str(e)}
