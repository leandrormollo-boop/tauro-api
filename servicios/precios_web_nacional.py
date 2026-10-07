"""Precio comercial de la cotización nacional pública.

La web pública y las cuentas autenticadas son canales distintos. OCA conserva
su pricing por cliente en ``cliente_courier_config``; este módulo sólo lee y
escribe las dos perillas globales reservadas para la web.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from core.database import get_conn
from servicios.numeros_humanos import decimal_a_texto, parse_numero_humano


PARAMETRO_HABILITADO = "WEB_OCA_PUBLICA_HABILITADA"
PARAMETRO_MARKUP_PCT = "WEB_MARKUP_PCT_OCA"
PARAMETROS_RESERVADOS = frozenset({PARAMETRO_HABILITADO, PARAMETRO_MARKUP_PCT})
_MAX_MARKUP_PCT = Decimal("300")


class MotivoPrecioWebNoDisponible(str, Enum):
    """Motivo estable para decidir qué estado puede mostrarse fuera de ADMIN."""

    CANAL_DESACTIVADO = "canal_desactivado"
    PRECIO_NO_CONFIGURADO = "precio_no_configurado"
    CONFIGURACION_INVALIDA = "configuracion_invalida"
    LECTURA_CONFIG = "lectura_config"


class PrecioWebNoDisponible(ValueError):
    """La web no tiene una regla OCA explícita y publicable."""

    def __init__(
        self,
        mensaje: str,
        *,
        motivo: MotivoPrecioWebNoDisponible = MotivoPrecioWebNoDisponible.LECTURA_CONFIG,
    ):
        super().__init__(mensaje)
        self.motivo = motivo


def _parsear_markup(valor: Any) -> Decimal | None:
    texto = "" if valor is None else str(valor).strip()
    if not texto:
        return None
    try:
        numero = parse_numero_humano(texto)
    except ValueError:
        raise ValueError(
            "El porcentaje de ganancia de OCA debe ser válido, por ejemplo 20 o 20,5."
        ) from None
    if numero is None or not numero.is_finite() or numero < 0:
        raise ValueError("El porcentaje de ganancia de OCA no puede ser negativo.")
    if numero > _MAX_MARKUP_PCT:
        raise ValueError("El porcentaje de ganancia de OCA no puede superar 300%.")
    if numero.as_tuple().exponent < -4:
        raise ValueError("El porcentaje de ganancia de OCA admite hasta cuatro decimales.")
    return numero


def _desde_filas(filas: Iterable[dict]) -> dict:
    valores = {
        str(fila.get("parametro") or "").strip().upper(): fila.get("valor")
        for fila in filas
    }
    habilitada = str(valores.get(PARAMETRO_HABILITADO) or "").strip() == "1"
    error = None
    try:
        markup = _parsear_markup(valores.get(PARAMETRO_MARKUP_PCT))
    except ValueError as exc:
        markup = None
        error = str(exc)

    if error:
        estado = "La configuración comercial de OCA es inválida."
    elif markup is None:
        estado = "Falta cargar el porcentaje de ganancia de OCA."
    elif not habilitada:
        estado = "La cotización pública de OCA está desactivada."
    else:
        estado = "La configuración comercial de OCA está lista."

    return {
        "habilitada": habilitada,
        "markup_pct": markup,
        "markup_texto": decimal_a_texto(markup) if markup is not None else "",
        "configuracion_completa": markup is not None and error is None,
        "publicable": habilitada and markup is not None and error is None,
        "estado": estado,
        "error": error,
    }


def _leer_filas(cur=None) -> list[dict]:
    consulta = """
        SELECT parametro, valor
          FROM config
         WHERE parametro IN (%s, %s)
    """
    parametros = (PARAMETRO_HABILITADO, PARAMETRO_MARKUP_PCT)
    if cur is not None:
        cur.execute(consulta, parametros)
        return [dict(fila) for fila in (cur.fetchall() or [])]
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(consulta, parametros)
            return [dict(fila) for fila in (cursor.fetchall() or [])]


def leer_configuracion_oca() -> dict:
    """Vista segura para ADMIN; una falla de DB nunca simula una activación."""
    try:
        return _desde_filas(_leer_filas())
    except Exception as exc:
        print(
            "[precios-web] no pude leer la configuración OCA: "
            f"{type(exc).__name__}"
        )
        return {
            "habilitada": False,
            "markup_pct": None,
            "markup_texto": "",
            "configuracion_completa": False,
            "publicable": False,
            "estado": "No pudimos leer la configuración de precios.",
            "error": "No pudimos leer la configuración de precios.",
        }


def pricing_publico_oca() -> dict:
    """Regla explícita para OCA web, sin fallback ni herencia de clientes."""
    try:
        configuracion = _desde_filas(_leer_filas())
    except Exception:
        raise PrecioWebNoDisponible(
            "No se pudo verificar el precio público de OCA.",
            motivo=MotivoPrecioWebNoDisponible.LECTURA_CONFIG,
        ) from None
    if not configuracion["publicable"]:
        if not configuracion["habilitada"]:
            motivo = MotivoPrecioWebNoDisponible.CANAL_DESACTIVADO
        elif configuracion["error"]:
            motivo = MotivoPrecioWebNoDisponible.CONFIGURACION_INVALIDA
        else:
            motivo = MotivoPrecioWebNoDisponible.PRECIO_NO_CONFIGURADO
        raise PrecioWebNoDisponible(configuracion["estado"], motivo=motivo)
    return {
        "tipo": "PCT",
        "valor": configuracion["markup_pct"],
        "oca_tarifa_neta_iva_21": True,
    }


def guardar_configuracion_oca(
    *,
    request,
    habilitada: bool,
    markup_pct: Any,
) -> dict:
    """Guarda precio y auditoría en una única transacción.

    Un markup vacío se puede conservar únicamente con el canal apagado. En ese
    caso se elimina la perilla comercial para que una activación posterior no
    herede un valor que el administrador creyó haber borrado.
    """
    markup = _parsear_markup(markup_pct)
    if habilitada and markup is None:
        raise ValueError(
            "Cargá el porcentaje de ganancia de OCA antes de activar la cotización pública."
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("tauro:web-pricing:oca",),
            )
            antes = _desde_filas(_leer_filas(cur))
            cur.execute(
                """
                INSERT INTO config (parametro, valor)
                VALUES (%s, %s)
                ON CONFLICT (parametro) DO UPDATE SET valor = EXCLUDED.valor
                """,
                (PARAMETRO_HABILITADO, "1" if habilitada else "0"),
            )
            if markup is None:
                cur.execute(
                    "DELETE FROM config WHERE parametro = %s",
                    (PARAMETRO_MARKUP_PCT,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO config (parametro, valor)
                    VALUES (%s, %s)
                    ON CONFLICT (parametro) DO UPDATE SET valor = EXCLUDED.valor
                    """,
                    (PARAMETRO_MARKUP_PCT, decimal_a_texto(markup)),
                )

            despues = _desde_filas([
                {"parametro": PARAMETRO_HABILITADO,
                 "valor": "1" if habilitada else "0"},
                {"parametro": PARAMETRO_MARKUP_PCT,
                 "valor": decimal_a_texto(markup) if markup is not None else ""},
            ])
            from servicios.auditoria import registrar_desde_request_con_cursor
            registrar_desde_request_con_cursor(
                cur,
                request,
                event="admin.configurar_precio_web_oca",
                actor_type="admin",
                actor_ref="WEB:OCA",
                status_code=303,
                metadata={
                    "antes": {
                        "habilitada": antes["habilitada"],
                        "markup_pct": antes["markup_texto"] or None,
                    },
                    "despues": {
                        "habilitada": despues["habilitada"],
                        "markup_pct": despues["markup_texto"] or None,
                    },
                },
            )
    return despues
