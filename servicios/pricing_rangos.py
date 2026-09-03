"""Rangos comerciales en ARS: desde inclusivo, hasta exclusivo."""
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def numero(valor):
    try:
        if isinstance(valor, bool):
            raise ValueError("Valor numérico inválido.")
        result = Decimal(str(valor))
    except (InvalidOperation, TypeError):
        raise ValueError("Valor numérico inválido.") from None
    if not result.is_finite() or result < 0 or result > Decimal("999999999999"):
        raise ValueError("El importe debe ser finito, no negativo y dentro del límite.")
    return result


def validar_rangos(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("Los rangos no tienen un formato válido.") from None
    if not isinstance(raw, list) or len(raw) > 30:
        raise ValueError("Se admiten hasta 30 rangos.")
    resultado = []
    esperado = Decimal(0)
    for i, fila in enumerate(raw):
        if not isinstance(fila, dict):
            raise ValueError("Rango inválido.")
        desde = numero(fila.get("desde"))
        hasta = None if fila.get("hasta") in (None, "") else numero(fila["hasta"])
        tipo = fila.get("tipo")
        valor = numero(fila.get("valor"))
        minimo = numero(fila.get("minimo", 0))
        if desde != esperado:
            raise ValueError("Los rangos deben empezar en cero y ser consecutivos, sin huecos ni superposiciones.")
        if tipo not in ("FIJO_ARS", "PCT"):
            raise ValueError("Elegí fijo ARS o porcentaje.")
        if tipo == "FIJO_ARS" and minimo:
            raise ValueError("La ganancia mínima sólo corresponde a porcentajes.")
        if tipo == "PCT" and valor > 300:
            raise ValueError("El porcentaje no puede superar 300%.")
        if hasta is not None and hasta <= desde:
            raise ValueError("El límite superior debe superar al inferior.")
        if (hasta is None) != (i == len(raw) - 1):
            raise ValueError("Sólo el último rango debe quedar sin límite superior.")
        resultado.append(dict(desde=str(desde), hasta=str(hasta) if hasta is not None else None,
                              tipo=tipo, valor=str(valor), minimo=str(minimo)))
        esperado = hasta
    return resultado


def calcular_rangos(costo, rangos):
    costo = numero(costo)
    for fila in validar_rangos(rangos):
        if costo >= Decimal(fila["desde"]) and (fila["hasta"] is None or costo < Decimal(fila["hasta"])):
            valor = Decimal(fila["valor"])
            ganancia = valor if fila["tipo"] == "FIJO_ARS" else max(
                costo * valor / 100, Decimal(fila["minimo"]))
            precio = (costo + ganancia).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return {"precio": precio, "ganancia": precio - costo, "rango": fila}
    raise ValueError("No hay un rango configurado para este costo.")
