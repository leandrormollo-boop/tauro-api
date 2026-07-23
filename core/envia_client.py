from __future__ import annotations

# ============================================================
# ENVIA.COM CLIENT — backend mayorista para envíos NACIONALES
# ------------------------------------------------------------
# envia.com agrega los couriers nacionales argentinos (OCA,
# Andreani, Correo Argentino, Urbano, DPD, Rueddo, Welivery)
# detrás de una sola API prepaga. TAURO cotiza acá, aplica su
# markup y emite la guía debitando el wallet de envia.
#
# Verificado contra la API real el 22/07/2026 (cuenta Pesca Jacks):
#   - POST api.envia.com/ship/rate/  → tarifas por carrier
#   - GET  queries.envia.com/available-carrier/AR/0 → carriers
#   - El rate exige shipment.carrier: se cotiza en paralelo
#     contra cada carrier disponible y se juntan las opciones.
# Doc: https://docs.envia.com
# ============================================================

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # fallback igual que fedex_client
    def load_dotenv(path=None):
        return False

load_dotenv()

API_URL = "https://api.envia.com"
QUERIES_URL = "https://queries.envia.com"

# Códigos ISO 3166-2:AR que espera envia en origin/destination.state
PROVINCIAS_AR = {
    "CIUDAD AUTONOMA DE BUENOS AIRES": "C", "CABA": "C", "CAPITAL FEDERAL": "C",
    "BUENOS AIRES": "B", "CATAMARCA": "K", "CHACO": "H", "CHUBUT": "U",
    "CORDOBA": "X", "CORRIENTES": "W", "ENTRE RIOS": "E", "FORMOSA": "P",
    "JUJUY": "Y", "LA PAMPA": "L", "LA RIOJA": "F", "MENDOZA": "M",
    "MISIONES": "N", "NEUQUEN": "Q", "RIO NEGRO": "R", "SALTA": "A",
    "SAN JUAN": "J", "SAN LUIS": "D", "SANTA CRUZ": "Z", "SANTA FE": "S",
    "SANTIAGO DEL ESTERO": "G", "TIERRA DEL FUEGO": "V", "TUCUMAN": "T",
}

NOMBRES_CARRIERS = {
    "oca": "OCA",
    "andreani": "Andreani",
    "correoArgentino": "Correo Argentino",
    "urbano": "Urbano",
    "dpd": "DPD",
    "rueddo": "Rueddo",
    "welivery": "Welivery",
    "dhl": "DHL Express",
}


def _sin_acentos(texto: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if unicodedata.category(c) != "Mn"
    )


def provincia_a_codigo(nombre: str) -> str:
    """'Córdoba' → 'X'. Acepta el código ya normalizado (pasa derecho)."""
    limpio = _sin_acentos((nombre or "").strip().upper())
    if len(limpio) == 1:
        return limpio
    return PROVINCIAS_AR.get(limpio, limpio[:1] or "B")


class EnviaClient:
    """Cliente de la API de envia.com. Requiere ENVIA_API_KEY en el entorno
    (se genera en shipping.envia.com/settings/developers)."""

    def __init__(self):
        self.api_key = os.getenv("ENVIA_API_KEY", "").strip()
        self._carriers_cache: tuple[float, list[str]] | None = None

    @property
    def configurado(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    # ── Carriers disponibles ─────────────────────────────────
    def carriers_nacionales(self) -> list[str]:
        """Carriers habilitados para AR nacional. Cache 1 hora: cambia poco
        y evita pegarle a queries.envia.com en cada cotización."""
        if self._carriers_cache and time.time() - self._carriers_cache[0] < 3600:
            return self._carriers_cache[1]
        try:
            resp = requests.get(
                f"{QUERIES_URL}/available-carrier/AR/0",
                headers=self._headers(), timeout=15,
            )
            data = resp.json() if resp.status_code == 200 else {}
            carriers = [
                c.get("name") or c.get("carrier")
                for c in (data.get("data") or [])
                if c.get("name") or c.get("carrier")
            ]
        except Exception as e:
            print(f"[envia] error listando carriers: {e}")
            carriers = []
        if not carriers:
            # Fallback verificado 22/07/2026 — si queries no responde,
            # cotizamos igual contra los carriers conocidos.
            carriers = ["oca", "andreani", "correoArgentino", "urbano",
                        "dpd", "rueddo", "welivery"]
        self._carriers_cache = (time.time(), carriers)
        return carriers

    # ── Payload común ────────────────────────────────────────
    @staticmethod
    def _direccion(d: dict) -> dict:
        return {
            "name": d.get("nombre") or "Cliente",
            "company": d.get("empresa") or "",
            "email": d.get("email") or "operaciones@taurosolutions.ar",
            "phone": str(d.get("telefono") or "1100000000"),
            "street": d.get("calle") or d.get("direccion") or "",
            "number": str(d.get("numero") or "1"),
            "district": d.get("barrio") or "Centro",
            "city": d.get("ciudad") or "",
            "state": provincia_a_codigo(d.get("provincia") or d.get("estado") or ""),
            "country": "AR",
            "postalCode": str(d.get("cp") or d.get("zip") or ""),
        }

    @staticmethod
    def _paquetes(bultos: list[dict]) -> list[dict]:
        out = []
        for b in bultos:
            out.append({
                "content": b.get("descripcion") or b.get("contenido") or "Merchandise",
                "amount": max(int(b.get("cantidad") or b.get("unidades") or 1), 1),
                "type": "box",
                "dimensions": {
                    "length": int(b.get("largo_cm") or b.get("largo") or 30),
                    "width": int(b.get("ancho_cm") or b.get("ancho") or 20),
                    "height": int(b.get("alto_cm") or b.get("alto") or 10),
                },
                "weight": float(b.get("peso_kg") or b.get("peso") or 0.5),
                "insurance": 0,
                "declaredValue": float(b.get("valor_declarado_ars") or 0),
                "weightUnit": "KG",
                "lengthUnit": "CM",
            })
        return out

    # ── Cotización nacional multi-carrier ────────────────────
    def cotizar_nacional(self, origen: dict, destino: dict, bultos: list[dict]) -> dict:
        """
        Cotiza AR→AR contra TODOS los carriers disponibles en paralelo
        (la API exige un carrier por request). Devuelve:
          {encontrado, opciones: [{carrier, carrier_nombre, servicio,
           servicio_nombre, costo_ars, dias_estimados}, ...]}
        Los carriers sin cobertura para la ruta se omiten en silencio.
        """
        if not self.configurado:
            return {"encontrado": False, "error": "ENVIA_API_KEY no configurada"}

        base = {
            "origin": self._direccion(origen),
            "destination": self._direccion(destino),
            "packages": self._paquetes(bultos),
            "settings": {"currency": "ARS"},
        }

        def _cotizar_carrier(carrier: str) -> list[dict]:
            payload = {**base, "shipment": {"carrier": carrier, "type": 1}}
            try:
                resp = requests.post(
                    f"{API_URL}/ship/rate/", json=payload,
                    headers=self._headers(), timeout=15,
                )
                data = resp.json()
            except Exception as e:
                print(f"[envia] rate {carrier} falló: {e}")
                return []
            if data.get("meta") == "error" or not data.get("data"):
                return []  # sin cobertura / error del carrier: se omite
            opciones = []
            for op in data["data"]:
                total = op.get("totalPrice") or op.get("basePrice")
                if not total:
                    continue
                opciones.append({
                    "carrier": op.get("carrier") or carrier,
                    "carrier_nombre": NOMBRES_CARRIERS.get(
                        op.get("carrier") or carrier,
                        (op.get("carrier") or carrier).title(),
                    ),
                    "servicio": op.get("service") or "",
                    "servicio_nombre": op.get("serviceDescription")
                        or op.get("service") or carrier,
                    "costo_ars": round(float(total), 2),
                    "dias_estimados": op.get("deliveryEstimate")
                        or op.get("deliveryestimate") or "2-7 días",
                })
            return opciones

        opciones: list[dict] = []
        carriers = self.carriers_nacionales()
        with ThreadPoolExecutor(max_workers=min(len(carriers), 7)) as pool:
            futuros = {pool.submit(_cotizar_carrier, c): c for c in carriers}
            for fut in as_completed(futuros):
                opciones.extend(fut.result())

        if not opciones:
            return {"encontrado": False,
                    "error": "Ningún courier cubre esa ruta con esas medidas."}
        opciones.sort(key=lambda o: o["costo_ars"])
        return {"encontrado": True, "opciones": opciones}

    # ── Emisión de guía nacional ─────────────────────────────
    def create_shipment_nacional(
        self, origen: dict, destino: dict, bultos: list[dict],
        carrier: str, servicio: str,
    ) -> dict:
        """
        Emite la guía en envia.com (debita el wallet prepago) y baja el
        label PDF. Devuelve {encontrado, tracking, label_pdf, costo_ars}.
        """
        if not self.configurado:
            return {"encontrado": False, "error": "ENVIA_API_KEY no configurada"}

        payload = {
            "origin": self._direccion(origen),
            "destination": self._direccion(destino),
            "packages": self._paquetes(bultos),
            "shipment": {"carrier": carrier, "service": servicio, "type": 1},
            "settings": {"currency": "ARS", "labelFormat": "pdf"},
        }
        try:
            resp = requests.post(
                f"{API_URL}/ship/generate/", json=payload,
                headers=self._headers(), timeout=45,
            )
            data = resp.json()
        except Exception as e:
            return {"encontrado": False, "error": f"envia.com no respondió: {e}"}

        if data.get("meta") == "error" or not data.get("data"):
            err = (data.get("error") or {})
            msg = err.get("message") or err.get("description") or "envia.com rechazó la emisión"
            # El error más probable en el arranque: wallet sin saldo.
            return {"encontrado": False, "error": msg}

        envio = data["data"][0]
        tracking = envio.get("trackingNumber") or envio.get("tracking") or ""
        label_url = envio.get("label") or envio.get("labelUrl") or ""

        label_pdf = None
        if label_url:
            try:
                pdf_resp = requests.get(label_url, timeout=30)
                if pdf_resp.status_code == 200 and pdf_resp.content[:4] == b"%PDF":
                    label_pdf = pdf_resp.content
            except Exception as e:
                print(f"[envia] no se pudo bajar el label: {e}")

        if not tracking:
            return {"encontrado": False, "error": "envia.com no devolvió tracking"}

        return {
            "encontrado": True,
            "tracking": tracking,
            "label_pdf": label_pdf,
            "label_url": label_url,
            "costo_ars": float(envio.get("totalPrice") or 0),
            "carrier": envio.get("carrier") or carrier,
        }
