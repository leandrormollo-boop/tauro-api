"""Parseo determinista de numeros escritos por personas.

El portal recibe valores de usuarios que alternan formato espanol e ingles.
No se puede decidir que ``10.000`` es decimal solo porque contiene un punto:
para un importe TAURO significa diez mil. Este modulo separa los dos contratos
para que cada caller elija la politica de ambiguedad de forma explicita.

Las funciones devuelven :class:`~decimal.Decimal` para no introducir redondeo
binario durante el parseo. Convertir a ``float`` (si un calculo legacy lo
necesita) queda a cargo del borde que integra este modulo.
"""

from __future__ import annotations

from decimal import Decimal
import math
from typing import Any


class NumeroHumanoInvalido(ValueError):
    """El valor no cumple ninguna de las sintaxis numericas admitidas."""


class NumeroHumanoAmbiguo(NumeroHumanoInvalido):
    """El texto admite interpretaciones decimal y de miles distintas."""


def politica_configuracion_numerica(parametro: str) -> str | None:
    """Devuelve ``importe``/``decimal`` para las perillas numéricas conocidas."""

    clave = (parametro or "").strip().upper()
    if (
        clave in {
            "COTIZACION_DOLAR_ARS", "MARGEN_MINIMO_ALERTA_ARS",
            "MARGEN_MINIMO_ARS",
        }
        or (clave.startswith("WEB_MARGEN_FIJO_") and clave.endswith("_ARS"))
        or (clave.startswith("WEB_ADICIONAL_") and clave.endswith("_ARS"))
    ):
        return "importe"
    if (
        clave.startswith("WEB_MARKUP_PCT")
        or clave in {"WEB_DESC_FEDEX_PCT", "WEB_MARGEN_MINIMO_PCT"}
    ):
        return "decimal"
    return None


def parse_configuracion_numerica(parametro: str, valor: Any) -> Decimal | None:
    """Parsea y valida una perilla financiera editable desde ``/admin/config``."""

    politica = politica_configuracion_numerica(parametro)
    if politica is None:
        raise NumeroHumanoInvalido("El parámetro no tiene una política numérica conocida.")
    numero = (
        parse_importe_humano(valor)
        if politica == "importe"
        else parse_numero_humano(valor)
    )
    if numero is None:
        return None
    if numero < 0:
        raise NumeroHumanoInvalido(f"{parametro}: el valor no puede ser negativo.")
    clave = (parametro or "").strip().upper()
    if clave == "WEB_DESC_FEDEX_PCT" and numero > 100:
        raise NumeroHumanoInvalido("WEB_DESC_FEDEX_PCT: el máximo es 100.")
    if clave == "COTIZACION_DOLAR_ARS" and numero <= 0:
        raise NumeroHumanoInvalido("COTIZACION_DOLAR_ARS debe ser mayor a cero.")
    return numero


def decimal_a_texto(numero: Decimal) -> str:
    """Serialización canónica sin notación científica ni ceros artificiales."""

    texto = format(numero, "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto or "0"


def parse_numero_humano(valor: Any) -> Decimal | None:
    """Parsea un numero ES/EN sin adivinar separadores ambiguos.

    Reglas:

    * ``5,5`` y ``5.5`` equivalen a ``Decimal("5.5")``.
    * Miles + decimales pueden escribirse como ``1.234,56`` o ``1,234.56``.
    * Agrupaciones repetidas validas, como ``1.234.567``, son enteros.
    * Una unica coma/punto seguida de exactamente tres cifras es ambigua y se
      rechaza: ``10.000`` podria ser diez o diez mil.
    * ``None`` y texto vacio devuelven ``None``. Todo otro formato invalido
      lanza :class:`NumeroHumanoInvalido`.

    No acepta simbolos monetarios, notacion cientifica, espacios internos ni
    valores no finitos. No redondea ni limita la cantidad de decimales.
    """

    return _parsear(valor, tres_decimales_como_miles=False)


def parse_importe_humano(valor: Any) -> Decimal | None:
    """Parsea un importe ES/EN con la convencion de miles de TAURO.

    Conserva las reglas de :func:`parse_numero_humano`, salvo una decision de
    negocio explicita: si hay un unico separador y exactamente tres cifras a
    su derecha, se interpreta como miles cuando la parte izquierda forma un
    primer grupo valido (de una a tres cifras y distinto de cero).

    Por eso ``10.000`` y ``10,000`` valen 10 000; ``100.000`` y ``100,000``
    valen 100 000. Una forma como ``1234.567`` sigue rechazandose por ambigua,
    porque no es una agrupacion de miles valida. ``0.500`` se conserva como
    decimal 0.500: nunca se transforma un importe menor que uno en 500.
    """

    return _parsear(valor, tres_decimales_como_miles=True)


def parse_float_formulario(
    valor: Any,
    campo: str,
    *,
    importe: bool = False,
    requerido: bool = True,
    minimo: int | float | Decimal | None = None,
    maximo: int | float | Decimal | None = None,
) -> float | None:
    """Borde común para formularios del portal y el admin.

    Mantiene el parseo exacto en ``Decimal`` hasta validar presencia y rango;
    recién entonces devuelve ``float`` para los modelos legacy. De este modo
    quitar ``type=number`` del HTML no elimina las reglas de negocio del
    servidor y todos los formularios muestran el mismo mensaje de ayuda.
    """

    try:
        numero = (
            parse_importe_humano(valor)
            if importe
            else parse_numero_humano(valor)
        )
    except ValueError:
        ejemplo = "100.000 o 100,000" if importe else "5,5 o 5.5"
        raise NumeroHumanoInvalido(
            f"{campo}: ingresá un número válido, por ejemplo {ejemplo}."
        ) from None

    if numero is None:
        if requerido:
            raise NumeroHumanoInvalido(f"{campo}: completá este valor.")
        return None

    if minimo is not None and numero < Decimal(str(minimo)):
        raise NumeroHumanoInvalido(f"{campo}: el mínimo es {minimo}.")
    if maximo is not None and numero > Decimal(str(maximo)):
        raise NumeroHumanoInvalido(f"{campo}: el máximo es {maximo}.")

    resultado = float(numero)
    if not math.isfinite(resultado):
        raise NumeroHumanoInvalido(f"{campo}: el valor es demasiado grande.")
    return resultado


def _parsear(valor: Any, *, tres_decimales_como_miles: bool) -> Decimal | None:
    directo = _numero_directo(valor)
    if directo is not _NO_DIRECTO:
        return directo

    texto = str(valor).strip()
    if not texto:
        return None

    signo = ""
    if texto[0] in "+-":
        signo, texto = texto[0], texto[1:]
    if not texto or any(caracter not in "0123456789.," for caracter in texto):
        raise NumeroHumanoInvalido("Formato numerico invalido.")

    puntos = texto.count(".")
    comas = texto.count(",")

    if puntos and comas:
        canonico = _parsear_dos_separadores(texto)
    elif puntos or comas:
        separador = "." if puntos else ","
        canonico = _parsear_un_separador(
            texto,
            separador,
            tres_decimales_como_miles=tres_decimales_como_miles,
        )
    else:
        if not texto.isascii() or not texto.isdigit():
            raise NumeroHumanoInvalido("Formato numerico invalido.")
        canonico = texto

    numero = Decimal(signo + canonico)
    if not numero.is_finite():
        raise NumeroHumanoInvalido("El numero debe ser finito.")
    return numero


def _parsear_dos_separadores(texto: str) -> str:
    """El ultimo tipo de separador es decimal; el otro agrupa miles."""

    decimal = "." if texto.rfind(".") > texto.rfind(",") else ","
    miles = "," if decimal == "." else "."

    # Un mismo caracter no puede actuar primero como miles y luego como
    # decimal (por ejemplo ``1,234,56``). El separador decimal aparece una vez.
    if texto.count(decimal) != 1:
        raise NumeroHumanoInvalido("Los separadores decimales estan repetidos.")

    entero, fraccion = texto.rsplit(decimal, 1)
    if not fraccion or not fraccion.isascii() or not fraccion.isdigit():
        raise NumeroHumanoInvalido("La parte decimal no es valida.")

    grupos = entero.split(miles)
    if not _grupos_de_miles_validos(grupos):
        raise NumeroHumanoInvalido("La agrupacion de miles no es valida.")
    return "".join(grupos) + "." + fraccion


def _parsear_un_separador(
    texto: str,
    separador: str,
    *,
    tres_decimales_como_miles: bool,
) -> str:
    cantidad = texto.count(separador)
    grupos = texto.split(separador)

    if cantidad > 1:
        if not _grupos_de_miles_validos(grupos):
            raise NumeroHumanoInvalido("La agrupacion de miles no es valida.")
        return "".join(grupos)

    entero, fraccion = grupos
    if (
        not entero
        or not fraccion
        or not entero.isascii()
        or not fraccion.isascii()
        or not entero.isdigit()
        or not fraccion.isdigit()
    ):
        raise NumeroHumanoInvalido("Formato numerico invalido.")

    if len(fraccion) == 3 and int(entero) != 0:
        primer_grupo_valido = 1 <= len(entero) <= 3
        if tres_decimales_como_miles and primer_grupo_valido:
            return entero + fraccion
        raise NumeroHumanoAmbiguo(
            "Una unica separacion con tres cifras puede ser decimal o de miles."
        )

    return entero + "." + fraccion


def _grupos_de_miles_validos(grupos: list[str]) -> bool:
    if len(grupos) < 2:
        return False
    primero, *resto = grupos
    return (
        1 <= len(primero) <= 3
        and primero.isascii()
        and primero.isdigit()
        and all(
            len(grupo) == 3 and grupo.isascii() and grupo.isdigit()
            for grupo in resto
        )
    )


class _NoDirecto:
    pass


_NO_DIRECTO = _NoDirecto()


def _numero_directo(valor: Any) -> Decimal | None | _NoDirecto:
    """Acepta tipos numericos sin convertir floats mediante su expansion binaria."""

    if valor is None:
        return None
    if isinstance(valor, bool):
        raise NumeroHumanoInvalido("Un booleano no es un numero ingresado.")
    if isinstance(valor, Decimal):
        if not valor.is_finite():
            raise NumeroHumanoInvalido("El numero debe ser finito.")
        return valor
    if isinstance(valor, int):
        return Decimal(valor)
    if isinstance(valor, float):
        if not math.isfinite(valor):
            raise NumeroHumanoInvalido("El numero debe ser finito.")
        return Decimal(str(valor))
    return _NO_DIRECTO
