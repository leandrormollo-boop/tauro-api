"""Rangos de ganancia para DHL web según costo real expresado en USD."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from servicios.numeros_humanos import decimal_a_texto


TIPOS = frozenset({"PCT", "FIJO_USD", "FIJO_ARS"})
MAX_RANGOS = 30
MAX_LIMITE_USD = Decimal("999999999")
MAX_PCT = Decimal("300")
MAX_FIJO = Decimal("999999999")


def _decimal(valor: Any, campo: str) -> Decimal:
    if isinstance(valor, bool):
        raise ValueError(f"{campo}: ingresá un número válido.")
    try:
        numero = Decimal(str(valor).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        raise ValueError(f"{campo}: ingresá un número válido.") from None
    if not numero.is_finite():
        raise ValueError(f"{campo}: ingresá un número finito.")
    return numero


def _no_negativo(
    valor: Any,
    campo: str,
    *,
    maximo: Decimal,
    decimales: int,
) -> Decimal:
    numero = _decimal(valor, campo)
    if numero < 0:
        raise ValueError(f"{campo}: el valor no puede ser negativo.")
    if numero > maximo:
        raise ValueError(f"{campo}: el valor supera el máximo permitido.")
    if numero.as_tuple().exponent < -decimales:
        raise ValueError(f"{campo}: admite hasta {decimales} decimales.")
    return numero


def normalizar_rangos_usd(raw: Any) -> list[dict[str, str | None]]:
    """Valida una partición completa de costos: ``[desde, hasta)``."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("Los rangos de DHL no tienen un JSON válido.") from None
    if not isinstance(raw, list) or isinstance(raw, (str, bytes)):
        raise ValueError("Los rangos de DHL deben ser una lista JSON.")
    if not 1 <= len(raw) <= MAX_RANGOS:
        raise ValueError("Cargá entre 1 y 30 rangos de DHL.")

    normalizados: list[dict[str, str | None]] = []
    esperado = Decimal("0")
    for indice, fila in enumerate(raw, start=1):
        if not isinstance(fila, Mapping):
            raise ValueError(f"Rango {indice}: el formato no es válido.")
        extras = set(fila) - {"desde", "hasta", "tipo", "valor"}
        if extras:
            raise ValueError(f"Rango {indice}: contiene campos no permitidos.")

        desde = _no_negativo(
            fila.get("desde"), f"Rango {indice}, desde",
            maximo=MAX_LIMITE_USD, decimales=4,
        )
        hasta_raw = fila.get("hasta")
        hasta = None if hasta_raw in (None, "") else _no_negativo(
            hasta_raw, f"Rango {indice}, hasta",
            maximo=MAX_LIMITE_USD, decimales=4,
        )
        tipo = str(fila.get("tipo") or "").strip().upper()
        if tipo not in TIPOS:
            raise ValueError(
                f"Rango {indice}: elegí PCT, FIJO_USD o FIJO_ARS."
            )
        if tipo == "PCT":
            valor = _no_negativo(
                fila.get("valor"), f"Rango {indice}, porcentaje",
                maximo=MAX_PCT, decimales=4,
            )
        else:
            valor = _no_negativo(
                fila.get("valor"), f"Rango {indice}, importe fijo",
                maximo=MAX_FIJO, decimales=2,
            )

        if desde != esperado:
            raise ValueError(
                "Los rangos de DHL deben empezar en 0 y ser consecutivos, "
                "sin huecos ni superposiciones."
            )
        if hasta is not None and hasta <= desde:
            raise ValueError(
                f"Rango {indice}: el límite superior debe superar al inferior."
            )
        es_ultimo = indice == len(raw)
        if es_ultimo and hasta is not None:
            raise ValueError("El último rango de DHL debe quedar abierto.")
        if not es_ultimo and hasta is None:
            raise ValueError("Sólo el último rango de DHL puede quedar abierto.")

        normalizados.append({
            "desde": decimal_a_texto(desde),
            "hasta": decimal_a_texto(hasta) if hasta is not None else None,
            "tipo": tipo,
            "valor": decimal_a_texto(valor),
        })
        esperado = hasta

    return normalizados


def serializar_rangos_usd(rangos: Any) -> str:
    return json.dumps(
        normalizar_rangos_usd(rangos),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def aplicar_rangos_usd(
    *,
    costo: Any,
    moneda: Any,
    dolar: Any,
    rangos: Iterable[Mapping[str, Any]],
) -> dict[str, int | float]:
    """Aplica exactamente un tramo sin redondear el costo usado como umbral."""
    costo_nativo = _decimal(costo, "Costo de DHL")
    if costo_nativo <= 0:
        raise ValueError("Costo de DHL: debe ser mayor a cero.")
    cambio = _decimal(dolar, "Tipo de cambio")
    if cambio <= 0:
        raise ValueError("Tipo de cambio: debe ser mayor a cero.")
    moneda_normalizada = str(moneda or "USD").strip().upper()
    if moneda_normalizada not in {"USD", "ARS"}:
        raise ValueError("La moneda de DHL no está soportada.")

    costo_usd = (
        costo_nativo
        if moneda_normalizada == "USD"
        else costo_nativo / cambio
    )
    costo_ars = (
        costo_nativo * cambio
        if moneda_normalizada == "USD"
        else costo_nativo
    )
    normalizados = normalizar_rangos_usd(list(rangos))
    seleccionado = next(
        (
            fila for fila in normalizados
            if costo_usd >= Decimal(fila["desde"])
            and (
                fila["hasta"] is None
                or costo_usd < Decimal(fila["hasta"])
            )
        ),
        None,
    )
    if seleccionado is None:
        raise ValueError("No hay un rango de DHL para este costo.")

    valor = Decimal(seleccionado["valor"])
    if seleccionado["tipo"] == "PCT":
        ganancia_ars = costo_ars * valor / Decimal("100")
    elif seleccionado["tipo"] == "FIJO_USD":
        ganancia_ars = valor * cambio
    else:
        ganancia_ars = valor

    precio_ars = (costo_ars + ganancia_ars).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    # La respuesta pública histórica expresa ARS enteros. Con ganancia cero,
    # HALF_UP podría redondear 100,40 a 100 y publicar menos que el costo real.
    # El piso hacia arriba conserva la regla 0 válida sin vender a pérdida.
    piso_costo_ars = costo_ars.quantize(Decimal("1"), rounding=ROUND_CEILING)
    precio_ars = max(precio_ars, piso_costo_ars)
    return {
        "precio_ars": int(precio_ars),
        "precio_usd": float(
            (precio_ars / cambio).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        ),
    }
