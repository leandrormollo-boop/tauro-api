"""Períodos mensuales compartidos por el portal y el ADMIN.

Las "semanas" son tramos contables fijos dentro del mes. Nunca cruzan meses:
1–7, 8–14, 15–21 y 22–fin. Esto permite que los conteos de operaciones y los
cargos de cuenta corriente describan exactamente el mismo intervalo.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Iterable


MESES = (
    (1, "Enero"), (2, "Febrero"), (3, "Marzo"), (4, "Abril"),
    (5, "Mayo"), (6, "Junio"), (7, "Julio"), (8, "Agosto"),
    (9, "Septiembre"), (10, "Octubre"), (11, "Noviembre"),
    (12, "Diciembre"),
)
SEMANAS = (
    (0, "Todo el mes"),
    (1, "Semana 1 · días 1–7"),
    (2, "Semana 2 · días 8–14"),
    (3, "Semana 3 · días 15–21"),
    (4, "Semana 4 · día 22 al cierre"),
)
_NOMBRE_MES = dict(MESES)


def _entero(valor, default: int) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


def _periodos_validos(periodos: Iterable) -> list[tuple[int, int]]:
    salida: set[tuple[int, int]] = set()
    for item in periodos or ():
        try:
            if isinstance(item, dict):
                anio, mes = int(item["anio"]), int(item["mes"])
            else:
                anio, mes = int(item[0]), int(item[1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if 2000 <= anio <= 2100 and 1 <= mes <= 12:
            salida.add((anio, mes))
    return sorted(salida, reverse=True)


def rango_periodo(anio: int, mes: int, semana: int = 0) -> tuple[date, date]:
    """Devuelve un rango semiabierto ``[desde, hasta)``."""
    anio, mes = int(anio), int(mes)
    semana = int(semana or 0)
    if not (2000 <= anio <= 2100 and 1 <= mes <= 12 and 0 <= semana <= 4):
        raise ValueError("Período inválido")

    if mes == 12:
        siguiente = date(anio + 1, 1, 1)
    else:
        siguiente = date(anio, mes + 1, 1)
    if semana == 0:
        return date(anio, mes, 1), siguiente

    inicio_dia = 1 + ((semana - 1) * 7)
    desde = date(anio, mes, inicio_dia)
    if semana == 4:
        return desde, siguiente
    return desde, date(anio, mes, inicio_dia + 7)


def normalizar_periodo(
    anio,
    mes,
    semana,
    periodos_disponibles: Iterable = (),
    *,
    hoy: date | None = None,
) -> dict:
    """Normaliza filtros opcionales; vacío significa todo el historial.

    La jerarquía evita períodos ambiguos: primero año, después mes y recién
    entonces semana. Por eso un mes sin año o una semana sin mes se ignoran.
    """
    hoy = hoy or date.today()
    disponibles = _periodos_validos(periodos_disponibles)
    anio_valor = _entero(anio, 0)
    if not 2000 <= anio_valor <= 2100:
        anio_valor = 0

    mes_valor = _entero(mes, 0) if anio_valor else 0
    if not 1 <= mes_valor <= 12:
        mes_valor = 0

    semana_valor = _entero(semana, 0) if mes_valor else 0
    if semana_valor not in {1, 2, 3, 4}:
        semana_valor = 0

    if not anio_valor:
        desde = hasta = None
        etiqueta = "Todo el historial"
    elif not mes_valor:
        desde = date(anio_valor, 1, 1)
        hasta = date(anio_valor + 1, 1, 1)
        etiqueta = f"Año {anio_valor}"
    else:
        desde, hasta = rango_periodo(anio_valor, mes_valor, semana_valor)

    if anio_valor and mes_valor and semana_valor:
        ultimo_dia = hasta.day - 1 if hasta.month == mes_valor else calendar.monthrange(
            anio_valor, mes_valor
        )[1]
        etiqueta = (
            f"{desde.day}–{ultimo_dia} de "
            f"{_NOMBRE_MES[mes_valor].lower()} de {anio_valor}"
        )
    elif anio_valor and mes_valor:
        etiqueta = f"{_NOMBRE_MES[mes_valor]} {anio_valor}"

    anios = sorted(
        {a for a, _ in disponibles} | {hoy.year, anio_valor}, reverse=True
    )
    return {
        "anio": anio_valor,
        "mes": mes_valor,
        "semana": semana_valor,
        "desde": desde,
        "hasta": hasta,
        "etiqueta": etiqueta,
        "anios": anios,
        "meses": MESES,
        "semanas": SEMANAS,
        "activo": bool(anio_valor),
        "tiene_actividad_historica": bool(disponibles),
    }
