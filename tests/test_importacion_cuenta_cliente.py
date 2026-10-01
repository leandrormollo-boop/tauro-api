"""Complemento de cuenta 2026 (MELCIOR / PRETE ROSSO) desde la planilla del cliente."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import psycopg2.extras
import pytest

from servicios import importacion_cuenta_cliente as imp


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
requiere_postgres = pytest.mark.skipif(not DATABASE_URL, reason="requiere TAURO_TEST_DATABASE_URL aislada")


def _envio(clave, mes, fila, tracking, importe="1000.00", dif="0.00", tax="0.00", **extra):
    base = {
        "source_key": f"MELCIOR-2026:{mes}:{clave}", "mes": mes, "fila_cliente": fila,
        "fecha": "2026-05-04", "fecha_completada_desde": "", "remitente": "JUAN PABLO MELCIOR",
        "destinatario": f"DEST {clave}", "pais_fuente": "USA", "peso_fuente": "5 KG",
        "medidas_fuente": "40X40X15", "tracking": tracking, "courier": "FEDEX",
        "tipo_fuente": "FLETE", "estado_portal": "DESPACHADO", "visible_cliente": True,
        "tracking_repetido": False, "crear_si_falta": True,
        "genera_deuda": Decimal(importe) > 0, "requiere_revision": False,
        "importe_inicial_ars": importe, "diferencia_flete_ars": dif, "tax_cliente_ars": tax,
        "tax_fuentes": [], "nro_cliente_fuente": "",
    }
    base.update(extra)
    return base


def _manifiesto(envios, pagos=None, saldo_2025="500.00", cliente="MELCIOR"):
    prefijo = imp.CLIENTES[cliente]["prefijo"]
    pagos = pagos if pagos is not None else [{
        "source_key": f"{prefijo}:PAGO:10", "fila_cliente": 10, "fecha": "2026-09-02",
        "monto_ars": "300.00", "referencia": "", "detalle_fuente": "", "fecha_original_informada": False,
    }]
    creables = [e for e in envios if e["crear_si_falta"]]
    suma = lambda campo: sum((Decimal(e[campo]) for e in creables), Decimal("0.00"))
    pagos_total = sum((Decimal(p["monto_ars"]) for p in pagos), Decimal("0.00"))
    m = {
        "schema_version": 2, "tipo": "CUENTA_CLIENTE_2026", "cliente_id": cliente, "periodo": 2026,
        "generado_at": "2026-10-01T12:00:00", "source_files_sha256": {"planilla_cliente": "a" * 64, "tauro_2026": "b" * 64},
        "politica_duplicados": "NO_CREAR" if cliente == "MELCIOR" else "CARGAR_OCULTO",
        "incluye_filas_ocultas": False, "filas_excluidas": [], "envios": envios,
        "saldo_pendiente_2025": {"source_key": f"{prefijo}:SALDO-PENDIENTE-2025", "fecha": "2025-12-31",
                                 "monto_ars": saldo_2025, "concepto": "SALDO PENDIENTE 2025"},
        "pagos": pagos, "resumen_mensual": {},
        "resumen": {
            "envios": len(envios), "cargos": sum(e["genera_deuda"] and e["crear_si_falta"] for e in envios),
            "cancelados": sum(e["estado_portal"] == "CANCELADO" for e in envios),
            "repetidos_sin_crear": sum(not e["crear_si_falta"] for e in envios),
            "repetidos_sin_crear_ars": str(sum((Decimal(e["importe_inicial_ars"]) + Decimal(e["diferencia_flete_ars"])
                                                + Decimal(e["tax_cliente_ars"]) for e in envios if not e["crear_si_falta"]), Decimal("0.00"))),
            "fletes_ars": str(suma("importe_inicial_ars")), "diferencias_ars": str(suma("diferencia_flete_ars")),
            "tax_ars": str(suma("tax_cliente_ars")), "saldo_pendiente_2025_ars": saldo_2025,
            "pagos": len(pagos), "pagos_ars": str(pagos_total),
            "saldo_resultante_ars": str(suma("importe_inicial_ars") + suma("diferencia_flete_ars")
                                        + suma("tax_cliente_ars") + Decimal(saldo_2025) - pagos_total),
            "saldo_segun_planilla_ars": "",
        },
    }
    m["manifest_sha256"] = imp.manifest_hash(m)
    return m


# ── validación (sin base) ───────────────────────────────────────────────────

def test_manifiesto_valido():
    m = _manifiesto([_envio("6", "MAYO", 6, "123456789012", dif="100.00", tax="50.00")])
    assert imp.validar_manifiesto(m) is m


def test_manifiesto_modificado_se_rechaza():
    m = _manifiesto([_envio("6", "MAYO", 6, "123456789012")])
    m["envios"][0]["importe_inicial_ars"] = "1.00"
    with pytest.raises(imp.ImportacionCuentaError, match="modificado"):
        imp.validar_manifiesto(m)


def test_cancelado_no_genera_saldo():
    e = _envio("6", "MAYO", 6, "123456789012", importe="0.00", estado_portal="CANCELADO", tax="10.00")
    with pytest.raises(imp.ImportacionCuentaError, match="cancelado"):
        imp.validar_manifiesto(_manifiesto([e]))


def test_tracking_repetido_creable_se_rechaza_en_melcior():
    m = _manifiesto([_envio("6", "MAYO", 6, "123456789012"), _envio("7", "MAYO", 7, "123456789012")])
    with pytest.raises(imp.ImportacionCuentaError, match="repetidos"):
        imp.validar_manifiesto(m)


def test_resumen_que_no_cierra_se_rechaza():
    m = _manifiesto([_envio("6", "MAYO", 6, "123456789012")])
    m["resumen"]["tax_ars"] = "1.00"
    m["manifest_sha256"] = imp.manifest_hash(m)
    with pytest.raises(imp.ImportacionCuentaError, match="tax_ars"):
        imp.validar_manifiesto(m)


# ── Postgres real ───────────────────────────────────────────────────────────

@pytest.fixture
def db(monkeypatch):
    schema = f"test_cuenta_{uuid.uuid4().hex}"
    sql = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")
    admin = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(sql)
        for cliente in ("MELCIOR", "PRETE ROSSO", "OTRO"):
            cur.execute("INSERT INTO clientes (cliente_id,email,nombre) VALUES (%s,%s,%s)",
                        (cliente, f"{cliente.replace(' ', '').lower()}@example.invalid", cliente))

    @contextmanager
    def get_conn():
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
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

    monkeypatch.setattr(imp, "get_conn", get_conn)
    try:
        yield get_conn
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _saldo(get_conn, cliente="MELCIOR"):
    with get_conn() as conn, conn.cursor() as cur:
        return Decimal(imp._saldo_portal(cur, cliente)["saldo_ars"])


@requiere_postgres
def test_carga_completa_vista_previa_e_idempotencia(db):
    envios = [
        _envio("6", "MAYO", 6, "111111111111", importe="1000.00", dif="100.00", tax="50.00"),
        _envio("7", "MAYO", 7, "222222222222", importe="2000.00"),
        _envio("8", "MAYO", 8, "333333333333", importe="0.00", estado_portal="CANCELADO"),
    ]
    m = _manifiesto(envios)
    previa = imp.procesar(m, aplicar=False, actor="test")
    assert previa["modo"] == "VISTA_PREVIA"
    assert _saldo(db) == Decimal("0.00")
    r = imp.procesar(m, aplicar=True, actor="test")
    assert Decimal(r["saldo_portal_despues"]["saldo_ars"]) == Decimal("3350.00")  # 3150 + 500 - 300
    assert r["diferencia_no_explicada_ars"] == "0.00"
    assert r["acciones"]["ENVIO_NUEVO"] == 3 and r["acciones"]["AJUSTE_AGREGADO"] == 1
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT diferencia_flete_ars, tax_cliente_ars FROM conciliaciones_envio")
        fila = cur.fetchone()
        assert Decimal(fila["diferencia_flete_ars"]) == Decimal("100") and Decimal(fila["tax_cliente_ars"]) == Decimal("50")
    otra = imp.procesar(m, aplicar=True, actor="test")
    assert "ENVIO_NUEVO" not in otra["acciones"] and otra["acciones"]["SIN_CAMBIOS"] == 3
    assert otra["pagos"]["existentes"] == 1 and not otra["pagos"]["nuevos"]
    assert _saldo(db) == Decimal("3350.00")


@requiere_postgres
def test_completa_cargo_en_cero_y_agrega_tax_incremental(db):
    inicial = _manifiesto([_envio("6", "AGOSTO", 6, "111111111111", importe="0.00", dif="0.00",
                                   genera_deuda=False)], pagos=[], saldo_2025="0.00")
    inicial["envios"][0]["genera_deuda"] = False
    inicial["manifest_sha256"] = imp.manifest_hash(inicial)
    imp.procesar(inicial, aplicar=True, actor="test")
    with db() as conn, conn.cursor() as cur:  # la carga base había dejado un cargo en $0
        cur.execute("SELECT id FROM solicitudes_guia")
        sid = cur.fetchone()["id"]
        cur.execute("INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito,solicitud_id,idempotency_key) "
                    "VALUES ('MELCIOR','2026-08-06',0,'ACTIVO','INTERNACIONAL',%s,%s)", (sid, "c" * 64))
    final = _manifiesto([_envio("6", "AGOSTO", 6, "111111111111", importe="1000.00", dif="200.00", tax="30.00")],
                        pagos=[], saldo_2025="0.00")
    r = imp.procesar(final, aplicar=True, actor="test")
    assert r["acciones"].get("CARGO_COMPLETADO") == 1 and r["acciones"].get("AJUSTE_AGREGADO") == 1
    assert _saldo(db) == Decimal("1230.00")
    mas_tax = _manifiesto([_envio("6", "AGOSTO", 6, "111111111111", importe="1000.00", dif="200.00", tax="80.00")],
                          pagos=[], saldo_2025="0.00")
    r2 = imp.procesar(mas_tax, aplicar=True, actor="test")
    assert r2["acciones"].get("AJUSTE_AGREGADO") == 1 and _saldo(db) == Decimal("1280.00")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT version, diferencia_flete_ars, tax_cliente_ars FROM conciliaciones_envio ORDER BY version")
        versiones = [(r["version"], Decimal(r["diferencia_flete_ars"]), Decimal(r["tax_cliente_ars"])) for r in cur.fetchall()]
    # Versiones acumulativas, como la conciliación courier.
    assert versiones == [(1, Decimal("200"), Decimal("30")), (2, Decimal("200"), Decimal("80"))]


@requiere_postgres
def test_cuenta_del_cliente_muestra_solo_lo_nuevo_de_cada_version(db, monkeypatch):
    from servicios import cuenta_corriente
    monkeypatch.setattr(cuenta_corriente, "get_conn", db)
    primero = _manifiesto([_envio("6", "MAYO", 6, "111111111111", importe="1000.00", dif="200.00", tax="30.00")],
                          pagos=[], saldo_2025="0.00")
    imp.procesar(primero, aplicar=True, actor="test")
    segundo = _manifiesto([_envio("6", "MAYO", 6, "111111111111", importe="1000.00", dif="250.00", tax="80.00")],
                          pagos=[], saldo_2025="0.00")
    imp.procesar(segundo, aplicar=True, actor="test")
    items = cuenta_corriente.movimientos_cuenta_paginados("MELCIOR", "consolidado", "todos", 1, 50)["items"]
    por_concepto = sorted((i["concepto"], i["debe_ars"]) for i in items if i["debe_ars"])
    # La diferencia (200 + 50) va en la fila del flete; el TAX, por versión, aparte.
    assert por_concepto == [("Flete", Decimal("1250.00")), ("TAX", Decimal("30.00")), ("TAX", Decimal("50.00"))]
    flete = next(i for i in items if i["concepto"] == "Flete")
    assert flete["diferencia_detalle"]["valor_inicial_ars"] == Decimal("1000.00")
    assert flete["diferencia_detalle"]["diferencia_ars"] == Decimal("250.00")
    mes = cuenta_corriente.resumen_mensual_cuenta("MELCIOR", "2026-05")
    assert Decimal(str(mes["tax_ars"])) == Decimal("80.00")
    assert Decimal(str(mes["diferencias_ars"])) == Decimal("250.00")
    assert Decimal(str(mes["total_mes_ars"])) == Decimal("1330.00")
    assert _saldo(db) == Decimal("1330.00")


@requiere_postgres
def test_pagos_corridos_en_la_planilla_no_se_duplican(db):
    def pago(fila, fecha, monto):
        return {"source_key": f"MELCIOR-2026:PAGO:{fila}", "fila_cliente": fila, "fecha": fecha, "monto_ars": monto,
                "referencia": "", "detalle_fuente": "", "fecha_original_informada": True}
    envio = [_envio("6", "MAYO", 6, "111111111111", importe="5000.00")]
    imp.procesar(_manifiesto(envio, pagos=[pago(10, "2026-03-01", "100.00"), pago(11, "2026-04-01", "200.00")],
                             saldo_2025="0.00"), aplicar=True, actor="test")
    # Se intercala un pago en la fila 11: el de abril pasa a la fila 12.
    corrido = [pago(10, "2026-03-01", "100.00"), pago(11, "2026-03-15", "50.00"), pago(12, "2026-04-01", "200.00")]
    r = imp.procesar(_manifiesto(envio, pagos=corrido, saldo_2025="0.00"), aplicar=True, actor="test")
    assert r["pagos"]["existentes"] == 2 and [p["monto_ars"] for p in r["pagos"]["nuevos"]] == ["50.00"]
    assert not r["pagos"]["conflictos"] and r["diferencia_no_explicada_ars"] == "0.00"
    assert _saldo(db) == Decimal("4650.00")
    otra = imp.procesar(_manifiesto(envio, pagos=corrido, saldo_2025="0.00"), aplicar=True, actor="test")
    assert otra["pagos"]["existentes"] == 3 and not otra["pagos"]["nuevos"]
    # Un importe corregido en la planilla no agrega nada: se informa.
    editado = [pago(10, "2026-03-01", "100.00"), pago(11, "2026-03-15", "60.00"), pago(12, "2026-04-01", "200.00"),
               pago(13, "2026-05-01", "10.00")]
    r3 = imp.procesar(_manifiesto(envio, pagos=editado, saldo_2025="0.00"), aplicar=True, actor="test")
    assert not r3["pagos"]["nuevos"] and len(r3["pagos"]["conflictos"]) == 2
    assert _saldo(db) == Decimal("4650.00")


@requiere_postgres
def test_mismo_tracking_con_otro_destinatario_es_conflicto(db):
    imp.procesar(_manifiesto([_fila(6, "111111111111", "ANA", "1000.00")], pagos=[], saldo_2025="0.00"),
                 aplicar=True, actor="test")
    r = imp.procesar(_manifiesto([_fila(9, "111111111111", "OTRA PERSONA", "1000.00")], pagos=[], saldo_2025="0.00"),
                     aplicar=True, actor="test")
    assert r["acciones"] == {"CONFLICTO": 1}
    assert "revisar el tracking" in r["conflictos"][0]["motivo"]


@requiere_postgres
def test_costo_de_tauro_da_margen_real_y_el_cliente_no_ve_notas_internas(db):
    e = _envio("6", "MAYO", 6, "111111111111", importe="1000.00", dif="200.00", tax="30.00",
               costo_courier_ars="900.00", requiere_revision=True)
    imp.procesar(_manifiesto([e], pagos=[], saldo_2025="0.00"), aplicar=True, actor="test")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT costo_courier_real_ars, margen_tauro_protegido_ars, precio_cliente_final_ars "
                    "FROM conciliaciones_envio")
        c = cur.fetchone()
        assert (Decimal(c["costo_courier_real_ars"]), Decimal(c["margen_tauro_protegido_ars"])) == (Decimal("900"), Decimal("330"))
        cur.execute("SELECT observaciones FROM solicitudes_guia")
        assert "revisión" not in cur.fetchone()["observaciones"].lower()


@requiere_postgres
def test_conflicto_y_repetido_no_se_tocan(db):
    imp.procesar(_manifiesto([_envio("6", "MAYO", 6, "111111111111", importe="1000.00")], pagos=[],
                             saldo_2025="0.00"), aplicar=True, actor="test")
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE envios SET monto_ars=1500")
    repetido = _envio("7", "MAYO", 7, "111111111111", importe="900.00", visible_cliente=False,
                      tracking_repetido=True, crear_si_falta=False)
    r = imp.procesar(_manifiesto([_envio("6", "MAYO", 6, "111111111111", importe="1000.00"), repetido],
                                 pagos=[], saldo_2025="0.00"), aplicar=True, actor="test")
    assert r["acciones"] == {"CONFLICTO": 1, "REPETIDO_NO_CARGADO": 1}
    assert "cobra 1500.00" in r["conflictos"][0]["motivo"]
    assert _saldo(db) == Decimal("1500.00")


@requiere_postgres
def test_tracking_de_otro_cliente_es_conflicto(db):
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO solicitudes_guia (cliente_id,estado,producto_alias,destino_pais,dest_nombre,dest_direccion,dest_ciudad,dest_zip,tracking,courier,ambito) "
                    "VALUES ('OTRO','DESPACHADO','Caja','US','X','','','','111111111111','FEDEX','INTERNACIONAL')")
    r = imp.procesar(_manifiesto([_envio("6", "MAYO", 6, "111111111111")], pagos=[], saldo_2025="0.00"),
                     aplicar=True, actor="test")
    assert r["acciones"] == {"CONFLICTO": 1}
    assert "otro cliente" in r["conflictos"][0]["motivo"]


@requiere_postgres
def test_pagos_del_portal_fuera_de_planilla_frenan_pagos_nuevos(db):
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pagos (cliente_id,fecha,monto_ars,metodo,estado,idempotency_key) "
                    "VALUES ('MELCIOR','2026-09-10',300,'Transferencia','APROBADO',%s) RETURNING id", ("p" * 40,))
        pago_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO pagos_aplicaciones (pago_id,ambito,monto_ars,estado) VALUES (%s,'INTERNACIONAL',300,'APLICADA')",
                    (pago_id,))
    r = imp.procesar(_manifiesto([_envio("6", "MAYO", 6, "111111111111")]), aplicar=True, actor="test")
    assert not r["pagos"]["nuevos"] and len(r["pagos"]["conflictos"]) == 1
    assert r["diferencia_no_explicada_ars"] == "0.00"


@requiere_postgres
def test_prete_tax_sin_flete_y_repeticion_contable(db):
    def pr(clave, fila, tracking, **kw):
        e = _envio(clave, "JUNIO", fila, tracking, **kw)
        e["source_key"] = f"PRETE-ROSSO-2026:JUNIO:{clave}"
        e["remitente"] = "PRETE ROSSO"
        return e
    envios = [
        pr("6:1", 6, "444444444444", importe="700.00"),
        pr("7:2", 7, "444444444444", importe="700.00", visible_cliente=False, tracking_repetido=True),
        pr("TAX:9", 9, "555555555555", importe="0.00", tax="40.00", visible_cliente=False,
           tipo_fuente="TAX_SIN_FLETE", genera_deuda=False),
    ]
    m = _manifiesto(envios, pagos=[], saldo_2025="0.00", cliente="PRETE ROSSO")
    r = imp.procesar(m, aplicar=True, actor="test")
    assert r["acciones"]["ENVIO_NUEVO"] == 3 and r["acciones"]["CARGO_ANCLA"] == 1
    assert _saldo(db, "PRETE ROSSO") == Decimal("1440.00")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT tracking, visible_cliente FROM solicitudes_guia ORDER BY id")
        filas = [(f["tracking"], f["visible_cliente"]) for f in cur.fetchall()]
    assert filas == [("444444444444", True), (None, False), ("555555555555", False)]


def _fila(fila, tracking, nombre, importe):
    """Fila MARZO:<fila> de un envío identificado por tracking y destinatario."""
    return _envio(str(fila), "MARZO", fila, tracking, importe=importe, destinatario=nombre)


@requiere_postgres
def test_filas_borradas_en_la_planilla_no_cruzan_envios(db):
    """Borrar una fila corre las claves de abajo: cada fila debe seguir
    encontrando su propio envío por tracking y nunca tocar el de otra."""
    base = [_fila(6, "111111111111", "ANA", "1000.00"), _fila(7, "222222222222", "BETO", "500.00"),
            _fila(8, "333333333333", "CARLA", "2000.00"), _fila(9, "444444444444", "DANI", "3000.00")]
    imp.procesar(_manifiesto(base, pagos=[], saldo_2025="0.00"), aplicar=True, actor="test")
    assert _saldo(db) == Decimal("6500.00")
    # Se borra la fila 7 (BETO): CARLA pasa a la 7 y DANI a la 8.
    sin_beto = [_fila(6, "111111111111", "ANA", "1000.00"), _fila(7, "333333333333", "CARLA", "2000.00"),
                _fila(8, "444444444444", "DANI", "3000.00")]
    m = _manifiesto(sin_beto, pagos=[], saldo_2025="0.00")
    r = imp.procesar(m, aplicar=True, actor="test")
    assert r["acciones"] == {"ENVIO_EXISTENTE": 1, "ENVIO_ADOPTADA": 2, "SIN_CAMBIOS": 3}
    assert not r["conflictos"] and not r["repetidos_no_cargados"]
    assert _saldo(db) == Decimal("6500.00")  # nada se agregó al envío de BETO
    assert Decimal(r["movimientos_portal_fuera_de_planilla_ars"]) == Decimal("500.00")
    assert r["diferencia_no_explicada_ars"] == "0.00"
    assert imp.procesar(m, aplicar=True, actor="test")["acciones"]["SIN_CAMBIOS"] == 3


@requiere_postgres
def test_fila_insertada_arriba_crea_con_clave_alterna(db):
    imp.procesar(_manifiesto([_fila(6, "111111111111", "ANA", "1000.00"), _fila(7, "222222222222", "BETO", "500.00")],
                             pagos=[], saldo_2025="0.00"), aplicar=True, actor="test")
    nuevo = [_fila(6, "999999999999", "NUEVA", "300.00"), _fila(7, "111111111111", "ANA", "1000.00"),
             _fila(8, "222222222222", "BETO", "500.00")]
    m = _manifiesto(nuevo, pagos=[], saldo_2025="0.00")
    r = imp.procesar(m, aplicar=True, actor="test")
    assert r["acciones"]["ENVIO_NUEVO"] == 1 and r["acciones"]["ENVIO_ADOPTADA"] == 2
    assert _saldo(db) == Decimal("1800.00") and r["diferencia_no_explicada_ars"] == "0.00"
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT api_referencia FROM solicitudes_guia WHERE tracking='999999999999'")
        assert cur.fetchone()["api_referencia"] == "MELCIOR-2026:MARZO:6#999999999999"
    otra = imp.procesar(m, aplicar=True, actor="test")
    assert otra["acciones"]["SIN_CAMBIOS"] == 3 and "ENVIO_NUEVO" not in otra["acciones"]
    assert _saldo(db) == Decimal("1800.00")


@requiere_postgres
def test_tracking_corregido_es_conflicto_y_no_duplica(db):
    imp.procesar(_manifiesto([_fila(6, "111111111111", "ANA", "1000.00")], pagos=[], saldo_2025="0.00"),
                 aplicar=True, actor="test")
    r = imp.procesar(_manifiesto([_fila(6, "111111111119", "ANA", "1000.00")], pagos=[], saldo_2025="0.00"),
                     aplicar=True, actor="test")
    assert r["acciones"] == {"CONFLICTO": 1}
    assert "no está en la planilla" in r["conflictos"][0]["motivo"]
    assert _saldo(db) == Decimal("1000.00")


def test_admin_exige_confirmacion_y_huella(monkeypatch):
    from endpoints import admin

    monkeypatch.setattr(admin, "_is_auth", lambda _t: True)
    capturado = {}

    def respuesta(*, request, name, context, status_code=200):
        capturado.update(context=context, status=status_code)
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(admin.templates, "TemplateResponse", respuesta)
    llamadas = []
    monkeypatch.setattr(imp, "procesar", lambda lote, aplicar, actor: llamadas.append(aplicar) or {
        "modo": "IMPORTACION" if aplicar else "VISTA_PREVIA"})
    m = _manifiesto([_envio("6", "MAYO", 6, "123456789012")])
    contenido = json.dumps(m).encode()

    class Archivo:
        filename = "m.json"

        async def read(self, _n):
            return contenido

    def post(**kw):
        datos = dict(accion="previsualizar", confirmacion="", huella="")
        datos.update(kw)
        return asyncio.run(admin.admin_importar_cuenta_cliente(
            request=SimpleNamespace(), manifiesto=Archivo(), admin_token="x", **datos))

    post()
    assert llamadas == [False] and capturado["status"] == 200
    post(accion="importar", confirmacion="IMPORTAR MELCIOR", huella="000000000000")
    assert capturado["status"] == 422 and llamadas == [False]
    post(accion="importar", confirmacion="IMPORTAR MELCIOR", huella=m["manifest_sha256"][:12])
    assert llamadas == [False, True] and capturado["status"] == 200
