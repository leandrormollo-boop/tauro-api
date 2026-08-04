"""Agentes comerciales de TAURO y controles deterministas.

Los modelos investigan y redactan. El codigo decide puntajes, estados y
permisos de envio. Ningun agente tiene una herramienta para mandar correo.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ICP_TAURO = {
    "nombre": "TAURO B2B 2026",
    "descripcion": (
        "Empresas y tiendas online con operaciones internacionales recurrentes: "
        "e-commerce exportador, pymes exportadoras por courier e importadores "
        "mayoristas que valoran cotizacion multi-courier, portal web e integracion."
    ),
    "segmentos": [
        "ECOMMERCE_EXPORTADOR",
        "PYME_EXPORTADORA",
        "IMPORTADOR_MAYORISTA",
        "MARCA_CON_VENTAS_INTERNACIONALES",
    ],
    "paises_objetivo": [
        "AR", "US", "UY", "BR", "CL", "MX", "PA", "CR", "ES", "IT",
        "CN", "IN", "BD",
    ],
    "umbral_calificado": 65,
}

SEGMENTOS = {
    "ECOMMERCE_EXPORTADOR",
    "PYME_EXPORTADORA",
    "IMPORTADOR_MAYORISTA",
    "MARCA_CON_VENTAS_INTERNACIONALES",
    "SERVICIOS_LOGISTICOS",
    "CONSUMIDOR_FINAL",
    "OTRO",
}

ESTADOS_MENSAJE = {"BORRADOR", "OBSERVADO", "APROBADO", "ENVIANDO", "ENVIADO", "CANCELADO"}
TRANSICIONES_MENSAJE = {
    "BORRADOR": {"APROBADO", "CANCELADO"},
    "OBSERVADO": {"CANCELADO"},
    "APROBADO": {"ENVIANDO", "CANCELADO"},
    "ENVIANDO": {"ENVIADO", "APROBADO"},
    "ENVIADO": set(),
    "CANCELADO": set(),
}


class AgenteNoConfigurado(RuntimeError):
    pass


class SalidaAgenteInvalida(RuntimeError):
    pass


class TransicionComercialInvalida(ValueError):
    pass


@dataclass(frozen=True)
class RutaAgente:
    task_type: str
    model: str
    role: str
    reasoning_effort: str


RUTAS_AGENTES = {
    "market_discovery": RutaAgente("market_discovery", "gpt-5.6-luna", "investigador", "low"),
    "company_research": RutaAgente("company_research", "gpt-5.6-luna", "investigador", "low"),
    "proposal_draft": RutaAgente("proposal_draft", "gpt-5.6-terra", "redactor", "medium"),
    "proposal_review": RutaAgente("proposal_review", "gpt-5.6-terra", "revisor", "medium"),
    "commercial_strategy": RutaAgente("commercial_strategy", "gpt-5.6-sol", "estratega", "high"),
}


def normalizar_dominio(valor: str | None) -> str | None:
    dominio = (valor or "").strip().lower()
    if not dominio:
        return None
    if "://" not in dominio:
        dominio = f"https://{dominio}"
    parsed = urlsplit(dominio)
    host = (parsed.hostname or "").strip(".").lower()
    host = host.removeprefix("www.")
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("Dominio invalido.")
    return host


def normalizar_email(valor: str | None) -> str | None:
    email = (valor or "").strip().lower()
    if not email:
        return None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", email):
        raise ValueError("Email invalido.")
    return email


def validar_transicion_mensaje(actual: str, destino: str) -> None:
    actual = (actual or "").upper()
    destino = (destino or "").upper()
    if actual not in ESTADOS_MENSAJE or destino not in TRANSICIONES_MENSAJE.get(actual, set()):
        raise TransicionComercialInvalida(f"Transicion no permitida: {actual} -> {destino}")


def validar_envio(
    *,
    estado_mensaje: str,
    estado_email: str,
    cuenta_excluida: bool,
    contacto_excluido: bool,
    email: str | None,
) -> None:
    if estado_mensaje != "APROBADO":
        raise TransicionComercialInvalida("El mensaje debe estar APROBADO antes de enviarse.")
    if cuenta_excluida or contacto_excluido or estado_email == "BAJA":
        raise TransicionComercialInvalida("El contacto esta excluido de comunicaciones.")
    if estado_email != "VERIFICADO":
        raise TransicionComercialInvalida("El email comercial debe estar verificado.")
    normalizar_email(email)


def _entero_no_negativo(valor: Any) -> int | None:
    if valor is None or valor == "":
        return None
    try:
        return max(0, int(float(valor)))
    except (TypeError, ValueError):
        return None


def puntuar_investigacion(datos: dict[str, Any]) -> tuple[int, dict[str, int], str]:
    """Puntua el fit sin delegar decisiones numericas al modelo."""
    puntos: dict[str, int] = {}
    segmento = str(datos.get("segmento") or "OTRO").upper()
    if segmento not in SEGMENTOS:
        segmento = "OTRO"

    es_empresa = bool(datos.get("es_empresa"))
    competidor = bool(datos.get("es_competidor_logistico"))
    restringido = bool(datos.get("mercaderia_restringida"))
    if not es_empresa:
        return 0, {"no_es_empresa": -100}, "DESCARTAR"
    if competidor:
        return 0, {"competidor_logistico": -100}, "DESCARTAR"
    if restringido:
        return 0, {"mercaderia_restringida": -100}, "REVISION_MANUAL"

    puntos["empresa_b2b"] = 10
    puntos["segmento"] = {
        "ECOMMERCE_EXPORTADOR": 20,
        "PYME_EXPORTADORA": 18,
        "IMPORTADOR_MAYORISTA": 18,
        "MARCA_CON_VENTAS_INTERNACIONALES": 16,
    }.get(segmento, 0)

    envios = _entero_no_negativo(datos.get("envios_internacionales_mes"))
    if envios is not None:
        puntos["volumen_envios"] = 25 if envios >= 100 else 20 if envios >= 30 else 12 if envios >= 10 else 4 if envios else 0

    kilos = _entero_no_negativo(datos.get("kg_internacionales_mes"))
    if kilos is not None:
        puntos["volumen_kg"] = 15 if kilos >= 500 else 12 if kilos >= 100 else 7 if kilos >= 20 else 2 if kilos else 0

    if datos.get("tiene_ecommerce"):
        puntos["ecommerce"] = 8
    if datos.get("opera_internacionalmente"):
        puntos["operacion_internacional"] = 8
    if datos.get("senal_necesidad_integracion"):
        puntos["necesidad_integracion"] = 8

    rutas = {str(p).upper() for p in (datos.get("paises_operacion") or [])}
    if rutas.intersection(ICP_TAURO["paises_objetivo"]):
        puntos["rutas_tauro"] = 8

    fuentes = datos.get("fuentes") or []
    puntos["evidencia_publica"] = 5 if len(fuentes) >= 2 else 2 if len(fuentes) == 1 else 0
    if datos.get("email_comercial_publico"):
        puntos["contacto_publico"] = 3

    score = max(0, min(100, sum(puntos.values())))
    if score >= int(ICP_TAURO["umbral_calificado"]):
        decision = "CALIFICADO"
    elif score >= 40:
        decision = "REVISAR"
    else:
        decision = "BAJA_PRIORIDAD"
    return score, puntos, decision


DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "candidatos": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "empresa": {"type": "string"},
                    "dominio": {"type": ["string", "null"]},
                    "sitio_web": {"type": ["string", "null"]},
                    "pais": {"type": "string"},
                    "segmento": {"type": "string", "enum": sorted(SEGMENTOS)},
                    "por_que_encaja": {"type": "string"},
                    "fuentes": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "titulo": {"type": "string"},
                                "evidencia": {"type": "string"},
                            },
                            "required": ["url", "titulo", "evidencia"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["empresa", "dominio", "sitio_web", "pais", "segmento", "por_que_encaja", "fuentes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidatos"],
    "additionalProperties": False,
}


RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "empresa": {"type": "string"},
        "dominio": {"type": ["string", "null"]},
        "pais": {"type": "string"},
        "segmento": {"type": "string", "enum": sorted(SEGMENTOS)},
        "resumen": {"type": "string"},
        "es_empresa": {"type": "boolean"},
        "es_competidor_logistico": {"type": "boolean"},
        "mercaderia_restringida": {"type": "boolean"},
        "tiene_ecommerce": {"type": "boolean"},
        "plataforma_ecommerce": {"type": ["string", "null"]},
        "opera_internacionalmente": {"type": "boolean"},
        "senal_necesidad_integracion": {"type": "boolean"},
        "envios_internacionales_mes": {"type": ["integer", "null"], "minimum": 0},
        "kg_internacionales_mes": {"type": ["integer", "null"], "minimum": 0},
        "paises_operacion": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "senales_comerciales": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
        "riesgos": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
        "email_comercial_publico": {"type": ["string", "null"]},
        "email_fuente_url": {"type": ["string", "null"]},
        "fuentes": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "titulo": {"type": "string"},
                    "evidencia": {"type": "string"},
                },
                "required": ["url", "titulo", "evidencia"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "empresa", "dominio", "pais", "segmento", "resumen", "es_empresa",
        "es_competidor_logistico", "mercaderia_restringida", "tiene_ecommerce",
        "plataforma_ecommerce", "opera_internacionalmente", "senal_necesidad_integracion",
        "envios_internacionales_mes", "kg_internacionales_mes", "paises_operacion",
        "senales_comerciales", "riesgos", "email_comercial_publico", "email_fuente_url", "fuentes",
    ],
    "additionalProperties": False,
}


DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "asunto": {"type": "string"},
        "cuerpo_texto": {"type": "string"},
        "base_personalizacion": {"type": "string"},
        "afirmaciones": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "alertas": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
    },
    "required": ["asunto", "cuerpo_texto", "base_personalizacion", "afirmaciones", "alertas"],
    "additionalProperties": False,
}


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "aprobado": {"type": "boolean"},
        "nivel_riesgo": {"type": "string", "enum": ["BAJO", "MEDIO", "ALTO"]},
        "problemas": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
        "asunto_corregido": {"type": "string"},
        "cuerpo_corregido": {"type": "string"},
    },
    "required": ["aprobado", "nivel_riesgo", "problemas", "asunto_corregido", "cuerpo_corregido"],
    "additionalProperties": False,
}


def _url_canonica(valor: str | None) -> str | None:
    try:
        parsed = urlsplit((valor or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))
    except Exception:
        return None


def _response_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return {}


def _fuentes_hospedadas(response: Any) -> dict[str, dict[str, str]]:
    resultado: dict[str, dict[str, str]] = {}
    for item in _response_dict(response).get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        for source in action.get("sources", []) or []:
            if not isinstance(source, dict):
                continue
            url = _url_canonica(source.get("url"))
            if url:
                resultado[url] = {"url": url, "titulo": str(source.get("title") or "Fuente publica")[:240]}
    return resultado


def _filtrar_fuentes_de_modelo(payload: dict[str, Any], response: Any) -> dict[str, Any]:
    permitidas = _fuentes_hospedadas(response)
    if not permitidas:
        payload["fuentes"] = []
        return payload
    limpias = []
    for source in payload.get("fuentes") or []:
        if not isinstance(source, dict):
            continue
        url = _url_canonica(source.get("url"))
        if not url or url not in permitidas:
            continue
        limpias.append({
            "url": url,
            "titulo": str(source.get("titulo") or permitidas[url]["titulo"])[:240],
            "evidencia": str(source.get("evidencia") or "")[:1000],
        })
    payload["fuentes"] = limpias
    return payload


class AgentesComercialesOpenAI:
    """Cliente sin herramientas externas irreversibles."""

    def __init__(self, client: Any | None = None) -> None:
        self.client = client
        if client is None and os.getenv("OPENAI_API_KEY", "").strip():
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise AgenteNoConfigurado("Falta instalar la dependencia openai.") from exc
            self.client = OpenAI()

    @property
    def configurado(self) -> bool:
        return self.client is not None

    def _crear_estructurado(
        self,
        *,
        ruta: RutaAgente,
        schema_name: str,
        schema: dict[str, Any],
        instructions: str,
        input_data: dict[str, Any],
        web_search: bool,
    ) -> tuple[dict[str, Any], Any]:
        if not self.client:
            raise AgenteNoConfigurado("OPENAI_API_KEY no esta configurada.")
        request: dict[str, Any] = {
            "model": ruta.model,
            "instructions": instructions,
            "input": json.dumps(input_data, ensure_ascii=False, default=str)[:30000],
            "reasoning": {"effort": ruta.reasoning_effort},
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
            "store": False,
            "metadata": {"tauro_task_type": ruta.task_type, "tauro_policy_version": "1"},
        }
        if web_search:
            request.update({
                "tools": [{"type": "web_search"}],
                "include": ["web_search_call.action.sources"],
                "max_tool_calls": 8,
            })
        response = self.client.responses.create(**request)
        try:
            data = json.loads(getattr(response, "output_text", "") or "")
        except json.JSONDecodeError as exc:
            raise SalidaAgenteInvalida("El agente no devolvio JSON valido.") from exc
        if not isinstance(data, dict):
            raise SalidaAgenteInvalida("La salida del agente no es un objeto.")
        return data, response

    def descubrir(self, brief: str, limite: int = 10) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
        limite = max(1, min(int(limite), 20))
        data, response = self._crear_estructurado(
            ruta=RUTAS_AGENTES["market_discovery"],
            schema_name="tauro_market_discovery",
            schema=DISCOVERY_SCHEMA,
            instructions=(
                "Sos el investigador comercial de TAURO Solutions. Busca solo empresas reales y datos "
                "empresariales publicos. No recolectes datos sensibles, no adivines emails y no inventes "
                "volumenes. Prioriza el ICP provisto. Cada candidato debe tener evidencia web verificable. "
                "No incluyas couriers, forwarders competidores ni consumidores finales."
            ),
            input_data={"brief": brief[:3000], "limite": limite, "icp": ICP_TAURO},
            web_search=True,
        )
        candidatos = []
        for candidato in (data.get("candidatos") or [])[:limite]:
            if not isinstance(candidato, dict):
                continue
            candidato = _filtrar_fuentes_de_modelo(candidato, response)
            try:
                candidato["dominio"] = normalizar_dominio(candidato.get("dominio") or candidato.get("sitio_web"))
            except ValueError:
                candidato["dominio"] = None
            if candidato.get("empresa") and candidato.get("fuentes"):
                candidatos.append(candidato)
        return candidatos, {
            "model": RUTAS_AGENTES["market_discovery"].model,
            "response_id": getattr(response, "id", None),
        }

    def investigar(self, cuenta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str | None]]:
        data, response = self._crear_estructurado(
            ruta=RUTAS_AGENTES["company_research"],
            schema_name="tauro_company_research",
            schema=RESEARCH_SCHEMA,
            instructions=(
                "Investiga esta empresa para una evaluacion comercial B2B de TAURO Solutions. Usa solo "
                "fuentes publicas y actuales. Si un dato no esta publicado, devuelve null; nunca estimes "
                "volumenes sin evidencia. No busques datos personales. Un email solo puede ser una casilla "
                "comercial publicada por la empresa y debe incluir su URL de origen. Separa hechos de riesgos."
            ),
            input_data={"cuenta": cuenta, "icp": ICP_TAURO},
            web_search=True,
        )
        data = _filtrar_fuentes_de_modelo(data, response)
        email_fuente = _url_canonica(data.get("email_fuente_url"))
        urls = {f["url"] for f in data.get("fuentes") or []}
        if email_fuente not in urls:
            data["email_comercial_publico"] = None
            data["email_fuente_url"] = None
        else:
            try:
                data["email_comercial_publico"] = normalizar_email(data.get("email_comercial_publico"))
            except ValueError:
                data["email_comercial_publico"] = None
                data["email_fuente_url"] = None
        return data, {
            "model": RUTAS_AGENTES["company_research"].model,
            "response_id": getattr(response, "id", None),
        }

    def redactar(self, cuenta: dict[str, Any], contacto: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str | None]]:
        data, response = self._crear_estructurado(
            ruta=RUTAS_AGENTES["proposal_draft"],
            schema_name="tauro_proposal_draft",
            schema=DRAFT_SCHEMA,
            instructions=(
                "Redacta un primer correo B2B breve, humano y personalizado en espanol. TAURO brinda "
                "soluciones logisticas integrales para empresas desde un portal: e-commerce, cross-border, "
                "importaciones y exportaciones. No afirmes alianzas, descuentos, tarifas, ahorros, tiempos, "
                "certificaciones ni volumenes que no esten en los datos. No digas que el correo fue escrito "
                "por IA. Incluye una llamada a una conversacion de 15 minutos y una linea sencilla para no "
                "recibir mas mensajes. Maximo 170 palabras."
            ),
            input_data={"cuenta": cuenta, "contacto": contacto, "icp": ICP_TAURO},
            web_search=False,
        )
        return data, {"model": RUTAS_AGENTES["proposal_draft"].model, "response_id": getattr(response, "id", None)}

    def revisar(
        self,
        cuenta: dict[str, Any],
        contacto: dict[str, Any],
        borrador: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str | None]]:
        data, response = self._crear_estructurado(
            ruta=RUTAS_AGENTES["proposal_review"],
            schema_name="tauro_proposal_review",
            schema=REVIEW_SCHEMA,
            instructions=(
                "Sos el revisor independiente de comunicaciones de TAURO. Rechaza datos inventados, "
                "afirmaciones sin fuente, tono de spam, promesas de precio o tiempo, falsa urgencia y "
                "cualquier referencia a una alianza no demostrada. Conserva el mensaje breve, profesional "
                "y personalizado. Aprobar significa solo que puede pasar a revision humana; nunca autoriza "
                "el envio."
            ),
            input_data={"cuenta": cuenta, "contacto": contacto, "borrador": borrador},
            web_search=False,
        )
        return data, {"model": RUTAS_AGENTES["proposal_review"].model, "response_id": getattr(response, "id", None)}
