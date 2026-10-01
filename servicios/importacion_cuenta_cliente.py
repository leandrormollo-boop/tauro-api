"""Completa la cuenta corriente 2026 de un cliente desde su planilla.

Se usa para MELCIOR (enero–agosto ya estaba importado sin TAX ni las
correcciones posteriores) y para PRETE ROSSO (sin importar). El servicio es
**incremental e idempotente**: compara cada envío del manifiesto con lo que ya
existe en el portal y agrega sólo lo que falta.

Reglas contables:
  * Nunca borra ni reduce nada. Si el portal cobra más que la planilla, la
    fila queda como CONFLICTO y no se toca.
  * El total de cada envío (cargo + diferencias + TAX) debe quedar igual al de
    la planilla. Lo faltante se agrega como una nueva conciliación CERRADA con
    su ajuste APLICADO, separando diferencia de flete y TAX.
  * Un cargo histórico en $0 (placeholder) se completa con el importe de la
    planilla sólo si no está facturado ni imputado.
  * Un tracking repetido sin crear, un tracking de otro cliente o un envío del
    portal con otro estado se informan como conflicto: deciden Leandro/admin.
  * Pagos y saldo 2025 se agregan por idempotencia. Si el portal tiene pagos
    aprobados que no vienen de la planilla, no se agregan pagos nuevos.
  * Todo corre en una transacción por cliente. La vista previa ejecuta lo mismo
    y revierte al final.

El portal nunca muestra costos de operador: sólo importe, diferencias y TAX.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor


CLIENTES = {
    "MELCIOR": {"prefijo": "MELCIOR-2026", "descripcion": "Envío histórico MELCIOR 2026"},
    "PRETE ROSSO": {"prefijo": "PRETE-ROSSO-2026", "descripcion": "Envío histórico PRETE ROSSO 2026"},
}
MESES = (
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
)
CENTAVO = Decimal("0.01")
TOLERANCIA = Decimal("0.01")
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
COURIERS = {"FEDEX", "DHL", "UPS"}
PAISES = {
    "": "XX", "USA": "US", "YSA": "US", "EEUU": "US", "CAN": "CA", "AUS": "AU",
    "COL": "CO", "MEX": "MX", "ALE": "DE", "TAI": "TH", "POL": "PL", "SUI": "CH",
    "FRA": "FR", "REIU": "GB", "REI": "GB", "UK": "GB", "JAP": "JP", "ITA": "IT",
    "CHI": "CL", "BRA": "BR", "NOR": "NO", "ISR": "IL", "IRL": "IE", "ESP": "ES",
    "ARABIA": "SA", "HOL": "NL", "BEL": "BE", "POR": "PT", "AUT": "AT",
    "NZ": "NZ", "SUE": "SE", "DIN": "DK", "URU": "UY", "PER": "PE",
}


class ImportacionCuentaError(ValueError):
    pass


class _VistaPrevia(Exception):
    """Revierte la transacción de una vista previa conservando el informe."""

    def __init__(self, informe: dict[str, Any]):
        super().__init__("vista previa")
        self.informe = informe


# ── utilidades ──────────────────────────────────────────────────────────────

def _texto(valor: Any, maximo: int = 400) -> str:
    return " ".join(str(valor or "").strip().split())[:maximo]


def _normal(valor: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _texto(valor).upper())


def _dinero(valor: Any, campo: str, *, permitir_negativo: bool = False) -> Decimal:
    try:
        numero = Decimal(str(valor)).quantize(CENTAVO)
    except (InvalidOperation, TypeError, ValueError):
        raise ImportacionCuentaError(f"{campo}: importe inválido.") from None
    if not numero.is_finite() or (numero < 0 and not permitir_negativo):
        raise ImportacionCuentaError(f"{campo}: importe fuera de rango.")
    return numero


def _dec(valor: Any) -> Decimal:
    return Decimal(str(valor or 0)).quantize(CENTAVO)


def _fecha(valor: Any, campo: str) -> date:
    try:
        return date.fromisoformat(str(valor))
    except (TypeError, ValueError):
        raise ImportacionCuentaError(f"{campo}: fecha inválida.") from None


def _instante(fecha_operacion: date) -> datetime:
    return datetime.combine(fecha_operacion, time(hour=12), ZoneInfo("America/Argentina/Buenos_Aires"))


def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _hash_json(datos: Any) -> str:
    return _hash(json.dumps(datos, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))


def manifest_hash(manifiesto: dict[str, Any]) -> str:
    copia = copy.deepcopy(manifiesto)
    copia.pop("manifest_sha256", None)
    return _hash_json(copia)


def _peso(valor: Any) -> Decimal | None:
    coincidencia = re.search(r"\d+(?:[.,]\d+)?", _texto(valor, 80))
    if not coincidencia:
        return None
    peso = Decimal(coincidencia.group(0).replace(",", ".")).quantize(Decimal("0.001"))
    return peso if peso > 0 else None


def _bultos(medidas_valor: Any, peso_valor: Any) -> list[dict[str, Any]]:
    medidas = _texto(medidas_valor, 240)
    peso_total = _peso(peso_valor)
    patron = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)(?!\d)")
    coincidencias = list(patron.finditer(medidas))
    resultado: list[dict[str, Any]] = []
    for indice, coincidencia in enumerate(coincidencias):
        fin = coincidencias[indice + 1].start() if indice + 1 < len(coincidencias) else len(medidas)
        repeticion = re.search(r"[xX×]\s*(\d+)\b", medidas[coincidencia.end():fin])
        cantidad = int(repeticion.group(1)) if repeticion else 1
        cantidad = cantidad if 1 <= cantidad <= 100 else 1
        largo, ancho, alto = [float(Decimal(v.replace(",", "."))) for v in coincidencia.groups()]
        resultado.append({"producto_alias": "Mercadería", "cantidad": cantidad,
                          "largo_cm": largo, "ancho_cm": ancho, "alto_cm": alto, "peso_kg": None})
    if sum(int(b["cantidad"]) for b in resultado) == 1 and peso_total is not None:
        resultado[0]["peso_kg"] = float(peso_total)
    elif not resultado and peso_total is not None:
        resultado.append({"producto_alias": "Mercadería", "cantidad": 1, "peso_kg": float(peso_total)})
    return resultado


def _pais(valor: Any) -> str:
    fuente = _texto(valor, 40).upper()
    if fuente in PAISES:
        return PAISES[fuente]
    return fuente if re.fullmatch(r"[A-Z]{2}", fuente) else "XX"


# ── validación ──────────────────────────────────────────────────────────────

def _total_envio(fila: dict[str, Any]) -> Decimal:
    if fila["estado_portal"] == "CANCELADO":
        return Decimal("0.00")
    return (_dec(fila["importe_inicial_ars"]) + _dec(fila["diferencia_flete_ars"])
            + _dec(fila["tax_cliente_ars"]))


def validar_manifiesto(manifiesto: Any) -> dict[str, Any]:
    if not isinstance(manifiesto, dict) or manifiesto.get("schema_version") != 2:
        raise ImportacionCuentaError("Versión de manifiesto no soportada.")
    if manifiesto.get("tipo") != "CUENTA_CLIENTE_2026" or manifiesto.get("periodo") != 2026:
        raise ImportacionCuentaError("El archivo no es un manifiesto de cuenta 2026.")
    cliente = _texto(manifiesto.get("cliente_id"), 40).upper()
    if cliente not in CLIENTES:
        raise ImportacionCuentaError("Cliente no habilitado para esta importación.")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifiesto.get("manifest_sha256") or "")):
        raise ImportacionCuentaError("Falta la huella del manifiesto.")
    if manifest_hash(manifiesto) != manifiesto["manifest_sha256"]:
        raise ImportacionCuentaError("El contenido del manifiesto fue modificado.")
    prefijo = CLIENTES[cliente]["prefijo"] + ":"
    envios = manifiesto.get("envios")
    if not isinstance(envios, list) or not envios:
        raise ImportacionCuentaError("El manifiesto no contiene envíos.")
    claves: set[str] = set()
    trackings_creables: Counter[str] = Counter()
    calculado = Counter()
    montos = defaultdict(lambda: Decimal("0.00"))
    for numero, fila in enumerate(envios, 1):
        if not isinstance(fila, dict):
            raise ImportacionCuentaError(f"Envío {numero}: formato inválido.")
        clave = _texto(fila.get("source_key"), 160)
        if not clave.startswith(prefijo) or clave in claves:
            raise ImportacionCuentaError(f"Envío {numero}: source_key inválida o repetida.")
        claves.add(clave)
        if _texto(fila.get("mes"), 20).upper() not in MESES:
            raise ImportacionCuentaError(f"{clave}: mes inválido.")
        if _fecha(fila.get("fecha"), clave).year != 2026:
            raise ImportacionCuentaError(f"{clave}: fecha fuera de 2026.")
        tracking = _texto(fila.get("tracking"), 30)
        if tracking and not re.fullmatch(r"\d{8,20}", tracking):
            raise ImportacionCuentaError(f"{clave}: tracking inválido.")
        estado = _texto(fila.get("estado_portal"), 20).upper()
        if estado not in {"DESPACHADO", "CANCELADO"}:
            raise ImportacionCuentaError(f"{clave}: estado inválido.")
        if _texto(fila.get("courier") or "FEDEX", 20).upper() not in COURIERS:
            raise ImportacionCuentaError(f"{clave}: courier inválido.")
        importe = _dinero(fila.get("importe_inicial_ars"), f"{clave} importe")
        diferencia = _dinero(fila.get("diferencia_flete_ars"), f"{clave} diferencia", permitir_negativo=True)
        tax = _dinero(fila.get("tax_cliente_ars"), f"{clave} TAX")
        if str(fila.get("costo_courier_ars") or "").strip():
            _dinero(fila.get("costo_courier_ars"), f"{clave} costo")
        if bool(fila.get("genera_deuda")) != (importe > 0 and estado != "CANCELADO"):
            raise ImportacionCuentaError(f"{clave}: deuda inconsistente.")
        if estado == "CANCELADO" and (importe or diferencia or tax):
            raise ImportacionCuentaError(f"{clave}: un cancelado no puede generar saldo.")
        if importe + diferencia + tax < 0:
            raise ImportacionCuentaError(f"{clave}: total negativo.")
        if fila.get("crear_si_falta") and tracking and estado != "CANCELADO":
            trackings_creables[tracking] += 1
        creable = bool(fila.get("crear_si_falta"))
        calculado["envios"] += 1
        calculado["cancelados"] += int(estado == "CANCELADO")
        calculado["cargos"] += int(bool(fila.get("genera_deuda")) and creable)
        calculado["repetidos_sin_crear"] += int(not creable)
        if creable:
            montos["fletes_ars"] += importe
            montos["diferencias_ars"] += diferencia
            montos["tax_ars"] += tax
    repetidos = [t for t, n in trackings_creables.items() if n > 1]
    if repetidos and manifiesto.get("politica_duplicados") != "CARGAR_OCULTO":
        raise ImportacionCuentaError(f"Trackings repetidos creables: {repetidos[:5]}.")
    pagos = manifiesto.get("pagos")
    if not isinstance(pagos, list):
        raise ImportacionCuentaError("Faltan los pagos.")
    claves_pago: set[str] = set()
    for pago in pagos:
        clave = _texto(pago.get("source_key"), 160)
        if not clave.startswith(prefijo) or clave in claves_pago:
            raise ImportacionCuentaError("Pago con source_key inválida o repetida.")
        claves_pago.add(clave)
        _fecha(pago.get("fecha"), clave)
        if _dinero(pago.get("monto_ars"), clave) <= 0:
            raise ImportacionCuentaError(f"{clave}: pago sin importe.")
        montos["pagos_ars"] += _dec(pago["monto_ars"])
    saldo = manifiesto.get("saldo_pendiente_2025") or {}
    if _texto(saldo.get("concepto"), 80) != "SALDO PENDIENTE 2025" or not _texto(saldo.get("source_key")).startswith(prefijo):
        raise ImportacionCuentaError("El saldo 2025 perdió su concepto.")
    _fecha(saldo.get("fecha"), "Saldo 2025")
    saldo_2025 = _dinero(saldo.get("monto_ars"), "Saldo 2025")
    resumen = manifiesto.get("resumen") or {}
    for campo in ("envios", "cargos", "cancelados", "repetidos_sin_crear"):
        if int(resumen.get(campo, -1)) != calculado[campo]:
            raise ImportacionCuentaError(f"Resumen: {campo} no coincide.")
    for campo in ("fletes_ars", "diferencias_ars", "tax_ars", "pagos_ars"):
        if _dinero(resumen.get(campo), campo, permitir_negativo=True) != montos[campo]:
            raise ImportacionCuentaError(f"Resumen: {campo} no coincide.")
    saldo_resultante = (montos["fletes_ars"] + montos["diferencias_ars"] + montos["tax_ars"]
                        + saldo_2025 - montos["pagos_ars"])
    if _dinero(resumen.get("saldo_resultante_ars"), "Saldo", permitir_negativo=True) != saldo_resultante:
        raise ImportacionCuentaError("El saldo resultante del manifiesto no cierra.")
    if int(resumen.get("pagos", -1)) != len(pagos):
        raise ImportacionCuentaError("Resumen: cantidad de pagos no coincide.")
    return manifiesto


def leer_manifiesto(contenido: bytes) -> dict[str, Any]:
    if not contenido or len(contenido) > MAX_MANIFEST_BYTES:
        raise ImportacionCuentaError("El archivo está vacío o supera 8 MB.")
    try:
        datos = json.loads(contenido.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportacionCuentaError("El archivo no es un JSON válido.") from None
    return validar_manifiesto(datos)


# ── estado del portal ──────────────────────────────────────────────────────

def _estado_solicitud(cur, solicitud_id: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM envios WHERE solicitud_id=%s", (solicitud_id,))
    cargo = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(monto_ars),0) AS ajustes FROM ajustes_cliente "
        "WHERE solicitud_id=%s AND estado='APLICADO'",
        (solicitud_id,),
    )
    ajustes = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM conciliaciones_envio WHERE solicitud_id=%s",
        (solicitud_id,),
    )
    version_max = int(cur.fetchone()["v"])
    # Cada versión CERRADA es acumulativa respecto del precio inicial del envío
    # (igual que la conciliación courier): la última trae la diferencia y el TAX
    # totales, el costo y el margen vigentes.
    cur.execute(
        "SELECT * FROM conciliaciones_envio "
        "WHERE solicitud_id=%s AND estado='CERRADA' ORDER BY version DESC LIMIT 1",
        (solicitud_id,),
    )
    ultima_cerrada = cur.fetchone()
    cargo_activo = cargo is not None and cargo["estado"] == "ACTIVO"
    return {
        "cargo": dict(cargo) if cargo else None,
        "cargo_activo": cargo_activo,
        "cargo_ars": _dec(cargo["monto_ars"]) if cargo_activo else Decimal("0.00"),
        "ajustes_ars": _dec(ajustes["ajustes"]),
        "tax_ars": _dec(ultima_cerrada["tax_cliente_ars"]) if ultima_cerrada else Decimal("0.00"),
        "version_max": version_max,
        "precio_final_cerrado": _dec(ultima_cerrada["precio_cliente_final_ars"]) if ultima_cerrada else None,
        "ultima_conciliacion": dict(ultima_cerrada) if ultima_cerrada else None,
    }


def _misma_guia(solicitud: dict[str, Any], fila: dict[str, Any]) -> bool:
    """La source_key es la posición de la fila en la planilla. Si se borran
    filas, las claves de las filas de abajo se corren y pueden apuntar a otro
    envío: sólo se acepta la coincidencia por clave si es el mismo envío."""
    t_fila = _normal(fila.get("tracking"))
    t_portal = _normal(solicitud.get("tracking"))
    if t_fila and t_portal:
        return t_fila == t_portal
    if t_fila and t_fila in _normal(solicitud.get("observaciones")):
        return True
    destino = _normal(fila.get("destinatario") or "Destinatario no informado")
    return _normal(solicitud.get("dest_nombre"))[:12] == destino[:12]


def _clave_alterna(fila: dict[str, Any]) -> str:
    """Clave para crear un envío cuya posición ya ocupa otro envío en el portal."""
    identidad = _normal(fila.get("tracking")) or _normal(fila.get("destinatario"))[:24]
    return f"{fila['source_key']}#{identidad}"


def _por_clave(cur, cliente: str, clave: str):
    cur.execute(
        "SELECT * FROM solicitudes_guia WHERE cliente_id=%s AND idempotency_key_hash=%s",
        (cliente, _hash(f"solicitud:{clave}")),
    )
    fila = cur.fetchone()
    return dict(fila) if fila else None


def _buscar_solicitud(cur, cliente: str, fila: dict[str, Any], filas_por_clave: dict[str, dict[str, Any]]):
    """Devuelve (solicitud | None, origen, clave efectiva para escribir).

    Orden: clave de la fila (si es el mismo envío) → clave alterna → tracking.
    La clave es la posición de la fila en la planilla; si se borran filas, las
    claves de abajo se corren y pueden apuntar a otro envío del portal."""
    clave = fila["source_key"]
    ocupada_por = _por_clave(cur, cliente, clave)
    if ocupada_por and _misma_guia(ocupada_por, fila):
        return ocupada_por, "EXISTENTE", clave
    if ocupada_por:
        clave = _clave_alterna(fila)
        alterna = _por_clave(cur, cliente, clave)
        if alterna and _misma_guia(alterna, fila):
            return alterna, "EXISTENTE", clave
        if alterna:
            raise _Conflicto(f"La clave {fila['source_key']} y su alterna pertenecen a otros envíos del portal.")
    tracking = _texto(fila.get("tracking"), 30)
    candidata = None
    if tracking and not fila.get("tracking_repetido"):
        # Una repetición contable nunca adopta la guía visible del tracking.
        cur.execute(
            "SELECT * FROM solicitudes_guia WHERE UPPER(BTRIM(tracking))=UPPER(%s) ORDER BY id",
            (tracking,),
        )
        candidatas = [dict(r) for r in cur.fetchall()]
        ajenas = [c for c in candidatas if _texto(c.get("cliente_id"), 40).upper() != cliente]
        if ajenas:
            raise _Conflicto(f"El tracking {tracking} pertenece a otro cliente en el portal.")
        if len(candidatas) > 1:
            raise _Conflicto(f"El tracking {tracking} tiene {len(candidatas)} envíos en el portal.")
        candidata = candidatas[0] if candidatas else None
    if candidata is not None:
        referencia = _texto(candidata.get("api_referencia"), 160)
        otra = filas_por_clave.get(referencia)
        if otra is not None and referencia != fila["source_key"] and _normal(otra.get("tracking")) == _normal(tracking):
            # Otra fila de la planilla con el mismo tracking ya es dueña del envío.
            raise _Repetido(f"Ya está cargado como {referencia}.")
        destino = _normal(fila.get("destinatario"))
        if destino and _normal(candidata.get("dest_nombre")) != destino:
            # Regla de Leandro: mismo tracking con otro destinatario es un error
            # de datos. No se adopta el envío de otro destinatario.
            raise _Conflicto(
                f"El tracking {tracking} está en el portal a nombre de "
                f"{candidata.get('dest_nombre')}: revisar el tracking."
            )
        return candidata, "ADOPTADA", clave
    if ocupada_por and not _sigue_en_planilla(ocupada_por, fila, filas_por_clave):
        # El envío que ocupa la posición ya no está en la planilla: lo más
        # probable es que se corrigió el tracking. No se crea un duplicado.
        raise _Conflicto(
            f"En el portal, {fila['source_key']} es otro envío "
            f"({ocupada_por.get('tracking') or ocupada_por.get('dest_nombre')}) que no está en la planilla."
        )
    return None, "NUEVA", clave


def _sigue_en_planilla(solicitud: dict[str, Any], fila: dict[str, Any],
                       filas_por_clave: dict[str, dict[str, Any]]) -> bool:
    t_portal = _normal(solicitud.get("tracking"))
    return any(
        otra is not fila and _misma_guia(solicitud, otra)
        for otra in filas_por_clave.values()
        if not t_portal or _normal(otra.get("tracking")) == t_portal
    )


class _Conflicto(Exception):
    pass


class _Repetido(Exception):
    pass


# ── escrituras ──────────────────────────────────────────────────────────────

def _crear_solicitud(cur, cliente: str, fila: dict[str, Any]) -> int:
    fecha_operacion = _fecha(fila["fecha"], "Fecha")
    instante = _instante(fecha_operacion)
    importacion = fila.get("tipo_fuente") == "IMPO"
    origen, destino = ("CN", "AR") if importacion else ("AR", _pais(fila.get("pais_fuente")))
    bultos = _bultos(fila.get("medidas_fuente"), fila.get("peso_fuente"))
    cantidad = sum(int(b.get("cantidad") or 1) for b in bultos) or 1
    visible = bool(fila.get("visible_cliente"))
    tracking = _texto(fila.get("tracking"), 30) if visible or fila.get("tipo_fuente") == "TAX_SIN_FLETE" else ""
    # El portal muestra observaciones al cliente: sólo texto apto para él. Las
    # marcas internas (repetido, ancla de TAX, revisión) quedan en el informe
    # de la importación y en la auditoría.
    observaciones = CLIENTES[cliente]["descripcion"]
    precio = _dec(fila["importe_inicial_ars"])
    final = _total_envio(fila)
    cur.execute(
        """
        INSERT INTO solicitudes_guia (
            cliente_id, estado, producto_alias, cantidad,
            remitente_nombre, remitente_pais, ambito,
            destino_pais, dest_nombre, dest_direccion, dest_ciudad, dest_zip,
            observaciones, peso_kg, valor_declarado_usd, ruta_id, coti_id,
            precio_tauro_ars, precio_cliente_final_ars, tracking, courier,
            servicio_courier, bultos, api_referencia, visible_cliente,
            idempotency_key_hash, request_fingerprint,
            guia_generada_at, created_at, updated_at
        ) VALUES (
            %s,%s,%s,%s,%s,%s,'INTERNACIONAL',%s,%s,'','','',%s,%s,0,%s,%s,
            %s,%s,%s,%s,'INTERNACIONAL',%s,%s,%s,%s,%s,%s,%s,%s
        ) RETURNING id
        """,
        (
            cliente, fila["estado_portal"],
            "Importación histórica" if importacion else "Envío histórico", cantidad,
            _texto(fila.get("remitente"), 160) or cliente, origen, destino,
            _texto(fila.get("destinatario"), 160) or "Destinatario no informado", observaciones,
            _peso(fila.get("peso_fuente")), f"{origen}-{destino}",
            f"HIST-{fila['mes']}-{fila['fila_cliente']}", precio, final, tracking or None,
            _texto(fila.get("courier") or "FEDEX", 20).upper(), Json(bultos), fila["source_key"],
            visible, _hash(f"solicitud:{fila['source_key']}"), _hash_json(fila),
            instante, instante, instante,
        ),
    )
    return int(cur.fetchone()["id"])


def _crear_cargo(cur, cliente: str, solicitud_id: int, fila: dict[str, Any], monto: Decimal) -> None:
    fecha_operacion = _fecha(fila["fecha"], "Fecha")
    cur.execute(
        """
        INSERT INTO envios (
            cliente_id, fecha, monto_ars, estado, descripcion, tracking,
            ambito, idempotency_key, solicitud_id, created_at
        ) VALUES (%s,%s,%s,'ACTIVO',%s,%s,'INTERNACIONAL',%s,%s,%s)
        """,
        (
            cliente, fecha_operacion, monto, CLIENTES[cliente]["descripcion"],
            _texto(fila.get("tracking"), 30), _hash(f"cargo:{fila['source_key']}"),
            solicitud_id, _instante(fecha_operacion),
        ),
    )


def _completar_cargo_cero(cur, cargo: dict[str, Any], solicitud_id: int, monto: Decimal) -> None:
    """Un cargo histórico en $0 era un lugar reservado: se completa una vez."""
    cur.execute(
        "SELECT COUNT(*) AS n FROM facturas_cliente_items WHERE envio_id=%s", (cargo["id"],)
    )
    facturado = int(cur.fetchone()["n"])
    cur.execute(
        "SELECT COUNT(*) AS n FROM pagos_aplicaciones WHERE envio_id=%s", (cargo["id"],)
    )
    imputado = int(cur.fetchone()["n"])
    if facturado or imputado:
        raise _Conflicto("El cargo en $0 ya está facturado o imputado; no se modifica.")
    cur.execute("UPDATE envios SET monto_ars=%s WHERE id=%s AND monto_ars=0", (monto, cargo["id"]))
    if cur.rowcount != 1:
        raise _Conflicto("El cargo cambió durante la importación.")
    cur.execute(
        "UPDATE solicitudes_guia SET precio_tauro_ars=%s, cargo_pendiente=FALSE, updated_at=NOW() "
        "WHERE id=%s AND COALESCE(precio_tauro_ars,0)=0",
        (monto, solicitud_id),
    )


def _agregar_ajuste(cur, solicitud_id: int, fila: dict[str, Any], estado: dict[str, Any],
                    delta: Decimal, *, actor: str, manifest_sha: str) -> None:
    """Nueva versión CERRADA, acumulativa como la conciliación courier:
    precio inicial fijo, diferencia y TAX totales, ajuste = final − inicial.
    El movimiento del cliente (ajustes_cliente) es sólo el delta."""
    previa = estado["ultima_conciliacion"]
    tax_objetivo = _dec(fila["tax_cliente_ars"])
    tax_inc = max(Decimal("0.00"), min(delta, tax_objetivo - estado["tax_ars"]))
    dif_inc = delta - tax_inc
    previo = estado["precio_final_cerrado"]
    if previo is None:
        previo = estado["cargo_ars"]
    final = previo + delta
    if previa:
        inicial = _dec(previa["precio_cliente_inicial_ars"])
        dif_total = _dec(previa["diferencia_flete_ars"]) + dif_inc
        tax_total = _dec(previa["tax_cliente_ars"]) + tax_inc
        costo_estimado = _dec(previa["costo_courier_estimado_ars"])
        margen = _dec(previa["margen_tauro_protegido_ars"])
    else:
        inicial = previo
        dif_total, tax_total = dif_inc, tax_inc
        costo_estimado = inicial
        margen = Decimal("0.00")
    # Costo real según TAURO 2026 (flete + TAX) si el manifiesto lo trae; si no,
    # se conserva el margen de la versión anterior (las diferencias y el TAX se
    # refacturan al costo). Nunca margen negativo.
    costo_informado = str(fila.get("costo_courier_ars") or "").strip()
    if costo_informado:
        costo_real = min(_dec(costo_informado), final)
        margen = final - costo_real
        if not previa:
            costo_estimado = min(costo_real, inicial)
    else:
        margen = min(margen, final)
        costo_real = final - margen
    ajuste_total = final - inicial
    if abs(ajuste_total - dif_total - tax_total) > TOLERANCIA:
        raise _Conflicto("La conciliación previa del portal no cierra; no se agrega el ajuste.")
    version = estado["version_max"] + 1
    instante = _instante(_fecha(fila["fecha"], "Fecha"))
    motivo = "IMPUESTOS" if dif_inc == 0 else ("OTRO" if tax_inc == 0 else "MIXTO")
    calculo = {
        "source_key": fila["source_key"], "manifest_sha256": manifest_sha,
        "version": version, "previo": str(previo), "diferencia": str(dif_inc),
        "tax": str(tax_inc), "final": str(final),
    }
    evidencia = [{
        "tipo": "PLANILLA_CLIENTE_2026",
        "manifest_sha256": manifest_sha,
        "fila_cliente": f"{fila['mes']}!{fila['fila_cliente']}",
        "tax_fuentes": fila.get("tax_fuentes") or [],
        "costo_informado": bool(costo_informado),
        "nota": "Complemento de la cuenta según la planilla facturada al cliente.",
    }]
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
            %s,%s,'CERRADA',%s,%s,%s,%s,%s,%s,%s,%s,%s,'NO_INFORMADO',%s,
            'HISTORICO_CLIENTE_V1',%s,%s,TRUE,%s,%s,%s,%s,NOW(),NOW()
        ) RETURNING id
        """,
        (
            solicitud_id, version, inicial, costo_estimado, margen, costo_real, final, ajuste_total,
            dif_total, tax_total, _peso(fila.get("peso_fuente")), motivo, _hash_json(calculo),
            Json(evidencia), actor, instante, actor, instante,
        ),
    )
    conciliacion_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO ajustes_cliente (
            conciliacion_id, solicitud_id, tipo, monto_ars,
            precio_anterior_ars, precio_nuevo_ars, estado, idempotency_key,
            motivo, propuesto_por, aprobado_por, aprobado_at,
            aplicado_por, aplicado_at, referencia_aplicacion, origen,
            created_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,'APLICADO',%s,%s,%s,%s,%s,%s,%s,%s,
                  'CONCILIACION_COURIER',NOW(),NOW())
        RETURNING id
        """,
        (
            conciliacion_id, solicitud_id, "DEBITO" if delta > 0 else "CREDITO", delta,
            previo, final, _hash(f"ajuste:{fila['source_key']}:v{version}:{manifest_sha}"),
            "Diferencia y/o TAX según la planilla del cliente", actor, actor, instante,
            actor, instante, fila["source_key"],
        ),
    )
    ajuste_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO auditoria_facturas_courier (
            evento, solicitud_id, conciliacion_id, ajuste_id, actor, metadata
        ) VALUES ('AJUSTE_HISTORICO_APLICADO', %s, %s, %s, %s, %s)
        """,
        (solicitud_id, conciliacion_id, ajuste_id, actor, Json({
            "manifest_sha256": manifest_sha, "source_key": fila["source_key"],
            "diferencia_ars": str(dif_inc), "tax_ars": str(tax_inc),
        })),
    )


def _sincronizar_envio(cur, cliente: str, fila: dict[str, Any], claves: dict[str, dict[str, Any]], *,
                       actor: str, manifest_sha: str) -> dict[str, Any]:
    """Devuelve el resultado de la fila. Las excepciones _Conflicto/_Repetido
    se convierten en filas informadas sin escrituras para esa fila."""
    existente, origen, clave = _buscar_solicitud(cur, cliente, fila, claves)
    if clave != fila["source_key"]:
        # Las escrituras nuevas usan la clave alterna para no chocar con el
        # envío que hoy ocupa esa posición.
        fila = {**fila, "source_key": clave}
    objetivo = _total_envio(fila)
    cancelado = fila["estado_portal"] == "CANCELADO"
    acciones: list[str] = []
    if existente is None:
        if not fila.get("crear_si_falta"):
            raise _Repetido("Tracking repetido en la planilla: no se crea sin decisión.")
        solicitud_id = _crear_solicitud(cur, cliente, fila)
        acciones.append("ENVIO_NUEVO")
    else:
        solicitud_id = int(existente["id"])
        estado_portal = _texto(existente.get("estado"), 30).upper()
        if cancelado != (estado_portal in {"CANCELADO", "REEMPLAZADO"}):
            raise _Conflicto(f"En el portal el envío está {estado_portal}.")
        if _normal(existente.get("dest_nombre"))[:12] != _normal(fila.get("destinatario"))[:12]:
            acciones.append("AVISO_DESTINATARIO_DISTINTO")
        acciones.append("ENVIO_" + origen)
    estado = _estado_solicitud(cur, solicitud_id)
    importe = _dec(fila["importe_inicial_ars"])
    if cancelado:
        if estado["cargo_ars"] + estado["ajustes_ars"] != 0:
            raise _Conflicto("Cancelado en la planilla pero con saldo en el portal.")
        if "ENVIO_NUEVO" not in acciones:
            acciones.append("SIN_CAMBIOS")
        return {"solicitud_id": solicitud_id, "acciones": acciones, "agregado_ars": Decimal("0.00")}
    agregado = Decimal("0.00")
    cargo = estado["cargo"]
    if cargo is not None and not estado["cargo_activo"]:
        raise _Conflicto(f"El cargo del portal está {cargo['estado']}.")
    if importe > 0:
        if cargo is None:
            _crear_cargo(cur, cliente, solicitud_id, fila, importe)
            acciones.append("CARGO_NUEVO")
            agregado += importe
        elif estado["cargo_ars"] == 0:
            _completar_cargo_cero(cur, cargo, solicitud_id, importe)
            acciones.append("CARGO_COMPLETADO")
            agregado += importe
    elif cargo is None and objetivo > 0:
        _crear_cargo(cur, cliente, solicitud_id, fila, Decimal("0.00"))
        acciones.append("CARGO_ANCLA")
    estado = _estado_solicitud(cur, solicitud_id)
    delta = objetivo - (estado["cargo_ars"] + estado["ajustes_ars"])
    if delta < -TOLERANCIA:
        raise _Conflicto(
            f"El portal cobra {estado['cargo_ars'] + estado['ajustes_ars']} y la planilla {objetivo}."
        )
    if delta > TOLERANCIA:
        _agregar_ajuste(cur, solicitud_id, fila, estado, delta, actor=actor, manifest_sha=manifest_sha)
        acciones.append("AJUSTE_AGREGADO")
        agregado += delta
    if not any(a in acciones for a in ("ENVIO_NUEVO", "CARGO_NUEVO", "CARGO_COMPLETADO",
                                       "CARGO_ANCLA", "AJUSTE_AGREGADO")):
        acciones.append("SIN_CAMBIOS")
    return {"solicitud_id": solicitud_id, "acciones": acciones, "agregado_ars": agregado}


def _sincronizar_saldo_2025(cur, cliente: str, saldo: dict[str, Any]) -> str:
    monto = _dec(saldo["monto_ars"])
    clave = _hash(f"cargo:{saldo['source_key']}")
    cur.execute("SELECT * FROM envios WHERE cliente_id=%s AND idempotency_key=%s", (cliente, clave))
    existente = cur.fetchone()
    if existente:
        if _dec(existente["monto_ars"]) != monto or existente["estado"] != "ACTIVO":
            raise _Conflicto("El saldo 2025 del portal no coincide con la planilla.")
        return "SALDO_2025_EXISTENTE"
    if monto == 0:
        return "SIN_SALDO_2025"
    fecha = _fecha(saldo["fecha"], "Saldo 2025")
    cur.execute(
        """INSERT INTO envios (cliente_id,fecha,monto_ars,estado,descripcion,ambito,idempotency_key,created_at)
           VALUES (%s,%s,%s,'ACTIVO','SALDO PENDIENTE 2025','INTERNACIONAL',%s,%s)""",
        (cliente, fecha, monto, clave, _instante(fecha)),
    )
    return "SALDO_2025_NUEVO"


def _sincronizar_pagos(cur, cliente: str, pagos: list[dict[str, Any]]) -> dict[str, Any]:
    """Los pagos de la planilla se identifican por su fila en DETALLE, que se
    puede correr si se insertan o borran filas. Por eso un pago del portal se
    reconoce por clave e importe, o por fecha e importe; y si queda algún pago
    del portal sin reconocer o algún conflicto, no se agrega ningún pago."""
    cur.execute(
        "SELECT id, fecha, monto_ars, estado, idempotency_key, referencia FROM pagos WHERE cliente_id=%s",
        (cliente,),
    )
    del_portal = [dict(r) for r in cur.fetchall()]
    por_clave = {p["idempotency_key"]: p for p in del_portal if p["idempotency_key"]}
    aprobados = [p for p in del_portal if p["estado"] == "APROBADO"]
    usados: set[int] = set()
    resultado = {"nuevos": [], "existentes": 0, "conflictos": [], "pagos_portal_fuera_de_planilla": []}
    faltantes: list[tuple[dict[str, Any], str]] = []
    for pago in pagos:
        clave = _hash(f"pago:{pago['source_key']}")
        monto = _dec(pago["monto_ars"])
        fecha = _fecha(pago["fecha"], pago["source_key"])
        propio = por_clave.get(clave)
        if propio and propio["id"] not in usados and _dec(propio["monto_ars"]) == monto:
            usados.add(propio["id"])
            if propio["estado"] != "APROBADO":
                resultado["conflictos"].append({"source_key": pago["source_key"], "monto_ars": pago["monto_ars"],
                                                "motivo": f"El pago del portal está {propio['estado']}."})
            else:
                resultado["existentes"] += 1
            continue
        igual = next((p for p in aprobados if p["id"] not in usados
                      and _dec(p["monto_ars"]) == monto and p["fecha"] == fecha), None)
        if igual:
            usados.add(igual["id"])
            resultado["existentes"] += 1
            continue
        # La clave puede pertenecer a otro pago (filas corridas): clave alterna.
        faltantes.append((pago, _hash(f"pago:{pago['source_key']}#{fecha}#{monto}") if propio else clave))
    ajenos = [p for p in aprobados if p["id"] not in usados]
    resultado["pagos_portal_fuera_de_planilla"] = [
        {"id": p["id"], "fecha": str(p["fecha"]), "monto_ars": str(_dec(p["monto_ars"]))} for p in ajenos
    ]
    if faltantes and (ajenos or resultado["conflictos"]):
        for pago, _ in faltantes:
            resultado["conflictos"].append({
                "source_key": pago["source_key"], "monto_ars": pago["monto_ars"],
                "motivo": "Hay pagos del portal que no coinciden con la planilla; revisar antes de agregar.",
            })
        return resultado
    for pago, clave in faltantes:
        fecha = _fecha(pago["fecha"], pago["source_key"])
        monto = _dec(pago["monto_ars"])
        nota = f"Importación cuenta 2026 · {pago['source_key']}"
        if not pago.get("fecha_original_informada"):
            nota += " · fecha original no informada en la planilla"
        cur.execute(
            """INSERT INTO pagos (cliente_id,fecha,monto_ars,metodo,referencia,nota,estado,
                                  idempotency_key,created_at,fecha_original_conocida)
               VALUES (%s,%s,%s,'Transferencia',%s,%s,'APROBADO',%s,%s,%s) RETURNING id""",
            (cliente, fecha, monto, _texto(pago.get("referencia"), 80), nota,
             clave, _instante(fecha), bool(pago.get("fecha_original_informada"))),
        )
        pago_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO pagos_aplicaciones (pago_id,ambito,monto_ars,estado) VALUES (%s,'INTERNACIONAL',%s,'APLICADA')",
            (pago_id, monto),
        )
        resultado["nuevos"].append({"source_key": pago["source_key"], "fecha": str(fecha), "monto_ars": str(monto)})
    return resultado


def _saldo_portal(cur, cliente: str) -> dict[str, str]:
    cur.execute(
        "SELECT COALESCE(SUM(monto_ars),0) AS t FROM envios WHERE cliente_id=%s AND estado='ACTIVO'",
        (cliente,),
    )
    cargos = _dec(cur.fetchone()["t"])
    cur.execute(
        """SELECT COALESCE(SUM(a.monto_ars),0) AS t FROM ajustes_cliente a
           JOIN solicitudes_guia s ON s.id=a.solicitud_id
           WHERE s.cliente_id=%s AND a.estado='APLICADO'""",
        (cliente,),
    )
    ajustes = _dec(cur.fetchone()["t"])
    cur.execute(
        "SELECT COALESCE(SUM(monto_ars),0) AS t FROM pagos WHERE cliente_id=%s AND estado='APROBADO'",
        (cliente,),
    )
    pagos = _dec(cur.fetchone()["t"])
    return {"cargos_ars": str(cargos), "ajustes_ars": str(ajustes), "pagos_ars": str(pagos),
            "saldo_ars": str(cargos + ajustes - pagos)}


# ── punto de entrada ───────────────────────────────────────────────────────

def procesar(manifiesto: dict[str, Any], *, aplicar: bool, actor: str = "admin") -> dict[str, Any]:
    """Sincroniza la cuenta. Con aplicar=False devuelve la vista previa sin escribir."""
    validar_manifiesto(manifiesto)
    cliente = _texto(manifiesto["cliente_id"], 40).upper()
    actor = _texto(actor, 120) or "admin"
    sha = manifiesto["manifest_sha256"]
    claves = {f["source_key"]: f for f in manifiesto["envios"]}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"CUENTA-2026:{cliente}",))
                cur.execute(
                    "SELECT cliente_id FROM clientes WHERE cliente_id=%s AND activo=TRUE FOR SHARE",
                    (cliente,),
                )
                if not cur.fetchone():
                    raise ImportacionCuentaError(f"El perfil {cliente} no existe o está inactivo.")
                antes = _saldo_portal(cur, cliente)
                acciones = Counter()
                por_mes: dict[str, Counter] = defaultdict(Counter)
                agregado_mes: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
                conflictos: list[dict[str, Any]] = []
                repetidos: list[dict[str, Any]] = []
                avisos: list[dict[str, Any]] = []
                procesadas: list[tuple[int, Decimal]] = []
                for fila in manifiesto["envios"]:
                    cur.execute("SAVEPOINT fila")
                    try:
                        r = _sincronizar_envio(cur, cliente, fila, claves, actor=actor,
                                               manifest_sha=sha)
                    except _Conflicto as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT fila")
                        conflictos.append({"source_key": fila["source_key"], "tracking": fila.get("tracking"),
                                           "destinatario": fila.get("destinatario"),
                                           "total_planilla_ars": str(_total_envio(fila)), "motivo": str(exc)})
                        acciones["CONFLICTO"] += 1
                        continue
                    except _Repetido as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT fila")
                        repetidos.append({"source_key": fila["source_key"], "tracking": fila.get("tracking"),
                                          "destinatario": fila.get("destinatario"),
                                          "total_planilla_ars": str(_total_envio(fila)), "motivo": str(exc)})
                        acciones["REPETIDO_NO_CARGADO"] += 1
                        continue
                    cur.execute("RELEASE SAVEPOINT fila")
                    for accion in r["acciones"]:
                        acciones[accion] += 1
                        por_mes[fila["mes"]][accion] += 1
                        if accion == "AVISO_DESTINATARIO_DISTINTO":
                            avisos.append({"source_key": fila["source_key"], "tracking": fila.get("tracking"),
                                           "aviso": "El destinatario del portal no coincide con la planilla."})
                    agregado_mes[fila["mes"]] += r["agregado_ars"]
                    procesadas.append((r["solicitud_id"], _total_envio(fila)))

                # Control: cada envío procesado quedó con el total de la planilla.
                ids = [i for i, _ in procesadas]
                cur.execute(
                    """
                    SELECT s.id,
                           COALESCE((SELECT monto_ars FROM envios e WHERE e.solicitud_id=s.id AND e.estado='ACTIVO'),0)
                         + COALESCE((SELECT SUM(monto_ars) FROM ajustes_cliente a WHERE a.solicitud_id=s.id AND a.estado='APLICADO'),0) AS total
                      FROM solicitudes_guia s WHERE s.id = ANY(%s)
                    """,
                    (ids,),
                )
                totales = {int(r["id"]): _dec(r["total"]) for r in cur.fetchall()}
                esperado: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
                for solicitud_id, total in procesadas:
                    esperado[solicitud_id] += total
                distintos = [i for i, t in esperado.items() if abs(totales.get(i, Decimal(0)) - t) > TOLERANCIA]
                if distintos:
                    raise ImportacionCuentaError(
                        f"Control posterior: {len(distintos)} envíos no quedaron con el total de la planilla."
                    )

                cur.execute("SAVEPOINT saldo")
                try:
                    estado_saldo = _sincronizar_saldo_2025(cur, cliente, manifiesto["saldo_pendiente_2025"])
                    cur.execute("RELEASE SAVEPOINT saldo")
                except _Conflicto as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT saldo")
                    estado_saldo = "CONFLICTO"
                    conflictos.append({"source_key": manifiesto["saldo_pendiente_2025"]["source_key"],
                                       "motivo": str(exc)})
                pagos = _sincronizar_pagos(cur, cliente, manifiesto["pagos"])
                despues = _saldo_portal(cur, cliente)
                resumen = manifiesto["resumen"]
                # Conciliación del resultado contra la planilla completa (incluye
                # las filas repetidas): lo único que puede faltar en el portal
                # son las filas informadas como repetidas o en conflicto.
                saldo_completo = _dec(resumen.get("saldo_resultante_ars")) + _dec(resumen.get("repetidos_sin_crear_ars"))
                no_cargado = sum((_dec(r["total_planilla_ars"]) for r in repetidos), Decimal("0.00"))
                no_cargado += sum((_dec(c.get("total_planilla_ars")) for c in conflictos), Decimal("0.00"))
                pagos_no_cargados = sum((_dec(c.get("monto_ars")) for c in pagos["conflictos"]), Decimal("0.00"))
                # Movimientos del portal que no vienen de la planilla (guías
                # emitidas por el portal, cargos manuales): se informan aparte.
                cur.execute(
                    """
                    SELECT e.id, e.fecha, e.monto_ars, e.descripcion, e.tracking, e.solicitud_id
                      FROM envios e
                     WHERE e.cliente_id=%s AND e.estado='ACTIVO'
                       AND (e.solicitud_id IS NULL OR NOT (e.solicitud_id = ANY(%s)))
                       AND COALESCE(e.idempotency_key,'') <> %s
                    """,
                    (cliente, ids, _hash(f"cargo:{manifiesto['saldo_pendiente_2025']['source_key']}")),
                )
                fuera = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    """
                    SELECT COALESCE(SUM(a.monto_ars),0) AS t FROM ajustes_cliente a
                      JOIN solicitudes_guia s ON s.id=a.solicitud_id
                     WHERE s.cliente_id=%s AND a.estado='APLICADO' AND NOT (s.id = ANY(%s))
                    """,
                    (cliente, ids),
                )
                ajustes_fuera = _dec(cur.fetchone()["t"])
                fuera_total = sum((_dec(f["monto_ars"]) for f in fuera), Decimal("0.00")) + ajustes_fuera
                pagos_fuera = sum((_dec(p["monto_ars"]) for p in pagos["pagos_portal_fuera_de_planilla"]), Decimal("0.00"))
                esperado_portal = saldo_completo - no_cargado + pagos_no_cargados + fuera_total - pagos_fuera
                informe = {
                    "cliente_id": cliente,
                    "manifest_sha256": sha,
                    "modo": "IMPORTACION" if aplicar else "VISTA_PREVIA",
                    "saldo_portal_antes": antes,
                    "saldo_portal_despues": despues,
                    "saldo_planilla_ars": resumen.get("saldo_resultante_ars"),
                    "saldo_segun_planilla_ars": resumen.get("saldo_segun_planilla_ars") or None,
                    "repetidos_planilla_ars": resumen.get("repetidos_sin_crear_ars"),
                    "saldo_planilla_completa_ars": str(saldo_completo),
                    "no_cargado_por_decision_ars": str(no_cargado),
                    "movimientos_portal_fuera_de_planilla_ars": str(fuera_total - pagos_fuera),
                    "movimientos_portal_fuera_de_planilla": [
                        {"envio_id": f["id"], "fecha": str(f["fecha"]), "monto_ars": str(_dec(f["monto_ars"])),
                         "descripcion": f["descripcion"], "tracking": f["tracking"]} for f in fuera[:50]
                    ],
                    "diferencia_no_explicada_ars": str(_dec(despues["saldo_ars"]) - esperado_portal),
                    "acciones": dict(acciones),
                    "agregado_por_mes_ars": {m: str(v) for m, v in agregado_mes.items() if v},
                    "acciones_por_mes": {m: dict(c) for m, c in por_mes.items()},
                    "saldo_2025": estado_saldo,
                    "pagos": pagos,
                    "conflictos": conflictos,
                    "repetidos_no_cargados": repetidos,
                    "avisos": avisos,
                }
                registrar_evento_con_cursor(
                    cur, event="cuenta.importacion_cuenta_cliente_2026",
                    actor_type="admin", actor_ref=actor, ip=None, method="POST",
                    path="/admin/importaciones-historicas/cuenta-cliente", status_code=200,
                    success=True, request_id=None,
                    metadata={k: informe[k] for k in (
                        "cliente_id", "manifest_sha256", "modo", "saldo_portal_antes",
                        "saldo_portal_despues", "acciones", "saldo_2025",
                    )} | {"conflictos": len(conflictos), "repetidos": len(repetidos),
                          "pagos_nuevos": len(pagos["nuevos"])},
                )
                if not aplicar:
                    raise _VistaPrevia(informe)
                return informe
    except _VistaPrevia as vista:
        return vista.informe


__all__ = [
    "MAX_MANIFEST_BYTES", "ImportacionCuentaError", "leer_manifiesto",
    "validar_manifiesto", "manifest_hash", "procesar",
]
