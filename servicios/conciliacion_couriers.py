"""Conciliación determinística entre guías y facturas de couriers.

El módulo registra evidencia, propone matches y calcula el ajuste. Aplicarlo
es una acción ADMIN separada, explícita y auditada; nunca ocurre al importar.
Todos los importes se procesan con ``Decimal``; el modelo o parser que extrae
un PDF nunca decide una suma financiera.
"""

from __future__ import annotations

import hashlib
import json
import re
import csv
import io
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
    archivo_contenido: bytes | None = None,
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
    contenido_archivo = bytes(archivo_contenido or b"")
    if contenido_archivo:
        if len(contenido_archivo) > 8 * 1024 * 1024:
            raise ConciliacionCourierError("La factura supera el maximo de 8 MB.")
        if not contenido_archivo.startswith(b"%PDF"):
            raise ConciliacionCourierError("La evidencia de la factura debe ser un PDF.")
    hash_calculado = (
        hashlib.sha256(contenido_archivo).hexdigest()
        if contenido_archivo else None
    )
    hash_archivo = _texto(archivo_sha256).lower() or hash_calculado
    if hash_calculado and hash_archivo != hash_calculado:
        raise ConciliacionCourierError("El SHA-256 no coincide con el archivo recibido.")
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
    if any(item["moneda"] != moneda for item in items_preparados):
        raise ConciliacionCourierError(
            "Todas las líneas deben usar la moneda de la factura."
        )
    if items_preparados:
        total_lineas = sum(
            item["importe"] * Decimal(item["signo"])
            for item in items_preparados
        ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
        if abs(total_lineas - total_decimal) > CENTAVO_CONTROL:
            raise ConciliacionCourierError(
                "La suma de las líneas no coincide con el total del documento."
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
                SELECT id, archivo_sha256, metadatos_origen,
                       (archivo_pdf IS NOT NULL) AS tiene_archivo
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
                    if hash_archivo and (
                        not hash_existente
                        or (contenido_archivo and not existente.get("tiene_archivo"))
                    ):
                        cur.execute(
                            """
                            UPDATE facturas_courier
                               SET archivo_sha256 = %s,
                                   archivo_nombre = COALESCE(%s, archivo_nombre),
                                   evidencia_uri = COALESCE(%s, evidencia_uri),
                                   archivo_pdf = COALESCE(%s, archivo_pdf),
                                   archivo_mime = COALESCE(%s, archivo_mime),
                                   updated_at = NOW()
                             WHERE id = %s
                            """,
                            (
                                hash_archivo, _texto(archivo_nombre) or None,
                                _texto(evidencia_uri) or None,
                                contenido_archivo or None,
                                "application/pdf" if contenido_archivo else None,
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
                    archivo_sha256, archivo_pdf, archivo_mime,
                    metadatos_origen
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s
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
                    hash_archivo, contenido_archivo or None,
                    "application/pdf" if contenido_archivo else None,
                    Json(_json_seguro(metadata)),
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
                   AND NOT EXISTS (
                       SELECT 1 FROM factura_courier_item_matches existente
                        WHERE existente.item_id = i.id
                          AND existente.estado IN ('PROPUESTO','CONFIRMADO')
                   )
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


def _actualizar_estado_factura(cur, factura_id: int) -> str:
    """Deriva el estado desde cobertura confirmada, sin confiar en la UI."""
    cur.execute(
        """
        SELECT CASE
                 WHEN f.estado = 'ANULADA' THEN 'ANULADA'
                 WHEN NOT EXISTS (
                     SELECT 1
                       FROM facturas_courier_items i
                      WHERE i.factura_id = f.id
                        AND i.estado <> 'IGNORADO'
                        AND COALESCE((
                            SELECT SUM(m.monto_asignado)
                              FROM factura_courier_item_matches m
                             WHERE m.item_id = i.id
                               AND m.estado = 'CONFIRMADO'
                        ), 0) < i.importe - %s
                 ) AND NOT EXISTS (
                     SELECT 1
                       FROM facturas_courier_items i
                       JOIN factura_courier_item_matches m ON m.item_id = i.id
                      WHERE i.factura_id = f.id AND m.estado = 'PROPUESTO'
                 ) THEN 'CONCILIADA'
                 ELSE 'PARCIAL'
               END AS estado
          FROM facturas_courier f
         WHERE f.id = %s
        """,
        (CENTAVO_CONTROL, int(factura_id)),
    )
    fila = cur.fetchone()
    if not fila:
        raise ConciliacionCourierError("La factura courier no existe.")
    estado = fila["estado"]
    cur.execute(
        """
        UPDATE facturas_courier
           SET estado = %s, updated_at = NOW()
         WHERE id = %s AND estado IS DISTINCT FROM %s
        """,
        (estado, int(factura_id), estado),
    )
    return estado


def proponer_match_manual(
    item_id: int,
    *,
    factura_id_esperada: int | None = None,
    identificador_envio: str,
    actor: str,
    motivo: str,
    monto_asignado: Any | None = None,
) -> dict[str, Any]:
    """Propone una excepción manual con evidencia y saldo controlado.

    ``identificador_envio`` acepta ``#123`` para un ID interno inequívoco o
    un tracking completo. Un número sin ``#`` se busca como tracking y como
    ID; si ambas vías apuntan a envíos distintos, se exige usar ``#ID``.
    """
    actor = _texto(actor)
    motivo = _texto(motivo)
    identificador = _texto(identificador_envio)
    if not actor:
        raise ConciliacionCourierError("Falta identificar al operador.")
    if len(motivo) < 8:
        raise ConciliacionCourierError(
            "Explicá el motivo del match manual (mínimo 8 caracteres)."
        )
    if not identificador:
        raise ConciliacionCourierError("Indicá el tracking o #ID del envío.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.factura_id, i.importe, i.importe_ars,
                       i.tipo_cambio_ars, i.estado AS item_estado,
                       f.courier, f.estado AS factura_estado
                  FROM facturas_courier_items i
                  JOIN facturas_courier f ON f.id = i.factura_id
                 WHERE i.id = %s
                 FOR UPDATE OF i, f
                """,
                (int(item_id),),
            )
            item = cur.fetchone()
            if not item:
                raise ConciliacionCourierError("La línea de factura no existe.")
            if (
                factura_id_esperada is not None
                and int(item["factura_id"]) != int(factura_id_esperada)
            ):
                raise ConciliacionCourierError(
                    "La línea no pertenece a la factura indicada."
                )
            if item["factura_estado"] == "ANULADA":
                raise ConciliacionCourierError(
                    "Una factura anulada no puede vincularse."
                )
            if item["item_estado"] == "IGNORADO":
                raise ConciliacionCourierError(
                    "Una línea ignorada no puede vincularse."
                )

            if identificador.startswith("#"):
                id_texto = identificador[1:].strip()
                if not id_texto.isdigit():
                    raise ConciliacionCourierError(
                        "El ID interno debe escribirse como #123."
                    )
                cur.execute(
                    """
                    SELECT s.id, s.cliente_id, s.tracking, s.courier,
                           e.estado AS cargo_estado
                      FROM solicitudes_guia s
                      LEFT JOIN envios e ON e.solicitud_id = s.id
                     WHERE s.id = %s
                       AND UPPER(BTRIM(s.courier)) = UPPER(BTRIM(%s))
                    """,
                    (int(id_texto), item["courier"]),
                )
            else:
                tracking = normalizar_tracking(identificador)
                if not tracking:
                    raise ConciliacionCourierError("El tracking no es válido.")
                es_id = identificador.isdigit()
                cur.execute(
                    """
                    SELECT s.id, s.cliente_id, s.tracking, s.courier,
                           e.estado AS cargo_estado
                      FROM solicitudes_guia s
                      LEFT JOIN envios e ON e.solicitud_id = s.id
                     WHERE UPPER(BTRIM(s.courier)) = UPPER(BTRIM(%s))
                       AND (
                           NULLIF(REGEXP_REPLACE(
                               UPPER(BTRIM(s.tracking)), '[^A-Z0-9]', '', 'g'
                           ), '') = %s
                           OR (%s AND s.id = %s)
                       )
                     ORDER BY s.id
                    """,
                    (
                        item["courier"], tracking, es_id,
                        int(identificador) if es_id else -1,
                    ),
                )
            candidatos = list(cur.fetchall())
            ids = {int(fila["id"]) for fila in candidatos}
            if not candidatos:
                raise ConciliacionCourierError(
                    "No existe un envío de ese courier con ese tracking o ID."
                )
            if len(ids) != 1:
                raise ConciliacionCourierError(
                    "La referencia es ambigua. Usá el ID interno con formato #123."
                )
            solicitud = candidatos[0]
            if solicitud.get("cargo_estado") != "ACTIVO":
                raise ConciliacionCourierError(
                    "El envío elegido no tiene un cargo activo en cuenta corriente."
                )
            cur.execute(
                """
                SELECT id
                  FROM conciliaciones_envio
                 WHERE solicitud_id = %s
                   AND estado IN ('BORRADOR','PARA_REVISION','APROBADA','RECLAMADA')
                 LIMIT 1
                """,
                (int(solicitud["id"]),),
            )
            if cur.fetchone():
                raise ConciliacionCourierError(
                    "El envío tiene una conciliación abierta; resolvela antes de sumar otra línea."
                )
            cur.execute(
                """
                SELECT COALESCE(SUM(monto_asignado), 0) AS asignado
                  FROM factura_courier_item_matches
                 WHERE item_id = %s AND estado IN ('PROPUESTO','CONFIRMADO')
                """,
                (int(item_id),),
            )
            asignado = _decimal(cur.fetchone()["asignado"], "Monto asignado")
            remanente = (
                _decimal(item["importe"], "Importe de línea") - asignado
            ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
            if remanente <= CENTAVO_CONTROL:
                raise ConciliacionCourierError(
                    "La línea ya está totalmente asignada."
                )
            monto = (
                _dinero(monto_asignado, "Monto a asignar", permite_cero=False)
                if monto_asignado is not None and _texto(monto_asignado)
                else remanente
            )
            if monto > remanente + CENTAVO_CONTROL:
                raise ConciliacionCourierError(
                    "El monto manual supera el saldo disponible de la línea."
                )
            monto_ars = (monto * _decimal(
                item["tipo_cambio_ars"], "Tipo de cambio"
            )).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
            evidencia = f"admin://match-manual/{hashlib.sha256(motivo.encode('utf-8')).hexdigest()}"
            try:
                cur.execute(
                    """
                    INSERT INTO factura_courier_item_matches (
                        item_id, solicitud_id, monto_asignado,
                        monto_asignado_ars, metodo, confianza, estado,
                        evidencia_uri, creado_por
                    ) VALUES (%s, %s, %s, %s, 'MANUAL', NULL, 'PROPUESTO', %s, %s)
                    RETURNING id
                    """,
                    (
                        int(item_id), int(solicitud["id"]), monto,
                        monto_ars, evidencia, actor,
                    ),
                )
            except Exception as exc:
                if getattr(exc, "pgcode", None) == "23505":
                    raise ConciliacionCourierError(
                        "Esa línea ya tiene un match histórico con el envío elegido."
                    ) from exc
                raise
            match_id = int(cur.fetchone()["id"])
            _registrar_auditoria(
                cur,
                evento="MATCH_MANUAL_PROPUESTO",
                actor=actor,
                factura_id=int(item["factura_id"]),
                item_id=int(item_id),
                solicitud_id=int(solicitud["id"]),
                metadata={
                    "match_id": match_id,
                    "monto_asignado": str(monto),
                    "monto_asignado_ars": str(monto_ars),
                    "motivo": motivo,
                },
            )
            _actualizar_estado_factura(cur, int(item["factura_id"]))
            return {
                "id": match_id,
                "factura_id": int(item["factura_id"]),
                "solicitud_id": int(solicitud["id"]),
                "cliente_id": solicitud["cliente_id"],
                "monto_asignado": monto,
                "monto_asignado_ars": monto_ars,
            }


def confirmar_match(
    match_id: int,
    *,
    actor: str,
    factura_id_esperada: int | None = None,
) -> dict[str, Any]:
    """Confirma una propuesta; no aprueba todavía ningún ajuste al cliente."""
    actor = _texto(actor)
    if not actor:
        raise ConciliacionCourierError("Falta identificar al operador.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.estado, m.item_id, m.solicitud_id,
                       i.factura_id, i.estado AS item_estado,
                       f.estado AS factura_estado
                  FROM factura_courier_item_matches m
                  JOIN facturas_courier_items i ON i.id = m.item_id
                  JOIN facturas_courier f ON f.id = i.factura_id
                 WHERE m.id = %s
                 FOR UPDATE OF m
                """,
                (int(match_id),),
            )
            match = cur.fetchone()
            if not match:
                raise ConciliacionCourierError("El match no existe.")
            if (
                factura_id_esperada is not None
                and int(match["factura_id"]) != int(factura_id_esperada)
            ):
                raise ConciliacionCourierError(
                    "El match no pertenece a la factura indicada."
                )
            if match["factura_estado"] == "ANULADA":
                raise ConciliacionCourierError(
                    "Una factura anulada no puede confirmarse."
                )
            if match["item_estado"] == "IGNORADO":
                raise ConciliacionCourierError(
                    "Una línea ignorada no puede confirmarse."
                )
            if match["estado"] == "RECHAZADO":
                raise ConciliacionCourierError(
                    "Un match rechazado no puede confirmarse."
                )
            if match["estado"] == "CONFIRMADO":
                return {
                    "id": int(match_id),
                    "factura_id": int(match["factura_id"]),
                    "solicitud_id": int(match["solicitud_id"]),
                    "duplicado": True,
                }
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
            _actualizar_estado_factura(cur, int(match["factura_id"]))
            return {
                "id": int(match_id),
                "factura_id": int(match["factura_id"]),
                "solicitud_id": int(match["solicitud_id"]),
                "duplicado": False,
            }


def rechazar_match(
    match_id: int,
    *,
    actor: str,
    motivo: str,
    factura_id_esperada: int | None = None,
) -> dict[str, Any]:
    """Rechaza una propuesta conservando el registro y su motivo."""
    actor = _texto(actor)
    motivo = _texto(motivo)
    if not actor:
        raise ConciliacionCourierError("Falta identificar al operador.")
    if len(motivo) < 8:
        raise ConciliacionCourierError(
            "Explicá el motivo del rechazo (mínimo 8 caracteres)."
        )
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
            if (
                factura_id_esperada is not None
                and int(match["factura_id"]) != int(factura_id_esperada)
            ):
                raise ConciliacionCourierError(
                    "El match no pertenece a la factura indicada."
                )
            if match["estado"] != "PROPUESTO":
                raise ConciliacionCourierError(
                    "Sólo se puede rechazar un match todavía propuesto."
                )
            cur.execute(
                """
                UPDATE factura_courier_item_matches
                   SET estado = 'RECHAZADO', motivo_rechazo = %s,
                       updated_at = NOW()
                 WHERE id = %s AND estado = 'PROPUESTO'
                """,
                (motivo, int(match_id)),
            )
            _registrar_auditoria(
                cur,
                evento="MATCH_RECHAZADO",
                actor=actor,
                factura_id=int(match["factura_id"]),
                item_id=int(match["item_id"]),
                solicitud_id=int(match["solicitud_id"]),
                metadata={"match_id": int(match_id), "motivo": motivo},
            )
            _actualizar_estado_factura(cur, int(match["factura_id"]))
            return {
                "id": int(match_id),
                "factura_id": int(match["factura_id"]),
                "solicitud_id": int(match["solicitud_id"]),
            }


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
                       i.importe AS item_importe,
                       i.peso_real_kg, i.peso_volumetrico_kg,
                       i.peso_facturado_kg, i.peso_base, i.estado AS item_estado,
                       COALESCE((
                           SELECT SUM(confirmado.monto_asignado)
                             FROM factura_courier_item_matches confirmado
                            WHERE confirmado.item_id = i.id
                              AND confirmado.estado = 'CONFIRMADO'
                       ), 0) AS item_asignado_confirmado,
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
            tax_cliente = sum(
                (
                    _decimal(fila["monto_asignado_ars"], "TAX asignado")
                    * Decimal(signo_documento(fila["tipo_documento"]))
                    * Decimal(int(fila["signo"]))
                    for fila in filas
                    if fila["concepto_tipo"] in {"IMPUESTO", "ADUANA"}
                ),
                Decimal("0"),
            ).quantize(
                CUATRO_DECIMALES, rounding=ROUND_HALF_UP
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
            diferencia_flete = (
                calculo["ajuste_cliente_ars"] - tax_cliente
            ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
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
                and _decimal(
                    fila["item_asignado_confirmado"], "Monto confirmado"
                ) >= _decimal(fila["item_importe"], "Importe de línea") - CENTAVO_CONTROL
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
                estado_existente = existente["estado"]
                if estado_existente == "BORRADOR" and evidencia_completa:
                    cur.execute(
                        """
                        UPDATE conciliaciones_envio
                           SET estado = 'PARA_REVISION',
                               evidencia_completa = TRUE,
                               updated_at = NOW()
                         WHERE id = %s AND estado = 'BORRADOR'
                        """,
                        (int(existente["id"]),),
                    )
                    _registrar_auditoria(
                        cur,
                        evento="EVIDENCIA_CONCILIACION_COMPLETADA",
                        actor=actor,
                        solicitud_id=int(solicitud_id),
                        conciliacion_id=int(existente["id"]),
                        metadata={"version": int(existente["version"])},
                    )
                    estado_existente = "PARA_REVISION"
                return {
                    "id": int(existente["id"]),
                    "version": int(existente["version"]),
                    "estado": estado_existente,
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
                    diferencia_flete_ars, tax_cliente_ars,
                    peso_cotizado_kg, peso_real_facturado_kg,
                    peso_volumetrico_facturado_kg, peso_final_facturado_kg,
                    peso_base_facturado, motivo_diferencia,
                    formula_version, calculo_hash, evidencias,
                    evidencia_completa, calculado_por
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, 'MARGEN_PROTEGIDO_V1',
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
                    calculo["ajuste_cliente_ars"], diferencia_flete,
                    tax_cliente, peso_cotizado,
                    peso_real, peso_volumetrico, peso_final, peso_base,
                    motivo, calculo_hash, Json(evidencias),
                    evidencia_completa, actor,
                ),
            )
            conciliacion_id = int(cur.fetchone()["id"])
            cur.execute(
                """
                SELECT precio_cliente_final_ars
                  FROM conciliaciones_envio
                 WHERE solicitud_id = %s
                   AND version < %s
                   AND estado = 'CERRADA'
                 ORDER BY version DESC
                 LIMIT 1
                """,
                (int(solicitud_id), version),
            )
            anterior = cur.fetchone()
            precio_anterior_movimiento = (
                anterior["precio_cliente_final_ars"]
                if anterior else calculo["precio_cliente_inicial_ars"]
            )
            movimiento_cliente = (
                calculo["precio_cliente_final_ars"]
                - _decimal(precio_anterior_movimiento, "Precio anterior")
            ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
            ajuste_id = None
            if abs(movimiento_cliente) > CENTAVO_CONTROL:
                tipo_ajuste = (
                    "DEBITO" if movimiento_cliente > 0 else "CREDITO"
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
                        movimiento_cliente,
                        precio_anterior_movimiento,
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
                    "movimiento_cliente_ars": str(movimiento_cliente),
                    "diferencia_flete_ars": str(diferencia_flete),
                    "tax_cliente_ars": str(tax_cliente),
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
                "diferencia_flete_ars": diferencia_flete,
                "tax_cliente_ars": tax_cliente,
                "movimiento_cliente_ars": movimiento_cliente,
                "duplicado": False,
            }


def parsear_lineas_factura_texto(
    texto: str,
    *,
    moneda: str,
    tipo_cambio_ars: Any,
) -> list[dict[str, Any]]:
    """Convierte una tabla pegada desde Excel en ítems financieros.

    Orden esperado: tracking; importe; concepto; peso facturado; base de
    peso; descripción. También acepta tabulaciones y una fila de encabezado.
    Las sumas se vuelven a validar con Decimal al registrar el documento.
    """
    moneda = _moneda(moneda)
    fx = _decimal(
        tipo_cambio_ars or (1 if moneda == "ARS" else None),
        "Tipo de cambio",
        minimo=Decimal("0"),
        permite_cero=False,
    )
    bruto = str(texto or "").strip()
    if not bruto:
        raise ConciliacionCourierError("Pegá al menos una línea de la factura.")
    delimitador = "\t" if "\t" in bruto else (";" if ";" in bruto else ",")
    lector = csv.reader(io.StringIO(bruto), delimiter=delimitador)
    items: list[dict[str, Any]] = []
    for numero_fisico, columnas in enumerate(lector, start=1):
        celdas = [str(celda or "").strip() for celda in columnas]
        if not any(celdas):
            continue
        primera = normalizar_identificador(celdas[0])
        if numero_fisico == 1 and primera in {
            "TRACKING", "GUIA", "NROGUIA", "NUMERODEGUIA",
        }:
            continue
        if len(celdas) < 2 or not celdas[0] or not celdas[1]:
            raise ConciliacionCourierError(
                f"La línea {numero_fisico} necesita tracking e importe."
            )
        from servicios.numeros_humanos import parse_importe_humano
        try:
            importe = parse_importe_humano(celdas[1])
        except ValueError as exc:
            raise ConciliacionCourierError(
                f"Importe inválido en la línea {numero_fisico}."
            ) from exc
        concepto = (celdas[2] if len(celdas) > 2 else "FLETE").upper()
        concepto = concepto.replace(" ", "_") or "FLETE"
        if concepto not in CONCEPTOS:
            raise ConciliacionCourierError(
                f"Concepto inválido en la línea {numero_fisico}: {concepto}."
            )
        peso = celdas[3] if len(celdas) > 3 else ""
        if peso:
            peso = peso.replace(" ", "").replace(",", ".")
        base = (celdas[4] if len(celdas) > 4 else "NO_INFORMADO")
        base = base.upper().replace(" ", "_") or "NO_INFORMADO"
        if base not in PESOS_BASE:
            raise ConciliacionCourierError(
                f"Base de peso inválida en la línea {numero_fisico}."
            )
        items.append({
            "linea_numero": len(items) + 1,
            "tracking": celdas[0],
            "importe": importe,
            "moneda": moneda,
            "tipo_cambio_ars": fx,
            "concepto_tipo": concepto,
            "peso_facturado_kg": peso or None,
            "peso_base": base,
            "descripcion": celdas[5] if len(celdas) > 5 else None,
            "datos_crudos": {"linea_pegada": numero_fisico},
        })
        if len(items) > 5000:
            raise ConciliacionCourierError(
                "La factura supera el máximo de 5.000 líneas por carga."
            )
    if not items:
        raise ConciliacionCourierError("La factura no contiene líneas válidas.")
    return items


def registrar_snapshot_manual_ars(
    solicitud_id: int,
    *,
    costo_estimado_ars: Any,
    actor: str,
) -> dict[str, Any]:
    """Completa la base histórica sin inventar costos ni reescribir precios."""
    costo = _dinero(costo_estimado_ars, "Costo estimado")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, coti_id, courier, servicio_courier,
                       precio_tauro_ars, peso_kg, bultos
                FROM solicitudes_guia WHERE id = %s
                """,
                (int(solicitud_id),),
            )
            solicitud = cur.fetchone()
    if not solicitud:
        raise ConciliacionCourierError("El envío no existe.")
    precio = _dinero(solicitud.get("precio_tauro_ars"), "Precio aceptado")
    if costo > precio + CENTAVO_CONTROL:
        raise ConciliacionCourierError(
            "El costo estimado supera el precio aceptado; revisá la base histórica."
        )
    return registrar_snapshot_cotizacion(
        solicitud_id=int(solicitud_id),
        coti_id=solicitud.get("coti_id"),
        courier=solicitud.get("courier"),
        servicio_courier=solicitud.get("servicio_courier"),
        moneda_courier="ARS",
        tipo_cambio_ars=1,
        costo_courier_estimado=costo,
        precio_cliente_inicial_ars=precio,
        margen_tauro_protegido_ars=precio - costo,
        peso_real_cotizado_kg=solicitud.get("peso_kg"),
        peso_facturable_cotizado_kg=solicitud.get("peso_kg"),
        bultos=solicitud.get("bultos") or [],
        origen_calculo={"fuente": "admin_base_historica"},
        actor=actor,
    )


def listar_control_envios(
    *,
    cliente: str = "",
    courier: str = "",
    estado: str = "",
    buscar: str = "",
    limite: int = 1000,
) -> dict[str, Any]:
    """Vista interna de cada envío y su estado de conciliación."""
    condiciones = ["(e.id IS NOT NULL OR NULLIF(BTRIM(s.tracking), '') IS NOT NULL)"]
    parametros: list[Any] = []
    if cliente.strip():
        condiciones.append("s.cliente_id = %s")
        parametros.append(cliente.strip().upper())
    if courier.strip():
        condiciones.append("UPPER(BTRIM(s.courier)) = %s")
        parametros.append(normalizar_courier(courier))
    if buscar.strip():
        condiciones.append(
            "(s.tracking ILIKE %s OR s.cliente_id ILIKE %s "
            "OR s.dest_nombre ILIKE %s OR s.id::text = %s)"
        )
        patron = f"%{buscar.strip()}%"
        parametros.extend([patron, patron, patron, buscar.strip()])
    where = " AND ".join(condiciones)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.id AS solicitud_id, s.cliente_id, s.created_at,
                       s.tracking, s.courier, s.dest_nombre, s.destino_pais,
                       s.peso_kg, s.precio_tauro_ars, s.estado AS envio_estado,
                       e.id AS cargo_id, e.estado AS cargo_estado, e.ambito,
                       snap.id AS snapshot_id,
                       snap.costo_courier_estimado_ars,
                       snap.margen_tauro_protegido_ars,
                       COALESCE(mc.confirmados, 0) AS matches_confirmados,
                       COALESCE(mc.propuestos, 0) AS matches_propuestos,
                       con.id AS conciliacion_id,
                       con.estado AS conciliacion_estado,
                       con.costo_courier_real_ars,
                       con.precio_cliente_final_ars,
                       con.ajuste_cliente_ars,
                       con.diferencia_flete_ars,
                       con.tax_cliente_ars,
                       con.peso_cotizado_kg,
                       con.peso_final_facturado_kg,
                       con.peso_base_facturado,
                       con.motivo_diferencia,
                       con.evidencia_completa,
                       aj.id AS ajuste_id, aj.estado AS ajuste_estado,
                       aj.tipo AS ajuste_tipo
                FROM solicitudes_guia s
                LEFT JOIN envios e ON e.solicitud_id = s.id
                LEFT JOIN envio_cotizacion_snapshots snap
                       ON snap.solicitud_id = s.id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) FILTER (WHERE estado='CONFIRMADO') AS confirmados,
                           COUNT(*) FILTER (WHERE estado='PROPUESTO') AS propuestos
                    FROM factura_courier_item_matches
                    WHERE solicitud_id = s.id
                ) mc ON TRUE
                LEFT JOIN LATERAL (
                    SELECT * FROM conciliaciones_envio
                    WHERE solicitud_id = s.id
                    ORDER BY version DESC LIMIT 1
                ) con ON TRUE
                LEFT JOIN ajustes_cliente aj ON aj.conciliacion_id = con.id
                WHERE {where}
                ORDER BY s.created_at DESC, s.id DESC
                LIMIT %s
                """,
                (*parametros, max(1, min(int(limite), 1000))),
            )
            filas = [dict(fila) for fila in cur.fetchall()]
    for fila in filas:
        if not fila.get("cargo_id") or fila.get("cargo_estado") != "ACTIVO":
            control = "SIN_CARGO"
        elif not fila.get("snapshot_id"):
            control = "BASE_PENDIENTE"
        elif fila.get("ajuste_estado") == "APLICADO" or (
            fila.get("conciliacion_estado") == "CERRADA"
        ):
            control = "CONCILIADO"
        elif fila.get("ajuste_estado") == "PROPUESTO" or (
            fila.get("conciliacion_estado") == "PARA_REVISION"
        ):
            control = "DIFERENCIA_PENDIENTE"
        elif fila.get("conciliacion_estado") == "BORRADOR":
            control = "EVIDENCIA_PENDIENTE"
        elif int(fila.get("matches_confirmados") or 0):
            control = "LISTO_PARA_CALCULAR"
        elif int(fila.get("matches_propuestos") or 0):
            control = "MATCH_PENDIENTE"
        else:
            control = "ESPERANDO_FACTURA"
        fila["control_estado"] = control
    estados = {}
    for fila in filas:
        estados[fila["control_estado"]] = estados.get(fila["control_estado"], 0) + 1
    if estado.strip():
        filas = [
            fila for fila in filas
            if fila["control_estado"] == estado.strip().upper()
        ]
    return {"items": filas, "totales": estados, "total": len(filas)}


def listar_facturas_courier_control(limite: int = 100) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.courier, f.tipo_documento, f.numero,
                       f.fecha_emision, f.moneda, f.total, f.estado,
                       f.archivo_nombre, f.created_at,
                       (f.archivo_pdf IS NOT NULL OR f.evidencia_uri IS NOT NULL)
                           AS tiene_evidencia,
                       COUNT(i.id) AS lineas,
                       COUNT(m.id) FILTER (WHERE m.estado='PROPUESTO') AS propuestos,
                       COUNT(m.id) FILTER (WHERE m.estado='CONFIRMADO') AS confirmados
                FROM facturas_courier f
                LEFT JOIN facturas_courier_items i ON i.factura_id = f.id
                LEFT JOIN factura_courier_item_matches m ON m.item_id = i.id
                GROUP BY f.id
                ORDER BY f.created_at DESC, f.id DESC
                LIMIT %s
                """,
                (max(1, min(int(limite), 500)),),
            )
            return [dict(fila) for fila in cur.fetchall()]


def obtener_factura_courier_control(factura_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, courier, tipo_documento, numero, fecha_emision,
                       fecha_vencimiento, periodo_desde, periodo_hasta,
                       moneda, subtotal, impuestos, total, estado,
                       archivo_nombre, archivo_sha256,
                       (archivo_pdf IS NOT NULL OR evidencia_uri IS NOT NULL)
                           AS tiene_evidencia, created_at
                FROM facturas_courier WHERE id = %s
                """,
                (int(factura_id),),
            )
            factura = cur.fetchone()
            if not factura:
                return None
            cur.execute(
                """
                SELECT i.id, i.linea_numero, i.tracking_raw, i.concepto_tipo,
                       i.descripcion, i.importe, i.moneda, i.importe_ars,
                       i.peso_facturado_kg, i.peso_base, i.estado,
                       COALESCE(SUM(m.monto_asignado) FILTER (
                           WHERE m.estado IN ('PROPUESTO','CONFIRMADO')
                       ), 0) AS monto_asignado
                FROM facturas_courier_items i
                LEFT JOIN factura_courier_item_matches m ON m.item_id = i.id
                WHERE i.factura_id = %s
                GROUP BY i.id
                ORDER BY i.linea_numero
                """,
                (int(factura_id),),
            )
            resultado = dict(factura)
            items = [dict(fila) for fila in cur.fetchall()]
            mapa = {int(item["id"]): item for item in items}
            for item in items:
                item["remanente"] = (
                    _decimal(item["importe"], "Importe")
                    - _decimal(item["monto_asignado"], "Monto asignado")
                ).quantize(CUATRO_DECIMALES, rounding=ROUND_HALF_UP)
                item["matches"] = []
            cur.execute(
                """
                SELECT m.id, m.item_id, m.estado AS match_estado, m.metodo,
                       m.monto_asignado, m.monto_asignado_ars,
                       m.solicitud_id, m.motivo_rechazo,
                       m.creado_por, m.confirmado_por, m.confirmado_at,
                       s.cliente_id, s.tracking
                  FROM factura_courier_item_matches m
                  JOIN facturas_courier_items i ON i.id = m.item_id
                  JOIN solicitudes_guia s ON s.id = m.solicitud_id
                 WHERE i.factura_id = %s
                 ORDER BY i.linea_numero, m.id
                """,
                (int(factura_id),),
            )
            for fila in cur.fetchall():
                match = dict(fila)
                mapa[int(match["item_id"])]["matches"].append(match)
            resultado["items"] = items
            return resultado


def obtener_control_envio(solicitud_id: int) -> dict[str, Any] | None:
    """Expediente ADMIN de una guía: base, documentos, cálculo y auditoría."""
    control = listar_control_envios(buscar=str(int(solicitud_id)), limite=1000)
    envio = next(
        (
            dict(fila) for fila in control["items"]
            if int(fila["solicitud_id"]) == int(solicitud_id)
        ),
        None,
    )
    if not envio:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id AS match_id, m.estado AS match_estado,
                       m.metodo, m.monto_asignado, m.monto_asignado_ars,
                       m.created_at AS match_created_at,
                       i.id AS item_id, i.linea_numero, i.tracking_raw,
                       i.concepto_tipo, i.descripcion, i.peso_facturado_kg,
                       i.peso_base, f.id AS factura_id, f.courier,
                       f.tipo_documento, f.numero, f.fecha_emision,
                       f.moneda, f.estado AS factura_estado
                  FROM factura_courier_item_matches m
                  JOIN facturas_courier_items i ON i.id = m.item_id
                  JOIN facturas_courier f ON f.id = i.factura_id
                 WHERE m.solicitud_id = %s
                 ORDER BY f.fecha_emision NULLS LAST, f.id, i.linea_numero
                """,
                (int(solicitud_id),),
            )
            envio["documentos"] = [dict(fila) for fila in cur.fetchall()]
            cur.execute(
                """
                SELECT c.id, c.version, c.estado,
                       c.precio_cliente_inicial_ars,
                       c.costo_courier_estimado_ars,
                       c.margen_tauro_protegido_ars,
                       c.costo_courier_real_ars,
                       c.precio_cliente_final_ars,
                       c.ajuste_cliente_ars,
                       c.diferencia_flete_ars, c.tax_cliente_ars,
                       c.peso_cotizado_kg, c.peso_final_facturado_kg,
                       c.peso_base_facturado, c.motivo_diferencia,
                       c.evidencia_completa, c.calculado_por,
                       c.calculado_at, c.aprobado_por, c.aprobado_at,
                       a.id AS ajuste_id, a.tipo AS ajuste_tipo,
                       a.monto_ars AS movimiento_cliente_ars,
                       a.precio_anterior_ars AS movimiento_desde_ars,
                       a.precio_nuevo_ars AS movimiento_hasta_ars,
                       a.estado AS ajuste_estado
                  FROM conciliaciones_envio c
                  LEFT JOIN ajustes_cliente a ON a.conciliacion_id = c.id
                 WHERE c.solicitud_id = %s
                 ORDER BY c.version DESC
                """,
                (int(solicitud_id),),
            )
            envio["conciliaciones"] = [dict(fila) for fila in cur.fetchall()]
            cur.execute(
                """
                SELECT id, evento, factura_id, item_id, conciliacion_id,
                       ajuste_id, actor, metadata, created_at
                  FROM auditoria_facturas_courier
                 WHERE solicitud_id = %s
                 ORDER BY created_at DESC, id DESC
                 LIMIT 500
                """,
                (int(solicitud_id),),
            )
            envio["auditoria"] = [dict(fila) for fila in cur.fetchall()]
    return envio


def obtener_factura_courier_pdf(factura_id: int) -> tuple[bytes, str] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT archivo_pdf, archivo_nombre, numero FROM facturas_courier WHERE id=%s",
                (int(factura_id),),
            )
            fila = cur.fetchone()
    if not fila or not fila.get("archivo_pdf"):
        return None
    return (
        bytes(fila["archivo_pdf"]),
        _texto(fila.get("archivo_nombre")) or f"factura_{fila['numero']}.pdf",
    )


def confirmar_y_calcular_factura(factura_id: int, *, actor: str) -> dict[str, Any]:
    """Confirma propuestas y calcula los envíos ya confirmados del documento."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.solicitud_id
                FROM factura_courier_item_matches m
                JOIN facturas_courier_items i ON i.id = m.item_id
                WHERE i.factura_id = %s AND m.estado = 'PROPUESTO'
                ORDER BY m.id
                """,
                (int(factura_id),),
            )
            propuestas = [dict(fila) for fila in cur.fetchall()]
            cur.execute(
                """
                SELECT DISTINCT m.solicitud_id
                  FROM factura_courier_item_matches m
                  JOIN facturas_courier_items i ON i.id = m.item_id
                 WHERE i.factura_id = %s AND m.estado = 'CONFIRMADO'
                """,
                (int(factura_id),),
            )
            confirmadas = [int(fila["solicitud_id"]) for fila in cur.fetchall()]
    if not propuestas and not confirmadas:
        raise ConciliacionCourierError(
            "La factura no tiene matches propuestos ni confirmados."
        )
    solicitudes = sorted(
        {int(fila["solicitud_id"]) for fila in propuestas} | set(confirmadas)
    )
    for fila in propuestas:
        confirmar_match(int(fila["id"]), actor=actor)
    calculadas = []
    errores = []
    for solicitud_id in solicitudes:
        try:
            calculadas.append(calcular_conciliacion_envio(solicitud_id, actor=actor))
        except ConciliacionCourierError as exc:
            errores.append({"solicitud_id": solicitud_id, "error": str(exc)})
    with get_conn() as conn:
        with conn.cursor() as cur:
            _actualizar_estado_factura(cur, int(factura_id))
    return {
        "matches_confirmados": len(propuestas),
        "conciliaciones": calculadas,
        "errores": errores,
    }


def listar_ajustes_para_revision(limite: int = 200) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.solicitud_id, a.tipo, a.monto_ars,
                       a.precio_anterior_ars, a.precio_nuevo_ars, a.estado,
                       a.motivo, a.created_at,
                       c.id AS conciliacion_id, c.estado AS conciliacion_estado,
                       c.evidencia_completa, c.peso_cotizado_kg,
                       c.peso_final_facturado_kg, c.peso_base_facturado,
                       c.motivo_diferencia, c.diferencia_flete_ars,
                       c.tax_cliente_ars,
                       s.cliente_id, s.tracking, s.courier
                FROM ajustes_cliente a
                JOIN conciliaciones_envio c ON c.id = a.conciliacion_id
                JOIN solicitudes_guia s ON s.id = a.solicitud_id
                WHERE a.estado = 'PROPUESTO'
                ORDER BY a.created_at ASC, a.id ASC
                LIMIT %s
                """,
                (max(1, min(int(limite), 1000)),),
            )
            return [dict(fila) for fila in cur.fetchall()]


def aprobar_y_aplicar_ajuste_cliente(
    ajuste_id: int,
    *,
    actor: str,
    referencia: str = "",
) -> dict[str, Any]:
    """Aplica la diferencia como movimiento separado, sin tocar el original."""
    actor = _texto(actor) or "admin"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, c.estado AS conciliacion_estado,
                       c.evidencia_completa, e.id AS cargo_id,
                       e.estado AS cargo_estado
                FROM ajustes_cliente a
                JOIN conciliaciones_envio c ON c.id = a.conciliacion_id
                LEFT JOIN envios e ON e.solicitud_id = a.solicitud_id
                WHERE a.id=%s
                FOR UPDATE OF a, c
                """,
                (int(ajuste_id),),
            )
            ajuste = cur.fetchone()
            if not ajuste:
                raise ConciliacionCourierError("La diferencia no existe.")
            if ajuste["estado"] == "APLICADO":
                return {"ok": True, "duplicado": True, "ajuste_id": int(ajuste_id)}
            if ajuste["estado"] != "PROPUESTO":
                raise ConciliacionCourierError("La diferencia ya fue resuelta.")
            if ajuste["conciliacion_estado"] != "PARA_REVISION":
                raise ConciliacionCourierError("La conciliación no está lista para aprobar.")
            if not ajuste["evidencia_completa"]:
                raise ConciliacionCourierError("Falta la factura del courier como evidencia.")
            if not ajuste.get("cargo_id") or ajuste.get("cargo_estado") != "ACTIVO":
                raise ConciliacionCourierError("El envío no tiene un cargo activo.")
            cur.execute(
                """
                UPDATE conciliaciones_envio
                SET estado='APROBADA', aprobado_por=%s, aprobado_at=NOW(),
                    updated_at=NOW()
                WHERE id=%s AND estado='PARA_REVISION'
                RETURNING id
                """,
                (actor, int(ajuste["conciliacion_id"])),
            )
            if not cur.fetchone():
                raise ConciliacionCourierError("La conciliación cambió durante la aprobación.")
            referencia_final = _texto(referencia)[:160] or f"ADMIN-AJUSTE-{ajuste_id}"
            cur.execute(
                """
                UPDATE ajustes_cliente
                SET estado='APLICADO', aprobado_por=%s, aprobado_at=NOW(),
                    aplicado_por=%s, aplicado_at=NOW(),
                    referencia_aplicacion=%s, updated_at=NOW()
                WHERE id=%s AND estado='PROPUESTO'
                RETURNING id
                """,
                (actor, actor, referencia_final, int(ajuste_id)),
            )
            if not cur.fetchone():
                raise ConciliacionCourierError("La diferencia cambió durante la aplicación.")
            cur.execute(
                "UPDATE conciliaciones_envio SET estado='CERRADA', updated_at=NOW() WHERE id=%s",
                (int(ajuste["conciliacion_id"]),),
            )
            _registrar_auditoria(
                cur,
                evento="AJUSTE_CLIENTE_APLICADO",
                actor=actor,
                solicitud_id=int(ajuste["solicitud_id"]),
                conciliacion_id=int(ajuste["conciliacion_id"]),
                ajuste_id=int(ajuste_id),
                metadata={
                    "tipo": ajuste["tipo"],
                    "monto_ars": str(ajuste["monto_ars"]),
                    "referencia": referencia_final,
                },
            )
            return {
                "ok": True,
                "duplicado": False,
                "ajuste_id": int(ajuste_id),
                "solicitud_id": int(ajuste["solicitud_id"]),
                "precio_final_ars": ajuste["precio_nuevo_ars"],
            }


def cerrar_conciliacion_sin_diferencia(
    conciliacion_id: int,
    *,
    actor: str,
) -> dict[str, Any]:
    """Cierra una factura coincidente sin crear un movimiento por cero pesos."""
    actor = _texto(actor) or "admin"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*, e.id AS cargo_id, e.estado AS cargo_estado
                FROM conciliaciones_envio c
                LEFT JOIN envios e ON e.solicitud_id=c.solicitud_id
                WHERE c.id=%s
                FOR UPDATE OF c
                """,
                (int(conciliacion_id),),
            )
            conciliacion = cur.fetchone()
            if not conciliacion:
                raise ConciliacionCourierError("La conciliación no existe.")
            if conciliacion["estado"] == "CERRADA":
                return {"ok": True, "duplicado": True}
            if conciliacion["estado"] != "PARA_REVISION":
                raise ConciliacionCourierError("La conciliación no está lista para cerrar.")
            if not conciliacion["evidencia_completa"]:
                raise ConciliacionCourierError("Falta evidencia documental.")
            if abs(_decimal(conciliacion["ajuste_cliente_ars"], "Diferencia")) > CENTAVO_CONTROL:
                raise ConciliacionCourierError("Esta conciliación tiene una diferencia a aprobar.")
            if not conciliacion.get("cargo_id") or conciliacion.get("cargo_estado") != "ACTIVO":
                raise ConciliacionCourierError("El envío no tiene un cargo activo.")
            cur.execute(
                """
                UPDATE conciliaciones_envio
                SET estado='APROBADA', aprobado_por=%s, aprobado_at=NOW(),
                    updated_at=NOW()
                WHERE id=%s AND estado='PARA_REVISION'
                """,
                (actor, int(conciliacion_id)),
            )
            cur.execute(
                "UPDATE conciliaciones_envio SET estado='CERRADA', updated_at=NOW() WHERE id=%s",
                (int(conciliacion_id),),
            )
            _registrar_auditoria(
                cur,
                evento="CONCILIACION_SIN_DIFERENCIA_CERRADA",
                actor=actor,
                solicitud_id=int(conciliacion["solicitud_id"]),
                conciliacion_id=int(conciliacion_id),
            )
            return {"ok": True, "duplicado": False}
