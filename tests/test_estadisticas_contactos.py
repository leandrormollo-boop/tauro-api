from contextlib import contextmanager
from datetime import datetime, timezone
import os

import psycopg2
import psycopg2.extras
import pytest

from servicios import estadisticas_contactos as estadisticas


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()


class _Cursor:
    def __init__(self, filas):
        self.filas = filas
        self.sql = None
        self.params = None

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.filas

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@contextmanager
def _conexion(cursor):
    class Conexion:
        @contextmanager
        def cursor(self):
            yield cursor
    yield Conexion()


def _contacto(ident=1, **cambios):
    base = {
        "id": ident, "cliente_id": "MELCIOR", "pais": "AR",
        "nombre": "Ana Pérez", "documento": "DNI 12.345.678",
        "email": "ana@example.com", "direccion": "Av. Córdoba 123",
        "ciudad": "CABA",
    }
    return {**base, **cambios}


def _envio(ident=10, **cambios):
    base = {
        "id": ident, "destino_pais": "AR", "dest_nombre": "ANA PEREZ",
        "dest_documento": "12345678", "dest_email": "",
        "dest_direccion": "Av Cordoba 123", "dest_ciudad": "CABA",
        "dest_zip": "1000", "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }
    return {**base, **cambios}


def test_nacional_apellido_y_ultimo_envio(monkeypatch):
    cursor = _Cursor([
        _envio(10),
        _envio(11, dest_nombre="Ana Pérez", dest_documento="", dest_email="ANA@example.COM",
                created_at=datetime(2026, 9, 3, tzinfo=timezone.utc)),
    ])
    monkeypatch.setattr(estadisticas, "get_conn", lambda: _conexion(cursor))
    contacto = _contacto(nombre="Ana", datos_nacionales={"nombre": "Ana", "apellido": "Pérez"})

    resultado = estadisticas.contar_envios_emitidos_por_contacto("melcior", [contacto])

    assert resultado[1] == {
        "envios_identificados": 2, "porcentaje_relativo": 100,
        "ultimo_envio_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
    }


def test_internacional_normaliza_acentos_y_mayusculas(monkeypatch):
    cursor = _Cursor([_envio(
        destino_pais="us", dest_nombre="ÉLLE MCGILL", dest_documento="us-tax-77",
        dest_direccion="1200 Brickell Ave", dest_ciudad="MIAMI",
    )])
    monkeypatch.setattr(estadisticas, "get_conn", lambda: _conexion(cursor))

    resultado = estadisticas.contar_envios_emitidos_por_contacto("MELCIOR", [_contacto(
        7, pais="US", nombre="Elle McGill", documento="US TAX 77",
        direccion="1200 Brickell Avenue", ciudad="Miami",
    )])

    assert resultado[7]["envios_identificados"] == 1


def test_no_atribuye_nombre_solo_ni_contactos_duplicados(monkeypatch):
    cursor = _Cursor([
        _envio(10, dest_documento="", dest_email="", dest_direccion="Otra 1"),
        _envio(11),
    ])
    monkeypatch.setattr(estadisticas, "get_conn", lambda: _conexion(cursor))
    solo_nombre = _contacto(1, documento="", email="", direccion="Otra 2")
    duplicado = _contacto(2)

    resultado = estadisticas.contar_envios_emitidos_por_contacto(
        "MELCIOR", [solo_nombre, duplicado, _contacto(3)]
    )

    assert {ident: dato["envios_identificados"] for ident, dato in resultado.items()} == {1: 0, 2: 0, 3: 0}


def test_emails_distintos_no_se_fusionan(monkeypatch):
    cursor = _Cursor([_envio(
        dest_documento="", dest_email="anaperez@example.com", dest_direccion="Otra 1",
    )])
    monkeypatch.setattr(estadisticas, "get_conn", lambda: _conexion(cursor))

    resultado = estadisticas.contar_envios_emitidos_por_contacto("MELCIOR", [_contacto(
        email="ana.perez@example.com", documento="", direccion="Otra 2",
    )])

    assert resultado[1]["envios_identificados"] == 0


def test_nombre_unicode_no_latino_se_conserva(monkeypatch):
    cursor = _Cursor([_envio(
        destino_pais="HK", dest_nombre="李小龍", dest_documento="HK-88",
        dest_direccion="1 Nathan Road", dest_ciudad="Hong Kong",
    )])
    monkeypatch.setattr(estadisticas, "get_conn", lambda: _conexion(cursor))

    resultado = estadisticas.contar_envios_emitidos_por_contacto("MELCIOR", [_contacto(
        88, pais="HK", nombre="李小龍", documento="HK88",
        direccion="otra", ciudad="Hong Kong",
    )])

    assert resultado[88]["envios_identificados"] == 1


def test_excluye_contacto_ajeno_y_sql_aisla_y_filtra(monkeypatch):
    cursor = _Cursor([])
    monkeypatch.setattr(estadisticas, "get_conn", lambda: _conexion(cursor))

    resultado = estadisticas.contar_envios_emitidos_por_contacto(
        "MELCIOR", [_contacto(1), _contacto(2, cliente_id="OTRA_CUENTA")]
    )

    assert set(resultado) == {1}
    assert cursor.params == ("MELCIOR",)
    assert "s.cliente_id=%s" in cursor.sql
    assert "s.test=FALSE" in cursor.sql
    assert "s.visible_cliente=TRUE" in cursor.sql
    assert "BTRIM(COALESCE(s.tracking, '')) <> ''" in cursor.sql
    assert "s.estado NOT IN ('CANCELADO', 'REEMPLAZADO')" in cursor.sql
    assert "e.cliente_id=s.cliente_id" in cursor.sql
    assert "e.estado='CANCELADO'" in cursor.sql
    assert "UPDATE " not in cursor.sql


def test_porcentaje_y_solicitudes_inactivas_se_filtran_en_sql(monkeypatch):
    cursor = _Cursor([_envio(10), _envio(11), _envio(
        12, dest_nombre="Bea López", dest_documento="B-9", dest_email="bea@example.com",
        dest_direccion="Calle 9", dest_ciudad="Rosario",
    )])
    monkeypatch.setattr(estadisticas, "get_conn", lambda: _conexion(cursor))
    bea = _contacto(2, nombre="Bea López", documento="B 9", email="bea@example.com",
                    direccion="Calle 9", ciudad="Rosario")

    resultado = estadisticas.contar_envios_emitidos_por_contacto("MELCIOR", [_contacto(1), bea])

    assert resultado[1]["envios_identificados"] == 2
    assert resultado[1]["porcentaje_relativo"] == 100
    assert resultado[2]["envios_identificados"] == 1
    assert resultado[2]["porcentaje_relativo"] == 50


@pytest.mark.skipif(
    not DATABASE_URL, reason="requiere TAURO_TEST_DATABASE_URL aislada"
)
def test_postgres_filtra_emitidos_inactivos_cargos_y_otra_cuenta(monkeypatch):
    """La consulta real conserva sólo snapshots emitidos y privados de MELCIOR."""
    db = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
    )

    @contextmanager
    def conexion_real():
        # No usamos ``with db``: el servicio es de lectura y el rollback final
        # elimina estas tablas temporales y todos sus datos de prueba.
        yield db

    try:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE solicitudes_guia (
                    id integer PRIMARY KEY, cliente_id text NOT NULL,
                    estado text NOT NULL, test boolean NOT NULL,
                    visible_cliente boolean NOT NULL, tracking text,
                    destino_pais text, dest_nombre text, dest_documento text,
                    dest_email text, dest_direccion text, dest_ciudad text,
                    created_at timestamptz NOT NULL
                )
            """)
            cur.execute("""
                CREATE TEMP TABLE envios (
                    solicitud_id integer, cliente_id text, estado text
                )
            """)
            filas = [
                # Las tres únicas emitidas que deben llegar a la tarjeta.
                (1, "MELCIOR", "DESPACHADO", False, True, "T-1"),
                (2, "MELCIOR", "ENTREGADO", False, True, "T-2"),
                (9, "MELCIOR", "GUIA_LISTA", False, True, "T-9"),
                # Historial no disponible para la estadística.
                (3, "MELCIOR", "CANCELADO", False, True, "T-3"),
                (4, "MELCIOR", "REEMPLAZADO", False, True, "T-4"),
                (5, "MELCIOR", "DESPACHADO", True, True, "T-5"),
                (6, "MELCIOR", "DESPACHADO", False, False, "T-6"),
                (7, "MELCIOR", "DESPACHADO", False, True, "  "),
                (8, "OTRA_CUENTA", "DESPACHADO", False, True, "T-8"),
                (10, "MELCIOR", "DESPACHADO", False, True, "T-10"),
            ]
            cur.executemany("""
                INSERT INTO solicitudes_guia (
                    id, cliente_id, estado, test, visible_cliente, tracking,
                    destino_pais, dest_nombre, dest_documento, dest_email,
                    dest_direccion, dest_ciudad, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'AR', 'Ana Pérez',
                          '12345678', '', 'Av Córdoba 123', 'CABA', NOW())
            """, filas)
            # La fila 9 conserva su conteo: ese cargo CANCELADO es de otra
            # cuenta. La 10 sí se excluye por el cargo de la misma cuenta.
            cur.executemany(
                "INSERT INTO envios (solicitud_id, cliente_id, estado) VALUES (%s, %s, %s)",
                [(9, "OTRA_CUENTA", "CANCELADO"), (10, "MELCIOR", "CANCELADO")],
            )
        monkeypatch.setattr(estadisticas, "get_conn", lambda: conexion_real())

        resultado = estadisticas.contar_envios_emitidos_por_contacto(
            "MELCIOR", [_contacto()]
        )

        assert resultado[1]["envios_identificados"] == 3
        assert resultado[1]["porcentaje_relativo"] == 100
    finally:
        db.rollback()
        db.close()
