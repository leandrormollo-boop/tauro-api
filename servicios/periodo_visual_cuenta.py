"""Normaliza la ventana visible de la cuenta sin cambiar el libro contable."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from servicios.cuenta_corriente import normalizar_periodo_mensual_cuenta
from servicios.filtros_cuenta import normalizar_filtros_cuenta
from servicios.periodo_cuenta import inicio_cuenta_cliente


_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")
_VENTANAS = {"anio", "septiembre", "rango", "mes"}


def _hoy_argentina() -> date:
    return datetime.now(_ARGENTINA).date()


def _label_rango(desde: date, hasta: date) -> str:
    return f"{desde.strftime('%d/%m/%Y')}–{hasta.strftime('%d/%m/%Y')}"


def normalizar_ventana_cuenta(
    cliente: str,
    *,
    ventana: str = "",
    periodo: str = "",
    desde: str = "",
    hasta: str = "",
    hoy: date | None = None,
) -> dict[str, str]:
    """Devuelve un rango cerrado y canónico para historial, pagos y exportación.

    La ventana explícita descarta los parámetros de modos inactivos. Sin ella,
    ``periodo`` identifica las URLs mensuales anteriores; las fechas indican
    un rango personalizado y, sin parámetros, se muestra el año corriente.
    """
    cliente_normalizado = str(cliente or "").strip().upper()
    if not cliente_normalizado or len(cliente_normalizado) > 80:
        raise ValueError("El cliente no es válido.")
    hoy = hoy or _hoy_argentina()
    if type(hoy) is not date:
        raise ValueError("La fecha actual no es válida.")

    ventana_normalizada = str(ventana or "").strip().lower()
    periodo_normalizado = str(periodo or "").strip()
    if not ventana_normalizada:
        if periodo_normalizado:
            ventana_normalizada = "mes"
        else:
            ventana_normalizada = "rango" if desde or hasta else "anio"
    if ventana_normalizada not in _VENTANAS:
        raise ValueError("El período seleccionado no es válido.")

    if ventana_normalizada == "mes":
        mes = normalizar_periodo_mensual_cuenta(periodo_normalizado)
        if not mes["clave"]:
            raise ValueError("Seleccioná un mes para ver sus movimientos.")
        return {
            "ventana": "mes",
            "desde": mes["desde"],
            "hasta": mes["hasta"],
            "periodo": mes["clave"],
            "label": mes["label"],
        }

    if ventana_normalizada == "rango":
        if not str(desde or "").strip() or not str(hasta or "").strip():
            raise ValueError("Completá las fechas Desde y Hasta.")
        fechas = normalizar_filtros_cuenta("", desde, hasta)
        inicio = date.fromisoformat(fechas["desde"])
        fin = date.fromisoformat(fechas["hasta"])
        return {
            "ventana": "rango",
            "desde": fechas["desde"],
            "hasta": fechas["hasta"],
            "periodo": "",
            "label": _label_rango(inicio, fin),
        }

    fin_anio = date(hoy.year, 12, 31)
    if ventana_normalizada == "anio":
        return {
            "ventana": "anio",
            "desde": date(hoy.year, 1, 1).isoformat(),
            "hasta": fin_anio.isoformat(),
            "periodo": "",
            "label": f"Este año · {hoy.year}",
        }

    inicio = inicio_cuenta_cliente(cliente_normalizado)
    if inicio is None:
        inicio = date(hoy.year, 9, 1)
        if hoy < inicio:
            raise ValueError("El período desde septiembre todavía no está disponible.")
    if inicio > fin_anio:
        raise ValueError("El período desde septiembre todavía no está disponible.")
    return {
        "ventana": "septiembre",
        "desde": inicio.isoformat(),
        "hasta": fin_anio.isoformat(),
        "periodo": "",
        "label": f"Desde septiembre de {inicio.year}",
    }
