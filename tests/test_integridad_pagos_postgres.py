"""Identidad documental y fecha de acreditación en PostgreSQL aislado."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import uuid

import psycopg2
import psycopg2.extras
import pytest

from servicios import cuenta_corriente as cuenta
from servicios import facturacion_clientes


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL local y aislada",
)


@pytest.fixture
def pagos_db(monkeypatch):
    schema = f"test_integridad_pagos_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    admin = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def conexion():
            conn = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{schema}"')
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        monkeypatch.setattr(cuenta, "get_conn", conexion)
        monkeypatch.setattr(facturacion_clientes, "get_conn", conexion)
        with conexion() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clientes(cliente_id,email,nombre) VALUES
                    ('PAGOS_A','pagos-a@example.invalid','Pagos A'),
                    ('PAGOS_B','pagos-b@example.invalid','Pagos B')
                """
            )
        yield conexion
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _alta(cliente: str, referencia: str, clave: str, **extra):
    return cuenta.registrar_pago(
        cliente_id=cliente,
        fecha=cuenta._hoy_argentina().isoformat(),
        monto_ars="100.00",
        metodo="transferencia",
        referencia=referencia,
        estado="APROBADO",
        idempotency_key=clave,
        **extra,
    )


def test_dos_formularios_concurrentes_no_acreditan_la_misma_referencia(pagos_db):
    barrera = __import__("threading").Barrier(2)

    def intentar(numero):
        barrera.wait()
        try:
            return _alta(
                "PAGOS_A",
                " Transferencia-ABC 001 " if numero == 1 else "transferencia abc-001",
                str(uuid.uuid4()),
            )
        except cuenta.ConflictoContableError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(intentar, (1, 2)))

    assert sum(resultado is not None for resultado in resultados) == 1
    assert cuenta.total_pagado("PAGOS_A") == 100.0
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cantidad FROM pagos")
        assert cur.fetchone()["cantidad"] == 1


def test_hash_del_comprobante_bloquea_duplicado_incluso_en_otro_cliente(pagos_db):
    comprobante = b"%PDF-1.4\ncomprobante-unico\n%%EOF\n"
    _alta("PAGOS_A", "REFERENCIA-A", str(uuid.uuid4()), comprobante=comprobante)

    with pytest.raises(cuenta.ConflictoContableError, match="ya fue registrado"):
        _alta("PAGOS_B", "REFERENCIA-B", str(uuid.uuid4()), comprobante=comprobante)


def test_excepcion_documental_exige_admin_motivo_y_queda_auditada(pagos_db):
    _alta("PAGOS_A", "REFERENCIA-EXCEPCION", str(uuid.uuid4()))
    with pytest.raises(ValueError, match="Sólo un administrador"):
        _alta(
            "PAGOS_A", "REFERENCIA EXCEPCION", str(uuid.uuid4()),
            duplicado_autorizado_motivo="Transferencias distintas con igual referencia",
        )

    segundo = _alta(
        "PAGOS_A", "REFERENCIA EXCEPCION", str(uuid.uuid4()),
        actor_tipo="admin", actor_ref="qa-finanzas",
        duplicado_autorizado_motivo="Transferencias distintas con igual referencia bancaria",
    )
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT duplicado_autorizado_motivo,duplicado_autorizado_por
              FROM pagos WHERE id=%s
            """, (segundo,),
        )
        fila = cur.fetchone()
        assert fila["duplicado_autorizado_por"] == "qa-finanzas"
        assert "distintas" in fila["duplicado_autorizado_motivo"]
        cur.execute(
            """
            SELECT metadata FROM security_audit
             WHERE event='cuenta.registrar_pago'
               AND (metadata->>'pago_id')::integer=%s
            """, (segundo,),
        )
        assert cur.fetchone()["metadata"]["excepcion_duplicado"] is True
    assert cuenta.listar_anomalias_pagos_documentales() == []


def test_pago_futuro_no_se_acredita_ni_al_aprobar_pendiente(pagos_db):
    futuro = (cuenta._hoy_argentina() + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="no futuro"):
        cuenta.registrar_pago(
            "PAGOS_A", futuro, "50", "transferencia",
            referencia="FUTURO-DIRECTO", estado="APROBADO",
        )

    pendiente = cuenta.registrar_pago(
        "PAGOS_A", futuro, "50", "transferencia",
        referencia="FUTURO-PENDIENTE", estado="PENDIENTE",
    )
    assert cuenta.total_pagado("PAGOS_A") == 0.0
    with pytest.raises(ValueError, match="no futuro"):
        cuenta.resolver_pago(pendiente, aprobar=True)
    assert cuenta.total_pagado("PAGOS_A") == 0.0


def test_trigger_marca_insert_y_update_futuros_y_aprobacion_no_libera_flag(pagos_db):
    futuro = (cuenta._hoy_argentina() + timedelta(days=1)).isoformat()
    insertado_id = cuenta.registrar_pago(
        "PAGOS_A", futuro, "25", "transferencia",
        referencia="FUTURO-MARCA-INSERT", estado="PENDIENTE",
    )
    actualizado_id = cuenta.registrar_pago(
        "PAGOS_A", cuenta._hoy_argentina().isoformat(), "30", "transferencia",
        referencia="FUTURO-MARCA-UPDATE", estado="PENDIENTE",
    )
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pagos
               SET fecha=CURRENT_DATE+1,
                   fecha_revision_confirmada_at=NOW(),
                   fecha_revision_confirmada_por='actor-viejo',
                   fecha_revision_motivo='Motivo anterior que no debe reutilizarse',
                   fecha_revision_evidencia='Evidencia anterior'
             WHERE id=%s
            """, (actualizado_id,),
        )
        cur.execute(
            """
            SELECT id,fecha_revision_requerida,fecha_revision_confirmada_at,
                   fecha_revision_confirmada_por,fecha_revision_motivo,
                   fecha_revision_evidencia
              FROM pagos WHERE id IN (%s,%s) ORDER BY id
            """, (insertado_id, actualizado_id),
        )
        filas = cur.fetchall()
        assert [fila["fecha_revision_requerida"] for fila in filas] == [True, True]
        for fila in filas:
            assert fila["fecha_revision_confirmada_at"] is None
            assert fila["fecha_revision_confirmada_por"] is None
            assert fila["fecha_revision_motivo"] is None
            assert fila["fecha_revision_evidencia"] is None

        # Simula el paso del día sin liberar la cuarentena persistente.
        cur.execute("UPDATE pagos SET fecha=CURRENT_DATE WHERE id=%s", (insertado_id,))

    with pytest.raises(ValueError, match="revisión administrativa con evidencia"):
        cuenta.resolver_pago(insertado_id, aprobar=True, actor_tipo="admin")
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT estado,fecha_revision_requerida FROM pagos WHERE id=%s",
            (insertado_id,),
        )
        fila = cur.fetchone()
        assert fila["estado"] == "PENDIENTE"
        assert fila["fecha_revision_requerida"] is True


def test_schema_expone_anomalias_legacy_sin_corregir_importes(pagos_db):
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE pagos DISABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute(
            """
            INSERT INTO pagos(
                cliente_id,fecha,monto_ars,metodo,referencia,
                referencia_normalizada,estado
            )
            VALUES
                ('PAGOS_A',CURRENT_DATE,10,'transferencia','LEGACY-1','LEGACY1','APROBADO'),
                ('PAGOS_A',CURRENT_DATE,20,'transferencia','legacy 1','LEGACY1','APROBADO')
            """
        )
        cur.execute("ALTER TABLE pagos ENABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute(
            "SELECT cantidad,pagos_ids FROM pagos_anomalias_documentales "
            "WHERE tipo='REFERENCIA_DUPLICADA'"
        )
        alerta = cur.fetchone()
        assert alerta["cantidad"] == 2
        cur.execute("SELECT SUM(monto_ars) AS total FROM pagos")
        assert cur.fetchone()["total"] == 30
    alertas = cuenta.listar_anomalias_pagos_documentales()
    assert alertas == [{
        "tipo": "REFERENCIA_DUPLICADA",
        "cliente_id": "PAGOS_A",
        "identidad": "LEGACY1",
        "cantidad": 2,
        "pagos_ids": alertas[0]["pagos_ids"],
        "identidad_visible": "LEGACY1",
    }]
    assert len(alertas[0]["pagos_ids"]) == 2


def test_factura_no_figura_pagada_por_aplicacion_futura_legacy(pagos_db):
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facturas_cliente(
                cliente_id,tipo,punto_venta,numero,fecha_emision,
                subtotal,total,pdf,created_by
            ) VALUES('PAGOS_A','FC',1,99,CURRENT_DATE,100,100,%s,'qa')
            RETURNING id
            """, (b'%PDF-factura',),
        )
        factura_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO envios(
                cliente_id,fecha,monto_ars,estado,descripcion,ambito
            ) VALUES('PAGOS_A',CURRENT_DATE,100,'ACTIVO','Servicio','INTERNACIONAL')
            RETURNING id
            """
        )
        envio_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO facturas_cliente_items(
                factura_id,envio_id,descripcion,monto
            ) VALUES(%s,%s,'Servicio',100)
            """, (factura_id, envio_id),
        )
        cur.execute("ALTER TABLE pagos DISABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute(
            """
            INSERT INTO pagos(
                cliente_id,fecha,monto_ars,metodo,estado,fecha_revision_requerida
            ) VALUES('PAGOS_A',CURRENT_DATE+1,100,'transferencia','APROBADO',TRUE)
            RETURNING id
            """
        )
        pago_id = cur.fetchone()["id"]
        cur.execute("ALTER TABLE pagos ENABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute("ALTER TABLE pagos_aplicaciones DISABLE TRIGGER trg_validar_pago_aplicacion")
        cur.execute(
            """
            INSERT INTO pagos_aplicaciones(
                pago_id,ambito,monto_ars,estado,factura_id
            ) VALUES(%s,'INTERNACIONAL',100,'APLICADA',%s)
            """, (pago_id, factura_id),
        )
        cur.execute("ALTER TABLE pagos_aplicaciones ENABLE TRIGGER trg_validar_pago_aplicacion")

    [factura] = facturacion_clientes.listar_facturas_cliente("PAGOS_A")
    detalle = facturacion_clientes.obtener_factura_cliente(
        factura_id, cliente_id="PAGOS_A",
    )
    assert factura["pagado"] == detalle["pagado"] == 0
    assert factura["saldo"] == detalle["saldo"] == 100


def test_cuarentena_persiste_tras_rollover_y_no_puede_sobreaplicar(pagos_db):
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facturas_cliente(
                cliente_id,tipo,punto_venta,numero,fecha_emision,
                subtotal,total,pdf,created_by
            ) VALUES('PAGOS_A','FC',1,100,CURRENT_DATE,100,100,%s,'qa')
            RETURNING id
            """, (b'%PDF-factura',),
        )
        factura_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO envios(
                cliente_id,fecha,monto_ars,estado,descripcion,ambito
            ) VALUES(
                'PAGOS_A',CURRENT_DATE,100,'ACTIVO','Servicio rollover','INTERNACIONAL'
            ) RETURNING id
            """
        )
        envio_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO facturas_cliente_items(
                factura_id,envio_id,descripcion,monto
            ) VALUES(%s,%s,'Servicio rollover',100)
            """, (factura_id, envio_id),
        )
        cur.execute("ALTER TABLE pagos DISABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute(
            """
            INSERT INTO pagos(
                cliente_id,fecha,monto_ars,metodo,referencia,estado,
                fecha_revision_requerida
            ) VALUES(
                'PAGOS_A',CURRENT_DATE+1,100,'transferencia','LEGACY-FUTURO',
                'APROBADO',TRUE
            ) RETURNING id
            """
        )
        legado_id = cur.fetchone()["id"]
        cur.execute("ALTER TABLE pagos ENABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute("ALTER TABLE pagos_aplicaciones DISABLE TRIGGER trg_validar_pago_aplicacion")
        cur.execute(
            """
            INSERT INTO pagos_aplicaciones(
                pago_id,ambito,monto_ars,estado,factura_id
            ) VALUES(%s,'INTERNACIONAL',100,'APLICADA',%s)
            """, (legado_id, factura_id),
        )
        cur.execute("ALTER TABLE pagos_aplicaciones ENABLE TRIGGER trg_validar_pago_aplicacion")

        # Simula que llegó la fecha. El flag es persistente: no se reactiva por reloj.
        cur.execute("UPDATE pagos SET fecha=CURRENT_DATE WHERE id=%s", (legado_id,))
        cur.execute(
            """
            INSERT INTO pagos(
                cliente_id,fecha,monto_ars,metodo,referencia,estado
            ) VALUES(
                'PAGOS_A',CURRENT_DATE,100,'transferencia','PAGO-VALIDO','APROBADO'
            ) RETURNING id
            """
        )
        valido_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO pagos_aplicaciones(
                pago_id,ambito,monto_ars,estado,factura_id
            ) VALUES(%s,'INTERNACIONAL',100,'APLICADA',%s)
            """, (valido_id, factura_id),
        )

    [factura] = facturacion_clientes.listar_facturas_cliente("PAGOS_A")
    assert factura["pagado"] == 100
    assert cuenta.total_pagado("PAGOS_A") == 100
    assert cuenta.listar_anomalias_pagos_documentales()[0]["tipo"] == (
        "PAGO_FECHA_PENDIENTE_REVISION"
    )

    with pytest.raises(ValueError, match="superiores al saldo"):
        cuenta.confirmar_fecha_pago(
            legado_id,
            actor_tipo="admin",
            actor_ref="qa-finanzas",
            motivo="La fecha se cotejó con el extracto bancario",
            evidencia_ref="Extracto QA línea 18",
        )
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT fecha_revision_requerida FROM pagos WHERE id=%s", (legado_id,),
        )
        assert cur.fetchone()["fecha_revision_requerida"] is True
        cur.execute(
            """
            SELECT COUNT(*) AS cantidad FROM security_audit
             WHERE event='cuenta.confirmar_fecha_pago'
               AND (metadata->>'pago_id')::integer=%s
            """, (legado_id,),
        )
        assert cur.fetchone()["cantidad"] == 0


def test_confirmacion_fecha_exige_evidencia_y_queda_auditada(pagos_db):
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE pagos DISABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute(
            """
            INSERT INTO pagos(
                cliente_id,fecha,monto_ars,metodo,referencia,estado,
                fecha_revision_requerida
            ) VALUES(
                'PAGOS_A',CURRENT_DATE,35,'transferencia','LEGACY-REVISAR',
                'APROBADO',TRUE
            ) RETURNING id
            """
        )
        pago_id = cur.fetchone()["id"]
        cur.execute("ALTER TABLE pagos ENABLE TRIGGER trg_validar_identidad_pago_cliente")

    with pytest.raises(ValueError, match="motivo"):
        cuenta.confirmar_fecha_pago(
            pago_id, actor_tipo="admin", actor_ref="qa-finanzas",
            motivo="corto", evidencia_ref="Extracto QA línea 22",
        )
    with pytest.raises(ValueError, match="evidencia"):
        cuenta.confirmar_fecha_pago(
            pago_id, actor_tipo="admin", actor_ref="qa-finanzas",
            motivo="Fecha cotejada contra el extracto bancario", evidencia_ref="x",
        )
    with pagos_db() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.Error, match="revisión administrativa"):
            cur.execute(
                "UPDATE pagos SET fecha_revision_requerida=FALSE WHERE id=%s",
                (pago_id,),
            )

    assert cuenta.confirmar_fecha_pago(
        pago_id,
        actor_tipo="admin",
        actor_ref="qa-finanzas",
        motivo="Fecha cotejada contra el extracto bancario",
        evidencia_ref="Extracto QA línea 22",
    ) is True
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT fecha_revision_requerida,fecha_revision_confirmada_por,
                   fecha_revision_motivo,fecha_revision_evidencia
              FROM pagos WHERE id=%s
            """, (pago_id,),
        )
        fila = cur.fetchone()
        assert fila == {
            "fecha_revision_requerida": False,
            "fecha_revision_confirmada_por": "qa-finanzas",
            "fecha_revision_motivo": "Fecha cotejada contra el extracto bancario",
            "fecha_revision_evidencia": "Extracto QA línea 22",
        }
        cur.execute(
            """
            SELECT actor_ref,metadata FROM security_audit
             WHERE event='cuenta.confirmar_fecha_pago'
               AND (metadata->>'pago_id')::integer=%s
            """, (pago_id,),
        )
        auditoria = cur.fetchone()
        assert auditoria["actor_ref"] == "qa-finanzas"
        assert auditoria["metadata"]["motivo"].startswith("Fecha cotejada")
        assert auditoria["metadata"]["evidencia_ref"] == "Extracto QA línea 22"
    assert cuenta.total_pagado("PAGOS_A") == 35


def test_confirmacion_concurrente_con_nueva_imputacion_no_sobreaplica(pagos_db):
    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facturas_cliente(
                cliente_id,tipo,punto_venta,numero,fecha_emision,
                subtotal,total,pdf,created_by
            ) VALUES('PAGOS_A','FC',1,101,CURRENT_DATE,100,100,%s,'qa')
            RETURNING id
            """, (b'%PDF-concurrencia',),
        )
        factura_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO envios(
                cliente_id,fecha,monto_ars,estado,descripcion,ambito
            ) VALUES(
                'PAGOS_A',CURRENT_DATE,100,'ACTIVO','Servicio concurrente','NACIONAL'
            ) RETURNING id
            """
        )
        envio_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO facturas_cliente_items(
                factura_id,envio_id,descripcion,monto
            ) VALUES(%s,%s,'Servicio concurrente',100)
            """, (factura_id, envio_id),
        )
        cur.execute("ALTER TABLE pagos DISABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute(
            """
            INSERT INTO pagos(
                cliente_id,fecha,monto_ars,metodo,referencia,estado,
                fecha_revision_requerida
            ) VALUES(
                'PAGOS_A',CURRENT_DATE,100,'transferencia','LEGACY-CONCURRENTE',
                'APROBADO',TRUE
            ) RETURNING id
            """
        )
        legado_id = cur.fetchone()["id"]
        cur.execute("ALTER TABLE pagos ENABLE TRIGGER trg_validar_identidad_pago_cliente")
        cur.execute("ALTER TABLE pagos_aplicaciones DISABLE TRIGGER trg_validar_pago_aplicacion")
        cur.execute(
            """
            INSERT INTO pagos_aplicaciones(
                pago_id,ambito,monto_ars,estado,factura_id
            ) VALUES(%s,'NACIONAL',100,'APLICADA',%s)
            """, (legado_id, factura_id),
        )
        cur.execute("ALTER TABLE pagos_aplicaciones ENABLE TRIGGER trg_validar_pago_aplicacion")
        cur.execute(
            """
            INSERT INTO pagos(
                cliente_id,fecha,monto_ars,metodo,referencia,estado
            ) VALUES(
                'PAGOS_A',CURRENT_DATE,100,'transferencia','VALIDO-CONCURRENTE',
                'APROBADO'
            ) RETURNING id
            """
        )
        valido_id = cur.fetchone()["id"]

    barrera = __import__("threading").Barrier(2)

    def confirmar():
        barrera.wait()
        try:
            return cuenta.confirmar_fecha_pago(
                legado_id,
                actor_tipo="admin",
                actor_ref="qa-concurrencia",
                motivo="Fecha verificada para la prueba concurrente",
                evidencia_ref="Extracto QA concurrencia",
            )
        except ValueError:
            return False

    def imputar():
        barrera.wait()
        try:
            with pagos_db() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pagos_aplicaciones(
                        pago_id,ambito,monto_ars,estado,factura_id
                    ) VALUES(%s,'NACIONAL',100,'APLICADA',%s)
                    """, (valido_id, factura_id),
                )
            return True
        except psycopg2.Error:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda fn: fn(), (confirmar, imputar)))
    assert sum(bool(resultado) for resultado in resultados) == 1

    with pagos_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT fecha_revision_requerida FROM pagos WHERE id=%s", (legado_id,),
        )
        legado_activo = not cur.fetchone()["fecha_revision_requerida"]
        cur.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM pagos_aplicaciones
                 WHERE pago_id=%s AND factura_id=%s
            ) AS existe
            """, (valido_id, factura_id),
        )
        valido_aplicado = cur.fetchone()["existe"]
        assert legado_activo is not valido_aplicado
        cur.execute(
            """
            SELECT COALESCE(SUM(pa.monto_ars),0) AS cubierto
              FROM pagos_aplicaciones pa
              JOIN pagos p ON p.id=pa.pago_id
             WHERE pa.factura_id=%s AND pa.estado='APLICADA'
               AND p.estado='APROBADO' AND NOT p.fecha_revision_requerida
               AND p.fecha <= CURRENT_DATE
            """, (factura_id,),
        )
        assert cur.fetchone()["cubierto"] == 100
