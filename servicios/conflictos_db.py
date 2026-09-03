"""Traducción de violaciones de PostgreSQL a conflictos de dominio.

Los triggers y constraints de ``sql/schema.sql`` son la última línea de
defensa de las invariantes financieras (doble facturación, sobrepago,
inmutabilidad). Cuando disparan, psycopg2 levanta ``RaiseException``,
``UniqueViolation`` o ``CheckViolation``. Este módulo convierte esas
excepciones en un mensaje legible para el operador, sin exponer SQL, nombres
de tablas ni detalles internos, para que los endpoints devuelvan un 4xx
de negocio y nunca un 500 crudo.

Ningún importe se calcula acá: sólo se interpreta el motivo del rechazo.
"""

from __future__ import annotations

from typing import Optional

import psycopg2
import psycopg2.errors


# Mensajes que los triggers del schema emiten con RAISE EXCEPTION. La clave
# es un fragmento estable del texto SQL; el valor, la explicación de negocio.
_MENSAJES_TRIGGER: tuple[tuple[str, str], ...] = (
    (
        "ya integra otra factura emitida",
        "Una de las partidas seleccionadas ya fue facturada en otro documento. "
        "Actualizá la lista y volvé a intentar.",
    ),
    (
        "ya tiene una factura legacy",
        "El cargo ya fue documentado con una factura histórica y no puede "
        "volver a facturarse.",
    ),
    (
        "no puede mezclar ámbitos",
        "Una factura no puede mezclar cargos nacionales e internacionales.",
    ),
    (
        "debe tener ámbito contable",
        "La partida no tiene ámbito contable asignado; clasificala antes de "
        "facturar.",
    ),
    (
        "no pertenece al cliente o no está activo",
        "El cargo no pertenece al cliente o ya no está activo.",
    ),
    (
        "no pertenece al cliente o no está aplicado",
        "La diferencia no pertenece al cliente o todavía no está aplicada.",
    ),
    (
        "sólo puede integrar una FC",
        "Un cargo de envío sólo puede documentarse en una factura FC.",
    ),
    (
        "tipo de factura no coincide con el ajuste",
        "El tipo de comprobante no coincide con el signo de la diferencia.",
    ),
    (
        "no coinciden con el total de la factura",
        "La suma de las partidas no coincide con el total del comprobante.",
    ),
    (
        "no existe o no está emitida",
        "La factura no existe o no está emitida.",
    ),
    (
        "es inmutable; sólo puede anularse",
        "Una factura emitida es inmutable; sólo puede anularse.",
    ),
    (
        "ítems de una factura cliente son inmutables",
        "Los renglones de una factura emitida no se modifican.",
    ),
    (
        "supera el saldo del documento",
        "La imputación supera el saldo pendiente del documento.",
    ),
    (
        "superan el monto del pago",
        "Las imputaciones superan el monto del pago.",
    ),
    (
        "ya está facturado; imputá la factura",
        "El cargo ya está facturado: imputá el pago a la factura.",
    ),
    (
        "no pertenece al cliente o no es imputable",
        "La factura no pertenece al cliente o no admite pagos.",
    ),
    (
        "mezcla ámbitos contables",
        "La factura mezcla ámbitos contables y no puede imputarse.",
    ),
    (
        "no tiene ámbito contable válido",
        "El documento no tiene un ámbito contable válido.",
    ),
    (
        "no está aprobado",
        "El pago todavía no está aprobado.",
    ),
    (
        "admite una imputación solicitada",
        "Sólo un pago pendiente admite una imputación solicitada.",
    ),
    (
        "sólo puede confirmarse; no se reescribe",
        "Una imputación ya registrada no se reescribe.",
    ),
    (
        "registros financieros no se eliminan",
        "Los registros financieros no se eliminan; deben anularse.",
    ),
    (
        "legado de sólo lectura",
        "Los campos de factura histórica del cargo son de sólo lectura. Usá "
        "el facturador por lote.",
    ),
)

_MENSAJES_CONSTRAINT: dict[str, str] = {
    "uq_factura_cliente_numero": (
        "Ya existe una factura con ese tipo, punto de venta y número."
    ),
    "uq_factura_cliente_item_envio": (
        "El mismo cargo aparece dos veces en la factura."
    ),
    "uq_factura_cliente_item_ajuste": (
        "La misma diferencia aparece dos veces en la factura."
    ),
    "uq_envios_fc_normalizada": "Ya existe una factura con ese número.",
    "uq_pago_aplicacion_factura": (
        "El pago ya tiene una imputación a esa factura."
    ),
    "uq_pago_aplicacion_envio": "El pago ya tiene una imputación a ese cargo.",
    "ck_factura_cliente_importes": (
        "Subtotal, IVA y total no cierran; revisá los importes."
    ),
    "ck_factura_cliente_fechas": (
        "Las fechas de la factura no son coherentes."
    ),
    "ck_factura_cliente_item_monto": (
        "Cada partida debe tener un importe mayor que cero."
    ),
    "ck_pagos_aplicaciones_monto": (
        "Cada imputación debe tener un importe mayor que cero."
    ),
    "ck_solicitudes_guia_estado": (
        "El estado operativo del envío no es válido."
    ),
    "ck_envios_estado": "El estado del cargo no es válido.",
    "ck_pagos_estado": "El estado del pago no es válido.",
}

def _texto_error(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    primario = getattr(diag, "message_primary", None) if diag else None
    texto = primario or str(exc)
    return " ".join(str(texto or "").split())


def _nombre_constraint(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    return str(getattr(diag, "constraint_name", "") or "") if diag else ""


def es_conflicto_db(exc: BaseException) -> bool:
    """Sólo las violaciones de reglas del propio schema son conflictos.

    Errores de conexión, sintaxis o permisos siguen siendo fallas técnicas y
    deben propagarse tal cual: no se disfrazan de mensaje de negocio.
    """
    return mensaje_conflicto_db(exc) is not None


def mensaje_conflicto_db(exc: BaseException) -> Optional[str]:
    """Traduce sólo reglas reconocidas; nunca devuelve texto SQL desconocido."""
    if not isinstance(exc, (
        psycopg2.errors.RaiseException,
        psycopg2.errors.UniqueViolation,
        psycopg2.errors.CheckViolation,
        psycopg2.errors.ExclusionViolation,
        psycopg2.errors.ForeignKeyViolation,
        psycopg2.errors.SerializationFailure,
        psycopg2.errors.DeadlockDetected,
    )):
        return None
    if isinstance(exc, (
        psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected,
    )):
        return (
            "Otra operación estaba modificando los mismos documentos. "
            "No se aplicó ningún cambio; volvé a intentar."
        )
    constraint = _nombre_constraint(exc)
    if constraint in _MENSAJES_CONSTRAINT:
        return _MENSAJES_CONSTRAINT[constraint]
    if not isinstance(exc, psycopg2.errors.RaiseException):
        return None
    texto = _texto_error(exc)
    texto_plano = texto.lower()
    for fragmento, mensaje in _MENSAJES_TRIGGER:
        if fragmento.lower() in texto_plano:
            return mensaje
    # Un P0001 o una violación desconocida puede ser un fallo técnico o
    # contener información interna. No se oculta como error de negocio ni
    # se copia al mensaje visible: el caller la propaga al handler técnico.
    return None
