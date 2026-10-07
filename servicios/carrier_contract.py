"""Contrato neutral para los operadores que puede conectar TAURO.

Este módulo no llama APIs, no lee credenciales y no habilita operaciones. Es
el catálogo estable que comparten portal, web, admin y los adapters reales.
Cada integración concreta debe implementar este contrato y registrarse aquí
antes de poder cotizar o emitir en producción.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import FrozenSet, Tuple


class Ambito(StrEnum):
    NACIONAL = "NACIONAL"
    INTERNACIONAL = "INTERNACIONAL"


class Capacidad(StrEnum):
    COTIZAR = "cotizar"
    EMITIR = "emitir"
    ETIQUETA = "etiqueta"
    RECOLECTAR = "recolectar"
    CANCELAR = "cancelar"
    TRACKING = "tracking"


CAPACIDADES_INTERNACIONALES_COMPLETAS = frozenset(Capacidad)
CAPACIDADES_SIN_PICKUP = frozenset({
    Capacidad.COTIZAR,
    Capacidad.EMITIR,
    Capacidad.ETIQUETA,
    Capacidad.CANCELAR,
    Capacidad.TRACKING,
})
CAPACIDADES_NACIONALES_CONFIRMADAS = frozenset({
    Capacidad.COTIZAR,
    Capacidad.EMITIR,
    Capacidad.ETIQUETA,
    Capacidad.TRACKING,
})
CAPACIDADES_SOLO_COTIZACION = frozenset({Capacidad.COTIZAR})


@dataclass(frozen=True)
class CarrierSpec:
    """Metadatos declarativos; la disponibilidad efectiva es otro control."""

    id: str
    nombre: str
    ambitos: FrozenSet[Ambito]
    logo: str
    capacidades: FrozenSet[Capacidad]
    implementacion: str
    variables_requeridas: Tuple[str, ...] = ()
    oferta_publica: str = ""
    capacidades_publicas: FrozenSet[Capacidad] | None = None

    @property
    def pendiente(self) -> bool:
        return self.implementacion != "operativa"


# Registro de producto. ``implementacion`` describe el estado de código, no
# garantiza que haya credenciales productivas ni que el UAT esté aprobado.
CARRIER_SPECS: Tuple[CarrierSpec, ...] = (
    CarrierSpec(
        id="dhl",
        nombre="DHL Express",
        ambitos=frozenset({Ambito.INTERNACIONAL}),
        logo="/static/img/carriers/dhl.svg",
        capacidades=CAPACIDADES_INTERNACIONALES_COMPLETAS,
        implementacion="operativa",
        variables_requeridas=(
            "DHL_API_KEY",
            "DHL_API_SECRET",
            "DHL_ACCOUNT_NUMBER_EXPO",
            "DHL_ACCOUNT_NUMBER_IMPO",
        ),
    ),
    CarrierSpec(
        id="fedex",
        nombre="FedEx",
        ambitos=frozenset({Ambito.INTERNACIONAL}),
        logo="/static/img/carriers/fedex.svg",
        capacidades=CAPACIDADES_INTERNACIONALES_COMPLETAS,
        implementacion="pendiente",
        variables_requeridas=(
            "FEDEX_API_KEY",
            "FEDEX_SECRET_KEY",
            "FEDEX_ACCOUNT_NUMBER",
        ),
    ),
    CarrierSpec(
        id="ups",
        nombre="UPS",
        ambitos=frozenset({Ambito.INTERNACIONAL}),
        logo="/static/img/carriers/ups.svg",
        capacidades=CAPACIDADES_SIN_PICKUP,
        implementacion="pendiente",
        variables_requeridas=(
            "UPS_CLIENT_ID",
            "UPS_CLIENT_SECRET",
            "UPS_ACCOUNT_NUMBER",
        ),
    ),
    CarrierSpec(
        id="andreani",
        nombre="Andreani",
        ambitos=frozenset({Ambito.NACIONAL}),
        logo="/static/img/carriers/andreani.svg",
        capacidades=CAPACIDADES_NACIONALES_CONFIRMADAS,
        implementacion="pendiente",
        variables_requeridas=("ANDREANI_API_KEY", "ANDREANI_CONTRATO"),
    ),
    CarrierSpec(
        id="oca",
        nombre="OCA",
        ambitos=frozenset({Ambito.NACIONAL}),
        logo="/static/img/carriers/oca.png",
        capacidades=CAPACIDADES_SIN_PICKUP,
        implementacion="operativa",
        variables_requeridas=(
            "OCA_USUARIO",
            "OCA_PASSWORD",
            "OCA_CUIT",
            "OCA_CUENTA",
            "OCA_OPERATIVA",
            "OCA_CENTRO_COSTO",
        ),
    ),
)


def carrier_spec(carrier_id: str) -> CarrierSpec | None:
    """Devuelve el spec por ID normalizado, sin tocar DB ni red."""
    buscado = (carrier_id or "").strip().lower()
    return next((spec for spec in CARRIER_SPECS if spec.id == buscado), None)


def carriers_for(ambito: Ambito | str) -> Tuple[CarrierSpec, ...]:
    """Lista ordenada de operadores declarados para un ámbito."""
    try:
        ambito = Ambito(str(ambito).upper())
    except ValueError:
        return ()
    return tuple(spec for spec in CARRIER_SPECS if ambito in spec.ambitos)


def capability_supported(carrier_id: str, capacidad: Capacidad | str) -> bool:
    """Comprueba sólo el contrato declarado, nunca habilita una operación."""
    spec = carrier_spec(carrier_id)
    if not spec:
        return False
    try:
        capacidad = Capacidad(str(capacidad).lower())
    except ValueError:
        return False
    return capacidad in spec.capacidades


def operation_implemented(
    carrier_id: str,
    capacidad: Capacidad | str,
    *,
    ambito: Ambito | str | None = None,
) -> bool:
    """Autoriza sólo operaciones cuyo código fue declarado operativo.

    Este control no mira credenciales ni permisos comerciales. Es el primer
    gate del runtime: una integración pendiente no puede tocar su API aunque
    existan variables de entorno o configuraciones antiguas en la base.
    """
    spec = carrier_spec(carrier_id)
    if not spec or spec.pendiente:
        return False
    try:
        capacidad = Capacidad(str(capacidad).lower())
    except ValueError:
        return False
    if capacidad not in spec.capacidades:
        return False
    if ambito is None:
        return True
    try:
        ambito = Ambito(str(ambito).upper())
    except ValueError:
        return False
    return ambito in spec.ambitos


def public_catalog(
    ambito: Ambito | str | None = None,
    *,
    canal: str,
) -> Tuple[dict, ...]:
    """Catálogo seguro para UI pública, sin estado de credenciales ni cuentas.

    ``canal="publico"`` describe exactamente la oferta que puede mostrarse en
    la web. ``canal="cuenta"`` sirve como base para el portal autenticado.
    Ambos publican capacidades sólo cuando la integración está preparada;
    ninguno comprueba credenciales, UAT ni permisos de cliente.
    """
    if canal not in {"publico", "cuenta"}:
        raise ValueError("Canal de catálogo desconocido.")
    specs = CARRIER_SPECS if ambito is None else carriers_for(ambito)

    def publicar(spec: CarrierSpec) -> dict:
        if spec.pendiente:
            estado = "integracion_pendiente"
            estado_label = "Integración pendiente"
            estado_corto = "Pendiente"
            capacidades = frozenset()
        elif canal == "publico" and spec.oferta_publica == "tarifario_publico":
            estado = "tarifario_publico"
            estado_label = "Tarifario público; emisión no habilitada"
            estado_corto = "Tarifario"
            capacidades = spec.capacidades_publicas or frozenset()
        else:
            estado = "integracion_preparada"
            estado_label = "Integración preparada; sujeta a habilitación"
            estado_corto = "Preparada"
            capacidades = (
                spec.capacidades_publicas
                if canal == "publico" and spec.capacidades_publicas is not None
                else spec.capacidades
            )
        return {
            "id": spec.id,
            "nombre": spec.nombre,
            "ambitos": tuple(sorted(item.value for item in spec.ambitos)),
            "logo": spec.logo if spec.id in {"dhl", "fedex", "ups"} else "",
            "estado": estado,
            "estado_label": estado_label,
            "estado_corto": estado_corto,
            "capacidades": tuple(sorted(item.value for item in capacidades)),
        }

    return tuple(publicar(spec) for spec in specs)
