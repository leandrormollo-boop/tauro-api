"""Conciliación determinística entre guías y facturas de couriers.

El módulo no aprueba ni aplica cargos al cliente. Registra evidencia,
propone matches y calcula el ajuste que un administrador deberá revisar.
Todos los importes se procesan con ``Decimal``; el modelo o parser que extrae
un PDF nunca decide una suma financiera.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from psycopg2.extras import Json

from core.database import get_conn


CENTAVO_CONTROL = Decimal("0.02")
CUATRO_DECIMALES = Decimal("0.0001")
SEIS_DECIMALES = Decimal("0.000001")
COURIERS = frozenset({"DHL", "FEDEX", "ANDREANI", "OCA"})
TIPOS_DOCUMENTO = frozenset({"FC", "NC", "ND"})
CONCEPTOS = frozenset({
    "FLETE", "COMBUSTIBLE", "IMPUESTO", "ADUANA", "MANEJO",
    "SEGURO", "DESCUENTO", "OTRO",
})
PESOS_BASE = frozenset({
    "REAL", "VOLUMETRICO", "DECLARADO", "OTRO", "NO_INFORMADO",
})


class ConciliacionCourierError(RuntimeError):
    """Error de dominio que debe mostrarse al operador sin reintento ciego."""


class DocumentoCourierDuplicadoError(ConciliacionCourierError):
    """La misma identidad documental llegó con contenido diferente."""


class SnapshotInmutableError(ConciliacionCourierError):
    """Se intentó cambiar una cotización ya aceptada."""


class ConciliacionActivaError(ConciliacionCourierError):
    """Existe otro cálculo activo que debe revisarse o anularse primero."""


def _texto(valor: Any) -> str:
    return str(valor or "").strip()


def normalizar_courier(valor: Any) -> str:
    courier = _texto(valor).upper()
    if courier not in COURIERS:
        raise ConciliacionCourierError(f"Courier no soportado: {courier or '-'}")
    return courier


def normalizar_tipo_documento(valor: Any) -> str:
    tipo = _texto(valor).upper()
    if tipo not in TIPOS_DOCUMENTO:
        raise ConciliacionCourierError(
            f"Tipo de documento inválido: {tipo or '-'}"
        )
    return tipo


def signo_documento(tipo_documento: Any) -> int:
    """FC/ND aumentan costo; NC lo reduce y jamás se vuelve una FC."""
    return -1 if normalizar_tipo_documento(tipo_documento) == "NC" else 1


def normalizar_identificador(valor: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _texto(valor).upper())


def normalizar_tracking(valor: Any) -> str:
    return normalizar_identificador(valor)


def normalizar_numero_documento(valor: Any) -> str:
    numero = normalizar_identificador(valor)
    if not numero:
        raise ConciliacionCourierError("El número de documento está vacío.")
    return numero


def _decimal(
    valor: Any,
    campo: str,
    *,
    minimo: Decimal | None = None,
    permite_cero: bool = True,
) -> Decimal:
    if isinstance(valor, bool):
        raise ConciliacionCourierError(f"{campo} no es un importe válido.")
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        raise ConciliacionCourierError(f"{campo} no es un importe válido.")
    if not numero.is_finite():
        raise ConciliacionCourierError(f"{campo} no es finito.")
    if minimo is not None and numero < minimo:
        raise ConciliacionCourierError(f"{campo} no puede ser negativo.")
    if not permite_cero and numero == 0:
        raise ConciliacionCourierError(f"{campo} debe ser mayor que cero.")
    return numero


def _dinero(valor: Any, campo: str, *, permite_cero: bool = True) -> Decimal:
    return _decimal(
        valor,
        campo,
        minimo=Decimal("0"),
        permite_cero=permite_cero,
    ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)


def _peso_opcional(valor: Any, campo: str) -> Decimal | None:
    if valor is None or _texto(valor) == "":
        return None
    return _decimal(valor, campo, minimo=Decimal("0")).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )


def _moneda(valor: Any) -> str:
    moneda = _texto(valor).upper()
    if not re.fullmatch(r"[A-Z]{3}", moneda):
        raise ConciliacionCourierError("La moneda debe tener tres letras ISO.")
    return moneda


def _hash_json(payload: Any) -> str:
    serializado = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(serializado.encode("utf-8")).hexdigest()


def _json_seguro(payload: Any) -> Any:
    """Convierte Decimal/fechas a JSON durable sin depender del parser."""
    return json.loads(json.dumps(payload, ensure_ascii=True, default=str))


def _registrar_auditoria(
    cur,
    *,
    evento: str,
    actor: str,
    factura_id: int | None = None,
    item_id: int | None = None,
    solicitud_id: int | None = None,
    conciliacion_id: int | None = None,
    ajuste_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO auditoria_facturas_courier (
            evento, factura_id, item_id, solicitud_id, conciliacion_id,
            ajuste_id, actor, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            evento,
            factura_id,
            item_id,
            solicitud_id,
            conciliacion_id,
            ajuste_id,
            _texto(actor) or "sistema",
            Json(metadata or {}),
        ),
    )


def calcular_precio_con_margen_protegido(
    *,
    costo_courier_real_ars: Any,
    margen_tauro_protegido_ars: Any,
    precio_cliente_inicial_ars: Any,
) -> dict[str, Decimal]:
    """Fórmula V1 exigida por negocio, aislada y testeable."""
    costo = _decimal(costo_courier_real_ars, "Costo courier real")
    margen = _dinero(
        margen_tauro_protegido_ars, "Margen TAURO protegido"
    )
    inicial = _dinero(
        precio_cliente_inicial_ars, "Precio inicial del cliente"
    )
    final = (costo + margen).quantize(
        CUATRO_DECIMALES, rounding=ROUND_HALF_UP
    )
    if final < 0:
        raise ConciliacionCourierError(
            "El costo neto deja un precio final negativo; requiere revisión."
        )
    ajuste = (final - inicial).quantize(
        CUATRO_DECIMALES, rounding=ROUND_HALF_UP
    )
    return {
        "costo_courier_real_ars": costo.quantize(
            CUATRO_DECIMALES, rounding=ROUND_HALF_UP
        ),
        "margen_tauro_protegido_ars": margen,
        "precio_cliente_inicial_ars": inicial,
        "precio_cliente_final_ars": final,
        "ajuste_cliente_ars": ajuste,
    }


def registrar_snapshot_cotizacion(
    *,
    solicitud_id: int,
    courier: str,
    moneda_courier: str,
    tipo_cambio_ars: Any,
    costo_courier_estimado: Any,
    precio_cliente_inicial_ars: Any,
    margen_tauro_protegido_ars: Any,
    actor: str,
    coti_id: str | None = None,
    servicio_courier: str | None = None,
    markup_tipo: str | None = None,
    markup_valor: Any = None,
    peso_real_cotizado_kg: Any = None,
    peso_volumetrico_cotizado_kg: Any = None,
    peso_facturable_cotizado_kg: Any = None,
    bultos: list[dict[str, Any]] | None = None,
    origen_calculo: dict[str, Any] | None = None,
    aceptado_at: datetime | None = None,
) -> dict[str, Any]:
    """Congela costo, precio y margen aceptados para una guía.

    Un retry idéntico es idempotente. Cualquier diferencia contra el snapshot
    existente falla cerrada porque modificarlo cambiaría la historia comercial.
    """
    courier = normalizar_courier(courier)
    moneda = _moneda(moneda_courier)
    tipo_cambio = _decimal(
        tipo_cambio_ars,
        "Tipo de cambio",
        minimo=Decimal("0"),
        permite_cero=False,
    ).quantize(SEIS_DECIMALES, rounding=ROUND_HALF_UP)
    costo_nativo = _dinero(
        costo_courier_estimado,
        "Costo courier estimado",
    )
    costo_ars = (costo_nativo * tipo_cambio).quantize(
        CUATRO_DECIMALES, rounding=ROUND_HALF_UP
    )
    precio = _dinero(precio_cliente_inicial_ars, "Precio inicial")
    margen = _dinero(margen_tauro_protegido_ars, "Margen protegido")
    markup_valor_decimal = (
        _dinero(markup_valor, "Valor del markup")
        if markup_valor not in (None, "") else None
    )
    if abs(precio - costo_ars - margen) > CENTAVO_CONTROL:
        raise ConciliacionCourierError(
            "El precio inicial no coincide con costo estimado + margen."
        )

    instante = aceptado_at or datetime.now(timezone.utc)
    datos_hash = {
        "solicitud_id": int(solicitud_id),
        "coti_id": _texto(coti_id) or None,
        "courier": courier,
        "servicio": _texto(servicio_courier) or None,
        "markup_tipo": _texto(markup_tipo).upper() or None,
        "markup_valor": str(markup_valor_decimal),
        "moneda": moneda,
        "tipo_cambio": str(tipo_cambio),
        "costo_nativo": str(costo_nativo),
        "costo_ars": str(costo_ars),
        "precio": str(precio),
        "margen": str(margen),
        "peso_real": str(_peso_opcional(peso_real_cotizado_kg, "Peso real")),
        "peso_volumetrico": str(_peso_opcional(
            peso_volumetrico_cotizado_kg, "Peso volumétrico"
        )),
        "peso_facturable": str(_peso_opcional(
            peso_facturable_cotizado_kg, "Peso facturable"
        )),
        "bultos": _json_seguro(bultos or []),
    }
    snapshot_hash = _hash_json(datos_hash)
    origen = dict(origen_calculo or {})
    origen["snapshot_sha256"] = snapshot_hash

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, courier, coti_id, precio_tauro_ars
                  FROM solicitudes_guia
                 WHERE id = %s
                 FOR UPDATE
                """,
                (int(solicitud_id),),
            )
            solicitud = cur.fetchone()
            if not solicitud:
                raise ConciliacionCourierError("La solicitud de guía no existe.")
            if normalizar_courier(solicitud["courier"]) != courier:
                raise ConciliacionCourierError(
                    "El courier del snapshot no coincide con la guía."
                )
            precio_guardado = solicitud.get("precio_tauro_ars")
            if precio_guardado is not None and abs(
                _decimal(precio_guardado, "Precio guardado") - precio
            ) > CENTAVO_CONTROL:
                raise ConciliacionCourierError(
                    "El precio aceptado no coincide con la solicitud."
                )
            coti_guardada = _texto(solicitud.get("coti_id"))
            if coti_guardada and _texto(coti_id) and coti_guardada != _texto(coti_id):
                raise ConciliacionCourierError(
                    "La cotización no coincide con la solicitud."
                )

            cur.execute(
                """
                SELECT id, origen_calculo
                  FROM envio_cotizacion_snapshots
                 WHERE solicitud_id = %s
                """,
                (int(solicitud_id),),
            )
            existente = cur.fetchone()
            if existente:
                origen_existente = existente.get("origen_calculo") or {}
                if origen_existente.get("snapshot_sha256") == snapshot_hash:
                    return {"id": int(existente["id"]), "duplicado": True}
                raise SnapshotInmutableError(
                    "La guía ya tiene otra cotización aceptada."
                )

            cur.execute(
                """
                INSERT INTO envio_cotizacion_snapshots (
                    solicitud_id, coti_id, courier, servicio_courier,
                    moneda_courier, tipo_cambio_ars,
                    costo_courier_estimado, costo_courier_estimado_ars,
                    precio_cliente_inicial_ars, margen_tauro_protegido_ars,
                    markup_tipo, markup_valor,
                    peso_real_cotizado_kg, peso_volumetrico_cotizado_kg,
                    peso_facturable_cotizado_kg, bultos, origen_calculo,
                    aceptado_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    int(solicitud_id), _texto(coti_id) or None, courier,
                    _texto(servicio_courier) or None, moneda, tipo_cambio,
                    costo_nativo, costo_ars, precio, margen,
                    _texto(markup_tipo).upper() or None,
                    markup_valor_decimal,
                    _peso_opcional(peso_real_cotizado_kg, "Peso real"),
                    _peso_opcional(
                        peso_volumetrico_cotizado_kg, "Peso volumétrico"
                    ),
                    _peso_opcional(
                        peso_facturable_cotizado_kg, "Peso facturable"
                    ),
                    Json(_json_seguro(bultos or [])),
                    Json(_json_seguro(origen)), instante,
                ),
            )
            snapshot_id = int(cur.fetchone()["id"])
            _registrar_auditoria(
                cur,
                evento="SNAPSHOT_COTIZACION_REGISTRADO",
                actor=actor,
                solicitud_id=int(solicitud_id),
                metadata={"snapshot_id": snapshot_id, "courier": courier},
            )
            return {"id": snapshot_id, "duplicado": False}


def _preparar_item(
    item: dict[str, Any],
    *,
    moneda_documento: str,
) -> dict[str, Any]:
    try:
        linea = int(item.get("linea_numero"))
    except (TypeError, ValueError):
        raise ConciliacionCourierError("Cada ítem necesita un número de línea.")
    if linea <= 0:
        raise ConciliacionCourierError("El número de línea debe ser positivo.")

    moneda = _moneda(item.get("moneda") or moneda_documento)
    importe = _dinero(item.get("importe"), "Importe del ítem", permite_cero=False)
    fx_crudo = item.get("tipo_cambio_ars")
    if fx_crudo in (None, ""):
        if moneda != "ARS":
            raise ConciliacionCourierError(
                "Un ítem en moneda extranjera necesita tipo de cambio ARS."
            )
        fx_crudo = 1
    tipo_cambio = _decimal(
        fx_crudo,
        "Tipo de cambio del ítem",
        minimo=Decimal("0"),
        permite_cero=False,
    ).quantize(SEIS_DECIMALES, rounding=ROUND_HALF_UP)
    calculado_ars = (importe * tipo_cambio).quantize(
        CUATRO_DECIMALES, rounding=ROUND_HALF_UP
    )
    declarado_ars = item.get("importe_ars")
    if declarado_ars not in (None, ""):
        declarado_ars = _dinero(
            declarado_ars, "Importe ARS del ítem", permite_cero=False
        )
        if abs(declarado_ars - calculado_ars) > CENTAVO_CONTROL:
            raise ConciliacionCourierError(
                f"La conversión ARS de la línea {linea} no coincide."
            )

    concepto = _texto(item.get("concepto_tipo") or "OTRO").upper()
    if concepto not in CONCEPTOS:
        raise ConciliacionCourierError(
            f"Concepto inválido en la línea {linea}: {concepto}"
        )
    try:
        signo = int(item.get("signo", 1))
    except (TypeError, ValueError):
        signo = 0
    if signo not in (-1, 1):
        raise ConciliacionCourierError(
            f"El signo de la línea {linea} debe ser 1 o -1."
        )
    peso_base = _texto(item.get("peso_base") or "NO_INFORMADO").upper()
    if peso_base not in PESOS_BASE:
        raise ConciliacionCourierError(
            f"Base de peso inválida en la línea {linea}."
        )
    confianza = item.get("parse_confianza")
    if confianza not in (None, ""):
        confianza = _decimal(confianza, "Confianza del parser")
        if not Decimal("0") <= confianza <= Decimal("1"):
            raise ConciliacionCourierError(
                "La confianza del parser debe estar entre 0 y 1."
            )
        confianza = confianza.quantize(Decimal("0.0001"))
    else:
        confianza = None

    return {
        "linea_numero": linea,
        "tracking_raw": _texto(item.get("tracking")) or None,
        "concepto_codigo": _texto(item.get("concepto_codigo")) or None,
        "concepto_tipo": concepto,
        "descripcion": _texto(item.get("descripcion")) or None,
        "signo": signo,
        "importe": importe,
        "moneda": moneda,
        "tipo_cambio_ars": tipo_cambio,
        "importe_ars": calculado_ars,
        "fecha_envio": item.get("fecha_envio"),
        "peso_real_kg": _peso_opcional(item.get("peso_real_kg"), "Peso real"),
        "peso_volumetrico_kg": _peso_opcional(
            item.get("peso_volumetrico_kg"), "Peso volumétrico"
        ),
        "peso_facturado_kg": _peso_opcional(
            item.get("peso_facturado_kg"), "Peso facturado"
        ),
        "peso_base": peso_base,
        "dimensiones": item.get("dimensiones") or [],
        "datos_crudos": item.get("datos_crudos") or {},
        "parse_confianza": confianza,
    }


def registrar_factura_courier(
    *,
    courier: str,
    tipo_documento: str,
    numero: str,
    moneda: str,
    total: Any,
    items: Iterable[dict[str, Any]],
    actor: str,
    subtotal: Any = 0,
    impuestos: Any = 0,
    factura_referenciada_id: int | None = None,
    fecha_emision: Any = None,
    fecha_vencimiento: Any = None,
    periodo_desde: Any = None,
    periodo_hasta: Any = None,
    mensaje_origen_id: str | None = None,
    evidencia_uri: str | None = None,
    archivo_nombre: str | None = None,
    archivo_sha256: str | None = None,
    metadatos_origen: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Registra documento e ítems de forma atómica e idempotente."""
    courier = normalizar_courier(courier)
    tipo = normalizar_tipo_documento(tipo_documento)
    numero_normalizado = normalizar_numero_documento(numero)
    moneda = _moneda(moneda)
    total_decimal = _dinero(total, "Total del documento")
    subtotal_decimal = _dinero(subtotal, "Subtotal del documento")
    impuestos_decimal = _dinero(impuestos, "Impuestos del documento")
    hash_archivo = _texto(archivo_sha256).lower() or None
    if hash_archivo and not re.fullmatch(r"[0-9a-f]{64}", hash_archivo):
        raise ConciliacionCourierError("El SHA-256 del archivo no es válido.")

    items_preparados = [
        _preparar_item(item, moneda_documento=moneda) for item in items
    ]
    items_preparados.sort(key=lambda item: item["linea_numero"])
    lineas = [item["linea_numero"] for item in items_preparados]
    if len(lineas) != len(set(lineas)):
        raise ConciliacionCourierError(
            "El documento contiene números de línea duplicados."
        )
    payload_hash = _hash_json({
        "courier": courier,
        "tipo": tipo,
        "numero": numero_normalizado,
        "moneda": moneda,
        "total": str(total_decimal),
        "subtotal": str(subtotal_decimal),
        "impuestos": str(impuestos_decimal),
        "factura_referenciada_id": factura_referenciada_id,
        "fecha_emision": str(fecha_emision or ""),
        "fecha_vencimiento": str(fecha_vencimiento or ""),
        "periodo_desde": str(periodo_desde or ""),
        "periodo_hasta": str(periodo_hasta or ""),
        "items": items_preparados,
    })
    metadata = dict(metadatos_origen or {})
    metadata["payload_sha256"] = payload_hash

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"tauro:factura:{courier}:{tipo}:{numero_normalizado}",),
            )
            cur.execute(
                """
                SELECT id, archivo_sha256, metadatos_origen
                  FROM facturas_courier
                 WHERE courier = %s
                   AND tipo_documento = %s
                   AND numero_normalizado = %s
                 FOR UPDATE
                """,
                (courier, tipo, numero_normalizado),
            )
            existente = cur.fetchone()
            if existente:
                metadata_existente = existente.get("metadatos_origen") or {}
                if metadata_existente.get("payload_sha256") == payload_hash:
                    hash_existente = existente.get("archivo_sha256")
                    if hash_existente and hash_archivo and hash_existente != hash_archivo:
                        raise DocumentoCourierDuplicadoError(
                            "El documento coincide pero el archivo es diferente."
                        )
                    evidencia_actualizada = False
                    if hash_archivo and not hash_existente:
                        cur.execute(
                            """
                            UPDATE facturas_courier
                               SET archivo_sha256 = %s,
                                   archivo_nombre = COALESCE(%s, archivo_nombre),
                                   evidencia_uri = COALESCE(%s, evidencia_uri),
                                   updated_at = NOW()
                             WHERE id = %s
                            """,
                            (
                                hash_archivo, _texto(archivo_nombre) or None,
                                _texto(evidencia_uri) or None,
                                int(existente["id"]),
                            ),
                        )
                        evidencia_actualizada = True
                        _registrar_auditoria(
                            cur,
                            evento="EVIDENCIA_FACTURA_COMPLETADA",
                            actor=actor,
                            factura_id=int(existente["id"]),
                            metadata={"archivo_sha256": hash_archivo},
                        )
                    return {
                        "id": int(existente["id"]),
                        "duplicado": True,
                        "evidencia_actualizada": evidencia_actualizada,
                    }
                raise DocumentoCourierDuplicadoError(
                    "El documento ya existe con otro contenido; quedó observado."
                )

            if hash_archivo:
                cur.execute(
                    """
                    SELECT id FROM facturas_courier
                     WHERE archivo_sha256 = %s
                    """,
                    (hash_archivo,),
                )
                mismo_archivo = cur.fetchone()
                if mismo_archivo:
                    raise DocumentoCourierDuplicadoError(
                        "El archivo ya fue registrado con otro documento."
                    )

            cur.execute(
                """
                INSERT INTO facturas_courier (
                    courier, tipo_documento, numero,
                    factura_referenciada_id, fecha_emision,
                    fecha_vencimiento, periodo_desde, periodo_hasta,
                    moneda, subtotal, impuestos, total, estado,
                    mensaje_origen_id, evidencia_uri, archivo_nombre,
                    archivo_sha256, metadatos_origen
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    courier, tipo, _texto(numero), factura_referenciada_id,
                    fecha_emision, fecha_vencimiento, periodo_desde,
                    periodo_hasta, moneda, subtotal_decimal,
                    impuestos_decimal, total_decimal,
                    "EXTRAIDA" if items_preparados else "RECIBIDA",
                    _texto(mensaje_origen_id) or None,
                    _texto(evidencia_uri) or None,
                    _texto(archivo_nombre) or None,
                    hash_archivo, Json(_json_seguro(metadata)),
                ),
            )
            factura_id = int(cur.fetchone()["id"])
            for item in items_preparados:
                cur.execute(
                    """
                    INSERT INTO facturas_courier_items (
                        factura_id, linea_numero, tracking_raw,
                        concepto_codigo, concepto_tipo, descripcion, signo,
                        importe, moneda, tipo_cambio_ars, importe_ars,
                        fecha_envio, peso_real_kg, peso_volumetrico_kg,
                        peso_facturado_kg, peso_base, dimensiones,
                        datos_crudos, parse_confianza
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        factura_id, item["linea_numero"], item["tracking_raw"],
                        item["concepto_codigo"], item["concepto_tipo"],
                        item["descripcion"], item["signo"], item["importe"],
                        item["moneda"], item["tipo_cambio_ars"],
                        item["importe_ars"], item["fecha_envio"],
                        item["peso_real_kg"], item["peso_volumetrico_kg"],
                        item["peso_facturado_kg"], item["peso_base"],
                        Json(_json_seguro(item["dimensiones"])),
                        Json(_json_seguro(item["datos_crudos"])),
                        item["parse_confianza"],
                    ),
                )
            _registrar_auditoria(
                cur,
                evento="FACTURA_COURIER_REGISTRADA",
                actor=actor,
                factura_id=factura_id,
                metadata={
                    "courier": courier,
                    "tipo_documento": tipo,
                    "lineas": len(items_preparados),
                    "payload_sha256": payload_hash,
                },
            )
            return {"id": factura_id, "duplicado": False}


def matchear_items_exactos(
    factura_id: int,
    *,
    actor: str = "sistema",
) -> dict[str, int]:
    """Propone matches únicamente cuando courier y tracking son exactos."""
    propuestos = 0
    sin_match = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT estado FROM facturas_courier
                 WHERE id = %s
                 FOR UPDATE
                """,
                (int(factura_id),),
            )
            factura = cur.fetchone()
            if not factura:
                raise ConciliacionCourierError("La factura courier no existe.")
            if factura["estado"] == "ANULADA":
                raise ConciliacionCourierError(
                    "Una factura anulada no puede matchearse."
                )
            cur.execute(
                """
                SELECT i.id, i.importe, i.importe_ars,
                       i.tracking_normalizado, f.courier
                  FROM facturas_courier_items i
                  JOIN facturas_courier f ON f.id = i.factura_id
                 WHERE i.factura_id = %s
                   AND i.estado <> 'IGNORADO'
                 ORDER BY i.linea_numero
                 FOR UPDATE OF i
                """,
                (int(factura_id),),
            )
            items = list(cur.fetchall())
            for item in items:
                tracking = item.get("tracking_normalizado")
                if not tracking:
                    sin_match += 1
                    continue
                cur.execute(
                    """
                    SELECT id
                      FROM solicitudes_guia
                     WHERE UPPER(BTRIM(courier)) = UPPER(BTRIM(%s))
                       AND NULLIF(REGEXP_REPLACE(
                           UPPER(BTRIM(tracking)), '[^A-Z0-9]', '', 'g'
                       ), '') = %s
                    """,
                    (item["courier"], tracking),
                )
                solicitudes = list(cur.fetchall())
                if len(solicitudes) != 1:
                    sin_match += 1
                    continue
                solicitud_id = int(solicitudes[0]["id"])
                cur.execute(
                    """
                    INSERT INTO factura_courier_item_matches (
                        item_id, solicitud_id, monto_asignado,
                        monto_asignado_ars, metodo, confianza,
                        estado, creado_por
                    ) VALUES (%s, %s, %s, %s, 'EXACTO_TRACKING', 1,
                              'PROPUESTO', %s)
                    ON CONFLICT (item_id, solicitud_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        int(item["id"]), solicitud_id, item["importe"],
                        item["importe_ars"], _texto(actor) or "sistema",
                    ),
                )
                insertado = cur.fetchone()
                if not insertado:
                    continue
                propuestos += 1
                _registrar_auditoria(
                    cur,
                    evento="MATCH_EXACTO_PROPUESTO",
                    actor=actor,
                    factura_id=int(factura_id),
                    item_id=int(item["id"]),
                    solicitud_id=solicitud_id,
                    metadata={"match_id": int(insertado["id"])},
                )
            if propuestos or sin_match:
                cur.execute(
                    """
                    UPDATE facturas_courier
                       SET estado = 'PARCIAL', updated_at = NOW()
                     WHERE id = %s AND estado NOT IN ('ANULADA','CERRADA')
                    """,
                    (int(factura_id),),
                )
    return {"propuestos": propuestos, "sin_match": sin_match}


def confirmar_match(match_id: int, *, actor: str) -> dict[str, Any]:
    """Confirma una propuesta; no aprueba todavía ningún ajuste al cliente."""
    actor = _texto(actor)
    if not actor:
        raise ConciliacionCourierError("Falta identificar al operador.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.estado, m.item_id, m.solicitud_id,
                       i.factura_id
                  FROM factura_courier_item_matches m
                  JOIN facturas_courier_items i ON i.id = m.item_id
                 WHERE m.id = %s
                 FOR UPDATE OF m
                """,
                (int(match_id),),
            )
            match = cur.fetchone()
            if not match:
                raise ConciliacionCourierError("El match no existe.")
            if match["estado"] == "RECHAZADO":
                raise ConciliacionCourierError(
                    "Un match rechazado no puede confirmarse."
                )
            if match["estado"] == "CONFIRMADO":
                return {"id": int(match_id), "duplicado": True}
            cur.execute(
                """
                UPDATE factura_courier_item_matches
                   SET estado = 'CONFIRMADO', confirmado_por = %s,
                       confirmado_at = NOW(), updated_at = NOW()
                 WHERE id = %s
                """,
                (actor, int(match_id)),
            )
            _registrar_auditoria(
                cur,
                evento="MATCH_CONFIRMADO",
                actor=actor,
                factura_id=int(match["factura_id"]),
                item_id=int(match["item_id"]),
                solicitud_id=int(match["solicitud_id"]),
                metadata={"match_id": int(match_id)},
            )
            return {"id": int(match_id), "duplicado": False}


def _max_decimal(filas: list[dict[str, Any]], campo: str) -> Decimal | None:
    valores = [
        _decimal(fila[campo], campo)
        for fila in filas
        if fila.get(campo) is not None
    ]
    return max(valores) if valores else None


def _motivo_diferencia(
    *,
    filas: list[dict[str, Any]],
    peso_cotizado: Decimal | None,
    peso_final: Decimal | None,
    peso_base: str,
    ajuste: Decimal,
) -> str:
    if abs(ajuste) <= CENTAVO_CONTROL:
        return "SIN_DIFERENCIA"
    if any(fila["tipo_documento"] == "NC" for fila in filas) and ajuste < 0:
        return "DESCUENTO"
    aumento_peso = (
        peso_cotizado is not None
        and peso_final is not None
        and peso_final > peso_cotizado + Decimal("0.001")
    )
    conceptos = {fila["concepto_tipo"] for fila in filas}
    recargos = conceptos & {
        "COMBUSTIBLE", "IMPUESTO", "ADUANA", "MANEJO", "SEGURO", "OTRO"
    }
    if aumento_peso and recargos:
        return "MIXTO"
    if aumento_peso and peso_base == "VOLUMETRICO":
        return "PESO_VOLUMETRICO"
    if aumento_peso and peso_base == "REAL":
        return "PESO_REAL"
    if conceptos & {"IMPUESTO", "ADUANA"}:
        return "IMPUESTOS"
    if recargos:
        return "RECARGO"
    return "OTRO"


def calcular_conciliacion_envio(
    solicitud_id: int,
    *,
    actor: str,
) -> dict[str, Any]:
    """Calcula una versión y propone el ajuste, sin aprobarlo ni aplicarlo."""
    actor = _texto(actor)
    if not actor:
        raise ConciliacionCourierError("Falta identificar al operador.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"tauro:conciliacion:{int(solicitud_id)}",),
            )
            cur.execute(
                """
                SELECT *
                  FROM envio_cotizacion_snapshots
                 WHERE solicitud_id = %s
                 FOR SHARE
                """,
                (int(solicitud_id),),
            )
            snapshot = cur.fetchone()
            if not snapshot:
                raise ConciliacionCourierError(
                    "La guía no tiene snapshot de la cotización aceptada."
                )
            cur.execute(
                """
                SELECT m.id AS match_id, m.monto_asignado_ars,
                       i.id AS item_id, i.concepto_tipo, i.signo,
                       i.peso_real_kg, i.peso_volumetrico_kg,
                       i.peso_facturado_kg, i.peso_base, i.estado AS item_estado,
                       f.id AS factura_id, f.tipo_documento, f.estado AS factura_estado,
                       f.archivo_sha256, f.evidencia_uri
                  FROM factura_courier_item_matches m
                  JOIN facturas_courier_items i ON i.id = m.item_id
                  JOIN facturas_courier f ON f.id = i.factura_id
                 WHERE m.solicitud_id = %s
                   AND m.estado = 'CONFIRMADO'
                 ORDER BY f.id, i.linea_numero
                 FOR SHARE OF m, i, f
                """,
                (int(solicitud_id),),
            )
            filas = list(cur.fetchall())
            if not filas:
                raise ConciliacionCourierError(
                    "La guía no tiene ítems de factura confirmados."
                )
            if any(fila["factura_estado"] == "ANULADA" for fila in filas):
                raise ConciliacionCourierError(
                    "Una factura vinculada está anulada."
                )
            cur.execute(
                """
                SELECT COUNT(*) AS cantidad
                  FROM factura_courier_item_matches
                 WHERE solicitud_id = %s AND estado = 'PROPUESTO'
                """,
                (int(solicitud_id),),
            )
            pendientes = int(cur.fetchone()["cantidad"])

            costo_real = sum(
                (
                    _decimal(fila["monto_asignado_ars"], "Monto asignado")
                    * Decimal(signo_documento(fila["tipo_documento"]))
                    * Decimal(int(fila["signo"]))
                )
                for fila in filas
            )
            calculo = calcular_precio_con_margen_protegido(
                costo_courier_real_ars=costo_real,
                margen_tauro_protegido_ars=(
                    snapshot["margen_tauro_protegido_ars"]
                ),
                precio_cliente_inicial_ars=(
                    snapshot["precio_cliente_inicial_ars"]
                ),
            )
            peso_cotizado = snapshot.get("peso_facturable_cotizado_kg")
            peso_cotizado = (
                _decimal(peso_cotizado, "Peso cotizado")
                if peso_cotizado is not None else None
            )
            peso_real = _max_decimal(filas, "peso_real_kg")
            peso_volumetrico = _max_decimal(filas, "peso_volumetrico_kg")
            peso_final = _max_decimal(filas, "peso_facturado_kg")
            bases = {
                fila["peso_base"] for fila in filas
                if fila["peso_base"] != "NO_INFORMADO"
            }
            peso_base = next(iter(bases)) if len(bases) == 1 else (
                "OTRO" if bases else "NO_INFORMADO"
            )
            motivo = _motivo_diferencia(
                filas=filas,
                peso_cotizado=peso_cotizado,
                peso_final=peso_final,
                peso_base=peso_base,
                ajuste=calculo["ajuste_cliente_ars"],
            )
            evidencia_completa = pendientes == 0 and all(
                bool(fila.get("archivo_sha256") or fila.get("evidencia_uri"))
                and fila["item_estado"] != "OBSERVADO"
                for fila in filas
            )
            evidencias = [
                {
                    "factura_id": int(fila["factura_id"]),
                    "item_id": int(fila["item_id"]),
                    "match_id": int(fila["match_id"]),
                }
                for fila in filas
            ]
            calculo_hash = _hash_json({
                "formula": "MARGEN_PROTEGIDO_V1",
                "solicitud_id": int(solicitud_id),
                "snapshot_id": int(snapshot["id"]),
                "filas": [
                    {
                        "match_id": int(fila["match_id"]),
                        "monto_ars": str(fila["monto_asignado_ars"]),
                        "documento": fila["tipo_documento"],
                        "signo": int(fila["signo"]),
                    }
                    for fila in filas
                ],
            })
            cur.execute(
                """
                SELECT id, version, estado
                  FROM conciliaciones_envio
                 WHERE calculo_hash = %s
                """,
                (calculo_hash,),
            )
            existente = cur.fetchone()
            if existente:
                return {
                    "id": int(existente["id"]),
                    "version": int(existente["version"]),
                    "estado": existente["estado"],
                    "duplicado": True,
                }
            cur.execute(
                """
                SELECT id
                  FROM conciliaciones_envio
                 WHERE solicitud_id = %s
                   AND estado IN (
                       'BORRADOR','PARA_REVISION','APROBADA','RECLAMADA'
                   )
                 FOR UPDATE
                """,
                (int(solicitud_id),),
            )
            activa = cur.fetchone()
            if activa:
                raise ConciliacionActivaError(
                    "La guía ya tiene una conciliación activa para revisar."
                )
            cur.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 AS version
                  FROM conciliaciones_envio
                 WHERE solicitud_id = %s
                """,
                (int(solicitud_id),),
            )
            version = int(cur.fetchone()["version"])
            estado = "PARA_REVISION" if evidencia_completa else "BORRADOR"
            cur.execute(
                """
                INSERT INTO conciliaciones_envio (
                    solicitud_id, version, estado,
                    precio_cliente_inicial_ars,
                    costo_courier_estimado_ars,
                    margen_tauro_protegido_ars,
                    costo_courier_real_ars,
                    precio_cliente_final_ars, ajuste_cliente_ars,
                    peso_cotizado_kg, peso_real_facturado_kg,
                    peso_volumetrico_facturado_kg, peso_final_facturado_kg,
                    peso_base_facturado, motivo_diferencia,
                    formula_version, calculo_hash, evidencias,
                    evidencia_completa, calculado_por
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, 'MARGEN_PROTEGIDO_V1',
                    %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    int(solicitud_id), version, estado,
                    calculo["precio_cliente_inicial_ars"],
                    snapshot["costo_courier_estimado_ars"],
                    calculo["margen_tauro_protegido_ars"],
                    calculo["costo_courier_real_ars"],
                    calculo["precio_cliente_final_ars"],
                    calculo["ajuste_cliente_ars"], peso_cotizado,
                    peso_real, peso_volumetrico, peso_final, peso_base,
                    motivo, calculo_hash, Json(evidencias),
                    evidencia_completa, actor,
                ),
            )
            conciliacion_id = int(cur.fetchone()["id"])
            ajuste_id = None
            if abs(calculo["ajuste_cliente_ars"]) > CENTAVO_CONTROL:
                tipo_ajuste = (
                    "DEBITO" if calculo["ajuste_cliente_ars"] > 0 else "CREDITO"
                )
                cur.execute(
                    """
                    INSERT INTO ajustes_cliente (
                        conciliacion_id, solicitud_id, tipo, monto_ars,
                        precio_anterior_ars, precio_nuevo_ars, estado,
                        idempotency_key, motivo, propuesto_por
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, 'PROPUESTO', %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        conciliacion_id, int(solicitud_id), tipo_ajuste,
                        calculo["ajuste_cliente_ars"],
                        calculo["precio_cliente_inicial_ars"],
                        calculo["precio_cliente_final_ars"],
                        f"conciliacion:{conciliacion_id}", motivo, actor,
                    ),
                )
                ajuste_id = int(cur.fetchone()["id"])
            _registrar_auditoria(
                cur,
                evento="CONCILIACION_CALCULADA",
                actor=actor,
                solicitud_id=int(solicitud_id),
                conciliacion_id=conciliacion_id,
                ajuste_id=ajuste_id,
                metadata={
                    "version": version,
                    "estado": estado,
                    "formula_version": "MARGEN_PROTEGIDO_V1",
                    "evidencia_completa": evidencia_completa,
                },
            )
            return {
                "id": conciliacion_id,
                "version": version,
                "estado": estado,
                "ajuste_id": ajuste_id,
                "precio_cliente_inicial_ars": (
                    calculo["precio_cliente_inicial_ars"]
                ),
                "costo_courier_real_ars": calculo["costo_courier_real_ars"],
                "margen_tauro_protegido_ars": (
                    calculo["margen_tauro_protegido_ars"]
                ),
                "precio_cliente_final_ars": (
                    calculo["precio_cliente_final_ars"]
                ),
                "ajuste_cliente_ars": calculo["ajuste_cliente_ars"],
                "duplicado": False,
            }
