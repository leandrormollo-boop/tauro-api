"""Regla comercial de DHL para el cotizador internacional público.

El checkout de Shopify, la caché y el portal comparten históricamente el motor
de carriers, pero no son el mismo canal comercial que ``/cotizar-web``. Este
módulo reserva claves nuevas para la web pública y conserva la regla histórica
de DHL solamente mientras no exista un modo explícito guardado.
"""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from core.database import get_conn
from servicios.numeros_humanos import (
    decimal_a_texto,
    parse_importe_humano,
    parse_numero_humano,
)


PARAMETRO_MODO = "WEB_DHL_PRICING_MODE"
PARAMETRO_MARKUP_PCT = "WEB_DHL_MARKUP_PCT"
PARAMETRO_FIJO_ARS = "WEB_DHL_MARGEN_FIJO_ARS"

PARAMETRO_MARKUP_PCT_LEGACY = "WEB_MARKUP_PCT_DHL"
PARAMETRO_FIJO_ARS_LEGACY = "WEB_MARGEN_FIJO_DHL_ARS"
PARAMETRO_MARKUP_GENERAL = "WEB_MARKUP_PCT"

# El admin genérico no debe permitir editar por otra vía ni las claves nuevas
# ni las anteriores: mezclar ambos juegos haría ambiguo el precio efectivo.
PARAMETROS_RESERVADOS = frozenset({
    PARAMETRO_MODO,
    PARAMETRO_MARKUP_PCT,
    PARAMETRO_FIJO_ARS,
    PARAMETRO_MARKUP_PCT_LEGACY,
    PARAMETRO_FIJO_ARS_LEGACY,
})

MODO_PCT = "PCT"
MODO_FIJO_ARS = "FIJO_ARS"
MODOS = frozenset({MODO_PCT, MODO_FIJO_ARS})

MAX_MARKUP_PCT = Decimal("300")
MAX_FIJO_ARS = Decimal("999999999")
DEFAULT_MARKUP_PCT = Decimal("20")

# Claves privadas para compartir una única lectura de DB con carriers.py. No
# salen en ninguna respuesta pública.
FILAS_CONFIG_KEY = "__dhl_pricing_rows__"
ERROR_DB_CONFIG_KEY = "__dhl_pricing_db_error__"

_CLAVES_DB = (
    PARAMETRO_MODO,
    PARAMETRO_MARKUP_PCT,
    PARAMETRO_FIJO_ARS,
    PARAMETRO_MARKUP_PCT_LEGACY,
    PARAMETRO_FIJO_ARS_LEGACY,
    PARAMETRO_MARKUP_GENERAL,
)


class PrecioWebDHLNoDisponible(ValueError):
    """DHL no tiene una regla pública verificable y segura."""


def _texto(valor: Any) -> str:
    return "" if valor is None else str(valor).strip()


def _porcentaje_visible(valor: Decimal) -> str:
    return decimal_a_texto(valor).replace(".", ",")


def _ars_visible(valor: Decimal) -> str:
    ingles = f"{valor.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
    return ingles.replace(",", "_").replace(".", ",").replace("_", ".")


def _parsear_markup(valor: Any) -> Decimal:
    try:
        numero = parse_numero_humano(valor)
    except ValueError:
        raise ValueError(
            "El porcentaje de DHL debe ser válido, por ejemplo 20 o 20,5."
        ) from None
    if numero is None:
        raise ValueError("Cargá el porcentaje de DHL.")
    if not numero.is_finite() or numero < 0:
        raise ValueError("El porcentaje de DHL no puede ser negativo.")
    if numero > MAX_MARKUP_PCT:
        raise ValueError("El porcentaje de DHL no puede superar 300%.")
    if numero.as_tuple().exponent < -4:
        raise ValueError("El porcentaje de DHL admite hasta cuatro decimales.")
    return numero


def _parsear_fijo(valor: Any) -> Decimal:
    try:
        numero = parse_importe_humano(valor)
    except ValueError:
        raise ValueError(
            "El margen fijo de DHL debe ser un importe válido, por ejemplo 135.000."
        ) from None
    if numero is None:
        raise ValueError("Cargá el margen fijo de DHL.")
    if not numero.is_finite() or numero < 0:
        raise ValueError("El margen fijo de DHL no puede ser negativo.")
    if numero > MAX_FIJO_ARS:
        raise ValueError("El margen fijo de DHL no puede superar ARS 999.999.999.")
    if numero.as_tuple().exponent < -2:
        raise ValueError("El margen fijo de DHL admite hasta dos decimales.")
    return numero


def _valores(filas: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        _texto(fila.get("parametro")).upper(): fila.get("valor")
        for fila in filas
        if _texto(fila.get("parametro"))
    }


def _elegir(
    opciones: Iterable[tuple[str, Any, bool]],
    *,
    default: tuple[str, Any],
) -> tuple[Any, str]:
    for fuente, valor, presente in opciones:
        if presente:
            return valor, fuente
    return default[1], default[0]


def _resolver(
    filas: Iterable[Mapping[str, Any]],
    *,
    entorno: Mapping[str, str] | None = None,
) -> dict:
    """Resuelve el modo sin validar una regla inactiva.

    Esto es esencial para la configuración histórica real: un porcentaje
    viejo fuera del rango de la nueva UI no puede apagar el fijo que hoy sí
    gobierna el precio.
    """
    entorno = os.environ if entorno is None else entorno
    db = _valores(filas)

    modo_crudo, modo_fuente = _elegir(
        (
            ("DB_NUEVA", db.get(PARAMETRO_MODO), PARAMETRO_MODO in db),
            ("ENV_NUEVO", entorno.get(PARAMETRO_MODO), PARAMETRO_MODO in entorno),
        ),
        default=("LEGACY", None),
    )
    modo_explicito = modo_crudo is not None
    modo_solicitado = _texto(modo_crudo).upper() if modo_explicito else None

    markup_nuevo = (
        ("DB_NUEVA", db.get(PARAMETRO_MARKUP_PCT), PARAMETRO_MARKUP_PCT in db),
        ("ENV_NUEVO", entorno.get(PARAMETRO_MARKUP_PCT), PARAMETRO_MARKUP_PCT in entorno),
    )
    markup_legacy = (
        ("DB_DHL_LEGACY", db.get(PARAMETRO_MARKUP_PCT_LEGACY), PARAMETRO_MARKUP_PCT_LEGACY in db),
        ("ENV_DHL_LEGACY", entorno.get(PARAMETRO_MARKUP_PCT_LEGACY), PARAMETRO_MARKUP_PCT_LEGACY in entorno),
        ("DB_GENERAL", db.get(PARAMETRO_MARKUP_GENERAL), PARAMETRO_MARKUP_GENERAL in db),
        ("ENV_GENERAL", entorno.get(PARAMETRO_MARKUP_GENERAL), PARAMETRO_MARKUP_GENERAL in entorno),
    )
    fijo_nuevo = (
        ("DB_NUEVA", db.get(PARAMETRO_FIJO_ARS), PARAMETRO_FIJO_ARS in db),
        ("ENV_NUEVO", entorno.get(PARAMETRO_FIJO_ARS), PARAMETRO_FIJO_ARS in entorno),
    )
    fijo_legacy = (
        ("DB_DHL_LEGACY", db.get(PARAMETRO_FIJO_ARS_LEGACY), PARAMETRO_FIJO_ARS_LEGACY in db),
        ("ENV_DHL_LEGACY", entorno.get(PARAMETRO_FIJO_ARS_LEGACY), PARAMETRO_FIJO_ARS_LEGACY in entorno),
    )

    if modo_explicito:
        # La regla activa exige su clave nueva. Para el campo inactivo sí se
        # muestra el valor histórico, para que el admin pueda corregirlo antes
        # de cambiar de modo, pero nunca se lo aplica a una cotización.
        #
        # Modo y valor forman una unidad por origen: un modo guardado en DB no
        # puede completar su valor desde Railway, ni un modo de Railway tomar
        # una fila huérfana de DB. Mezclarlos haría que una configuración
        # parcial publicara un precio distinto del que el admin cree activo.
        if modo_fuente == "DB_NUEVA":
            markup_del_modo = markup_nuevo[:1]
            fijo_del_modo = fijo_nuevo[:1]
        else:
            markup_del_modo = markup_nuevo[1:]
            fijo_del_modo = fijo_nuevo[1:]
        markup_opciones = (
            markup_del_modo
            if modo_solicitado == MODO_PCT
            else markup_del_modo + markup_legacy
        )
        fijo_opciones = (
            fijo_del_modo
            if modo_solicitado == MODO_FIJO_ARS
            else fijo_del_modo + fijo_legacy
        )
        markup_crudo, markup_fuente = _elegir(
            markup_opciones, default=("AUSENTE", None)
        )
        fijo_crudo, fijo_fuente = _elegir(
            fijo_opciones, default=("AUSENTE", None)
        )
    else:
        markup_crudo, markup_fuente = _elegir(
            markup_legacy,
            default=("DEFAULT", DEFAULT_MARKUP_PCT),
        )
        fijo_crudo, fijo_fuente = _elegir(
            fijo_legacy,
            default=("DEFAULT", Decimal("0")),
        )

    try:
        markup = _parsear_markup(markup_crudo)
        markup_error = None
    except ValueError as exc:
        markup = None
        markup_error = str(exc)
    try:
        fijo = _parsear_fijo(fijo_crudo)
        fijo_error = None
    except ValueError as exc:
        fijo = None
        fijo_error = str(exc)

    error_modo = None
    if modo_explicito:
        if modo_solicitado not in MODOS:
            modo = modo_solicitado or ""
            error_modo = "El modo de precio de DHL no es válido."
        else:
            modo = modo_solicitado
    elif fijo_error:
        # La prioridad legacy depende del fijo. Si no podemos interpretarlo,
        # tampoco podemos decidir con seguridad si corresponde porcentaje.
        modo = ""
    else:
        modo = MODO_FIJO_ARS if fijo and fijo > 0 else MODO_PCT

    if error_modo:
        error = error_modo
    elif modo == MODO_PCT:
        error = markup_error
    elif modo == MODO_FIJO_ARS:
        error = fijo_error
    else:
        error = fijo_error or "No se pudo determinar el modo de precio de DHL."

    if error:
        estado = error
    elif modo == MODO_PCT:
        estado = f"Ganancia aplicada: {_porcentaje_visible(markup)}% sobre costo."
    else:
        estado = f"Ganancia aplicada: $ {_ars_visible(fijo)} por envío."

    def texto_visible(crudo: Any, numero: Decimal | None) -> str:
        return decimal_a_texto(numero) if numero is not None else _texto(crudo)

    return {
        "modo": modo,
        "modo_explicito": modo_explicito,
        "modo_fuente": modo_fuente,
        "markup_pct": markup,
        "markup_texto": texto_visible(markup_crudo, markup),
        "markup_fuente": markup_fuente,
        "markup_valido": markup_error is None,
        "markup_error": markup_error,
        "margen_fijo_ars": fijo,
        "fijo_texto": texto_visible(fijo_crudo, fijo),
        "fijo_fuente": fijo_fuente,
        "fijo_valido": fijo_error is None,
        "fijo_error": fijo_error,
        "configuracion_completa": error is None,
        "publicable": error is None,
        "estado": estado,
        "error": error,
    }


def _leer_filas(cur=None) -> list[dict]:
    consulta = "SELECT parametro, valor FROM config WHERE parametro = ANY(%s)"
    parametros = (list(_CLAVES_DB),)
    if cur is not None:
        cur.execute(consulta, parametros)
        return [dict(fila) for fila in (cur.fetchall() or [])]
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(consulta, parametros)
            return [dict(fila) for fila in (cursor.fetchall() or [])]


def _error_lectura() -> dict:
    return {
        "modo": "",
        "modo_explicito": False,
        "modo_fuente": "ERROR_DB",
        "markup_pct": None,
        "markup_texto": "",
        "markup_fuente": "ERROR_DB",
        "markup_valido": False,
        "markup_error": "No pudimos leer la configuración de precios.",
        "margen_fijo_ars": None,
        "fijo_texto": "",
        "fijo_fuente": "ERROR_DB",
        "fijo_valido": False,
        "fijo_error": "No pudimos leer la configuración de precios.",
        "configuracion_completa": False,
        "publicable": False,
        "estado": "No pudimos leer la configuración de precios.",
        "error": "No pudimos leer la configuración de precios.",
    }


def leer_configuracion_dhl() -> dict:
    """Vista del admin con modo, valores efectivos y su procedencia."""
    try:
        return _resolver(_leer_filas())
    except Exception as exc:
        print(
            "[precios-web-dhl] no pude leer la configuración: "
            f"{type(exc).__name__}"
        )
        return _error_lectura()


def pricing_publico_dhl(config_compartida: Mapping[str, Any] | None = None) -> dict:
    """Devuelve solamente la regla activa que puede publicar la web."""
    if config_compartida is None:
        try:
            filas = _leer_filas()
        except Exception:
            raise PrecioWebDHLNoDisponible(
                "No se pudo verificar el precio público de DHL."
            ) from None
    else:
        if config_compartida.get(ERROR_DB_CONFIG_KEY):
            raise PrecioWebDHLNoDisponible(
                "No se pudo verificar el precio público de DHL."
            )
        filas = config_compartida.get(FILAS_CONFIG_KEY)
        if filas is None:
            # Compatibilidad con tests y callers internos que inyectan el mapa
            # numérico histórico en lugar de las filas crudas.
            filas = [
                {"parametro": clave, "valor": config_compartida[clave]}
                for clave in _CLAVES_DB
                if clave in config_compartida
            ]

    configuracion = _resolver(filas)
    if not configuracion["publicable"]:
        raise PrecioWebDHLNoDisponible(configuracion["estado"])
    valor = (
        configuracion["markup_pct"]
        if configuracion["modo"] == MODO_PCT
        else configuracion["margen_fijo_ars"]
    )
    return {
        "tipo": configuracion["modo"],
        "valor": valor,
        "fuente": (
            configuracion["markup_fuente"]
            if configuracion["modo"] == MODO_PCT
            else configuracion["fijo_fuente"]
        ),
        "modo_explicito": configuracion["modo_explicito"],
    }


def _decimal_positivo(valor: Any, campo: str) -> Decimal:
    try:
        numero = Decimal(str(valor))
    except Exception:
        raise PrecioWebDHLNoDisponible(f"{campo} inválido.") from None
    if not numero.is_finite() or numero <= 0:
        raise PrecioWebDHLNoDisponible(f"{campo} inválido.")
    return numero


def calcular_precio_publico_dhl(
    resultado: Mapping[str, Any],
    dolar: Any,
    pricing: Mapping[str, Any],
) -> dict:
    """Aplica la única regla seleccionada con Decimal y piso en el costo."""
    costo = _decimal_positivo(resultado.get("costo"), "Costo de DHL")
    tipo = _texto(pricing.get("tipo")).upper()
    valor = pricing.get("valor")
    if tipo == MODO_PCT:
        markup = _parsear_markup(valor)
        fijo = None
    elif tipo == MODO_FIJO_ARS:
        fijo = _parsear_fijo(valor)
        markup = None
    else:
        raise PrecioWebDHLNoDisponible("La regla de DHL no es válida.")

    moneda = _texto(resultado.get("moneda") or "USD").upper()
    if moneda not in {"USD", "ARS"}:
        raise PrecioWebDHLNoDisponible("La moneda de DHL no está soportada.")
    cambio = _decimal_positivo(dolar, "Tipo de cambio")

    # Antes del primer guardado se conserva incluso el redondeo Python del
    # motor histórico. Cambiar half-even por half-up en esa etapa podría mover
    # un peso sin que el admin hubiera elegido una regla nueva.
    if not bool(pricing.get("modo_explicito")):
        costo_float = float(costo)
        cambio_float = float(cambio)
        costo_ars_legacy = (
            round(costo_float * cambio_float)
            if moneda == "USD"
            else round(costo_float)
        )
        if tipo == MODO_FIJO_ARS:
            precio_ars_legacy = round(costo_ars_legacy + float(fijo))
        else:
            base_cruda = resultado.get("costo_lista")
            base_float = (
                costo_float
                if base_cruda in (None, "")
                else float(_decimal_positivo(base_cruda, "Tarifa de lista de DHL"))
            )
            lista_ars_legacy = (
                round(base_float * cambio_float)
                if moneda == "USD"
                else round(base_float)
            )
            precio_ars_legacy = round(
                lista_ars_legacy * (1 + float(markup) / 100)
            )
        if precio_ars_legacy < costo_ars_legacy:
            raise PrecioWebDHLNoDisponible(
                "La regla de DHL no cubre el costo del operador."
            )
        return {
            "precio_ars": precio_ars_legacy,
            "precio_usd": round(precio_ars_legacy / cambio_float, 2),
        }

    costo_ars = (
        costo * cambio if moneda == "USD" else costo
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    if tipo == MODO_FIJO_ARS:
        precio_ars = (costo_ars + fijo).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    else:
        # El modo nuevo es "porcentaje sobre costo". La tarifa de lista sólo
        # pertenece a la semántica histórica anterior al discriminador.
        base = costo
        base_ars = (
            base * cambio if moneda == "USD" else base
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        precio_ars = (
            base_ars * (Decimal("1") + markup / Decimal("100"))
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    if precio_ars < costo_ars:
        raise PrecioWebDHLNoDisponible(
            "La regla de DHL no cubre el costo del operador."
        )
    return {
        "precio_ars": int(precio_ars),
        "precio_usd": float(
            (precio_ars / cambio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ),
    }


def guardar_configuracion_dhl(
    *,
    request,
    modo: Any,
    markup_pct: Any,
    margen_fijo_ars: Any,
) -> dict:
    """Guarda sólo la regla activa y la auditoría en una transacción."""
    modo_normalizado = _texto(modo).upper()
    if modo_normalizado not in MODOS:
        raise ValueError("Elegí porcentaje o margen fijo para DHL.")

    markup = _parsear_markup(markup_pct) if modo_normalizado == MODO_PCT else None
    fijo = _parsear_fijo(margen_fijo_ars) if modo_normalizado == MODO_FIJO_ARS else None

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("tauro:web-pricing:dhl",),
            )
            filas_antes = _leer_filas(cur)
            antes = _resolver(filas_antes)
            valores = _valores(filas_antes)
            valores[PARAMETRO_MODO] = modo_normalizado
            cambios = [(PARAMETRO_MODO, modo_normalizado)]
            if modo_normalizado == MODO_PCT:
                texto_valor = decimal_a_texto(markup)
                valores[PARAMETRO_MARKUP_PCT] = texto_valor
                cambios.append((PARAMETRO_MARKUP_PCT, texto_valor))
            else:
                texto_valor = decimal_a_texto(fijo)
                valores[PARAMETRO_FIJO_ARS] = texto_valor
                cambios.append((PARAMETRO_FIJO_ARS, texto_valor))

            for parametro, valor in cambios:
                cur.execute(
                    """
                    INSERT INTO config (parametro, valor)
                    VALUES (%s, %s)
                    ON CONFLICT (parametro) DO UPDATE SET valor = EXCLUDED.valor
                    """,
                    (parametro, valor),
                )

            despues = _resolver([
                {"parametro": parametro, "valor": valor}
                for parametro, valor in valores.items()
            ])
            from servicios.auditoria import registrar_desde_request_con_cursor
            registrar_desde_request_con_cursor(
                cur,
                request,
                event="admin.configurar_precio_web_dhl",
                actor_type="admin",
                actor_ref="WEB:DHL",
                status_code=303,
                metadata={
                    "antes": {
                        "modo": antes["modo"] or None,
                        "modo_explicito": antes["modo_explicito"],
                        "markup_pct": antes["markup_texto"] or None,
                        "margen_fijo_ars": antes["fijo_texto"] or None,
                    },
                    "despues": {
                        "modo": despues["modo"],
                        "modo_explicito": despues["modo_explicito"],
                        "markup_pct": despues["markup_texto"] or None,
                        "margen_fijo_ars": despues["fijo_texto"] or None,
                    },
                },
            )
    return despues
