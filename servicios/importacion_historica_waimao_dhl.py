"""Importa envios historicos WAIMAO ya documentados en facturas DHL.

Este flujo crea solamente el registro operativo necesario para conciliar la
factura. No crea cargos al cliente, pagos, ajustes ni snapshots financieros.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor
from servicios.conciliacion_couriers import matchear_items_exactos


CLIENTE_ID = "WAIMAO"
COURIER = "DHL"
MAX_MANIFEST_BYTES = 512 * 1024
SCHEMA_VERSION = 1


class ImportacionHistoricaWaimaoError(ValueError):
    pass


def _texto(valor: Any, *, maximo: int = 300) -> str:
    return str(valor or "").strip()[:maximo]


def _clave(valor: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _texto(valor).upper())


def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _fecha(valor: Any) -> date:
    try:
        return date.fromisoformat(str(valor))
    except (TypeError, ValueError):
        raise ImportacionHistoricaWaimaoError("Fecha de envio invalida.") from None


def _peso(valor: Any) -> Decimal:
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        raise ImportacionHistoricaWaimaoError("Peso historico invalido.") from None
    if not numero.is_finite() or numero <= 0 or numero > Decimal("10000"):
        raise ImportacionHistoricaWaimaoError("Peso historico fuera de rango.")
    return numero.quantize(Decimal("0.001"))


def _instante(fecha_operacion: date) -> datetime:
    return datetime.combine(
        fecha_operacion,
        time(hour=12),
        ZoneInfo("America/Argentina/Buenos_Aires"),
    )


def leer_manifiesto(contenido: bytes) -> dict[str, Any]:
    if not contenido or len(contenido) > MAX_MANIFEST_BYTES:
        raise ImportacionHistoricaWaimaoError("El manifiesto esta vacio o excede 512 KB.")
    try:
        lote = json.loads(contenido.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportacionHistoricaWaimaoError("El manifiesto JSON no es valido.") from None
    if not isinstance(lote, dict):
        raise ImportacionHistoricaWaimaoError("El manifiesto debe ser un objeto JSON.")
    if lote.get("schema_version") != SCHEMA_VERSION:
        raise ImportacionHistoricaWaimaoError("Version de manifiesto no admitida.")
    if _texto(lote.get("cliente_id")).upper() != CLIENTE_ID:
        raise ImportacionHistoricaWaimaoError("El manifiesto no corresponde a WAIMAO.")
    if _texto(lote.get("courier")).upper() != COURIER:
        raise ImportacionHistoricaWaimaoError("El manifiesto no corresponde a DHL.")
    registros = lote.get("registros")
    if not isinstance(registros, list) or not registros:
        raise ImportacionHistoricaWaimaoError("El manifiesto no contiene envios.")
    if len(registros) > 100:
        raise ImportacionHistoricaWaimaoError("El lote supera 100 envios.")

    trackings: set[str] = set()
    normalizados = []
    for numero, registro in enumerate(registros, 1):
        if not isinstance(registro, dict):
            raise ImportacionHistoricaWaimaoError(f"Registro {numero}: formato invalido.")
        tracking = _clave(registro.get("tracking"))
        factura = _clave(registro.get("factura"))
        if not re.fullmatch(r"\d{10}", tracking):
            raise ImportacionHistoricaWaimaoError(
                f"Registro {numero}: tracking DHL invalido."
            )
        if tracking in trackings:
            raise ImportacionHistoricaWaimaoError(f"Tracking duplicado: {tracking}.")
        trackings.add(tracking)
        if not factura:
            raise ImportacionHistoricaWaimaoError(
                f"Registro {numero}: falta la factura DHL."
            )
        destinatario = _texto(registro.get("destinatario"), maximo=160)
        if not destinatario:
            raise ImportacionHistoricaWaimaoError(
                f"Registro {numero}: falta el destinatario."
            )
        origen_pais = _texto(registro.get("origen_pais"), maximo=2).upper()
        destino_pais = _texto(registro.get("destino_pais"), maximo=2).upper()
        if not (
            re.fullmatch(r"[A-Z]{2}", origen_pais)
            and re.fullmatch(r"[A-Z]{2}", destino_pais)
        ):
            raise ImportacionHistoricaWaimaoError(
                f"Registro {numero}: pais de origen o destino invalido."
            )
        fecha_operacion = _fecha(registro.get("fecha"))
        peso = _peso(registro.get("peso_kg"))
        normalizados.append({
            "tracking": tracking,
            "factura": factura,
            "fecha": fecha_operacion,
            "remitente": _texto(registro.get("remitente"), maximo=160),
            "origen_pais": origen_pais,
            "destinatario": destinatario,
            "destino_pais": destino_pais,
            "peso_kg": peso,
            "fuente": _texto(registro.get("fuente"), maximo=300),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "cliente_id": CLIENTE_ID,
        "courier": COURIER,
        "source_sha256": _texto(lote.get("source_sha256"), maximo=64),
        "registros": normalizados,
    }


def _verificar_cliente(cur) -> None:
    cur.execute(
        "SELECT cliente_id FROM clientes WHERE cliente_id=%s AND activo=TRUE FOR SHARE",
        (CLIENTE_ID,),
    )
    if not cur.fetchone():
        raise ImportacionHistoricaWaimaoError("El perfil WAIMAO no existe o esta inactivo.")


def _factura_y_guia(cur, registro: dict[str, Any]) -> int:
    cur.execute(
        """
        SELECT f.id
          FROM facturas_courier f
          JOIN facturas_courier_items i ON i.factura_id=f.id
         WHERE UPPER(BTRIM(f.courier))='DHL'
           AND f.numero_normalizado=%s
           AND i.tracking_normalizado=%s
           AND i.estado <> 'IGNORADO'
         GROUP BY f.id
        """,
        (registro["factura"], registro["tracking"]),
    )
    facturas = [int(fila["id"]) for fila in cur.fetchall()]
    if len(facturas) != 1:
        raise ImportacionHistoricaWaimaoError(
            f"{registro['tracking']}: la guia no pertenece de forma unica a "
            f"la factura {registro['factura']}."
        )
    return facturas[0]


def _solicitud_existente(cur, tracking: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT id, cliente_id, courier, tracking
          FROM solicitudes_guia
         WHERE UPPER(BTRIM(courier))='DHL'
           AND NULLIF(REGEXP_REPLACE(UPPER(BTRIM(tracking)), '[^A-Z0-9]', '', 'g'), '')=%s
        """,
        (tracking,),
    )
    filas = [dict(fila) for fila in cur.fetchall()]
    if len(filas) > 1:
        raise ImportacionHistoricaWaimaoError(
            f"{tracking}: existen varias solicitudes DHL con la misma guia."
        )
    if not filas:
        return None
    existente = filas[0]
    if _texto(existente.get("cliente_id")).upper() != CLIENTE_ID:
        raise ImportacionHistoricaWaimaoError(
            f"{tracking}: la guia pertenece a otro cliente."
        )
    return existente


def _crear_solicitud(cur, registro: dict[str, Any], source_sha256: str) -> int:
    tracking = registro["tracking"]
    fecha_operacion = registro["fecha"]
    instante = _instante(fecha_operacion)
    fuente = registro["fuente"] or "Evidencia documental TAURO/DHL"
    fingerprint = _hash(json.dumps({
        "tracking": tracking,
        "factura": registro["factura"],
        "fecha": fecha_operacion.isoformat(),
        "destinatario": registro["destinatario"],
        "destino_pais": registro["destino_pais"],
        "peso_kg": str(registro["peso_kg"]),
    }, sort_keys=True, ensure_ascii=True))
    idempotencia = _hash(f"WAIMAO:DHL:HISTORICO:{tracking}")
    bultos = [{
        "tipo": "CAJA",
        "cantidad": 1,
        "peso_kg": float(registro["peso_kg"]),
        "fuente": "HISTORICO_DOCUMENTAL",
    }]
    observaciones = (
        f"Envio historico WAIMAO conciliable con FC DHL {registro['factura']}. "
        f"Sin cargo al cliente. Fuente: {fuente}."
    )
    if source_sha256:
        observaciones += f" SHA256: {source_sha256}."
    cur.execute(
        """
        INSERT INTO solicitudes_guia (
            cliente_id, estado, producto_alias, cantidad,
            remitente_nombre, remitente_pais, ambito,
            destino_pais, dest_nombre, dest_direccion, dest_ciudad, dest_zip,
            observaciones, peso_kg, valor_declarado_usd, ruta_id,
            precio_tauro_ars, precio_cliente_final_ars, tracking, courier,
            servicio_courier, bultos, api_referencia,
            idempotency_key_hash, request_fingerprint,
            guia_generada_at, visible_cliente, cargo_pendiente,
            created_at, updated_at
        ) VALUES (
            %s, 'GUIA_LISTA', 'Envio historico WAIMAO 2026', 1,
            %s, %s, 'INTERNACIONAL',
            %s, %s, '', '', '',
            %s, %s, 0, %s,
            NULL, NULL, %s, 'DHL',
            'HISTORICO', %s, %s,
            %s, %s,
            %s, FALSE, FALSE,
            %s, %s
        ) RETURNING id
        """,
        (
            CLIENTE_ID,
            registro["remitente"], registro["origen_pais"],
            registro["destino_pais"], registro["destinatario"],
            observaciones, registro["peso_kg"],
            f"{registro['origen_pais']}-{registro['destino_pais']}",
            tracking, Json(bultos), f"HIST-WAIMAO-2026-DHL-{tracking}",
            idempotencia, fingerprint, instante, instante, instante,
        ),
    )
    return int(cur.fetchone()["id"])


def importar_manifiesto(lote: dict[str, Any], *, actor: str = "admin") -> dict[str, Any]:
    registros = lote["registros"]
    facturas: dict[str, int] = {}
    creados = 0
    existentes = 0
    solicitudes: dict[str, int] = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("WAIMAO-DHL-HISTORICO",))
            _verificar_cliente(cur)
            for registro in registros:
                factura_id = _factura_y_guia(cur, registro)
                facturas[registro["factura"]] = factura_id
                existente = _solicitud_existente(cur, registro["tracking"])
                if existente:
                    solicitudes[registro["tracking"]] = int(existente["id"])
                    existentes += 1
                else:
                    solicitudes[registro["tracking"]] = _crear_solicitud(
                        cur, registro, lote.get("source_sha256") or ""
                    )
                    creados += 1

            propuestas = 0
            for factura_id in sorted(set(facturas.values())):
                resultado = matchear_items_exactos(
                    factura_id, actor=actor, _conn=conn,
                )
                propuestas += int(resultado["propuestos"])

            for registro in registros:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT m.item_id) AS lineas
                      FROM factura_courier_item_matches m
                      JOIN facturas_courier_items i ON i.id=m.item_id
                      JOIN facturas_courier f ON f.id=i.factura_id
                     WHERE m.solicitud_id=%s
                       AND f.numero_normalizado=%s
                       AND i.tracking_normalizado=%s
                       AND m.estado IN ('PROPUESTO','CONFIRMADO')
                    """,
                    (
                        solicitudes[registro["tracking"]],
                        registro["factura"], registro["tracking"],
                    ),
                )
                if int(cur.fetchone()["lineas"]) < 1:
                    raise ImportacionHistoricaWaimaoError(
                        f"{registro['tracking']}: no quedo vinculada a su factura DHL."
                    )

            resultado = {
                "envios_manifestados": len(registros),
                "envios_creados": creados,
                "envios_existentes": existentes,
                "facturas_reprocesadas": len(set(facturas.values())),
                "guias_con_match": len(registros),
                "propuestas_nuevas": propuestas,
                "sin_cargos_cliente": len(registros),
            }
            registrar_evento_con_cursor(
                cur,
                event="conciliacion.importacion_historica_waimao_dhl",
                actor_type="admin", actor_ref=_texto(actor, maximo=120) or "admin",
                ip=None, method="POST",
                path="/admin/importaciones-historicas/waimao-dhl",
                status_code=200, success=True, request_id=None,
                metadata={
                    **resultado,
                    "cliente_id": CLIENTE_ID,
                    "courier": COURIER,
                    "trackings": sorted(solicitudes),
                    "source_sha256": lote.get("source_sha256") or None,
                    "controls": [
                        "factura_y_tracking", "tracking_unico", "idempotencia",
                        "sin_cargo_cliente", "match_exacto_verificado",
                    ],
                },
            )
            return resultado
