"""Persistencia, cola y aprobaciones del CRM comercial de TAURO."""
from __future__ import annotations

import hashlib
from typing import Any

from psycopg2.extras import Json

from core.database import get_conn
from servicios.agentes_comerciales import (
    ICP_TAURO,
    AgentesComercialesOpenAI,
    normalizar_dominio,
    normalizar_email,
    puntuar_investigacion,
    validar_envio,
    validar_transicion_mensaje,
)

TIPOS_TRABAJO = {"DESCUBRIR", "INVESTIGAR", "PROPUESTA"}


def _evento(
    event: str,
    *,
    entity_type: str,
    entity_id: int | None,
    actor: str = "system",
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO crm_eventos (event, entity_type, entity_id, actor, metadata)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (event[:80], entity_type[:30], entity_id, actor[:80], Json(metadata or {})),
                )
    except Exception as exc:
        print(f"[crm] no se pudo registrar {event}: {type(exc).__name__}")


def crear_cuenta(
    *,
    empresa: str,
    dominio: str | None,
    pais: str = "AR",
    segmento: str = "OTRO",
    contacto_nombre: str | None = None,
    contacto_cargo: str | None = None,
    contacto_email: str | None = None,
    email_verificado: bool = False,
    fuente: str = "MANUAL",
) -> int:
    empresa = (empresa or "").strip()
    if len(empresa) < 2:
        raise ValueError("La empresa es obligatoria.")
    dominio = normalizar_dominio(dominio)
    email = normalizar_email(contacto_email)
    if not dominio and not email:
        raise ValueError("Carga al menos un dominio o un email comercial.")
    pais = (pais or "AR").strip().upper()[:3]
    segmento = (segmento or "OTRO").strip().upper()[:50]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crm_cuentas
                    (empresa, dominio, sitio_web, pais, segmento, estado, fuente)
                VALUES (%s, %s, %s, %s, %s, 'NUEVO', %s)
                RETURNING id
                """,
                (empresa, dominio, f"https://{dominio}" if dominio else None, pais, segmento, fuente[:40]),
            )
            cuenta_id = int(cur.fetchone()["id"])
            if email:
                cur.execute(
                    """
                    INSERT INTO crm_contactos
                        (cuenta_id, nombre, cargo, email, estado_email, es_principal)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    """,
                    (
                        cuenta_id,
                        (contacto_nombre or empresa).strip()[:160],
                        (contacto_cargo or "Contacto comercial").strip()[:120],
                        email,
                        "VERIFICADO" if email_verificado else "NO_VERIFICADO",
                    ),
                )
    _evento("cuenta.creada", entity_type="cuenta", entity_id=cuenta_id, actor="admin", metadata={"fuente": fuente})
    return cuenta_id


def _insertar_candidatos(candidatos: list[dict[str, Any]], job_id: int) -> dict[str, int]:
    creados = 0
    duplicados = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for candidato in candidatos:
                dominio = candidato.get("dominio")
                if not dominio:
                    continue
                cur.execute(
                    """
                    INSERT INTO crm_cuentas
                        (empresa, dominio, sitio_web, pais, segmento, estado, fuente,
                         discovery_payload, discovery_job_id)
                    VALUES (%s, %s, %s, %s, %s, 'NUEVO', 'AGENTE_MERCADO', %s, %s)
                    ON CONFLICT (dominio) DO NOTHING
                    RETURNING id
                    """,
                    (
                        str(candidato.get("empresa") or dominio)[:200],
                        dominio,
                        candidato.get("sitio_web") or f"https://{dominio}",
                        str(candidato.get("pais") or "")[:3].upper(),
                        str(candidato.get("segmento") or "OTRO")[:50].upper(),
                        Json(candidato),
                        job_id,
                    ),
                )
                row = cur.fetchone()
                if row:
                    creados += 1
                    cuenta_id = int(row["id"])
                    for source in candidato.get("fuentes") or []:
                        cur.execute(
                            """
                            INSERT INTO crm_fuentes (cuenta_id, url, titulo, evidencia, tipo)
                            VALUES (%s, %s, %s, %s, 'DESCUBRIMIENTO')
                            ON CONFLICT (cuenta_id, url) DO NOTHING
                            """,
                            (
                                cuenta_id,
                                str(source.get("url") or "")[:1500],
                                str(source.get("titulo") or "")[:300],
                                str(source.get("evidencia") or "")[:2000],
                            ),
                        )
                else:
                    duplicados += 1
    return {"creados": creados, "duplicados": duplicados}


def encolar_trabajo(
    tipo: str,
    *,
    cuenta_id: int | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "admin",
) -> int:
    tipo = (tipo or "").upper()
    if tipo not in TIPOS_TRABAJO:
        raise ValueError("Tipo de trabajo comercial invalido.")
    if tipo in {"INVESTIGAR", "PROPUESTA"} and not cuenta_id:
        raise ValueError("El trabajo requiere una cuenta.")
    payload = payload or {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            if cuenta_id:
                cur.execute(
                    """
                    SELECT id FROM crm_trabajos_agente
                    WHERE tipo=%s AND cuenta_id=%s AND estado IN ('PENDIENTE', 'EJECUTANDO')
                    LIMIT 1
                    """,
                    (tipo, cuenta_id),
                )
                existente = cur.fetchone()
                if existente:
                    return int(existente["id"])
            cur.execute(
                """
                INSERT INTO crm_trabajos_agente (tipo, cuenta_id, payload, estado, creado_por)
                VALUES (%s, %s, %s, 'PENDIENTE', %s)
                RETURNING id
                """,
                (tipo, cuenta_id, Json(payload), actor[:80]),
            )
            job_id = int(cur.fetchone()["id"])
    _evento("agente.encolado", entity_type="trabajo", entity_id=job_id, actor=actor, metadata={"tipo": tipo})
    return job_id


def _obtener_cuenta(cuenta_id: int) -> dict[str, Any]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM crm_cuentas WHERE id=%s", (cuenta_id,))
        row = cur.fetchone()
    if not row:
        raise ValueError("Cuenta comercial inexistente.")
    return dict(row)


def _obtener_contacto(cuenta_id: int) -> dict[str, Any]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT * FROM crm_contactos
                WHERE cuenta_id=%s AND excluido=FALSE AND estado_email <> 'BAJA'
                ORDER BY (estado_email='VERIFICADO') DESC, es_principal DESC, id
                LIMIT 1
                """,
            (cuenta_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError("La cuenta no tiene un contacto comercial.")
    return dict(row)


def _terminar_job(job_id: int, *, estado: str, resultado: dict[str, Any] | None = None, error: str | None = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crm_trabajos_agente
                SET estado=%s, resultado=%s, error=%s, finalizado_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (estado, Json(resultado or {}), (error or "")[:2000] or None, job_id),
            )


def _procesar_descubrimiento(job: dict[str, Any], agentes: AgentesComercialesOpenAI) -> dict[str, Any]:
    payload = job.get("payload") or {}
    brief = str(payload.get("brief") or "").strip()
    if len(brief) < 10:
        raise ValueError("El brief de mercado es demasiado corto.")
    candidatos, meta = agentes.descubrir(brief, int(payload.get("limite") or 10))
    importados = _insertar_candidatos(candidatos, int(job["id"]))
    return {**importados, **meta}


def _procesar_investigacion(job: dict[str, Any], agentes: AgentesComercialesOpenAI) -> dict[str, Any]:
    cuenta_id = int(job["cuenta_id"])
    cuenta = _obtener_cuenta(cuenta_id)
    investigacion, meta = agentes.investigar({
        "empresa": cuenta["empresa"],
        "dominio": cuenta.get("dominio"),
        "sitio_web": cuenta.get("sitio_web"),
        "pais": cuenta.get("pais"),
        "segmento_preliminar": cuenta.get("segmento"),
    })
    score, breakdown, decision = puntuar_investigacion(investigacion)
    estado = "CALIFICADO" if decision == "CALIFICADO" else "INVESTIGADO"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crm_cuentas
                SET pais=%s, segmento=%s, estado=%s, score=%s, score_breakdown=%s,
                    research_summary=%s, research_payload=%s, research_model=%s,
                    research_response_id=%s, investigado_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (
                    str(investigacion.get("pais") or cuenta.get("pais") or "")[:3].upper(),
                    str(investigacion.get("segmento") or cuenta.get("segmento") or "OTRO")[:50],
                    estado,
                    score,
                    Json(breakdown),
                    str(investigacion.get("resumen") or "")[:4000],
                    Json(investigacion),
                    meta.get("model"),
                    meta.get("response_id"),
                    cuenta_id,
                ),
            )
            for source in investigacion.get("fuentes") or []:
                cur.execute(
                    """
                    INSERT INTO crm_fuentes (cuenta_id, url, titulo, evidencia, tipo)
                    VALUES (%s, %s, %s, %s, 'INVESTIGACION')
                    ON CONFLICT (cuenta_id, url) DO UPDATE
                    SET titulo=EXCLUDED.titulo, evidencia=EXCLUDED.evidencia, verificado_at=NOW()
                    """,
                    (
                        cuenta_id,
                        str(source.get("url") or "")[:1500],
                        str(source.get("titulo") or "")[:300],
                        str(source.get("evidencia") or "")[:2000],
                    ),
                )
            email_publico = investigacion.get("email_comercial_publico")
            if email_publico:
                cur.execute(
                    """
                    INSERT INTO crm_contactos
                        (cuenta_id, nombre, cargo, email, estado_email, fuente_url, es_principal)
                    VALUES (%s, %s, 'Contacto comercial', %s, 'NO_VERIFICADO', %s, TRUE)
                    ON CONFLICT (email) DO NOTHING
                    """,
                    (
                        cuenta_id,
                        cuenta["empresa"],
                        email_publico,
                        investigacion.get("email_fuente_url"),
                    ),
                )
    _evento(
        "cuenta.investigada",
        entity_type="cuenta",
        entity_id=cuenta_id,
        metadata={"score": score, "decision": decision, "modelo": meta.get("model")},
    )
    return {"score": score, "decision": decision, **meta}


def _procesar_propuesta(job: dict[str, Any], agentes: AgentesComercialesOpenAI) -> dict[str, Any]:
    cuenta_id = int(job["cuenta_id"])
    cuenta = _obtener_cuenta(cuenta_id)
    contacto = _obtener_contacto(cuenta_id)
    if cuenta.get("excluida") or contacto.get("excluido"):
        raise ValueError("La cuenta o el contacto esta excluido.")
    if not cuenta.get("research_payload"):
        raise ValueError("Primero hay que investigar la cuenta.")

    contexto = {
        "empresa": cuenta["empresa"],
        "pais": cuenta.get("pais"),
        "segmento": cuenta.get("segmento"),
        "score": cuenta.get("score"),
        "resumen": cuenta.get("research_summary"),
        "investigacion": cuenta.get("research_payload"),
    }
    contacto_minimo = {
        "nombre": contacto.get("nombre"),
        "cargo": contacto.get("cargo"),
        "email": contacto.get("email"),
    }
    borrador, meta_draft = agentes.redactar(contexto, contacto_minimo)
    revision, meta_review = agentes.revisar(contexto, contacto_minimo, borrador)
    estado = "BORRADOR" if revision.get("aprobado") else "OBSERVADO"
    asunto = str(revision.get("asunto_corregido") or borrador.get("asunto") or "")[:240]
    cuerpo = str(revision.get("cuerpo_corregido") or borrador.get("cuerpo_texto") or "")[:12000]
    checksum = hashlib.sha256(f"{asunto}\n{cuerpo}".encode()).hexdigest()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crm_mensajes
                    (cuenta_id, contacto_id, tipo, asunto, cuerpo_texto, estado,
                     draft_payload, review_payload, draft_model, review_model,
                     draft_response_id, review_response_id, checksum)
                VALUES (%s, %s, 'PRIMER_CONTACTO', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    cuenta_id,
                    contacto["id"],
                    asunto,
                    cuerpo,
                    estado,
                    Json(borrador),
                    Json(revision),
                    meta_draft.get("model"),
                    meta_review.get("model"),
                    meta_draft.get("response_id"),
                    meta_review.get("response_id"),
                    checksum,
                ),
            )
            mensaje_id = int(cur.fetchone()["id"])
    _evento(
        "mensaje.redactado",
        entity_type="mensaje",
        entity_id=mensaje_id,
        metadata={"estado": estado, "modelo_redactor": meta_draft.get("model"), "modelo_revisor": meta_review.get("model")},
    )
    return {"mensaje_id": mensaje_id, "estado": estado, "model": meta_review.get("model")}


def procesar_trabajo(job_id: int, agentes: AgentesComercialesOpenAI | None = None) -> dict[str, Any]:
    agentes = agentes or AgentesComercialesOpenAI()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crm_trabajos_agente
                SET estado='EJECUTANDO', iniciado_at=NOW(), updated_at=NOW(), intentos=intentos+1
                WHERE id=%s AND estado='PENDIENTE'
                RETURNING *
                """,
                (job_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"ok": False, "motivo": "no_disponible"}
    job = dict(row)
    try:
        if job["tipo"] == "DESCUBRIR":
            resultado = _procesar_descubrimiento(job, agentes)
        elif job["tipo"] == "INVESTIGAR":
            resultado = _procesar_investigacion(job, agentes)
        elif job["tipo"] == "PROPUESTA":
            resultado = _procesar_propuesta(job, agentes)
        else:
            raise ValueError(f"Tipo de trabajo desconocido: {job['tipo']}")
        _terminar_job(job_id, estado="COMPLETADO", resultado=resultado)
        _evento("agente.completado", entity_type="trabajo", entity_id=job_id, metadata={"tipo": job["tipo"]})
        return {"ok": True, **resultado}
    except Exception as exc:
        _terminar_job(job_id, estado="FALLIDO", error=f"{type(exc).__name__}: {exc}")
        _evento(
            "agente.fallido",
            entity_type="trabajo",
            entity_id=job_id,
            metadata={"tipo": job["tipo"], "error_type": type(exc).__name__},
        )
        print(f"[crm] trabajo #{job_id} fallo: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}


def procesar_pendientes(limite: int = 2) -> list[dict[str, Any]]:
    limite = max(1, min(int(limite), 10))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT id FROM crm_trabajos_agente
                WHERE estado='PENDIENTE'
                ORDER BY created_at
                LIMIT %s
                """,
            (limite,),
        )
        ids = [int(row["id"]) for row in cur.fetchall()]
    return [procesar_trabajo(job_id) for job_id in ids]


def verificar_contacto(contacto_id: int, actor: str = "admin") -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE crm_contactos
                SET estado_email='VERIFICADO', updated_at=NOW()
                WHERE id=%s AND estado_email='NO_VERIFICADO' AND excluido=FALSE
                RETURNING id
                """,
            (contacto_id,),
        )
        if not cur.fetchone():
            raise ValueError("El contacto no puede verificarse en su estado actual.")
    _evento("contacto.verificado", entity_type="contacto", entity_id=contacto_id, actor=actor)


def aprobar_mensaje(mensaje_id: int, actor: str = "admin") -> None:
    validar_transicion_mensaje("BORRADOR", "APROBADO")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crm_mensajes
                SET estado='APROBADO', aprobado_por=%s, aprobado_at=NOW(), updated_at=NOW()
                WHERE id=%s AND estado='BORRADOR'
                RETURNING id
                """,
                (actor[:80], mensaje_id),
            )
            if not cur.fetchone():
                raise ValueError("Solo se puede aprobar un BORRADOR.")
    _evento("mensaje.aprobado", entity_type="mensaje", entity_id=mensaje_id, actor=actor)


def cancelar_mensaje(mensaje_id: int, actor: str = "admin") -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE crm_mensajes
                SET estado='CANCELADO', updated_at=NOW()
                WHERE id=%s AND estado IN ('BORRADOR', 'OBSERVADO', 'APROBADO')
                RETURNING id
                """,
            (mensaje_id,),
        )
        if not cur.fetchone():
            raise ValueError("El mensaje ya no puede cancelarse.")
    _evento("mensaje.cancelado", entity_type="mensaje", entity_id=mensaje_id, actor=actor)


def enviar_mensaje(mensaje_id: int, actor: str = "admin") -> bool:
    """Reserva el mensaje antes del SMTP para impedir dobles envios concurrentes."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.*, c.email, c.estado_email, c.excluido AS contacto_excluido,
                       a.excluida AS cuenta_excluida
                FROM crm_mensajes m
                JOIN crm_contactos c ON c.id=m.contacto_id
                JOIN crm_cuentas a ON a.id=m.cuenta_id
                WHERE m.id=%s
                """,
                (mensaje_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Mensaje inexistente.")
            mensaje = dict(row)
            validar_envio(
                estado_mensaje=mensaje["estado"],
                estado_email=mensaje["estado_email"],
                cuenta_excluida=bool(mensaje["cuenta_excluida"]),
                contacto_excluido=bool(mensaje["contacto_excluido"]),
                email=mensaje["email"],
            )
            cur.execute(
                """
                UPDATE crm_mensajes
                SET estado='ENVIANDO', envio_intentos=envio_intentos+1, updated_at=NOW(), ultimo_error=NULL
                WHERE id=%s AND estado='APROBADO'
                RETURNING id
                """,
                (mensaje_id,),
            )
            if not cur.fetchone():
                raise ValueError("El mensaje ya fue tomado para envio.")

    from core.email_sender import enviar_email_comercial
    enviado = enviar_email_comercial(mensaje["email"], mensaje["asunto"], mensaje["cuerpo_texto"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            if enviado:
                cur.execute(
                    """
                    UPDATE crm_mensajes
                    SET estado='ENVIADO', enviado_at=NOW(), enviado_por=%s, updated_at=NOW()
                    WHERE id=%s AND estado='ENVIANDO'
                    """,
                    (actor[:80], mensaje_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE crm_mensajes
                    SET estado='APROBADO', ultimo_error='SMTP no confirmo el envio', updated_at=NOW()
                    WHERE id=%s AND estado='ENVIANDO'
                    """,
                    (mensaje_id,),
                )
    _evento(
        "mensaje.enviado" if enviado else "mensaje.envio_fallido",
        entity_type="mensaje",
        entity_id=mensaje_id,
        actor=actor,
        metadata={"checksum": mensaje.get("checksum")},
    )
    return enviado


def obtener_dashboard(limite: int = 100) -> dict[str, Any]:
    limite = max(10, min(int(limite), 300))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM crm_cuentas) AS cuentas,
                    (SELECT COUNT(*) FROM crm_cuentas WHERE estado='CALIFICADO') AS calificadas,
                    (SELECT COUNT(*) FROM crm_mensajes WHERE estado='BORRADOR') AS por_aprobar,
                    (SELECT COUNT(*) FROM crm_mensajes WHERE estado='ENVIADO') AS enviados,
                    (SELECT COUNT(*) FROM crm_trabajos_agente WHERE estado='PENDIENTE') AS trabajos_pendientes
                """
            )
            stats = dict(cur.fetchone())
            cur.execute(
                """
                SELECT a.*,
                       c.id AS contacto_id, c.nombre AS contacto_nombre, c.cargo AS contacto_cargo,
                       c.email AS contacto_email, c.estado_email,
                       (SELECT COUNT(*) FROM crm_fuentes f WHERE f.cuenta_id=a.id) AS fuentes_count
                FROM crm_cuentas a
                LEFT JOIN LATERAL (
                    SELECT * FROM crm_contactos c0
                    WHERE c0.cuenta_id=a.id
                    ORDER BY c0.es_principal DESC, (c0.estado_email='VERIFICADO') DESC, c0.id
                    LIMIT 1
                ) c ON TRUE
                ORDER BY a.score DESC, a.created_at DESC
                LIMIT %s
                """,
                (limite,),
            )
            cuentas = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT m.*, a.empresa, c.email, c.estado_email
                FROM crm_mensajes m
                JOIN crm_cuentas a ON a.id=m.cuenta_id
                JOIN crm_contactos c ON c.id=m.contacto_id
                ORDER BY m.created_at DESC
                LIMIT 80
                """
            )
            mensajes = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT j.*, a.empresa
                FROM crm_trabajos_agente j
                LEFT JOIN crm_cuentas a ON a.id=j.cuenta_id
                ORDER BY j.created_at DESC
                LIMIT 40
                """
            )
            trabajos = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT COUNT(*) AS total FROM leads_cotizacion lc
                WHERE NOT EXISTS (
                    SELECT 1 FROM crm_contactos c WHERE LOWER(c.email)=LOWER(lc.email)
                )
                """
            )
            leads_web_pendientes = int(cur.fetchone()["total"])
    return {
        "stats": stats,
        "cuentas": cuentas,
        "mensajes": mensajes,
        "trabajos": trabajos,
        "leads_web_pendientes": leads_web_pendientes,
        "icp": ICP_TAURO,
        "ia_configurada": AgentesComercialesOpenAI().configurado,
    }


def estado_configuracion() -> dict[str, Any]:
    return {
        "ia_configurada": AgentesComercialesOpenAI().configurado,
        "icp": ICP_TAURO,
        "agentes_habilitados": False,
    }
