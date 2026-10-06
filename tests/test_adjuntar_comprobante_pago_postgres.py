"""Adjuntar evidencia a pagos existentes sobre PostgreSQL aislado."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from servicios import cuenta_corriente as cc
from test_pagos_documentales_postgres import DATABASE_URL, cuenta_db


pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


PDF = b"%PDF-1.4\ncomprobante unico\n%%EOF\n"


def _crear_cliente(cur, cliente_id: str) -> None:
    cur.execute(
        "INSERT INTO clientes (cliente_id,email) VALUES (%s,%s)",
        (cliente_id, f"{cliente_id.lower()}@example.invalid"),
    )


def _crear_pago(
    cur, cliente_id: str, estado="APROBADO", referencia="REF-ORIGINAL",
) -> int:
    cur.execute(
        """
        INSERT INTO pagos (
            cliente_id,fecha,monto_ars,metodo,referencia,nota,estado
        ) VALUES (%s,CURRENT_DATE,1250.50,'transferencia',%s,
                  'Pago ya registrado',%s)
        RETURNING id
        """,
        (cliente_id, referencia, estado),
    )
    return int(cur.fetchone()["id"])


@pytest.mark.parametrize("estado", ["APROBADO", "PENDIENTE", "RECHAZADO", None])
def test_adjunta_en_cualquier_estado_sin_modificar_contabilidad(cuenta_db, estado):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "CLIENTE")
            pago_id = _crear_pago(cur, "CLIENTE", estado)
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id,fecha,monto_ars,estado,tracking,ambito
                ) VALUES ('CLIENTE',CURRENT_DATE,1250.50,'ACTIVO',
                          'TRACK-COMPROBANTE','INTERNACIONAL')
                RETURNING id
                """
            )
            envio_id = int(cur.fetchone()["id"])
            if estado != "RECHAZADO":
                cur.execute(
                    """
                    INSERT INTO pagos_aplicaciones (
                        pago_id,ambito,monto_ars,estado,envio_id
                    ) VALUES (%s,'INTERNACIONAL',1250.50,%s,%s)
                    """,
                    (
                        pago_id,
                        "SOLICITADA" if estado == "PENDIENTE" else "APLICADA",
                        envio_id,
                    ),
                )
            cur.execute(
                """
                SELECT fecha,monto_ars,metodo,referencia,nota,estado,
                       fecha_original_conocida,fecha_revision_requerida
                  FROM pagos WHERE id=%s
                """,
                (pago_id,),
            )
            pago_antes = dict(cur.fetchone())
            cur.execute(
                """
                SELECT pago_id,ambito,monto_ars,estado,factura_id,envio_id
                  FROM pagos_aplicaciones WHERE pago_id=%s ORDER BY id
                """,
                (pago_id,),
            )
            aplicaciones_antes = [dict(fila) for fila in cur.fetchall()]

    total_antes = cc.total_pagado("CLIENTE")
    assert cc.adjuntar_comprobante_pago(
        "  cliente  ", pago_id, PDF, "../carpeta/recibo.pdf",
    ) is True
    assert cc.total_pagado("CLIENTE") == total_antes

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fecha,monto_ars,metodo,referencia,nota,estado,
                       fecha_original_conocida,fecha_revision_requerida,
                       comprobante,comprobante_tipo,comprobante_nombre,
                       comprobante_sha256
                  FROM pagos WHERE id=%s
                """,
                (pago_id,),
            )
            pago_despues = dict(cur.fetchone())
            assert {
                clave: pago_despues[clave] for clave in pago_antes
            } == pago_antes
            assert bytes(pago_despues["comprobante"]) == PDF
            assert pago_despues["comprobante_tipo"] == "application/pdf"
            assert pago_despues["comprobante_nombre"] == "recibo.pdf"
            assert pago_despues["comprobante_sha256"] == hashlib.sha256(PDF).hexdigest()
            cur.execute(
                """
                SELECT pago_id,ambito,monto_ars,estado,factura_id,envio_id
                  FROM pagos_aplicaciones WHERE pago_id=%s ORDER BY id
                """,
                (pago_id,),
            )
            assert [dict(fila) for fila in cur.fetchall()] == aplicaciones_antes


def test_oculta_pago_ajeno_o_inexistente_y_no_escribe(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "PROPIO")
            _crear_cliente(cur, "AJENO")
            pago_id = _crear_pago(cur, "PROPIO")

    with pytest.raises(LookupError, match="no existe"):
        cc.adjuntar_comprobante_pago("AJENO", pago_id, PDF, "recibo.pdf")
    with pytest.raises(LookupError, match="no existe"):
        cc.adjuntar_comprobante_pago("PROPIO", pago_id + 9999, PDF, "recibo.pdf")

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comprobante,comprobante_sha256 FROM pagos WHERE id=%s",
                (pago_id,),
            )
            assert dict(cur.fetchone()) == {
                "comprobante": None,
                "comprobante_sha256": None,
            }
            cur.execute(
                "SELECT COUNT(*) AS n FROM security_audit "
                "WHERE event='cuenta.adjuntar_comprobante_pago'"
            )
            assert cur.fetchone()["n"] == 0


def test_reintento_es_idempotente_y_un_archivo_distinto_no_reemplaza(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "CLIENTE")
            pago_id = _crear_pago(cur, "CLIENTE")

    assert cc.adjuntar_comprobante_pago(
        "CLIENTE", pago_id, PDF, "primero.pdf",
    ) is True
    assert cc.adjuntar_comprobante_pago(
        "CLIENTE", pago_id, PDF, "renombrado.pdf",
    ) is False
    with pytest.raises(ValueError, match="otro comprobante"):
        cc.adjuntar_comprobante_pago(
            "CLIENTE", pago_id, b"%PDF-1.4\notro\n%%EOF", "otro.pdf",
        )

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comprobante,comprobante_nombre FROM pagos WHERE id=%s",
                (pago_id,),
            )
            fila = cur.fetchone()
            assert bytes(fila["comprobante"]) == PDF
            assert fila["comprobante_nombre"] == "primero.pdf"
            cur.execute(
                "SELECT COUNT(*) AS n FROM security_audit "
                "WHERE event='cuenta.adjuntar_comprobante_pago'"
            )
            assert cur.fetchone()["n"] == 1


def test_doble_click_concurrente_adjunta_una_sola_vez(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "CLIENTE")
            pago_id = _crear_pago(cur, "CLIENTE", "PENDIENTE")

    barrera = Barrier(2)

    def adjuntar():
        barrera.wait()
        return cc.adjuntar_comprobante_pago(
            "CLIENTE", pago_id, PDF, "doble-click.pdf",
        )

    with ThreadPoolExecutor(max_workers=2) as ejecutor:
        resultados = list(ejecutor.map(lambda _: adjuntar(), range(2)))
    assert sorted(resultados) == [False, True]

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM security_audit "
                "WHERE event='cuenta.adjuntar_comprobante_pago'"
            )
            assert cur.fetchone()["n"] == 1


def test_mismo_documento_no_se_asocia_a_otro_pago_aun_rechazado(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "CLIENTE_A")
            _crear_cliente(cur, "CLIENTE_B")
            pago_a = _crear_pago(cur, "CLIENTE_A", "APROBADO")
            pago_b = _crear_pago(cur, "CLIENTE_B", "RECHAZADO")

    assert cc.adjuntar_comprobante_pago("CLIENTE_A", pago_a, PDF, "a.pdf")
    with pytest.raises(ValueError, match="otro pago"):
        cc.adjuntar_comprobante_pago("CLIENTE_B", pago_b, PDF, "b.pdf")

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT comprobante FROM pagos WHERE id=%s", (pago_b,))
            assert cur.fetchone()["comprobante"] is None


def test_mismo_documento_concurrente_solo_gana_un_pago(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "CLIENTE_A")
            _crear_cliente(cur, "CLIENTE_B")
            pago_a = _crear_pago(cur, "CLIENTE_A")
            pago_b = _crear_pago(cur, "CLIENTE_B")

    barrera = Barrier(2)

    def adjuntar(cliente, pago):
        barrera.wait()
        try:
            return cc.adjuntar_comprobante_pago(cliente, pago, PDF, "mismo.pdf")
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as ejecutor:
        futuros = [
            ejecutor.submit(adjuntar, "CLIENTE_A", pago_a),
            ejecutor.submit(adjuntar, "CLIENTE_B", pago_b),
        ]
        resultados = [futuro.result() for futuro in futuros]
    assert resultados.count(True) == 1
    assert sum("otro pago" in str(resultado) for resultado in resultados) == 1

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM pagos WHERE comprobante_sha256=%s",
                (hashlib.sha256(PDF).hexdigest(),),
            )
            assert cur.fetchone()["n"] == 1
            cur.execute(
                "SELECT COUNT(*) AS n FROM security_audit "
                "WHERE event='cuenta.adjuntar_comprobante_pago'"
            )
            assert cur.fetchone()["n"] == 1


def test_fallo_de_auditoria_revierte_el_comprobante(cuenta_db, monkeypatch):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "CLIENTE")
            pago_id = _crear_pago(cur, "CLIENTE")

    def fallar_auditoria(*args, **kwargs):
        raise RuntimeError("auditoría indisponible")

    monkeypatch.setattr(cc, "registrar_evento_con_cursor", fallar_auditoria)
    with pytest.raises(RuntimeError, match="auditoría indisponible"):
        cc.adjuntar_comprobante_pago("CLIENTE", pago_id, PDF, "recibo.pdf")

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT comprobante,comprobante_tipo,comprobante_nombre,
                       comprobante_sha256,monto_ars,estado
                  FROM pagos WHERE id=%s
                """,
                (pago_id,),
            )
            assert dict(cur.fetchone()) == {
                "comprobante": None,
                "comprobante_tipo": None,
                "comprobante_nombre": None,
                "comprobante_sha256": None,
                "monto_ars": Decimal("1250.50"),
                "estado": "APROBADO",
            }


def test_valida_tamano_formato_y_sanitiza_nombre(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "CLIENTE")
            pago_id = _crear_pago(cur, "CLIENTE")
            pago_chino = _crear_pago(
                cur, "CLIENTE", referencia="REF-COMPROBANTE-CHINO",
            )
            pago_emoji = _crear_pago(
                cur, "CLIENTE", referencia="REF-COMPROBANTE-EMOJI",
            )

    with pytest.raises(ValueError, match="foto.*PDF"):
        cc.adjuntar_comprobante_pago("CLIENTE", pago_id, b"GIF89a", "archivo.gif")
    demasiado_grande = b"%PDF" + b"0" * (cc.COMPROBANTE_MAX_BYTES - 3)
    with pytest.raises(ValueError, match="8 MB"):
        cc.adjuntar_comprobante_pago(
            "CLIENTE", pago_id, demasiado_grande, "archivo.pdf",
        )

    nombre_hostil = "../carpeta\\otra/\";" + "x" * 190 + "\r\n.pdf"
    assert cc.adjuntar_comprobante_pago(
        "CLIENTE", pago_id, b"\x89PNG\r\n\x1a\ncontenido", nombre_hostil,
    )
    assert cc.adjuntar_comprobante_pago(
        "CLIENTE", pago_chino, b"%PDF-1.4\nchino\n%%EOF", "支付.pdf",
    )
    assert cc.adjuntar_comprobante_pago(
        "CLIENTE", pago_emoji, b"%PDF-1.4\nemoji\n%%EOF", "📎 PDF.exe",
    )
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comprobante_nombre,comprobante_tipo FROM pagos WHERE id=%s",
                (pago_id,),
            )
            fila = cur.fetchone()
            assert fila["comprobante_tipo"] == "image/png"
            assert len(fila["comprobante_nombre"]) <= 160
            assert not any(
                separador in fila["comprobante_nombre"]
                for separador in ("/", "\\", "\r", "\n", '"', ";")
            )
            cur.execute(
                """
                SELECT id,comprobante_nombre FROM pagos
                 WHERE id IN (%s,%s) ORDER BY id
                """,
                (pago_chino, pago_emoji),
            )
            nombres = {
                fila["id"]: fila["comprobante_nombre"] for fila in cur.fetchall()
            }
            assert nombres[pago_chino] == "comprobante.pdf"
            assert nombres[pago_emoji] == "PDF.pdf"
            assert all(nombre.isascii() for nombre in nombres.values())
