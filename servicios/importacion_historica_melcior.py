"""Importación contable controlada de MELCIOR 2026.

El manifiesto se genera fuera de producción después de cruzar MELCIOR 2026
contra TAURO 2026. Producción acepta sólo la huella auditada, procesa un mes
por transacción y verifica los totales antes del commit.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor


CLIENTE_ID = "MELCIOR"
PERIODOS = (
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "CIERRE",
)
MESES = PERIODOS[:-1]
EXPECTED_SOURCE_SHA256 = "4b2436d4f7361848c80fe8ab411a0ac826f7b737d0d976f85a3fe0fe9e6b89b2"
EXPECTED_MANIFEST_SHA256 = "04dc6275f8cb5f5bedbcc004d88978560d4a63497bd77e991c3e3665e671bef0"
CENTAVO = Decimal("0.01")
CUATRO = Decimal("0.0001")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

PAISES_HISTORICOS = {
    "USA": "US", "CAN": "CA", "AUS": "AU", "COL": "CO", "MEX": "MX",
    "ALE": "DE", "TAI": "TH", "POL": "PL", "SUI": "CH", "FRA": "FR",
    "REIU": "GB", "REI": "GB", "JAP": "JP", "": "XX",
}


class ImportacionHistoricaError(ValueError):
    pass


def _texto(valor: Any, *, maximo: int = 400) -> str:
    return str(valor or "").strip()[:maximo]


def _dinero(valor: Any, campo: str, *, permitir_negativo: bool = False) -> Decimal:
    try:
        numero = Decimal(str(valor)).quantize(CENTAVO)
    except (InvalidOperation, ValueError, TypeError):
        raise ImportacionHistoricaError(f"{campo}: importe inválido.") from None
    if not numero.is_finite() or (numero < 0 and not permitir_negativo):
        raise ImportacionHistoricaError(f"{campo}: importe fuera de rango.")
    return numero


def _fecha(valor: Any, campo: str) -> date:
    try:
        return date.fromisoformat(str(valor))
    except (TypeError, ValueError):
        raise ImportacionHistoricaError(f"{campo}: fecha inválida.") from None


def _instante(fecha_operacion: date) -> datetime:
    return datetime.combine(
        fecha_operacion, time(hour=12), ZoneInfo("America/Argentina/Buenos_Aires")
    )


def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _hash_json(datos: Any) -> str:
    serializado = json.dumps(
        datos, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return _hash(serializado)


def _manifest_hash(manifiesto: dict[str, Any]) -> str:
    contenido = copy.deepcopy(manifiesto)
    contenido.pop("manifest_sha256", None)
    return _hash_json(contenido)


def _peso(valor: Any) -> Decimal | None:
    coincidencia = re.search(r"\d+(?:[.,]\d+)?", _texto(valor, maximo=80))
    if not coincidencia:
        return None
    peso = Decimal(coincidencia.group(0).replace(",", ".")).quantize(Decimal("0.001"))
    return peso if peso > 0 else None


def _bultos_historicos(medidas_valor: Any, peso_valor: Any) -> list[dict[str, Any]]:
    """Conserva las cajas informadas sin inventar peso por caja.

    La planilla histórica usa tanto ``40X40X30 X2`` como secuencias de
    medidas distintas. El peso disponible es el total del envío; sólo se lo
    asignamos a una caja cuando la fuente describe una única caja.
    """
    medidas = _texto(medidas_valor, maximo=240)
    peso_total = _peso(peso_valor)
    patron = re.compile(
        r"(?<!\d)(\d+(?:[.,]\d+)?)\s*[xX×]\s*"
        r"(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)(?!\d)"
    )
    coincidencias = list(patron.finditer(medidas))
    bultos: list[dict[str, Any]] = []
    for indice, coincidencia in enumerate(coincidencias):
        siguiente = (
            coincidencias[indice + 1].start()
            if indice + 1 < len(coincidencias)
            else len(medidas)
        )
        sufijo = medidas[coincidencia.end():siguiente]
        repeticion = re.search(r"[xX×]\s*(\d+)\b", sufijo)
        cantidad = int(repeticion.group(1)) if repeticion else 1
        if cantidad < 1 or cantidad > 100:
            cantidad = 1
        largo, ancho, alto = (
            Decimal(valor.replace(",", ".")).quantize(Decimal("0.001"))
            for valor in coincidencia.groups()
        )
        if min(largo, ancho, alto) <= 0:
            continue
        bultos.append({
            "producto_alias": "Mercadería",
            "cantidad": cantidad,
            "largo_cm": float(largo),
            "ancho_cm": float(ancho),
            "alto_cm": float(alto),
            "peso_kg": None,
        })

    cantidad_total = sum(int(bulto["cantidad"]) for bulto in bultos)
    if cantidad_total == 1 and peso_total is not None:
        bultos[0]["peso_kg"] = float(peso_total)
    elif not bultos and peso_total is not None:
        bultos.append({
            "producto_alias": "Mercadería",
            "cantidad": 1,
            "peso_kg": float(peso_total),
        })
    return bultos


def _pais(valor: Any) -> str:
    crudo = _texto(valor, maximo=30).upper()
    if crudo in PAISES_HISTORICOS:
        return PAISES_HISTORICOS[crudo]
    if re.fullmatch(r"[A-Z]{2}", crudo):
        return crudo
    raise ImportacionHistoricaError(f"País histórico sin normalizar: {crudo!r}.")


def _igual_dinero(a: Any, b: Any) -> bool:
    return abs(Decimal(str(a or 0)) - Decimal(str(b or 0))) <= CENTAVO


def validar_manifiesto(manifiesto: Any) -> dict[str, Any]:
    if not isinstance(manifiesto, dict):
        raise ImportacionHistoricaError("El manifiesto no es un objeto JSON.")
    if manifiesto.get("schema_version") != 1:
        raise ImportacionHistoricaError("Versión de manifiesto no soportada.")
    if _texto(manifiesto.get("cliente_id"), maximo=30).upper() != CLIENTE_ID:
        raise ImportacionHistoricaError("El manifiesto no pertenece a MELCIOR.")
    if manifiesto.get("periodo") != 2026:
        raise ImportacionHistoricaError("El manifiesto no corresponde a 2026.")
    if manifiesto.get("source_sha256") != EXPECTED_SOURCE_SHA256:
        raise ImportacionHistoricaError("La evidencia fuente cambió desde la auditoría.")
    if manifiesto.get("manifest_sha256") != EXPECTED_MANIFEST_SHA256:
        raise ImportacionHistoricaError("La huella del manifiesto no es la aprobada.")
    if _manifest_hash(manifiesto) != EXPECTED_MANIFEST_SHA256:
        raise ImportacionHistoricaError("El contenido del manifiesto fue modificado.")
    fuentes_sha = manifiesto.get("source_files_sha256") or {}
    if set(fuentes_sha) != {"melcior_2026", "tauro_2026"} or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(huella or ""))
        for huella in fuentes_sha.values()
    ):
        raise ImportacionHistoricaError("Faltan las huellas de las dos planillas fuente.")
    descartados = manifiesto.get("duplicados_descartados")
    if not isinstance(descartados, list) or len(descartados) != 5:
        raise ImportacionHistoricaError("La resolución de duplicados no es la auditada.")

    envios = manifiesto.get("envios")
    if not isinstance(envios, list) or not envios:
        raise ImportacionHistoricaError("El manifiesto no contiene envíos.")
    fuentes: set[str] = set()
    trackings: set[str] = set()
    totales_mes: dict[str, dict[str, Any]] = {}
    for mes in MESES:
        totales_mes[mes] = {
            "envios": 0, "cargos": 0, "cancelados": 0, "requieren_revision": 0,
            "tracking_pendiente": 0, "con_diferencia": 0,
            "importe_inicial_ars": Decimal("0"), "diferencias_ars": Decimal("0"),
        }

    for numero, fila in enumerate(envios, start=1):
        if not isinstance(fila, dict):
            raise ImportacionHistoricaError(f"Envío {numero}: formato inválido.")
        source_key = _texto(fila.get("source_key"), maximo=120)
        if not re.fullmatch(r"MELCIOR-2026:[A-Z]+:\d+", source_key):
            raise ImportacionHistoricaError(f"Envío {numero}: source_key inválida.")
        if source_key in fuentes:
            raise ImportacionHistoricaError(f"Source key duplicada: {source_key}.")
        fuentes.add(source_key)
        mes = _texto(fila.get("mes"), maximo=20).upper()
        if mes not in MESES:
            raise ImportacionHistoricaError(f"Envío {numero}: mes inválido.")
        fecha_operacion = _fecha(fila.get("fecha"), f"Envío {numero}")
        # Tres filas viven en la solapa MARZO pero conservan fecha operativa de
        # enero en ambas planillas. El lote se ordena por solapa fuente y el
        # portal muestra la fecha real; no se reescribe ninguna de las dos.
        if fecha_operacion.year != 2026:
            raise ImportacionHistoricaError(f"Envío {numero}: fecha fuera de 2026.")
        if not _texto(fila.get("destinatario"), maximo=160):
            raise ImportacionHistoricaError(f"Envío {numero}: falta destinatario.")
        _pais(fila.get("pais_fuente"))
        tracking = _texto(fila.get("tracking"), maximo=30)
        pendiente = bool(fila.get("tracking_pendiente"))
        if tracking:
            if not re.fullmatch(r"\d{8,20}", tracking):
                raise ImportacionHistoricaError(f"Envío {numero}: tracking inválido.")
            if tracking in trackings:
                raise ImportacionHistoricaError(f"Tracking repetido: {tracking}.")
            trackings.add(tracking)
        elif not pendiente:
            raise ImportacionHistoricaError(f"Envío {numero}: falta tracking sin marcar pendiente.")
        importe = _dinero(fila.get("importe_inicial_ars"), f"Envío {numero}")
        diferencia = _dinero(
            fila.get("diferencia_ars"), f"Diferencia {numero}", permitir_negativo=True
        )
        genera_deuda = bool(fila.get("genera_deuda"))
        estado = _texto(fila.get("estado_portal"), maximo=30).upper()
        if estado not in {"DESPACHADO", "CANCELADO"}:
            raise ImportacionHistoricaError(f"Envío {numero}: estado no permitido.")
        if (estado == "CANCELADO") != (not genera_deuda):
            raise ImportacionHistoricaError(f"Envío {numero}: deuda/estado inconsistentes.")
        if not genera_deuda and (importe != 0 or diferencia != 0):
            raise ImportacionHistoricaError(f"Envío {numero}: un cancelado no puede generar saldo.")
        if diferencia:
            costo_estimado = _dinero(
                fila.get("costo_estimado_ars"), f"Costo estimado {numero}"
            )
            costo_real = _dinero(fila.get("costo_real_ars"), f"Costo real {numero}")
            if costo_estimado > importe + CENTAVO:
                raise ImportacionHistoricaError(f"Envío {numero}: margen inicial negativo.")
            if abs(costo_real - costo_estimado - diferencia) > CENTAVO:
                raise ImportacionHistoricaError(f"Envío {numero}: diferencia no conciliada.")
            if importe + diferencia < 0:
                raise ImportacionHistoricaError(f"Envío {numero}: precio final negativo.")

        total = totales_mes[mes]
        total["envios"] += 1
        total["cargos"] += int(genera_deuda)
        total["cancelados"] += int(not genera_deuda)
        total["requieren_revision"] += int(bool(fila.get("requiere_revision")))
        total["tracking_pendiente"] += int(pendiente)
        total["con_diferencia"] += int(diferencia != 0)
        if genera_deuda:
            total["importe_inicial_ars"] += importe
        total["diferencias_ars"] += diferencia

    declarados = manifiesto.get("resumen_mensual") or {}
    for mes, calculado in totales_mes.items():
        if calculado["envios"] == 0:
            continue
        esperado = declarados.get(mes)
        if not isinstance(esperado, dict):
            raise ImportacionHistoricaError(f"Falta el resumen de {mes}.")
        for campo in (
            "envios", "cargos", "cancelados", "requieren_revision",
            "tracking_pendiente", "con_diferencia",
        ):
            if int(esperado.get(campo, -1)) != calculado[campo]:
                raise ImportacionHistoricaError(f"Resumen de {mes}: {campo} no coincide.")
        for campo in ("importe_inicial_ars", "diferencias_ars"):
            if _dinero(esperado.get(campo), f"Resumen {mes}") != calculado[campo]:
                raise ImportacionHistoricaError(f"Resumen de {mes}: {campo} no coincide.")

    resumen = manifiesto.get("resumen") or {}
    comprobaciones = {
        "envios": sum(t["envios"] for t in totales_mes.values()),
        "cargos": sum(t["cargos"] for t in totales_mes.values()),
        "cancelados": sum(t["cancelados"] for t in totales_mes.values()),
        "requieren_revision": sum(t["requieren_revision"] for t in totales_mes.values()),
        "tracking_pendiente": sum(t["tracking_pendiente"] for t in totales_mes.values()),
        "con_diferencia": sum(t["con_diferencia"] for t in totales_mes.values()),
    }
    for campo, calculado in comprobaciones.items():
        if int(resumen.get(campo, -1)) != calculado:
            raise ImportacionHistoricaError(f"Resumen general: {campo} no coincide.")
    for campo in ("importe_inicial_ars", "diferencias_ars"):
        calculado = sum((t[campo] for t in totales_mes.values()), Decimal("0"))
        if _dinero(resumen.get(campo), "Resumen general") != calculado:
            raise ImportacionHistoricaError(f"Resumen general: {campo} no coincide.")

    saldo_2025 = manifiesto.get("saldo_pendiente_2025") or {}
    if _texto(saldo_2025.get("concepto"), maximo=80) != "SALDO PENDIENTE 2025":
        raise ImportacionHistoricaError("El saldo 2025 perdió su concepto contable.")
    _fecha(saldo_2025.get("fecha"), "Saldo 2025")
    _dinero(saldo_2025.get("monto_ars"), "Saldo 2025")
    pagos = manifiesto.get("pagos")
    if not isinstance(pagos, list) or len(pagos) != 6:
        raise ImportacionHistoricaError("El lote debe contener los seis pagos históricos.")
    claves_pago: set[str] = set()
    for numero, pago in enumerate(pagos, start=1):
        clave = _texto(pago.get("source_key"), maximo=120)
        if clave in claves_pago:
            raise ImportacionHistoricaError("Hay pagos duplicados en el manifiesto.")
        claves_pago.add(clave)
        _fecha(pago.get("fecha"), f"Pago {numero}")
        _dinero(pago.get("monto_ars"), f"Pago {numero}")
        if pago.get("fecha_original_informada") is not False:
            raise ImportacionHistoricaError("No se puede inventar una fecha original de pago.")
    saldo_monto = _dinero(saldo_2025.get("monto_ars"), "Saldo 2025")
    pagos_total = sum(
        (_dinero(pago["monto_ars"], "Pago") for pago in pagos), Decimal("0")
    )
    if _dinero(resumen.get("saldo_pendiente_2025_ars"), "Resumen saldo 2025") != saldo_monto:
        raise ImportacionHistoricaError("El saldo 2025 no coincide con el resumen.")
    if _dinero(resumen.get("pagos_ars"), "Resumen pagos") != pagos_total:
        raise ImportacionHistoricaError("Los pagos no coinciden con el resumen.")
    saldo_calculado = (
        _dinero(resumen["importe_inicial_ars"], "Resumen envíos")
        + _dinero(resumen["diferencias_ars"], "Resumen diferencias", permitir_negativo=True)
        + saldo_monto - pagos_total
    )
    if _dinero(
        resumen.get("saldo_resultante_ars"), "Saldo resultante", permitir_negativo=True
    ) != saldo_calculado:
        raise ImportacionHistoricaError("El saldo resultante del manifiesto no cierra.")
    return manifiesto


def leer_manifiesto(contenido: bytes) -> dict[str, Any]:
    if not contenido:
        raise ImportacionHistoricaError("El archivo está vacío.")
    if len(contenido) > MAX_MANIFEST_BYTES:
        raise ImportacionHistoricaError("El manifiesto supera el máximo de 4 MB.")
    try:
        datos = json.loads(contenido.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportacionHistoricaError("El archivo no es un JSON válido.") from None
    return validar_manifiesto(datos)


def resumen_periodo(manifiesto: dict[str, Any], periodo: str) -> dict[str, Any]:
    periodo = _texto(periodo, maximo=20).upper()
    if periodo not in PERIODOS:
        raise ImportacionHistoricaError("Período de importación inválido.")
    if periodo == "CIERRE":
        return {
            "periodo": periodo,
            "saldo_pendiente_2025_ars": _dinero(
                manifiesto["saldo_pendiente_2025"]["monto_ars"], "Saldo 2025"
            ),
            "pagos": len(manifiesto["pagos"]),
            "pagos_ars": sum(
                (_dinero(p["monto_ars"], "Pago") for p in manifiesto["pagos"]),
                Decimal("0"),
            ),
        }
    resumen = dict(manifiesto["resumen_mensual"][periodo])
    resumen["periodo"] = periodo
    for campo in ("importe_inicial_ars", "diferencias_ars"):
        resumen[campo] = _dinero(resumen[campo], f"Resumen {periodo}")
    return resumen


def _validar_cliente(cur) -> None:
    cur.execute(
        "SELECT cliente_id FROM clientes WHERE cliente_id=%s AND activo=TRUE FOR SHARE",
        (CLIENTE_ID,),
    )
    if not cur.fetchone():
        raise ImportacionHistoricaError("El perfil MELCIOR no existe o está inactivo.")


def _solicitud_existente(cur, fila: dict[str, Any], idempotencia: str):
    cur.execute(
        "SELECT * FROM solicitudes_guia WHERE cliente_id=%s AND idempotency_key_hash=%s",
        (CLIENTE_ID, idempotencia),
    )
    por_idempotencia = cur.fetchone()
    if por_idempotencia:
        return dict(por_idempotencia), "IDEMPOTENTE"
    tracking = _texto(fila.get("tracking"), maximo=30)
    if not tracking:
        return None, "NUEVA"
    cur.execute(
        """
        SELECT * FROM solicitudes_guia
        WHERE UPPER(courier)='FEDEX' AND UPPER(BTRIM(tracking))=UPPER(%s)
        LIMIT 1
        """,
        (tracking,),
    )
    existente = cur.fetchone()
    return (dict(existente), "ADOPTADA") if existente else (None, "NUEVA")


def _verificar_solicitud(existente: dict, fila: dict[str, Any], fingerprint: str) -> None:
    if _texto(existente.get("cliente_id"), maximo=30).upper() != CLIENTE_ID:
        raise ImportacionHistoricaError(
            f"El tracking {_texto(fila.get('tracking'))} pertenece a otro cliente."
        )
    huella = _texto(existente.get("request_fingerprint"), maximo=64)
    if huella and huella != fingerprint:
        raise ImportacionHistoricaError(
            f"La fila {fila['source_key']} ya existe con otros datos."
        )
    if _texto(existente.get("dest_nombre"), maximo=160).casefold() != _texto(
        fila.get("destinatario"), maximo=160
    ).casefold():
        raise ImportacionHistoricaError(
            f"El tracking {_texto(fila.get('tracking'))} tiene otro destinatario en el portal."
        )
    precio = existente.get("precio_tauro_ars")
    if precio is not None and not _igual_dinero(precio, fila["importe_inicial_ars"]):
        raise ImportacionHistoricaError(
            f"El tracking {_texto(fila.get('tracking'))} tiene otro precio en el portal."
        )
    estado = _texto(existente.get("estado"), maximo=30).upper()
    esperado = _texto(fila.get("estado_portal"), maximo=30).upper()
    if (estado == "CANCELADO") != (esperado == "CANCELADO"):
        raise ImportacionHistoricaError(
            f"El tracking {_texto(fila.get('tracking'))} tiene otro estado en el portal."
        )


def _crear_solicitud(cur, fila: dict[str, Any], idempotencia: str, fingerprint: str) -> int:
    fecha_operacion = _fecha(fila["fecha"], "Fecha de envío")
    instante = _instante(fecha_operacion)
    pais = _pais(fila.get("pais_fuente"))
    peso = _peso(fila.get("peso_fuente"))
    tracking = _texto(fila.get("tracking"), maximo=30) or None
    observaciones = "Envío histórico importado"
    medidas_fuente = _texto(fila.get("medidas_fuente"), maximo=240)
    peso_fuente = _texto(fila.get("peso_fuente"), maximo=80)
    if medidas_fuente:
        observaciones += f" · Medidas fuente: {medidas_fuente}"
    if peso_fuente:
        observaciones += f" · Peso fuente: {peso_fuente}"
    if pais == "XX":
        observaciones += " · País no informado en la planilla fuente"
    if fila.get("tracking_pendiente"):
        observaciones += " · PENDIENTE DE TRACKING"
    if fila.get("requiere_revision"):
        observaciones += " · REVISAR según planilla fuente"
    precio = _dinero(fila["importe_inicial_ars"], "Precio inicial")
    diferencia = _dinero(fila["diferencia_ars"], "Diferencia", permitir_negativo=True)
    bultos = _bultos_historicos(medidas_fuente, peso_fuente)
    cantidad_bultos = sum(int(bulto.get("cantidad") or 1) for bulto in bultos) or 1
    cur.execute(
        """
        INSERT INTO solicitudes_guia (
            cliente_id, estado, producto_alias, cantidad,
            remitente_nombre, remitente_pais, ambito,
            destino_pais, dest_nombre, dest_direccion, dest_ciudad, dest_zip,
            observaciones, peso_kg, valor_declarado_usd, ruta_id, coti_id,
            precio_tauro_ars, precio_cliente_final_ars, tracking, courier,
            servicio_courier, bultos, api_referencia,
            idempotency_key_hash, request_fingerprint,
            guia_generada_at, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, 'AR', 'INTERNACIONAL',
            %s, %s, '', '', '',
            %s, %s, 0, %s, %s,
            %s, %s, %s, 'FEDEX',
            'INTERNACIONAL', %s, %s,
            %s, %s,
            %s, %s, %s
        )
        RETURNING id
        """,
        (
            CLIENTE_ID, fila["estado_portal"], "Envío histórico MELCIOR 2026",
            cantidad_bultos,
            _texto(fila.get("remitente"), maximo=160), pais,
            _texto(fila.get("destinatario"), maximo=160), observaciones,
            peso, f"AR-{pais}", f"HIST-{fila['mes']}-{fila['fila_cliente']}",
            precio, precio + diferencia, tracking, Json(bultos), fila["source_key"],
            idempotencia, fingerprint, instante, instante, instante,
        ),
    )
    return int(cur.fetchone()["id"])


def _asegurar_cargo(cur, solicitud_id: int, fila: dict[str, Any]) -> str:
    cur.execute("SELECT * FROM envios WHERE solicitud_id=%s", (solicitud_id,))
    existente = cur.fetchone()
    genera_deuda = bool(fila.get("genera_deuda"))
    if not genera_deuda:
        if existente and _texto(existente.get("estado"), maximo=20).upper() == "ACTIVO":
            raise ImportacionHistoricaError(
                f"El envío cancelado {fila['source_key']} tiene un cargo activo."
            )
        return "SIN_CARGO"
    monto = _dinero(fila["importe_inicial_ars"], "Importe inicial")
    fecha_operacion = _fecha(fila["fecha"], "Fecha de envío")
    if existente:
        if (
            _texto(existente.get("estado"), maximo=20).upper() != "ACTIVO"
            or existente.get("fecha") != fecha_operacion
            or not _igual_dinero(existente.get("monto_ars"), monto)
            or _texto(existente.get("ambito"), maximo=20).upper() != "INTERNACIONAL"
        ):
            raise ImportacionHistoricaError(
                f"El cargo existente de {fila['source_key']} no coincide con la planilla."
            )
        return "CARGO_EXISTENTE"
    cur.execute(
        """
        INSERT INTO envios (
            cliente_id, fecha, monto_ars, estado, descripcion, tracking,
            ambito, idempotency_key, solicitud_id, created_at
        ) VALUES (%s, %s, %s, 'ACTIVO', %s, %s, 'INTERNACIONAL', %s, %s, %s)
        RETURNING id
        """,
        (
            CLIENTE_ID, fecha_operacion, monto, "Envío histórico MELCIOR 2026",
            _texto(fila.get("tracking"), maximo=30),
            _hash(f"cargo:{fila['source_key']}"), solicitud_id,
            _instante(fecha_operacion),
        ),
    )
    cur.fetchone()
    return "CARGO_NUEVO"


def _asegurar_diferencia(
    cur, solicitud_id: int, fila: dict[str, Any], *, actor: str, source_sha: str
) -> str:
    diferencia = _dinero(fila["diferencia_ars"], "Diferencia", permitir_negativo=True)
    if diferencia == 0:
        return "SIN_DIFERENCIA"
    precio_contable = _dinero(fila["importe_inicial_ars"], "Precio inicial")
    # solicitudes_guia.precio_tauro_ars sigue siendo REAL en instalaciones
    # históricas. El trigger del snapshot compara contra ese valor almacenado;
    # el libro contable exacto permanece en envios.monto_ars NUMERIC(14,2).
    cur.execute(
        "SELECT precio_tauro_ars::numeric AS precio_snapshot "
        "FROM solicitudes_guia WHERE id=%s",
        (solicitud_id,),
    )
    solicitud = cur.fetchone()
    precio = Decimal(str(solicitud["precio_snapshot"])).quantize(CUATRO)
    if abs(precio - precio_contable) > Decimal("1.00"):
        raise ImportacionHistoricaError(
            f"{fila['source_key']}: el precio operativo no coincide con el cargo exacto."
        )
    costo_estimado = _dinero(fila["costo_estimado_ars"], "Costo estimado")
    costo_real = _dinero(fila["costo_real_ars"], "Costo real")
    margen = precio - costo_estimado
    precio_final = precio + diferencia
    instante = _instante(_fecha(fila["fecha"], "Fecha de envío"))
    origen = {
        "fuente": "importacion_historica_melcior_2026",
        "source_sha256": source_sha,
        "fila_cliente": f"{fila['mes']}!{fila['fila_cliente']}",
        "fila_maestra": fila.get("fila_maestra"),
        "costo_real_derivado": bool(fila.get("costo_real_derivado")),
        "precio_contable_exacto_ars": str(precio_contable),
    }
    cur.execute(
        "SELECT * FROM envio_cotizacion_snapshots WHERE solicitud_id=%s",
        (solicitud_id,),
    )
    snapshot = cur.fetchone()
    if snapshot:
        if not (
            _igual_dinero(snapshot.get("costo_courier_estimado_ars"), costo_estimado)
            and _igual_dinero(snapshot.get("precio_cliente_inicial_ars"), precio)
            and _igual_dinero(snapshot.get("margen_tauro_protegido_ars"), margen)
        ):
            raise ImportacionHistoricaError(
                f"El snapshot de {fila['source_key']} no coincide con la evidencia."
            )
    else:
        cur.execute(
            """
            INSERT INTO envio_cotizacion_snapshots (
                solicitud_id, coti_id, courier, servicio_courier,
                moneda_courier, tipo_cambio_ars,
                costo_courier_estimado, costo_courier_estimado_ars,
                precio_cliente_inicial_ars, margen_tauro_protegido_ars,
                peso_real_cotizado_kg, peso_facturable_cotizado_kg,
                bultos, origen_calculo, aceptado_at
            ) VALUES (
                %s, %s, 'FEDEX', 'INTERNACIONAL', 'ARS', 1,
                %s, %s, %s, %s, %s, %s, '[]'::jsonb, %s, %s
            )
            """,
            (
                solicitud_id, f"HIST-{fila['mes']}-{fila['fila_cliente']}",
                costo_estimado, costo_estimado, precio, margen,
                _peso(fila.get("peso_fuente")), _peso(fila.get("peso_fuente")),
                Json(origen), instante,
            ),
        )

    cur.execute(
        "SELECT * FROM conciliaciones_envio WHERE solicitud_id=%s ORDER BY version DESC",
        (solicitud_id,),
    )
    conciliaciones = [dict(registro) for registro in cur.fetchall()]
    for conciliacion in conciliaciones:
        if conciliacion["estado"] == "CERRADA" and (
            _igual_dinero(conciliacion["precio_cliente_inicial_ars"], precio)
            and _igual_dinero(conciliacion["costo_courier_real_ars"], costo_real)
            and _igual_dinero(conciliacion["ajuste_cliente_ars"], diferencia)
        ):
            cur.execute(
                "SELECT * FROM ajustes_cliente WHERE conciliacion_id=%s",
                (conciliacion["id"],),
            )
            ajuste = cur.fetchone()
            if ajuste and ajuste["estado"] == "APLICADO" and _igual_dinero(
                ajuste["monto_ars"], diferencia
            ):
                return "DIFERENCIA_EXISTENTE"
        if conciliacion["estado"] != "ANULADA":
            raise ImportacionHistoricaError(
                f"{fila['source_key']} ya tiene otra conciliación financiera."
            )

    version = max((int(c["version"]) for c in conciliaciones), default=0) + 1
    evidencia = [{
        "tipo": "PLANILLA_HISTORICA_CRUZADA",
        "source_sha256": source_sha,
        "cliente": f"{fila['mes']}!{fila['fila_cliente']}",
        "maestra": fila.get("fila_maestra"),
        "costo_real_derivado": bool(fila.get("costo_real_derivado")),
    }]
    calculo = {
        "solicitud_id": solicitud_id,
        "version": version,
        "precio_inicial": str(precio),
        "costo_estimado": str(costo_estimado),
        "margen": str(margen),
        "costo_real": str(costo_real),
        "precio_final": str(precio_final),
        "diferencia": str(diferencia),
        "source_key": fila["source_key"],
    }
    cur.execute(
        """
        INSERT INTO conciliaciones_envio (
            solicitud_id, version, estado,
            precio_cliente_inicial_ars, costo_courier_estimado_ars,
            margen_tauro_protegido_ars, costo_courier_real_ars,
            precio_cliente_final_ars, ajuste_cliente_ars,
            diferencia_flete_ars, tax_cliente_ars,
            peso_cotizado_kg, peso_base_facturado, motivo_diferencia,
            formula_version, calculo_hash, evidencias, evidencia_completa,
            calculado_por, calculado_at, aprobado_por, aprobado_at,
            created_at, updated_at
        ) VALUES (
            %s, %s, 'CERRADA', %s, %s, %s, %s, %s, %s, %s, 0,
            %s, 'NO_INFORMADO', 'OTRO', 'MARGEN_PROTEGIDO_V1', %s, %s, TRUE,
            %s, NOW(), %s, NOW(), NOW(), NOW()
        ) RETURNING id
        """,
        (
            solicitud_id, version, precio, costo_estimado, margen, costo_real,
            precio_final, diferencia, diferencia, _peso(fila.get("peso_fuente")),
            _hash_json(calculo), Json(evidencia), actor, actor,
        ),
    )
    conciliacion_id = int(cur.fetchone()["id"])
    tipo = "DEBITO" if diferencia > 0 else "CREDITO"
    cur.execute(
        """
        INSERT INTO ajustes_cliente (
            conciliacion_id, solicitud_id, tipo, monto_ars,
            precio_anterior_ars, precio_nuevo_ars, estado, idempotency_key,
            motivo, propuesto_por, aprobado_por, aprobado_at,
            aplicado_por, aplicado_at, referencia_aplicacion,
            created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, 'APLICADO', %s,
            %s, %s, %s, NOW(), %s, %s, %s, NOW(), NOW()
        ) RETURNING id
        """,
        (
            conciliacion_id, solicitud_id, tipo, diferencia, precio, precio_final,
            _hash(f"ajuste:{fila['source_key']}"),
            "Diferencia histórica según planillas conciliadas",
            actor, actor, actor, instante, fila["source_key"],
        ),
    )
    ajuste_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO auditoria_facturas_courier (
            evento, solicitud_id, conciliacion_id, ajuste_id, actor, metadata
        ) VALUES ('AJUSTE_HISTORICO_APLICADO', %s, %s, %s, %s, %s)
        """,
        (
            solicitud_id, conciliacion_id, ajuste_id, actor,
            Json({
                "source_sha256": source_sha,
                "source_key": fila["source_key"],
                "diferencia_ars": str(diferencia),
                "tax_ars": "0.00",
            }),
        ),
    )
    return "DIFERENCIA_NUEVA"


def _importar_mes(
    manifiesto: dict[str, Any], mes: str, *, actor: str
) -> dict[str, Any]:
    filas = [fila for fila in manifiesto["envios"] if fila["mes"] == mes]
    esperado = resumen_periodo(manifiesto, mes)
    estados = Counter()
    solicitudes_ids: list[int] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("MELCIOR-2026",))
            _validar_cliente(cur)
            for fila in filas:
                idempotencia = _hash(f"solicitud:{fila['source_key']}")
                fingerprint = _hash_json({
                    clave: fila.get(clave) for clave in (
                        "source_key", "fecha", "destinatario", "pais_fuente",
                        "tracking", "importe_inicial_ars", "diferencia_ars",
                        "estado_portal", "genera_deuda",
                    )
                })
                existente, estado = _solicitud_existente(cur, fila, idempotencia)
                if existente:
                    _verificar_solicitud(existente, fila, fingerprint)
                    solicitud_id = int(existente["id"])
                else:
                    solicitud_id = _crear_solicitud(cur, fila, idempotencia, fingerprint)
                solicitudes_ids.append(solicitud_id)
                estados[estado] += 1
                estados[_asegurar_cargo(cur, solicitud_id, fila)] += 1
                estados[_asegurar_diferencia(
                    cur, solicitud_id, fila, actor=actor,
                    source_sha=manifiesto["source_sha256"],
                )] += 1

            cur.execute(
                "SELECT COUNT(*) AS n FROM solicitudes_guia WHERE id = ANY(%s)",
                (solicitudes_ids,),
            )
            verificados = int(cur.fetchone()["n"])
            cur.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(monto_ars), 0) AS total
                FROM envios
                WHERE solicitud_id = ANY(%s) AND estado='ACTIVO'
                """,
                (solicitudes_ids,),
            )
            cargos = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(monto_ars), 0) AS total
                FROM ajustes_cliente
                WHERE solicitud_id = ANY(%s) AND estado='APLICADO'
                """,
                (solicitudes_ids,),
            )
            diferencias = cur.fetchone()
            if verificados != int(esperado["envios"]):
                raise ImportacionHistoricaError(f"{mes}: cantidad final de envíos no coincide.")
            if int(cargos["n"]) != int(esperado["cargos"]) or not _igual_dinero(
                cargos["total"], esperado["importe_inicial_ars"]
            ):
                raise ImportacionHistoricaError(f"{mes}: los cargos finales no coinciden.")
            if int(diferencias["n"]) != int(esperado["con_diferencia"]) or not _igual_dinero(
                diferencias["total"], esperado["diferencias_ars"]
            ):
                raise ImportacionHistoricaError(f"{mes}: las diferencias finales no coinciden.")
            resultado = {
                "periodo": mes,
                "envios": verificados,
                "cargos": int(cargos["n"]),
                "importe_inicial_ars": str(Decimal(str(cargos["total"])).quantize(CENTAVO)),
                "diferencias": int(diferencias["n"]),
                "diferencias_ars": str(Decimal(str(diferencias["total"])).quantize(CENTAVO)),
                "detalle": dict(estados),
            }
            registrar_evento_con_cursor(
                cur,
                event="cuenta.importacion_historica_mes",
                actor_type="admin", actor_ref=actor,
                ip=None, method="POST", path="/admin/importaciones-historicas/melcior",
                status_code=200, success=True, request_id=None,
                metadata={
                    **resultado,
                    "cliente_id": CLIENTE_ID,
                    "source_sha256": manifiesto["source_sha256"],
                    "route_model": "gpt-5.6-sol",
                    "controls": [
                        "cruce_doble_planilla", "idempotencia", "tracking_unico",
                        "totales_deterministicos", "transaccion_por_mes",
                    ],
                },
            )
            return resultado


def _insertar_cargo_cierre(cur, saldo: dict[str, Any]) -> str:
    clave = _hash(f"cargo:{saldo['source_key']}")
    cur.execute(
        "SELECT * FROM envios WHERE cliente_id=%s AND idempotency_key=%s",
        (CLIENTE_ID, clave),
    )
    existente = cur.fetchone()
    monto = _dinero(saldo["monto_ars"], "Saldo 2025")
    fecha_saldo = _fecha(saldo["fecha"], "Saldo 2025")
    if existente:
        if (
            existente["fecha"] != fecha_saldo
            or not _igual_dinero(existente["monto_ars"], monto)
            or _texto(existente["descripcion"], maximo=80) != "SALDO PENDIENTE 2025"
            or _texto(existente["estado"], maximo=20).upper() != "ACTIVO"
        ):
            raise ImportacionHistoricaError("El saldo 2025 existente no coincide.")
        return "SALDO_EXISTENTE"
    cur.execute(
        """
        INSERT INTO envios (
            cliente_id, fecha, monto_ars, estado, descripcion, ambito,
            idempotency_key, created_at
        ) VALUES (%s, %s, %s, 'ACTIVO', 'SALDO PENDIENTE 2025',
                  'INTERNACIONAL', %s, %s)
        """,
        (CLIENTE_ID, fecha_saldo, monto, clave, _instante(fecha_saldo)),
    )
    return "SALDO_NUEVO"


def _insertar_pago(cur, pago: dict[str, Any]) -> str:
    clave = _hash(f"pago:{pago['source_key']}")
    cur.execute(
        "SELECT * FROM pagos WHERE cliente_id=%s AND idempotency_key=%s",
        (CLIENTE_ID, clave),
    )
    existente = cur.fetchone()
    monto = _dinero(pago["monto_ars"], "Pago")
    fecha_pago = _fecha(pago["fecha"], "Pago")
    if existente:
        if (
            existente["fecha"] != fecha_pago
            or not _igual_dinero(existente["monto_ars"], monto)
            or _texto(existente["estado"], maximo=20).upper() != "APROBADO"
        ):
            raise ImportacionHistoricaError("Un pago histórico existente no coincide.")
        pago_id = int(existente["id"])
        estado = "PAGO_EXISTENTE"
    else:
        cur.execute(
            """
            INSERT INTO pagos (
                cliente_id, fecha, monto_ars, metodo, referencia, nota,
                estado, idempotency_key, created_at
            ) VALUES (
                %s, %s, %s, 'Transferencia', '', %s, 'APROBADO', %s, NOW()
            ) RETURNING id
            """,
            (
                CLIENTE_ID, fecha_pago, monto,
                f"Importación histórica MELCIOR 2026 · fecha original no informada · {pago['source_key']}",
                clave,
            ),
        )
        pago_id = int(cur.fetchone()["id"])
        estado = "PAGO_NUEVO"
    cur.execute(
        "SELECT * FROM pagos_aplicaciones WHERE pago_id=%s AND ambito='INTERNACIONAL'",
        (pago_id,),
    )
    aplicacion = cur.fetchone()
    if aplicacion:
        if aplicacion["estado"] != "APLICADA" or not _igual_dinero(
            aplicacion["monto_ars"], monto
        ):
            raise ImportacionHistoricaError("La aplicación de un pago no coincide.")
    else:
        cur.execute(
            """
            INSERT INTO pagos_aplicaciones (pago_id, ambito, monto_ars, estado)
            VALUES (%s, 'INTERNACIONAL', %s, 'APLICADA')
            """,
            (pago_id, monto),
        )
    return estado


def _importar_cierre(manifiesto: dict[str, Any], *, actor: str) -> dict[str, Any]:
    esperado = resumen_periodo(manifiesto, "CIERRE")
    estados = Counter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("MELCIOR-2026",))
            _validar_cliente(cur)
            estados[_insertar_cargo_cierre(cur, manifiesto["saldo_pendiente_2025"])] += 1
            claves_pago = []
            for pago in manifiesto["pagos"]:
                estados[_insertar_pago(cur, pago)] += 1
                claves_pago.append(_hash(f"pago:{pago['source_key']}"))
            cur.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(pa.monto_ars), 0) AS total
                FROM pagos p
                JOIN pagos_aplicaciones pa ON pa.pago_id=p.id
                WHERE p.cliente_id=%s AND p.idempotency_key=ANY(%s)
                  AND p.estado='APROBADO' AND pa.ambito='INTERNACIONAL'
                  AND pa.estado='APLICADA'
                """,
                (CLIENTE_ID, claves_pago),
            )
            pagos = cur.fetchone()
            if int(pagos["n"]) != int(esperado["pagos"]) or not _igual_dinero(
                pagos["total"], esperado["pagos_ars"]
            ):
                raise ImportacionHistoricaError("El cierre no conserva los pagos aprobados.")
            resultado = {
                "periodo": "CIERRE",
                "saldo_pendiente_2025_ars": str(esperado["saldo_pendiente_2025_ars"]),
                "pagos": int(pagos["n"]),
                "pagos_ars": str(Decimal(str(pagos["total"])).quantize(CENTAVO)),
                "detalle": dict(estados),
            }
            registrar_evento_con_cursor(
                cur,
                event="cuenta.importacion_historica_cierre",
                actor_type="admin", actor_ref=actor,
                ip=None, method="POST", path="/admin/importaciones-historicas/melcior",
                status_code=200, success=True, request_id=None,
                metadata={
                    **resultado,
                    "cliente_id": CLIENTE_ID,
                    "source_sha256": manifiesto["source_sha256"],
                    "route_model": "gpt-5.6-sol",
                    "controls": ["idempotencia", "aplicacion_internacional", "totales_deterministicos"],
                },
            )
            return resultado


def importar_periodo(
    manifiesto: dict[str, Any], periodo: str, *, actor: str = "admin"
) -> dict[str, Any]:
    validar_manifiesto(manifiesto)
    periodo = _texto(periodo, maximo=20).upper()
    if periodo not in PERIODOS:
        raise ImportacionHistoricaError("Período de importación inválido.")
    if periodo == "CIERRE":
        return _importar_cierre(manifiesto, actor=_texto(actor, maximo=120) or "admin")
    return _importar_mes(
        manifiesto, periodo, actor=_texto(actor, maximo=120) or "admin"
    )
