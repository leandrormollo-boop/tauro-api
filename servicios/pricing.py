# ============================================================
# Reglas de pricing por cliente
# ============================================================

import math
from typing import Optional

from core.database import get_conn
from servicios.numeros_humanos import parse_importe_humano, parse_numero_humano


PRICING_MODES = {
    "PCT": "Porcentaje",
    "FIJO_ARS": "Fijo ARS",
    "MULTIPLICADOR": "Multiplicador",
}


class PricingNacionalNoConfigurado(ValueError):
    """El cliente no tiene una regla nacional explícita y válida."""


def normalizar_pricing(markup_tipo: str, markup_valor: Optional[float], fallback_pct: float = 25.0) -> dict:
    """Normaliza la regla de pricing para guardar o calcular."""
    tipo = (markup_tipo or "PCT").strip().upper()
    if tipo not in PRICING_MODES:
        tipo = "PCT"

    if markup_valor is None:
        valor = fallback_pct if tipo == "PCT" else (1.0 if tipo == "MULTIPLICADOR" else 0.0)
    else:
        valor = float(markup_valor)

    if tipo == "PCT":
        valor = max(valor, 0.0)
    elif tipo == "FIJO_ARS":
        valor = max(valor, 0.0)
    elif tipo == "MULTIPLICADOR":
        valor = max(valor, 1.0)

    return {"tipo": tipo, "valor": valor}


def parse_monto_ars(raw) -> Optional[float]:
    """
    Parsea un monto en pesos escrito en formato argentino o ingles.
      "1.500" / "1,500"       -> 1500.0
      "9.100,50" / "9,100.50" -> 9100.50
      "1450"                    -> 1450.0
    Devuelve None si el string está vacío. Lanza ValueError si no es numérico.
    """
    numero = parse_importe_humano(raw)
    if numero is None:
        return None
    resultado = float(numero)
    if not math.isfinite(resultado):
        raise ValueError("El importe es demasiado grande.")
    return resultado


def parse_pricing_value(raw: str, markup_tipo: str, fallback_pct: float = 25.0) -> dict:
    """
    Interpreta el valor tipeado en el admin respetando formatos ES/EN.

    La ambigüedad del punto depende del tipo:
      - FIJO_ARS: es un monto en pesos, el punto es separador de miles.
          "9.100" -> 9100 ; "14.000" -> 14000 ; "9.100,50" -> 9100.50
      - MULTIPLICADOR / PCT: el punto es decimal.
          "1.30" -> 1.30 ; "22.5" -> 22.5 ; "22,5" -> 22.5
    """
    raw = (raw or "").strip()
    valor = None
    if raw:
        tipo = (markup_tipo or "PCT").strip().upper()
        numero = (
            parse_importe_humano(raw)
            if tipo == "FIJO_ARS"
            else parse_numero_humano(raw)
        )
        valor = float(numero)
        if not math.isfinite(valor):
            raise ValueError("El valor de pricing debe ser un número finito.")
        if tipo == "MULTIPLICADOR" and valor < 1:
            raise ValueError("El multiplicador no puede ser menor a 1.")
        if tipo in {"PCT", "FIJO_ARS"} and valor < 0:
            raise ValueError("El valor de pricing no puede ser negativo.")
    return normalizar_pricing(markup_tipo, valor, fallback_pct=fallback_pct)


def get_pricing_config(cliente: str, fallback_pct: float = 25.0,
                       ambito: str = "internacional") -> dict:
    """
    Lee la regla de pricing del cliente. Compatible con clientes viejos.

    `ambito` existe porque el margen es distinto por tipo de envío (decisión
    de Leandro 28/07): el +$14.500 que tiene sentido sobre un FedEx a Miami
    casi triplica un Andreani de $8.000. Con ambito="nacional" se usa la
    regla nacional del cliente SI la tiene cargada; si no, cae a la regla
    internacional de siempre — así los clientes existentes no cambian de
    precio hasta que el admin les configure el margen nacional.
    """
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT markup_pct, markup_tipo, markup_valor,
                       markup_nac_tipo, markup_nac_valor
                FROM clientes
                WHERE cliente_id = %s AND activo = TRUE
                """,
                (cliente,),
            )
            row = cur.fetchone()

    if not row:
        return normalizar_pricing("PCT", fallback_pct, fallback_pct=fallback_pct)

    if ambito == "nacional" and (row.get("markup_nac_tipo") or "").strip():
        return normalizar_pricing(
            row["markup_nac_tipo"],
            row.get("markup_nac_valor"),
            fallback_pct=fallback_pct,
        )

    legacy_pct = float(row.get("markup_pct") or fallback_pct)
    return normalizar_pricing(
        row.get("markup_tipo") or "PCT",
        row.get("markup_valor"),
        fallback_pct=legacy_pct,
    )


def get_pricing_nacional_estricto(cliente: str) -> dict:
    """Obtiene pricing nacional sin heredar márgenes internacionales.

    Tiendanube nunca debe publicar una tarifa nacional calculada con el
    fallback global o con la regla internacional del cliente. Ante cualquier
    ausencia o valor inválido se falla de forma cerrada.
    """
    cliente_normalizado = (cliente or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT markup_nac_tipo, markup_nac_valor
                FROM clientes
                WHERE cliente_id = %s AND activo = TRUE
                """,
                (cliente_normalizado,),
            )
            row = cur.fetchone()

    tipo = str((row or {}).get("markup_nac_tipo") or "").strip().upper()
    raw_valor = (row or {}).get("markup_nac_valor")
    try:
        valor = float(raw_valor)
    except (TypeError, ValueError):
        valor = math.nan

    valido = math.isfinite(valor) and (
        (tipo == "PCT" and 0 <= valor <= 300)
        or (tipo == "FIJO_ARS" and valor >= 0)
        or (tipo == "MULTIPLICADOR" and valor >= 1)
    )
    if not valido:
        raise PricingNacionalNoConfigurado(
            "El cliente no tiene un pricing nacional explícito y válido."
        )

    return {"tipo": tipo, "valor": valor}


def aplicar_pricing(
    *,
    costo_usd: float,
    costo_ars: float,
    dolar: float,
    pricing: dict,
) -> dict:
    """Calcula precio final usando la regla base y overrides por costo USD.

    Los límites de los tramos son estrictos: ``costo < bajo`` y
    ``costo > alto``. En los bordes y en el intervalo central se aplica la
    regla base, evitando huecos de pricing.
    """
    tipo = pricing["tipo"]
    valor = float(pricing["valor"])
    costo_ars = float(costo_ars or 0)
    costo_usd = float(costo_usd or 0)
    dolar = float(dolar or 1)

    tramos = pricing.get("tramos_usd") or {}
    bajo_hasta = tramos.get("bajo_hasta_usd")
    bajo_ars = tramos.get("bajo_markup_ars")
    alto_desde = tramos.get("alto_desde_usd")
    alto_usd = tramos.get("alto_markup_usd")

    if (
        bajo_hasta is not None and bajo_ars is not None
        and costo_usd < float(bajo_hasta)
    ):
        valor_aplicado = float(bajo_ars)
        tipo_aplicado = "FIJO_ARS"
        precio_ars = round(costo_ars + valor_aplicado, 0)
    elif (
        alto_desde is not None and alto_usd is not None
        and costo_usd > float(alto_desde)
    ):
        valor_aplicado = round(float(alto_usd) * dolar, 4)
        tipo_aplicado = "FIJO_ARS"
        precio_ars = round(costo_ars + valor_aplicado, 0)
    elif tipo == "FIJO_ARS":
        tipo_aplicado = tipo
        valor_aplicado = valor
        precio_ars = round(costo_ars + valor, 0)
    elif tipo == "MULTIPLICADOR":
        tipo_aplicado = tipo
        valor_aplicado = valor
        precio_ars = round(costo_ars * valor, 0)
    else:
        tipo_aplicado = tipo
        valor_aplicado = valor
        precio_ars = round(costo_ars * (1 + valor / 100), 0)

    precio_usd = round(precio_ars / dolar, 2) if dolar else round(costo_usd, 2)
    markup_pct_equivalente = (
        round(((precio_ars / costo_ars) - 1) * 100, 2)
        if costo_ars > 0 else 0.0
    )

    return {
        "precio_final_ars": precio_ars,
        "precio_final_usd": precio_usd,
        "markup_pct_equivalente": markup_pct_equivalente,
        "markup_tipo": tipo_aplicado,
        "markup_valor": valor_aplicado,
    }


def describir_pricing(row: dict) -> str:
    pricing = normalizar_pricing(
        row.get("markup_tipo") or "PCT",
        row.get("markup_valor"),
        fallback_pct=float(row.get("markup_pct") or 25.0),
    )
    tipo = pricing["tipo"]
    valor = pricing["valor"]
    if tipo == "FIJO_ARS":
        return f"+ ARS {valor:,.0f}".replace(",", ".")
    if tipo == "MULTIPLICADOR":
        return f"Costo x {valor:g}"
    return f"{valor:g}%"
