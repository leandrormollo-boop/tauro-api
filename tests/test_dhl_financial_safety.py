"""Guardas financieras/operativas del piloto DHL, sin llamadas externas."""
from contextlib import contextmanager
from unittest import mock

import pytest

from servicios import solicitudes_guia as sg


def test_limite_credito_usa_un_snapshot_y_no_duplica_cargo_ya_asentado(monkeypatch):
    consultas = []

    class Cursor:
        def __init__(self):
            self.respuestas = iter([
                {
                    "cliente_id": "WAIMAO",
                    "tracking": None,
                    "estado": "SOLICITADO",
                    "precio_tauro_ars": 95_000,
                    "courier": "DHL", "ambito": "INTERNACIONAL",
                    "remitente_pais": "AR", "destino_pais": "US",
                    "activo": True,
                    "puede_emitir": True,
                    "tope_deuda_ars": 250_000,
                },
                {"deuda": 100_000, "reservado": 80_000},
            ])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, sql, params=None):
            consultas.append((" ".join(sql.split()), params))

        def fetchone(self):
            return next(self.respuestas)

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    @contextmanager
    def conexion():
        yield Conn()

    monkeypatch.setattr(sg, "get_conn", conexion)
    monkeypatch.setattr(
        "servicios.configuracion_couriers_cliente.estado_integracion",
        lambda _courier: {"operativa": True},
    )

    salida = sg._reservar_credito_cliente(9, "WAIMAO")

    assert not salida["ok"]
    assert "275.000" in salida["error"]
    sql_credito, params_credito = consultas[1]
    assert "SELECT SUM(e.monto_ars)" in sql_credito
    assert "SELECT SUM(p.monto_ars)" in sql_credito
    assert "NOT EXISTS" in sql_credito
    assert "e2.solicitud_id=s2.id" in sql_credito
    assert params_credito == ("WAIMAO", "WAIMAO", "WAIMAO", 9)
    assert not any("SET estado='EMITIENDO'" in sql for sql, _ in consultas)


def test_excepcion_previa_a_recotizar_libera_reserva_y_no_emite(monkeypatch):
    monkeypatch.setattr(sg, "_reservar_credito_cliente", lambda *_: {"ok": True})
    monkeypatch.setattr(
        sg,
        "obtener_solicitud",
        lambda sid: {"id": sid, "cliente_id": "WAIMAO", "courier": "DHL"},
    )
    monkeypatch.setattr(
        sg,
        "_recotizar_dhl_antes_de_emitir",
        lambda _sol: (_ for _ in ()).throw(ValueError("bulto legacy invalido")),
    )
    liberar = mock.Mock()
    monkeypatch.setattr(sg, "_liberar_reserva", liberar)
    monkeypatch.setattr(
        sg,
        "generar_guia",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no debe llamar al courier")
        ),
    )

    salida = sg.emitir_guia_como_cliente(12, "WAIMAO")

    assert not salida["ok"]
    assert "No emitimos ni cobramos" in salida["error"]
    liberar.assert_called_once_with(12)


def test_cuenta_desactivada_no_puede_emitir_con_sesion_vieja(monkeypatch):
    consultas = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, params=None): consultas.append(" ".join(sql.split()))
        def fetchone(self):
                return {"cliente_id": "WAIMAO", "tracking": None,
                        "estado": "SOLICITADO", "precio_tauro_ars": 95_000,
                        "courier": "DHL", "ambito": "INTERNACIONAL",
                        "remitente_pais": "AR", "destino_pais": "US",
                        "activo": False, "puede_emitir": True,
                    "tope_deuda_ars": None}

    class Conn:
        def cursor(self): return Cursor()
        def commit(self): pass

    @contextmanager
    def conexion(): yield Conn()

    monkeypatch.setattr(sg, "get_conn", conexion)

    salida = sg._reservar_credito_cliente(3, "WAIMAO")

    assert not salida["ok"] and "desactivada" in salida["error"]
    assert not any("SET estado='EMITIENDO'" in sql for sql in consultas)


def test_conciliar_guia_real_pasa_por_guardado_y_cargo(monkeypatch):
    class Cursor:
        consulta = ""
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, _params=None): self.consulta = " ".join(sql.split())
        def fetchone(self):
            if "SELECT id FROM solicitudes_guia" in self.consulta:
                return None
            return {"courier": "DHL", "courier_message_reference": "ref-9"}

    class Conn:
        def cursor(self): return Cursor()

    @contextmanager
    def conexion(): yield Conn()

    guardar = mock.Mock()
    monkeypatch.setattr(sg, "get_conn", conexion)
    monkeypatch.setattr(sg, "guardar_guia_generada", guardar)

    salida = sg.resolver_verificacion_courier(
        9, "CREADA", "DHL-TRACK-9", b"%PDF-1.4 etiqueta",
    )

    assert salida["ok"] and salida["estado"] == "GUIA_LISTA"
    guardar.assert_called_once_with(
        9, "DHL-TRACK-9", b"%PDF-1.4 etiqueta",
        courier="DHL", message_reference="ref-9",
    )


def test_conciliar_rechaza_tracking_ya_usado_en_el_mismo_courier(monkeypatch):
    class Cursor:
        consulta = ""
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, _params=None): self.consulta = " ".join(sql.split())
        def fetchone(self):
            if "SELECT id FROM solicitudes_guia" in self.consulta:
                return {"id": 4}
            return {"courier": "DHL", "courier_message_reference": "ref-9"}

    class Conn:
        def cursor(self): return Cursor()

    @contextmanager
    def conexion(): yield Conn()

    guardar = mock.Mock()
    monkeypatch.setattr(sg, "get_conn", conexion)
    monkeypatch.setattr(sg, "guardar_guia_generada", guardar)

    salida = sg.resolver_verificacion_courier(
        9, "CREADA", "DHL-REPETIDO", b"%PDF-1.4 etiqueta",
    )

    assert not salida["ok"] and "otra solicitud" in salida["error"]
    guardar.assert_not_called()


def test_estado_generico_no_puede_sacar_una_guia_de_verificacion(monkeypatch):
    consultas = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, _params=None): consultas.append(" ".join(sql.split()))
        def fetchone(self): return None

    class Conn:
        def cursor(self): return Cursor()

    @contextmanager
    def conexion(): yield Conn()

    monkeypatch.setattr(sg, "get_conn", conexion)

    with pytest.raises(ValueError, match="conciliación"):
        sg.actualizar_solicitud_guia(9, estado="GUIA_LISTA", tracking="DHL-9")

    assert "estado NOT IN ('EMITIENDO', 'VERIFICAR_COURIER')" in consultas[0]


def test_recuperar_label_no_modifica_tracking_estado_ni_cargo(monkeypatch):
    consultas = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, params=None): consultas.append((" ".join(sql.split()), params))
        def fetchone(self): return {"tracking": "DHL-9"}

    class Conn:
        def cursor(self): return Cursor()

    @contextmanager
    def conexion(): yield Conn()

    monkeypatch.setattr(sg, "get_conn", conexion)
    salida = sg.adjuntar_label_guia(9, b"%PDF-1.4 etiqueta")

    assert salida == {"ok": True, "tracking": "DHL-9"}
    sql = consultas[0][0]
    assert "SET label_pdf=" in sql
    assert "tracking=" not in sql and "estado=" not in sql and "cargo_" not in sql


def test_emitiendo_stale_sin_referencia_se_puede_liberar_sin_courier(monkeypatch):
    consultas = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def execute(self, sql, params=None): consultas.append((" ".join(sql.split()), params))
        def fetchone(self): return {"id": 9}

    class Conn:
        def cursor(self): return Cursor()

    @contextmanager
    def conexion(): yield Conn()

    monkeypatch.setattr(sg, "get_conn", conexion)
    salida = sg.liberar_reserva_sin_operacion_courier(9)

    assert salida == {"ok": True, "estado": "SOLICITADO"}
    sql, params = consultas[0]
    assert "estado='EMITIENDO'" in sql
    assert "courier_message_reference IS NULL" in sql
    assert "INTERVAL '10 minutes'" in sql
    assert params == (9,)
