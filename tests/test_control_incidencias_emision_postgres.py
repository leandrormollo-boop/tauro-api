"""Controles de incidencias de emisión sobre un schema PostgreSQL descartable."""

from __future__ import annotations

import sys
import types
import uuid
from decimal import Decimal

import pytest

from servicios import control_incidencias_emision as control
from test_conciliacion_couriers_postgres import conciliacion_db  # noqa: F401


@pytest.fixture
def incidencias_db(conciliacion_db, monkeypatch):
    monkeypatch.setattr(control, "get_conn", conciliacion_db)
    return conciliacion_db


def _crear_solicitud(db, *, cliente: str, precio: str = "10000") -> int:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clientes (cliente_id, email, nombre)
            VALUES (%s, %s, %s)
            ON CONFLICT (cliente_id) DO NOTHING
            """,
            (cliente, f"{cliente.lower()}@example.invalid", cliente),
        )
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, producto_alias, destino_pais, dest_nombre,
                dest_direccion, dest_ciudad, dest_zip, courier,
                coti_id, precio_tauro_ars, estado
            ) VALUES (
                %s, 'Producto', 'US', 'Destinatario', 'Calle 1',
                'Miami', '33101', 'DHL', %s, %s, 'SOLICITADO'
            ) RETURNING id
            """,
            (cliente, f"COTI-{uuid.uuid4().hex[:10]}", Decimal(precio)),
        )
        return int(cur.fetchone()["id"])


def _iniciar(solicitud_id: int, *, actor_type: str = "admin", actor_ref: str = "admin") -> int:
    intento_id = control.iniciar_intento(
        solicitud_id,
        f"EMI-{uuid.uuid4().hex[:16]}",
        actor_type,
        actor_ref,
    )
    assert intento_id is not None
    return int(intento_id)


def _finalizar_fallido(intento_id: int, codigo: str = "DATOS_ENVIO_INVALIDOS") -> None:
    control.finalizar_intento(
        intento_id,
        {"ok": False, "codigo_error": codigo, "error": "detalle no persistido"},
        {
            "codigo": codigo,
            "etapa": "emision",
            "error_tipo": "ErrorPrueba",
            "motivo": "Motivo seguro",
            "referencia": f"INT-{intento_id}",
        },
    )


def _incidencia(db, solicitud_id: int, codigo: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM emision_incidencias
               WHERE solicitud_id=%s AND codigo=%s""",
            (solicitud_id, codigo),
        )
        return cur.fetchone()


def _hacer_emitida_coherente(db, solicitud_id: int, *, cargo_pendiente: bool = False):
    tracking = f"DHL-{solicitud_id}"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE solicitudes_guia
               SET estado='GUIA_LISTA', tracking=%s, label_pdf=%s,
                   cargo_pendiente=%s, guia_generada_at=NOW(),
                   courier_message_reference=%s
             WHERE id=%s
            RETURNING cliente_id, precio_tauro_ars
            """,
            (
                tracking, b"%PDF-1.4\nprueba", cargo_pendiente,
                f"TEST-{solicitud_id}", solicitud_id,
            ),
        )
        solicitud = cur.fetchone()
        cur.execute(
            """
            INSERT INTO envios (
                cliente_id, fecha, monto_ars, estado, descripcion,
                tracking, solicitud_id, ambito
            ) VALUES (
                %s, CURRENT_DATE, %s, 'ACTIVO', 'Envío de prueba',
                %s, %s, 'INTERNACIONAL'
            )
            """,
            (
                solicitud["cliente_id"], solicitud["precio_tauro_ars"],
                tracking, solicitud_id,
            ),
        )


def test_iniciar_intento_es_durable_antes_de_finalizar(incidencias_db):
    solicitud_id = _crear_solicitud(incidencias_db, cliente="CLIENTE_DURABLE")
    referencia = f"EMI-{uuid.uuid4().hex}"

    intento_id = control.iniciar_intento(
        solicitud_id, referencia, "admin", "admin"
    )

    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT referencia, solicitud_id, actor_type, estado, finalizado_en
                 FROM emision_intentos WHERE id=%s""",
            (intento_id,),
        )
        assert cur.fetchone() == {
            "referencia": referencia,
            "solicitud_id": solicitud_id,
            "actor_type": "admin",
            "estado": "INICIADO",
            "finalizado_en": None,
        }


def test_finalizar_dos_veces_es_idempotente(incidencias_db):
    solicitud_id = _crear_solicitud(incidencias_db, cliente="CLIENTE_IDEM")
    intento_id = _iniciar(solicitud_id)

    _finalizar_fallido(intento_id)
    _finalizar_fallido(intento_id)

    incidencia = _incidencia(incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS")
    assert incidencia["cantidad"] == 1
    assert incidencia["ultimo_intento_id"] == intento_id
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT estado, metadata FROM emision_intentos WHERE id=%s",
            (intento_id,),
        )
        intento = cur.fetchone()
    assert intento["estado"] == "FALLIDO"
    assert intento["metadata"]["motivo"] == "Motivo seguro"


def test_agrupa_fallas_y_respeta_la_separacion_entre_clientes(incidencias_db):
    solicitud_a = _crear_solicitud(incidencias_db, cliente="CLIENTE_A")
    solicitud_b = _crear_solicitud(incidencias_db, cliente="CLIENTE_B")

    assert control.iniciar_intento(
        solicitud_a, f"EMI-{uuid.uuid4().hex}", "cliente", "CLIENTE_B"
    ) is None
    assert _iniciar(
        solicitud_a, actor_type="cliente", actor_ref="CLIENTE_A"
    )
    _finalizar_fallido(_iniciar(solicitud_a))
    _finalizar_fallido(_iniciar(solicitud_a))
    _finalizar_fallido(_iniciar(solicitud_b))

    incidencia_a = _incidencia(
        incidencias_db, solicitud_a, "DATOS_ENVIO_INVALIDOS"
    )
    incidencia_b = _incidencia(
        incidencias_db, solicitud_b, "DATOS_ENVIO_INVALIDOS"
    )
    assert incidencia_a["cantidad"] == 2
    assert incidencia_b["cantidad"] == 1
    assert incidencia_a["id"] != incidencia_b["id"]


def test_falla_demora_no_reabre_estado_coherente_resuelto(incidencias_db):
    solicitud_id = _crear_solicitud(incidencias_db, cliente="CLIENTE_DEMORA")
    intento_demorado = _iniciar(solicitud_id)
    intento_fallido = _iniciar(solicitud_id)
    _finalizar_fallido(intento_fallido)
    _hacer_emitida_coherente(incidencias_db, solicitud_id)
    intento_exitoso = _iniciar(solicitud_id)
    control.finalizar_intento(intento_exitoso, {"ok": True}, {})

    _finalizar_fallido(intento_demorado)

    incidencia = _incidencia(incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS")
    assert incidencia["estado"] == "RESUELTO"
    assert incidencia["ultimo_intento_id"] == intento_fallido
    assert incidencia["cantidad"] == 1
    assert incidencia["resuelta_en"] is not None


def test_exito_solo_cierra_con_tracking_pdf_cargo_activo_y_sin_pendiente(
    incidencias_db,
):
    solicitud_id = _crear_solicitud(incidencias_db, cliente="CLIENTE_COHERENCIA")
    _finalizar_fallido(_iniciar(solicitud_id))

    control.finalizar_intento(_iniciar(solicitud_id), {"ok": True}, {})
    assert _incidencia(
        incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS"
    )["estado"] == "PENDIENTE"

    tracking = f"DHL-{solicitud_id}"
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE solicitudes_guia
                  SET estado='GUIA_LISTA',tracking=%s,label_pdf=%s,
                      guia_generada_at=NOW(),courier_message_reference=%s
                WHERE id=%s""",
            (
                tracking, b"%PDF-1.4\nprueba", f"TEST-{solicitud_id}",
                solicitud_id,
            ),
        )
    control.finalizar_intento(_iniciar(solicitud_id), {"ok": True}, {})
    assert _incidencia(
        incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS"
    )["estado"] == "PENDIENTE"

    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cliente_id,precio_tauro_ars FROM solicitudes_guia WHERE id=%s",
            (solicitud_id,),
        )
        solicitud = cur.fetchone()
        cur.execute(
            """INSERT INTO envios (
                    cliente_id,fecha,monto_ars,estado,descripcion,tracking,
                    solicitud_id,ambito
                ) VALUES (%s,CURRENT_DATE,%s,'ACTIVO','Prueba',%s,%s,'INTERNACIONAL')""",
            (
                solicitud["cliente_id"], solicitud["precio_tauro_ars"],
                tracking, solicitud_id,
            ),
        )
        cur.execute(
            "UPDATE solicitudes_guia SET cargo_pendiente=TRUE WHERE id=%s",
            (solicitud_id,),
        )
    control.finalizar_intento(_iniciar(solicitud_id), {"ok": True}, {})
    assert _incidencia(
        incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS"
    )["estado"] == "PENDIENTE"

    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE solicitudes_guia SET cargo_pendiente=FALSE WHERE id=%s",
            (solicitud_id,),
        )
    control.finalizar_intento(_iniciar(solicitud_id), {"ok": True}, {})
    incidencia = _incidencia(
        incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS"
    )
    assert incidencia["estado"] == "RESUELTO"
    assert incidencia["resolucion"] == "Guía, documento y cargo verificados."


def test_exito_incoherente_abre_incidencia_por_pdf_o_cargo_faltante(
    incidencias_db,
):
    sin_pdf = _crear_solicitud(incidencias_db, cliente="CLIENTE_SIN_PDF")
    _hacer_emitida_coherente(incidencias_db, sin_pdf)
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE solicitudes_guia
                  SET label_pdf=NULL,guia_url='https://example.invalid/etiqueta'
                WHERE id=%s""",
            (sin_pdf,),
        )
    intento_sin_pdf = _iniciar(sin_pdf)
    control.finalizar_intento(intento_sin_pdf, {"ok": True}, {})

    etiqueta = _incidencia(incidencias_db, sin_pdf, "ETIQUETA_PENDIENTE")
    assert etiqueta["estado"] == "PENDIENTE"
    assert etiqueta["ultimo_intento_id"] == intento_sin_pdf

    sin_cargo = _crear_solicitud(incidencias_db, cliente="CLIENTE_SIN_CARGO")
    _hacer_emitida_coherente(incidencias_db, sin_cargo)
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM envios WHERE solicitud_id=%s", (sin_cargo,))
    intento_sin_cargo = _iniciar(sin_cargo)
    control.finalizar_intento(intento_sin_cargo, {"ok": True}, {})

    cargo = _incidencia(incidencias_db, sin_cargo, "CARGO_PENDIENTE")
    assert cargo["estado"] == "PENDIENTE"
    assert cargo["ultimo_intento_id"] == intento_sin_cargo


def test_evidencia_exige_fecha_referencia_y_cargo_del_mismo_cliente(
    incidencias_db,
):
    sin_fecha = _crear_solicitud(incidencias_db, cliente="CLIENTE_SIN_FECHA")
    _hacer_emitida_coherente(incidencias_db, sin_fecha)
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE solicitudes_guia SET guia_generada_at=NULL WHERE id=%s",
            (sin_fecha,),
        )
    control.finalizar_intento(_iniciar(sin_fecha), {"ok": True}, {})
    assert _incidencia(
        incidencias_db, sin_fecha, "EMISION_NO_CLASIFICADA"
    )["estado"] == "PENDIENTE"

    sin_referencia = _crear_solicitud(
        incidencias_db, cliente="CLIENTE_SIN_REFERENCIA"
    )
    _hacer_emitida_coherente(incidencias_db, sin_referencia)
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE solicitudes_guia
                  SET courier_message_reference=NULL WHERE id=%s""",
            (sin_referencia,),
        )
    control.finalizar_intento(_iniciar(sin_referencia), {"ok": True}, {})
    assert _incidencia(
        incidencias_db, sin_referencia, "EMISION_NO_CLASIFICADA"
    )["estado"] == "PENDIENTE"

    cargo_ajeno = _crear_solicitud(incidencias_db, cliente="CLIENTE_CARGO_PROPIO")
    _hacer_emitida_coherente(incidencias_db, cargo_ajeno)
    _crear_solicitud(incidencias_db, cliente="CLIENTE_CARGO_AJENO")
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE envios SET cliente_id='CLIENTE_CARGO_AJENO'
                WHERE solicitud_id=%s""",
            (cargo_ajeno,),
        )
    control.finalizar_intento(_iniciar(cargo_ajeno), {"ok": True}, {})
    assert _incidencia(
        incidencias_db, cargo_ajeno, "CARGO_PENDIENTE"
    )["estado"] == "PENDIENTE"


def test_exito_lento_coherente_cierra_error_posterior_con_id_mayor(
    incidencias_db,
):
    solicitud_id = _crear_solicitud(incidencias_db, cliente="CLIENTE_CARRERA")
    intento_lento = _iniciar(solicitud_id)
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE solicitudes_guia SET estado='EMITIENDO' WHERE id=%s",
            (solicitud_id,),
        )
    intento_rapido = _iniciar(solicitud_id)
    assert intento_rapido > intento_lento
    _finalizar_fallido(intento_rapido, "DATOS_ENVIO_INVALIDOS")
    assert _incidencia(
        incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS"
    )["estado"] == "PENDIENTE"

    _hacer_emitida_coherente(incidencias_db, solicitud_id)
    control.finalizar_intento(intento_lento, {"ok": True}, {})

    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT codigo,estado FROM emision_incidencias WHERE solicitud_id=%s",
            (solicitud_id,),
        )
        incidencias = cur.fetchall()
    assert incidencias
    assert {item["estado"] for item in incidencias} == {"RESUELTO"}


def test_verificar_inconcluso_concilia_solo_con_evidencia_completa(
    incidencias_db,
):
    coherente = _crear_solicitud(incidencias_db, cliente="CLIENTE_HUERFANA_OK")
    intento_coherente = _iniciar(coherente)
    _hacer_emitida_coherente(incidencias_db, coherente)
    incompleta = _crear_solicitud(
        incidencias_db, cliente="CLIENTE_HUERFANA_PENDIENTE"
    )
    intento_incompleto = _iniciar(incompleta)
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE emision_intentos
                  SET iniciado_en=NOW()-INTERVAL '11 minutes'
                WHERE id IN (%s,%s)""",
            (intento_coherente, intento_incompleto),
        )

    assert control.verificar_inconcluso(intento_coherente) is True
    assert control.verificar_inconcluso(intento_incompleto) is False

    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id,estado FROM emision_intentos WHERE id IN (%s,%s) ORDER BY id",
            (intento_coherente, intento_incompleto),
        )
        estados = {item["id"]: item["estado"] for item in cur.fetchall()}
    assert estados == {
        intento_coherente: "CONCILIADO",
        intento_incompleto: "INICIADO",
    }
    listado = control.listar_incidencias("pendientes")
    assert [item["id"] for item in listado["inconclusos"]] == [intento_incompleto]


def test_resumen_distingue_pendientes_resueltos_e_inconclusos(incidencias_db):
    pendiente = _crear_solicitud(incidencias_db, cliente="CLIENTE_PENDIENTE")
    resuelta = _crear_solicitud(incidencias_db, cliente="CLIENTE_RESUELTA")
    huerfana = _crear_solicitud(incidencias_db, cliente="CLIENTE_HUERFANA")
    _finalizar_fallido(_iniciar(pendiente), "EMISION_NO_CLASIFICADA")
    _finalizar_fallido(_iniciar(resuelta))
    _hacer_emitida_coherente(incidencias_db, resuelta)
    control.finalizar_intento(_iniciar(resuelta), {"ok": True}, {})
    intento_huerfano = _iniciar(huerfana)
    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE emision_intentos
                  SET iniciado_en=NOW()-INTERVAL '11 minutes'
                WHERE id=%s""",
            (intento_huerfano,),
        )

    resumen = control.resumen_incidencias()
    pendientes = control.listar_incidencias("pendientes")
    resueltos = control.listar_incidencias("resueltos")

    assert resumen == {
        "pendientes": 1,
        "criticas": 1,
        "inconclusos": 1,
        "total": 2,
        "disponible": True,
    }
    assert [item["solicitud_id"] for item in pendientes["items"]] == [pendiente]
    assert [item["solicitud_id"] for item in resueltos["items"]] == [resuelta]
    assert [item["id"] for item in pendientes["inconclusos"]] == [intento_huerfano]


def test_tomar_revision_rechaza_intento_desactualizado(incidencias_db):
    solicitud_id = _crear_solicitud(incidencias_db, cliente="CLIENTE_REVISION")
    intento_viejo = _iniciar(solicitud_id)
    _finalizar_fallido(intento_viejo)
    intento_vigente = _iniciar(solicitud_id)
    _finalizar_fallido(intento_vigente)
    incidencia = _incidencia(
        incidencias_db, solicitud_id, "DATOS_ENVIO_INVALIDOS"
    )

    assert control.tomar_revision(incidencia["id"], intento_viejo) is False
    assert control.tomar_revision(incidencia["id"], intento_vigente) is True
    assert control.tomar_revision(incidencia["id"], intento_vigente) is False


def test_analisis_por_defecto_no_usa_ia_ni_modifica_envio_o_finanzas(
    incidencias_db, monkeypatch,
):
    solicitud_id = _crear_solicitud(
        incidencias_db, cliente="CLIENTE_LECTURA", precio="12345.67"
    )
    _hacer_emitida_coherente(
        incidencias_db, solicitud_id, cargo_pendiente=True
    )
    intento_id = _iniciar(solicitud_id)
    _finalizar_fallido(intento_id, "EMISION_NO_CLASIFICADA")
    incidencia = _incidencia(
        incidencias_db, solicitud_id, "EMISION_NO_CLASIFICADA"
    )
    modulo_falso = types.SimpleNamespace(
        sugerir_diagnostico=lambda *_args, **_kwargs: pytest.fail(
            "No debe invocar IA por defecto"
        )
    )
    monkeypatch.setitem(sys.modules, "servicios.agente_incidencias_ia", modulo_falso)

    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT estado,tracking,label_pdf,guia_url,cargo_pendiente,
                      precio_tauro_ars,precio_tauro_usd,precio_cliente_final_ars
                 FROM solicitudes_guia WHERE id=%s""",
            (solicitud_id,),
        )
        solicitud_antes = dict(cur.fetchone())
        cur.execute(
            """SELECT estado,monto_ars,tracking,solicitud_id
                 FROM envios WHERE solicitud_id=%s""",
            (solicitud_id,),
        )
        cargo_antes = dict(cur.fetchone())

    assert control.analizar_incidencia(
        incidencia["id"], intento_id
    ) == "analizado"

    with incidencias_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT estado,tracking,label_pdf,guia_url,cargo_pendiente,
                      precio_tauro_ars,precio_tauro_usd,precio_cliente_final_ars
                 FROM solicitudes_guia WHERE id=%s""",
            (solicitud_id,),
        )
        assert dict(cur.fetchone()) == solicitud_antes
        cur.execute(
            """SELECT estado,monto_ars,tracking,solicitud_id
                 FROM envios WHERE solicitud_id=%s""",
            (solicitud_id,),
        )
        assert dict(cur.fetchone()) == cargo_antes
        cur.execute(
            "SELECT estado,analisis FROM emision_incidencias WHERE id=%s",
            (incidencia["id"],),
        )
        analizada = cur.fetchone()
    assert analizada["estado"] == "EN_REVISION"
    assert analizada["analisis"]["fuente"] == "reglas"
    assert "ia" not in analizada["analisis"]


def test_analisis_cas_descarta_resultado_si_llega_un_error_nuevo(
    incidencias_db, monkeypatch,
):
    solicitud_id = _crear_solicitud(incidencias_db, cliente="CLIENTE_CAS")
    intento_viejo = _iniciar(solicitud_id)
    _finalizar_fallido(intento_viejo, "EMISION_NO_CLASIFICADA")
    incidencia = _incidencia(
        incidencias_db, solicitud_id, "EMISION_NO_CLASIFICADA"
    )
    nuevo = {}

    def diagnostico_lento(_entrada):
        nuevo["intento_id"] = _iniciar(solicitud_id)
        _finalizar_fallido(nuevo["intento_id"], "EMISION_NO_CLASIFICADA")
        return {"model": "modelo-prueba", "resumen": "resultado viejo"}

    monkeypatch.setitem(
        sys.modules,
        "servicios.agente_incidencias_ia",
        types.SimpleNamespace(sugerir_diagnostico=diagnostico_lento),
    )

    resultado = control.analizar_incidencia(
        incidencia["id"], intento_viejo, usar_ia=True
    )

    assert resultado == "desactualizado"
    vigente = _incidencia(
        incidencias_db, solicitud_id, "EMISION_NO_CLASIFICADA"
    )
    assert vigente["ultimo_intento_id"] == nuevo["intento_id"]
    assert vigente["cantidad"] == 2
    assert vigente["estado"] == "PENDIENTE"
    assert vigente["analisis"] is None
