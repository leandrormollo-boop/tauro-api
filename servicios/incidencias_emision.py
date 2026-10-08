"""Intentos de emisión consultables por el Admin, sin guardar el formulario."""
from __future__ import annotations

import json
import re
import traceback
import uuid

from core.database import get_conn
from servicios.auditoria import registrar_desde_request
from servicios.estados_envio import presentar_estados_envio


def _mensaje_registro(valor) -> str:
    texto = str(valor or "No se pudo completar la emisión.")
    # Los adaptadores pueden incluir un correo, una URL o una cabecera en
    # su respuesta. No conservamos esos datos en el historial de errores.
    texto = re.sub(r"https?://\S+", "[enlace omitido]", texto)
    texto = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[correo omitido]", texto)
    texto = re.sub(r"(?is)\b(authorization|bearer|api[_-]?key|password|token|secret)\b.*",
                   r"\1 [omitido]", texto)
    return texto[:800]


def ejecutar_emision_registrada(request, solicitud_id, actor_type, actor_ref, emitir):
    """Un resultado incierto nunca provoca un reintento ni libera la reserva."""
    referencia = "EMI-" + uuid.uuid4().hex[:12].upper()
    try:
        resultado = emitir()
    except Exception as exc:
        traza = traceback.extract_tb(exc.__traceback__)
        origen = traza[-1] if traza else None
        print(json.dumps({"event": "emision.excepcion", "referencia": referencia,
                          "solicitud_id": solicitud_id, "error_tipo": type(exc).__name__,
                          "funcion": origen.name if origen else None,
                          "linea": origen.lineno if origen else None}))
        resultado = {"ok": False, "codigo_error": "RESULTADO_NO_CONFIRMADO",
                     "error_tipo": type(exc).__name__, "etapa": "emision",
                     "error": "No pudimos confirmar el resultado de la emisión. "
                              "Pedí a Tauro que revise este envío antes de volver a emitir."}
    ok = bool(resultado.get("ok"))
    metadata = {
        "solicitud_id": int(solicitud_id), "referencia": referencia,
        "codigo": str(resultado.get("codigo_error") or (
            "GUIA_EMITIDA" if ok else "EMISION_NO_COMPLETADA"))[:80],
        "etapa": str(resultado.get("etapa") or "emision")[:80],
        "error_tipo": str(resultado.get("error_tipo") or "")[:80],
        "motivo": "Guía emitida." if ok else _mensaje_registro(resultado.get("error")),
    }
    # El log del proceso conserva la referencia aunque la DB esté caída.
    print(json.dumps({"event": "emision.resultado", "success": ok,
                      **{k: v for k, v in metadata.items() if k != "motivo"}}))
    registrar_desde_request(
        request, event=f"{'portal' if actor_type == 'cliente' else 'admin'}.emitir_guia", actor_type=actor_type,
        actor_ref=actor_ref, success=ok, status_code=200 if ok else 409,
        metadata=metadata,
    )
    if not ok:
        resultado = {**resultado, "referencia_error": referencia,
                     "error": f"{resultado.get('error') or 'No se pudo emitir.'} Referencia: {referencia}."}
    return resultado


def listar_intentos(*, solicitud_id: int = 0, antes: int = 0, resultado: str = "fallidos"):
    filtros = ["a.event IN ('portal.emitir_guia', 'cliente.emitir_guia', 'admin.emitir_guia')",
               "a.metadata ? 'referencia'"]
    params = []
    if solicitud_id > 0:
        filtros.append("a.metadata->>'solicitud_id' = %s")
        params.append(str(solicitud_id))
    if antes > 0:
        filtros.append("a.id < %s")
        params.append(antes)
    if resultado in {"fallidos", "exitosos"}:
        filtros.append("a.success = %s")
        params.append(resultado == "exitosos")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT a.id, a.created_at AT TIME ZONE 'America/Argentina/Buenos_Aires' AS fecha,
                   a.actor_type, a.success, a.metadata,
                   s.id AS solicitud_id, s.cliente_id, s.courier, s.estado, s.tracking
            FROM security_audit a
            LEFT JOIN solicitudes_guia s ON s.id::text = a.metadata->>'solicitud_id'
            WHERE {' AND '.join(filtros)}
            ORDER BY a.id DESC LIMIT 51
        """, params)
        filas = [presentar_estados_envio(dict(f)) for f in cur.fetchall()]
    return {"items": filas[:50], "siguiente": filas[49]["id"] if len(filas) > 50 else 0}
