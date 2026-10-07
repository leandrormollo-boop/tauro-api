"""Estadísticas privadas y conservadoras de contactos del portal.

Una solicitud no conserva el ``direccion_id`` del destinatario. Por eso este
módulo sólo atribuye un envío cuando su snapshot permite verificar la misma
persona o empresa; los casos dudosos se dejan fuera del contador.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any

from core.database import get_conn


TOOLTIP_ENVIOS_IDENTIFICADOS = (
    "Envíos identificados por una coincidencia verificable de país, nombre y "
    "documento, email o dirección con ciudad. Los casos ambiguos no se cuentan."
)


def _texto(valor: Any) -> str:
    """Normaliza acentos, mayúsculas y separadores sin aproximar valores."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).casefold()
    return "".join(c for c in texto if c.isalnum())


def _email(valor: Any) -> str:
    """Un email sólo admite normalización de espacios y mayúsculas."""
    return str(valor or "").strip().casefold()


def _cliente(valor: Any) -> str:
    return str(valor or "").strip().upper()


def _identidad_contacto(contacto: dict[str, Any], cliente_id: str) -> dict[str, Any] | None:
    """Prepara sólo campos aptos para corroborar una identidad.

    ``alias`` y teléfono son datos auxiliares: nunca alcanzan para atribuir un
    envío histórico. Para agenda nacional se agrega el nombre+apellido guardado
    separadamente, que es el que se congela como ``dest_nombre`` en OCA.
    """
    contacto_cliente = contacto.get("cliente_id")
    if contacto_cliente and _cliente(contacto_cliente) != cliente_id:
        return None
    contacto_id = contacto.get("id")
    if contacto_id is None:
        return None

    nombres = {_texto(contacto.get("nombre"))}
    nacional = contacto.get("datos_nacionales") or {}
    if isinstance(nacional, dict):
        completo = " ".join(
            str(nacional.get(campo) or "").strip()
            for campo in ("nombre", "apellido")
            if nacional.get(campo)
        )
        if completo:
            nombres.add(_texto(completo))
    nombres.discard("")
    pais = _cliente(contacto.get("pais"))
    if not pais or not nombres:
        return None
    return {
        "id": contacto_id,
        "pais": pais,
        "nombres": nombres,
        "documento": _texto(contacto.get("documento")),
        "email": _email(contacto.get("email")),
        "direccion": _texto(contacto.get("direccion")),
        "ciudad": _texto(contacto.get("ciudad")),
    }


def _snapshot_normalizado(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "pais": _cliente(snapshot.get("destino_pais")),
        "nombre": _texto(snapshot.get("dest_nombre")),
        "documento": _texto(snapshot.get("dest_documento")),
        "email": _email(snapshot.get("dest_email")),
        "direccion": _texto(snapshot.get("dest_direccion")),
        "ciudad": _texto(snapshot.get("dest_ciudad")),
    }


def _coincide(snapshot: dict[str, Any], contacto: dict[str, Any]) -> bool:
    """El índice ya corroboró país y nombre; falta un identificador fuerte."""
    documento = snapshot["documento"]
    email = snapshot["email"]
    direccion = snapshot["direccion"]
    ciudad = snapshot["ciudad"]
    return bool(
        (documento and documento == contacto["documento"])
        or (email and email == contacto["email"])
        or (
            direccion and ciudad
            and direccion == contacto["direccion"]
            and ciudad == contacto["ciudad"]
        )
    )


def contar_envios_emitidos_por_contacto(
    cliente_id: str,
    contactos: Iterable[dict[str, Any]],
) -> dict[Any, dict[str, Any]]:
    """Devuelve envíos identificados por id de contacto de una sola cuenta.

    El contrato por contacto es ``envios_identificados``,
    ``porcentaje_relativo`` (0..100 respecto del mayor contador recibido) y
    ``ultimo_envio_at``. No hay escritura ni reconciliación: si el ledger tiene
    un cargo CANCELADO, se excluye igual que el historial ya reconciliado.
    """
    cliente = _cliente(cliente_id)
    if not cliente:
        return {}
    identidades: list[dict[str, Any]] = []
    vistos: set[Any] = set()
    for contacto in contactos:
        if not isinstance(contacto, dict):
            continue
        identidad = _identidad_contacto(contacto, cliente)
        if identidad and identidad["id"] not in vistos:
            identidades.append(identidad)
            vistos.add(identidad["id"])

    resultado = {
        identidad["id"]: {
            "envios_identificados": 0,
            "porcentaje_relativo": 0,
            "ultimo_envio_at": None,
        }
        for identidad in identidades
    }
    if not identidades:
        return resultado
    indice: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for identidad in identidades:
        for nombre in identidad["nombres"]:
            indice.setdefault((identidad["pais"], nombre), []).append(identidad)

    # Consulta ligera: no carga PDFs ni datos del remitente. El NOT EXISTS
    # representa la misma condición que convierte una solicitud en CANCELADO
    # durante el listado del historial, sin convertir esta lectura en escritura.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.destino_pais, s.dest_nombre, s.dest_documento,
                       s.dest_email, s.dest_direccion, s.dest_ciudad, s.created_at
                FROM solicitudes_guia s
                WHERE s.cliente_id=%s
                  AND s.test=FALSE
                  AND s.visible_cliente=TRUE
                  AND BTRIM(COALESCE(s.tracking, '')) <> ''
                  AND s.estado NOT IN ('CANCELADO', 'REEMPLAZADO')
                  AND NOT EXISTS (
                      SELECT 1 FROM envios e
                      WHERE e.solicitud_id=s.id
                        AND e.cliente_id=s.cliente_id
                        AND e.estado='CANCELADO'
                  )
                """,
                (cliente,),
            )
            snapshots = [dict(fila) for fila in cur.fetchall()]

    for snapshot in snapshots:
        normalizado = _snapshot_normalizado(snapshot)
        candidatos = [
            identidad for identidad in indice.get(
                (normalizado["pais"], normalizado["nombre"]), []
            ) if _coincide(normalizado, identidad)
        ]
        # Dos contactos verificables para el mismo snapshot siguen siendo
        # ambiguos para la tarjeta: no duplicamos ni elegimos arbitrariamente.
        if len(candidatos) != 1:
            continue
        datos = resultado[candidatos[0]["id"]]
        datos["envios_identificados"] += 1
        fecha = snapshot.get("created_at")
        if fecha is not None and (
            datos["ultimo_envio_at"] is None or fecha > datos["ultimo_envio_at"]
        ):
            datos["ultimo_envio_at"] = fecha

    maximo = max((dato["envios_identificados"] for dato in resultado.values()), default=0)
    if maximo:
        for dato in resultado.values():
            dato["porcentaje_relativo"] = round(
                100 * dato["envios_identificados"] / maximo
            )
    return resultado
