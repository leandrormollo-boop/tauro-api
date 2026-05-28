# ============================================================
# Reglas de pricing por cliente
# ============================================================

from typing import Optional

from core.database import get_conn


PRICING_MODES = {
    "PCT": "Porcentaje",
    "FIJO_ARS": "Fijo ARS",
    "MULTIPLICADOR": "Multiplicador",
}


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


def parse_pricing_value(raw: str, markup_tipo: str, fallback_pct: float = 25.0) -> dict:
    raw = (raw or "").strip()
    valor = None
    if raw:
        if "," in raw:
            raw = raw.replace(".", "").replace(",", ".")
        valor = float(raw)
    return normalizar_pricing(markup_tipo, valor, fallback_pct=fallback_pct)


def get_pricing_config(cliente: str, fallback_pct: float = 25.0) -> dict:
    """Lee la regla de pricing del cliente. Compatible con clientes viejos."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT markup_pct, markup_tipo, markup_valor
                FROM clientes
                WHERE cliente_id = %s AND activo = TRUE
                """,
                (cliente,),
            )
            row = cur.fetchone()

    if not row:
        return normalizar_pricing("PCT", fallback_pct, fallback_pct=fallback_pct)

    legacy_pct = float(row.get("markup_pct") or fallback_pct)
    return normalizar_pricing(
        row.get("markup_tipo") or "PCT",
        row.get("markup_valor"),
        fallback_pct=legacy_pct,
    )


def aplicar_pricing(
    *,
    costo_usd: float,
    costo_ars: float,
    dolar: float,
    pricing: dict,
) -> dict:
    """Calcula precio final usando la regla del cliente."""
    tipo = pricing["tipo"]
    valor = float(pricing["valor"])
    costo_ars = float(costo_ars or 0)
    costo_usd = float(costo_usd or 0)
    dolar = float(dolar or 1)

    if tipo == "FIJO_ARS":
        precio_ars = round(costo_ars + valor, 0)
    elif tipo == "MULTIPLICADOR":
        precio_ars = round(costo_ars * valor, 0)
    else:
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
        "markup_tipo": tipo,
        "markup_valor": valor,
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
