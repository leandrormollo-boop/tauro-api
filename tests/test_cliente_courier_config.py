"""Matriz de autogestión y pricing por cliente + courier."""
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from servicios import carriers
from servicios import configuracion_couriers_cliente as config
from servicios import solicitudes_guia
from endpoints import admin


def _cliente(**cambios):
    base = {
        "cliente_id": "MELCIOR",
        "nombre": "Melcior",
        "activo": True,
        "markup_pct": 25,
        "markup_tipo": "FIJO_ARS",
        "markup_valor": 95_000,
        "puede_emitir": False,
        "puede_recolectar": False,
        "tope_deuda_ars": None,
        "courier_default": "",
    }
    base.update(cambios)
    return base


def _dhl_produccion(monkeypatch):
    monkeypatch.setenv("DHL_ENVIRONMENT", "production")
    monkeypatch.setenv("DHL_API_KEY", "k")
    monkeypatch.setenv("DHL_API_SECRET", "s")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_EXPO", "123456789")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_IMPO", "987654321")


def test_sin_filas_conserva_pricing_pero_no_habilita_ninguna_operacion(monkeypatch):
    _dhl_produccion(monkeypatch)
    matriz = config._armar_matriz(
        _cliente(puede_emitir=True, puede_recolectar=True), [],
    )

    # Todos los permisos son opt-in por cliente, incluida la cotización.
    assert not any(c["puede_cotizar"] for c in matriz["couriers"])
    assert not next(c for c in matriz["couriers"] if c["id"] == "dhl")["puede_emitir"]
    assert not next(c for c in matriz["couriers"] if c["id"] == "fedex")["puede_emitir"]
    assert all(c["pricing"] == {"tipo": "FIJO_ARS", "valor": 95_000}
               for c in matriz["couriers"])


def test_melcior_dhl_mas_14000_no_cambia_fedex(monkeypatch):
    _dhl_produccion(monkeypatch)
    matriz = config._armar_matriz(_cliente(), [{
        "courier": "dhl",
        "puede_cotizar": True,
        "puede_emitir": True,
        "puede_recolectar": False,
        "markup_tipo": "FIJO_ARS",
        "markup_valor": 14_000,
    }])
    por_id = {c["id"]: c for c in matriz["couriers"]}

    assert por_id["dhl"]["pricing"] == {"tipo": "FIJO_ARS", "valor": 14_000}
    assert por_id["dhl"]["puede_emitir"] is True
    assert por_id["fedex"]["pricing"] == {"tipo": "FIJO_ARS", "valor": 95_000}
    assert por_id["fedex"]["puede_emitir"] is False


def test_admin_no_admite_emitir_sin_cotizar_ni_pickup_ups():
    with pytest.raises(ValueError, match="también debe poder cotizar"):
        config.parsear_fila(
            "dhl", puede_cotizar=False, puede_emitir=True,
            puede_recolectar=False, markup_tipo="", markup_valor="",
        )
    with pytest.raises(ValueError, match="no admite recolecciones"):
        config.parsear_fila(
            "ups", puede_cotizar=True, puede_emitir=False,
            puede_recolectar=True, markup_tipo="", markup_valor="",
        )


def test_admin_no_puede_activar_apis_pendientes():
    with pytest.raises(ValueError, match="integración todavía no está habilitada"):
        config.parsear_fila(
            "fedex", puede_cotizar=True, puede_emitir=False,
            puede_recolectar=False, markup_tipo="", markup_valor="",
        )


def test_dhl_se_puede_preconfigurar_pero_no_opera_fuera_de_produccion(monkeypatch):
    monkeypatch.setenv("DHL_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("DHL_API_KEY", "k")
    monkeypatch.setenv("DHL_API_SECRET", "s")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_EXPO", "123456789")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_IMPO", "987654321")

    fila = config.parsear_fila(
        "dhl", puede_cotizar=True, puede_emitir=True,
        puede_recolectar=False, markup_tipo="FIJO_ARS", markup_valor="14000",
    )
    matriz = config._armar_matriz(_cliente(), [fila])
    dhl = next(c for c in matriz["couriers"] if c["id"] == "dhl")
    assert dhl["config_puede_emitir"] is True
    assert dhl["puede_emitir"] is False


def test_dhl_operativo_exige_ambas_cuentas_y_produccion(monkeypatch):
    monkeypatch.setenv("DHL_ENVIRONMENT", "production")
    monkeypatch.setenv("DHL_API_KEY", "k")
    monkeypatch.setenv("DHL_API_SECRET", "s")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_EXPO", "123456789")
    monkeypatch.delenv("DHL_ACCOUNT_NUMBER_IMPO", raising=False)
    monkeypatch.delenv("DHL_ACCOUNT_NUMBER_IMPORT", raising=False)
    assert config.estado_integracion("dhl")["operativa"] is False

    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_IMPO", "987654321")
    assert config.estado_integracion("dhl")["operativa"] is True


def test_dhl_rechaza_cuentas_con_formato_invalido(monkeypatch):
    monkeypatch.setenv("DHL_ENVIRONMENT", "production")
    monkeypatch.setenv("DHL_API_KEY", "k")
    monkeypatch.setenv("DHL_API_SECRET", "s")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_EXPO", "123")
    monkeypatch.setenv("DHL_ACCOUNT_NUMBER_IMPO", "ABCDEFGHI")

    estado = config.estado_integracion("dhl")
    assert estado["operativa"] is False
    assert "válida" in estado["detalle"]


@pytest.mark.parametrize("valor", ["nan", "inf", "-inf"])
def test_admin_rechaza_ganancias_no_finitas(valor, monkeypatch):
    _dhl_produccion(monkeypatch)
    with pytest.raises(ValueError, match="número válido"):
        config.parsear_fila(
            "dhl", puede_cotizar=True, puede_emitir=False,
            puede_recolectar=False, markup_tipo="FIJO_ARS", markup_valor=valor,
        )


def test_courier_bloqueado_no_se_instancia_ni_recibe_get_rates():
    class NoDebeCrearse:
        def __init__(self):
            raise AssertionError("FedEx bloqueado no debe instanciarse")

    class DHLFalso:
        def get_rates(self, *_args, **_kwargs):
            return {
                "encontrado": True, "costo": 100, "moneda": "USD",
                "servicio": "EXPRESS", "dias_estimados": "2",
            }

    registro = [
        {"id": "fedex", "nombre": "FedEx", "logo": "/f.svg",
         "servicio": "Priority", "requisitos": ("FAKE_FEDEX",),
         "cliente": NoDebeCrearse},
        {"id": "dhl", "nombre": "DHL Express", "logo": "/d.svg",
         "servicio": "Express", "requisitos": ("FAKE_DHL",),
         "cliente": DHLFalso},
    ]
    with mock.patch.object(carriers, "CARRIERS", registro), \
         mock.patch.dict(os.environ, {"FAKE_FEDEX": "1", "FAKE_DHL": "1"}, clear=True):
        resultado = carriers.costos_carriers(
            origen={"country": "AR"}, destino={"country": "US"},
            paquete={"peso_kg": 1}, couriers_habilitados={"dhl"},
        )

    por_id = {r["id"]: r for r in resultado}
    assert por_id["fedex"]["estado"] == "no_habilitado"
    assert por_id["dhl"]["estado"] == "cotizado"


def test_cada_courier_aplica_su_ganancia_sin_exponerla():
    class Tarifa:
        def get_rates(self, *_args, **_kwargs):
            return {
                "encontrado": True, "costo": 100, "moneda": "USD",
                "servicio": "EXPRESS", "dias_estimados": "2",
            }

    registro = [
        {"id": courier, "nombre": courier.upper(), "logo": "/x.svg",
         "servicio": "Express", "requisitos": ("FAKE",), "cliente": Tarifa}
        for courier in ("fedex", "dhl")
    ]
    with mock.patch.object(carriers, "CARRIERS", registro), \
         mock.patch.dict(os.environ, {"FAKE": "1"}, clear=True):
        resultado = carriers.cotizar_carriers_cliente(
            origen={"country": "AR"}, destino={"country": "US"},
            paquete={"peso_kg": 1}, dolar=1_000,
            pricing_cliente={"tipo": "PCT", "valor": 25},
            pricing_por_courier={
                "fedex": {"tipo": "FIJO_ARS", "valor": 50_000},
                "dhl": {"tipo": "FIJO_ARS", "valor": 14_000},
            },
            couriers_habilitados={"fedex", "dhl"},
        )

    por_id = {r["id"]: r for r in resultado}
    assert por_id["fedex"]["precio_ars"] == 150_000
    assert por_id["dhl"]["precio_ars"] == 114_000
    assert not any(
        clave.startswith(("costo", "margen", "markup"))
        for fila in resultado for clave in fila
    )


def test_cotizacion_dhl_exige_fila_explicita_y_usa_14000(monkeypatch):
    _dhl_produccion(monkeypatch)
    sin_permiso = config._armar_matriz(_cliente(), [])
    monkeypatch.setattr(config, "obtener_matriz", lambda _cliente_id: sin_permiso)
    assert config.configuracion_cotizacion("MELCIOR")["couriers_habilitados"] == set()

    con_permiso = config._armar_matriz(_cliente(), [{
        "courier": "dhl",
        "puede_cotizar": True,
        "puede_emitir": True,
        "puede_recolectar": False,
        "markup_tipo": "FIJO_ARS",
        "markup_valor": 14_000,
    }])
    monkeypatch.setattr(config, "obtener_matriz", lambda _cliente_id: con_permiso)
    cotizacion = config.configuracion_cotizacion("MELCIOR")
    assert cotizacion["couriers_habilitados"] == {"dhl"}
    assert cotizacion["pricing_por_courier"]["dhl"] == {
        "tipo": "FIJO_ARS", "valor": 14_000,
    }


def test_emision_dhl_fuera_de_produccion_se_bloquea_antes_de_reservar(
    monkeypatch,
):
    monkeypatch.setenv("DHL_ENVIRONMENT", "sandbox")
    ejecutadas = []

    class CursorFalso:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            ejecutadas.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {
                "cliente_id": "MELCIOR", "tracking": None,
                "estado": "SOLICITADO", "precio_tauro_ars": 100_000,
                "courier": "DHL", "activo": True, "puede_emitir": True,
                "tope_deuda_ars": None,
            }

    class ConexionFalsa:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return CursorFalso()

    monkeypatch.setattr(solicitudes_guia, "get_conn", lambda: ConexionFalsa())
    resultado = solicitudes_guia._reservar_credito_cliente(7, "MELCIOR")

    assert resultado["ok"] is False
    assert "DHL no está habilitado en producción" in resultado["error"]
    assert len(ejecutadas) == 1
    assert "UPDATE solicitudes_guia" not in ejecutadas[0][0]


def test_emision_resuelve_permiso_del_courier_dentro_del_lock():
    fuente = inspect.getsource(solicitudes_guia._reservar_credito_cliente)
    assert "LEFT JOIN cliente_courier_config" in fuente
    assert "cc.courier = LOWER" in fuente
    assert "THEN COALESCE(cc.puede_emitir, FALSE)" in fuente
    assert "THEN COALESCE(c.puede_emitir, FALSE)" in fuente  # ENVIA nacional
    assert "LOWER(COALESCE(s.courier, '')) = 'dhl'" in fuente


def test_schema_y_admin_exponen_la_matriz_completa():
    raiz = Path(__file__).resolve().parents[1]
    schema = (raiz / "sql/schema.sql").read_text()
    template = (raiz / "templates/admin/cliente_acceso_precios.html").read_text()
    detalle = (raiz / "templates/admin/cliente_detail.html").read_text()

    assert "CREATE TABLE IF NOT EXISTS cliente_courier_config" in schema
    assert "puede_cotizar     BOOLEAN NOT NULL DEFAULT FALSE" in schema
    assert "ck_cliente_courier_markup" in schema
    for courier in ("fedex", "dhl", "ups"):
        assert f'name="{{{{ c.id }}}}_cotizar"' in template
    assert "Crear guías" in template and "Pickups" in template
    assert "Ganancia de TAURO" in template
    assert "estado_integracion" in template and "detalle_integracion" in template
    assert "/acceso-precios" in detalle


def test_formulario_general_no_puede_escribir_permisos_operativos():
    firma_alta = inspect.signature(admin.admin_cliente_nuevo)
    firma_edicion = inspect.signature(admin.admin_cliente_editar)
    sensibles = {
        "puede_emitir", "puede_recolectar", "tope_deuda_ars", "courier_default",
    }
    assert sensibles.isdisjoint(firma_alta.parameters)
    assert sensibles.isdisjoint(firma_edicion.parameters)

    fuente_alta = inspect.getsource(admin.admin_cliente_nuevo)
    fuente_edicion = inspect.getsource(admin.admin_cliente_editar)
    assert "False, False, None" in fuente_alta
    assert "puede_emitir=%s" not in fuente_edicion
    assert "puede_recolectar=%s" not in fuente_edicion
    assert "tope_deuda_ars=%s" not in fuente_edicion
    assert "courier_default=%s" not in fuente_edicion


def test_post_admin_persiste_dhl_14000_y_auditoria_en_la_misma_transaccion(
    monkeypatch,
):
    _dhl_produccion(monkeypatch)
    ejecutadas = []

    class CursorFalso:
        def __init__(self):
            self._fila = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            ejecutadas.append((" ".join(sql.split()), params))
            self._fila = (
                {"cliente_id": "MELCIOR"}
                if "SELECT cliente_id FROM clientes" in sql else None
            )

        def fetchone(self):
            return self._fila

    class ConexionFalsa:
        def __init__(self):
            self.cursor_obj = CursorFalso()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self.cursor_obj

    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    monkeypatch.setattr(admin, "get_conn", lambda: ConexionFalsa())
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/admin/clientes/MELCIOR/acceso-precios"),
        headers={},
        client=None,
    )

    respuesta = admin.admin_cliente_acceso_precios_guardar(
        request=request,
        cliente_id="melcior",
        fedex_cotizar="", fedex_emitir="", fedex_pickup="",
        fedex_markup_tipo="", fedex_markup_valor="",
        dhl_cotizar="1", dhl_emitir="1", dhl_pickup="",
        dhl_markup_tipo="FIJO_ARS", dhl_markup_valor="14.000",
        ups_cotizar="", ups_emitir="", ups_pickup="",
        ups_markup_tipo="", ups_markup_valor="",
        courier_default="dhl", tope_deuda_ars="500.000",
        admin_token="valido",
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].endswith(
        "/admin/clientes/MELCIOR/acceso-precios?ok=guardado"
    )
    update_cliente = next(
        params for sql, params in ejecutadas
        if "UPDATE clientes SET courier_default" in sql
    )
    assert update_cliente == ("dhl", 500_000.0, "MELCIOR")
    filas = [
        params for sql, params in ejecutadas
        if "INSERT INTO cliente_courier_config" in sql
    ]
    assert filas == [
        ("MELCIOR", "fedex", False, False, False, None, None),
        ("MELCIOR", "dhl", True, True, False, "FIJO_ARS", 14_000.0),
        ("MELCIOR", "ups", False, False, False, None, None),
    ]
    auditorias = [
        params for sql, params in ejecutadas if "INSERT INTO security_audit" in sql
    ]
    assert len(auditorias) == 1
    assert auditorias[0][0] == "admin.configurar_couriers_cliente"
    assert ejecutadas.index(next(e for e in ejecutadas if "INSERT INTO security_audit" in e[0])) \
        > ejecutadas.index(next(e for e in ejecutadas if "INSERT INTO cliente_courier_config" in e[0]))


def test_post_admin_manipulado_no_activa_fedex_ni_escribe(monkeypatch):
    _dhl_produccion(monkeypatch)
    conexiones = []
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    monkeypatch.setattr(admin, "get_conn", lambda: conexiones.append(True))
    monkeypatch.setattr(admin, "obtener_matriz", lambda _cliente_id: {
        **config._armar_matriz(_cliente(), []),
    })
    monkeypatch.setattr(
        admin.templates,
        "TemplateResponse",
        lambda **kwargs: SimpleNamespace(status_code=kwargs["status_code"]),
    )
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/admin/clientes/MELCIOR/acceso-precios"),
        headers={},
        client=None,
    )

    respuesta = admin.admin_cliente_acceso_precios_guardar(
        request=request,
        cliente_id="MELCIOR",
        fedex_cotizar="1", fedex_emitir="", fedex_pickup="",
        fedex_markup_tipo="", fedex_markup_valor="",
        dhl_cotizar="", dhl_emitir="", dhl_pickup="",
        dhl_markup_tipo="", dhl_markup_valor="",
        ups_cotizar="", ups_emitir="", ups_pickup="",
        ups_markup_tipo="", ups_markup_valor="",
        courier_default="", tope_deuda_ars="",
        admin_token="valido",
    )

    assert respuesta.status_code == 422
    assert conexiones == []


def test_roundtrip_admin_post_get_relee_permisos_y_default(monkeypatch):
    _dhl_produccion(monkeypatch)

    class BaseFalsa:
        def __init__(self):
            self.cliente = _cliente(courier_default="", tope_deuda_ars=None)
            self.configs = {}
            self.auditorias = []

        def conexion(self):
            base = self

            class Cursor:
                def __init__(self):
                    self.una = None
                    self.varias = []

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def execute(self, sql, params=None):
                    compacto = " ".join(sql.split())
                    self.una, self.varias = None, []
                    if "SELECT cliente_id FROM clientes" in compacto:
                        self.una = {"cliente_id": base.cliente["cliente_id"]}
                    elif "UPDATE clientes SET courier_default" in compacto:
                        default, tope, _cliente_id = params
                        base.cliente["courier_default"] = default
                        base.cliente["tope_deuda_ars"] = tope
                    elif "INSERT INTO cliente_courier_config" in compacto:
                        (_cliente_id, courier, cotizar, emitir, pickup,
                         tipo, valor) = params
                        base.configs[courier] = {
                            "courier": courier,
                            "puede_cotizar": cotizar,
                            "puede_emitir": emitir,
                            "puede_recolectar": pickup,
                            "markup_tipo": tipo,
                            "markup_valor": valor,
                        }
                    elif "INSERT INTO security_audit" in compacto:
                        base.auditorias.append(params)
                    elif "SELECT cliente_id, nombre, activo" in compacto:
                        self.una = dict(base.cliente)
                    elif "SELECT courier, puede_cotizar" in compacto:
                        self.varias = list(base.configs.values())
                    else:
                        raise AssertionError(f"SQL no contemplado: {compacto}")

                def fetchone(self):
                    return self.una

                def fetchall(self):
                    return self.varias

            class Conexion:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def cursor(self):
                    return Cursor()

            return Conexion()

    base = BaseFalsa()
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    monkeypatch.setattr(admin, "get_conn", base.conexion)
    monkeypatch.setattr(config, "get_conn", base.conexion)
    request_post = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/admin/clientes/MELCIOR/acceso-precios"),
        headers={}, client=None,
    )
    respuesta = admin.admin_cliente_acceso_precios_guardar(
        request=request_post, cliente_id="MELCIOR",
        fedex_cotizar="", fedex_emitir="", fedex_pickup="",
        fedex_markup_tipo="", fedex_markup_valor="",
        dhl_cotizar="1", dhl_emitir="1", dhl_pickup="1",
        dhl_markup_tipo="FIJO_ARS", dhl_markup_valor="14000",
        ups_cotizar="", ups_emitir="", ups_pickup="",
        ups_markup_tipo="", ups_markup_valor="",
        courier_default="dhl", tope_deuda_ars="250000",
        admin_token="valido",
    )
    assert respuesta.status_code == 303

    capturado = {}

    def template_response(**kwargs):
        capturado.update(kwargs["context"])
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(admin.templates, "TemplateResponse", template_response)
    request_get = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/admin/clientes/MELCIOR/acceso-precios"),
        headers={}, client=None,
    )
    get_respuesta = admin.admin_cliente_acceso_precios_form(
        request=request_get, cliente_id="MELCIOR", admin_token="valido",
    )

    assert get_respuesta.status_code == 200
    dhl = next(c for c in capturado["matriz"]["couriers"] if c["id"] == "dhl")
    assert dhl["config_puede_cotizar"] is True
    assert dhl["config_puede_emitir"] is True
    assert dhl["config_puede_recolectar"] is True
    assert dhl["puede_emitir"] is True
    assert dhl["pricing"] == {"tipo": "FIJO_ARS", "valor": 14_000.0}
    assert capturado["matriz"]["courier_default_configurado"] == "dhl"
    assert capturado["matriz"]["tope_deuda_ars"] == 250_000.0
    assert len(base.auditorias) == 1
