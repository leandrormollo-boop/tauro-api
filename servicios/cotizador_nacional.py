"""Contrato neutral del cotizador nacional.

Todavía no llama a OCA ni Andreani: valida y normaliza exactamente la
entrada que consumirán esos adapters, y devuelve estados explícitos. Esto
permite terminar la experiencia del portal sin inventar precios ni guías.
"""
from __future__ import annotations

from decimal import Decimal

from servicios.numeros_humanos import (
    decimal_a_texto,
    parse_importe_humano,
    parse_numero_humano,
)
from servicios.provincias import (
    descomponer_codigo_postal,
    nombre_provincia,
    normalizar_localidad,
    normalizar_provincia,
)


CARRIERS_NACIONALES = (
    {
        "id": "oca",
        "nombre": "OCA",
        "estado": "integracion_pendiente",
        "detalle": "Credenciales, operativas y contrato pendientes",
    },
    {
        "id": "andreani",
        "nombre": "Andreani",
        "estado": "integracion_pendiente",
        "detalle": "Credenciales, contratos y sucursales pendientes",
    },
)

MODALIDADES = {"domicilio", "sucursal"}


def _decimal_positivo(
    valor,
    campo: str,
    maximo: Decimal,
    *,
    importe: bool = False,
) -> Decimal:
    try:
        numero = parse_importe_humano(valor) if importe else parse_numero_humano(valor)
    except ValueError:
        ejemplo = "40.000 o 40,000" if importe else "5,5 o 5.5"
        raise ValueError(f"{campo} debe ser un número válido, por ejemplo {ejemplo}.") from None
    if numero is None or numero <= 0:
        raise ValueError(f"{campo} debe ser mayor a cero.")
    if numero > maximo:
        raise ValueError(f"{campo} supera el máximo admitido para preparar la consulta.")
    return numero


def _cantidad(valor) -> int:
    numero = _decimal_positivo(valor, "La cantidad de bultos", Decimal("20"))
    if numero != numero.to_integral_value():
        raise ValueError("La cantidad de bultos debe ser un número entero.")
    return int(numero)


def _modalidad(valor, campo: str) -> str:
    modalidad = str(valor or "").strip().lower()
    if modalidad not in MODALIDADES:
        raise ValueError(f"Elegí una modalidad de {campo} válida.")
    return modalidad


def preparar_cotizacion_nacional(
    *,
    origen_provincia: str,
    origen_localidad: str,
    origen_cp: str,
    modalidad_origen: str,
    destino_provincia: str,
    destino_localidad: str,
    destino_cp: str,
    modalidad_destino: str,
    cantidad_bultos,
    peso_kg,
    largo_cm,
    ancho_cm,
    alto_cm,
    valor_declarado_ars,
) -> dict:
    """Valida cualquier ruta provincia→provincia y arma el contrato común.

    Los límites son defensivos para el formulario, no promesas de cobertura.
    Cada adapter aplicará luego los topes comerciales de la cuenta y servicio.
    """
    origen_codigo = normalizar_provincia(origen_provincia)
    destino_codigo = normalizar_provincia(destino_provincia)
    if not origen_codigo:
        raise ValueError("Elegí una provincia de origen válida.")
    if not destino_codigo:
        raise ValueError("Elegí una provincia de destino válida.")

    origen_localidad = normalizar_localidad(origen_localidad)
    destino_localidad = normalizar_localidad(destino_localidad)
    if not origen_localidad:
        raise ValueError("Completá una localidad de origen válida.")
    if not destino_localidad:
        raise ValueError("Completá una localidad de destino válida.")

    origen_postal = descomponer_codigo_postal(origen_cp, origen_codigo)
    destino_postal = descomponer_codigo_postal(destino_cp, destino_codigo)
    if not origen_postal:
        raise ValueError(
            "El código postal de origen debe tener 4 dígitos o ser un CPA completo de esa provincia."
        )
    if not destino_postal:
        raise ValueError(
            "El código postal de destino debe tener 4 dígitos o ser un CPA completo de esa provincia."
        )

    cantidad = _cantidad(cantidad_bultos)
    peso = _decimal_positivo(peso_kg, "El peso por bulto", Decimal("1000"))
    largo = _decimal_positivo(largo_cm, "El largo", Decimal("300"))
    ancho = _decimal_positivo(ancho_cm, "El ancho", Decimal("300"))
    alto = _decimal_positivo(alto_cm, "El alto", Decimal("300"))
    valor_declarado = _decimal_positivo(
        valor_declarado_ars,
        "El valor declarado",
        Decimal("999999999"),
        importe=True,
    )
    # OCA documenta ValorDeclarado como entero. No se redondea dinero del
    # cliente de forma silenciosa para hacer que el payload "entre".
    if valor_declarado != valor_declarado.to_integral_value():
        raise ValueError(
            "El valor declarado debe ingresarse en pesos enteros para poder comparar OCA."
        )

    modo_origen = _modalidad(modalidad_origen, "origen")
    modo_destino = _modalidad(modalidad_destino, "destino")
    volumen_unitario_cm3 = largo * ancho * alto
    volumen_total_cm3 = volumen_unitario_cm3 * cantidad
    peso_total = peso * cantidad
    origen_nombre = nombre_provincia(origen_codigo)
    destino_nombre = nombre_provincia(destino_codigo)

    return {
        "ambito": "NACIONAL",
        "moneda": "ARS",
        "listo": True,
        "encontrado": False,
        "motivo": "integraciones_pendientes",
        "ruta": f"{origen_nombre} → {destino_nombre}",
        "origen": {
            "pais": "AR",
            "provincia_codigo": origen_codigo,
            "provincia_iso": f"AR-{origen_codigo}",
            "provincia_nombre": origen_nombre,
            "localidad_input": origen_localidad,
            "localidad_verificada": False,
            **origen_postal,
        },
        "destino": {
            "pais": "AR",
            "provincia_codigo": destino_codigo,
            "provincia_iso": f"AR-{destino_codigo}",
            "provincia_nombre": destino_nombre,
            "localidad_input": destino_localidad,
            "localidad_verificada": False,
            **destino_postal,
        },
        "modalidad": {"origen": modo_origen, "destino": modo_destino},
        "bultos": [{
            "cantidad": cantidad,
            "peso_unitario_kg": decimal_a_texto(peso),
            "largo_cm": decimal_a_texto(largo),
            "ancho_cm": decimal_a_texto(ancho),
            "alto_cm": decimal_a_texto(alto),
            "volumen_unitario_cm3": decimal_a_texto(volumen_unitario_cm3),
            "valor_declarado_ars_total": decimal_a_texto(valor_declarado),
        }],
        "totales": {
            "cantidad_bultos": cantidad,
            "peso_kg": decimal_a_texto(peso_total),
            "volumen_cm3": decimal_a_texto(volumen_total_cm3),
            "volumen_m3": decimal_a_texto(volumen_total_cm3 / Decimal("1000000")),
            "valor_declarado_ars": decimal_a_texto(valor_declarado),
        },
        "carriers": [dict(carrier) for carrier in CARRIERS_NACIONALES],
        "mensaje": (
            "Los datos quedaron preparados. OCA y Andreani aparecerán "
            "con precio y plazo cuando conectemos sus credenciales y contratos."
        ),
    }
