"""Controles admin para operar la cuenta corriente sin mezclar ámbitos."""

import asyncio
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from endpoints import admin
from servicios import cuenta_corriente


IDEMPOTENCY_KEY = "b" * 43


@pytest.fixture
def admin_autenticado(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)

    async def archivo_vacio(_archivo):
        return b""

    monkeypatch.setattr(cuenta_corriente, "leer_comprobante_con_tope", archivo_vacio)
    return SimpleNamespace()


def test_aplicaciones_preservan_decimal_y_permiten_credito_remanente():
    aplicaciones = admin._aplicaciones_pago_form(
        "100.000,50",
        "DIVIDIR",
        "30.000,25",
        "60.000,25",
    )

    assert aplicaciones == {
        "NACIONAL": Decimal("30000.25"),
        "INTERNACIONAL": Decimal("60000.25"),
    }
    assert sum(aplicaciones.values()) == Decimal("90000.50")


def test_division_que_supera_el_pago_falla_cerrada():
    with pytest.raises(ValueError, match="no puede superar"):
        admin._aplicaciones_pago_form("100.000", "DIVIDIR", "60.000", "50.000")


def test_cargo_manual_exige_y_envia_ambito(monkeypatch, admin_autenticado):
    guardado = []
    monkeypatch.setattr(admin, "registrar_envio", lambda **datos: guardado.append(datos))

    respuesta = asyncio.run(admin.admin_envio_nuevo(
        request=admin_autenticado,
        cliente_id="melcior",
        fecha="2026-08-17",
        nro_fc="0001-42",
        monto_ars="100.000,25",
        ambito="NACIONAL",
        idempotency_key=IDEMPOTENCY_KEY,
        descripcion="Cargo manual",
        tracking="ABC",
        estado="ACTIVO",
        factura_pdf=None,
        admin_token="token",
    ))

    assert respuesta.status_code == 303
    assert guardado[0]["cliente_id"] == "MELCIOR"
    assert guardado[0]["ambito"] == "NACIONAL"
    assert guardado[0]["monto_ars"] == Decimal("100000.25")
    assert guardado[0]["idempotency_key"] == IDEMPOTENCY_KEY


def test_cargo_manual_rechaza_ambito_inventado(monkeypatch, admin_autenticado):
    guardado = []
    monkeypatch.setattr(admin, "registrar_envio", lambda **datos: guardado.append(datos))
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(
        admin.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context
        ),
    )

    respuesta = asyncio.run(admin.admin_envio_nuevo(
        request=admin_autenticado,
        cliente_id="MELCIOR",
        fecha="2026-08-17",
        nro_fc="",
        monto_ars="100.000",
        ambito="OTRO",
        idempotency_key=IDEMPOTENCY_KEY,
        descripcion="",
        tracking="",
        estado="ACTIVO",
        factura_pdf=None,
        admin_token="token",
    ))

    assert respuesta.status_code == 200
    assert "Nacional o Internacional" in respuesta.context["flash_error"]
    assert respuesta.context["form_data"]["monto_ars"] == "100.000"
    assert guardado == []


def test_admin_factura_cargo_legacy_redirige_al_lote_sin_escribir_envios(
    monkeypatch, admin_autenticado,
):
    cargo = {
        "id": 77,
        "cliente_id": "MELCIOR",
        "estado": "ACTIVO",
        "nro_fc": "",
        "monto_ars": Decimal("150000.00"),
        "ambito": "INTERNACIONAL",
    }
    llamadas = []

    monkeypatch.setattr(admin, "_cargo_para_facturar", lambda *_a: cargo)

    async def leer_pdf(_archivo):
        return b"%PDF-1.4 factura"

    monkeypatch.setattr(cuenta_corriente, "leer_comprobante_con_tope", leer_pdf)
    monkeypatch.setattr(
        admin,
        "facturar_cargo",
        lambda **datos: llamadas.append(datos) or {**cargo, "nro_fc": "FC-100"},
    )

    respuesta = asyncio.run(admin.admin_facturar_cargo(
        request=admin_autenticado,
        cliente_id="melcior",
        envio_id=77,
        nro_fc="FC-100",
        factura_pdf=SimpleNamespace(filename="fc-100.pdf"),
        admin_token="token",
    ))

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == (
        "/admin/clientes/MELCIOR/facturas/nueva?envio=77"
    )
    assert llamadas == []


def test_admin_no_permite_cancelar_una_fc_facturada(monkeypatch, admin_autenticado):
    monkeypatch.setattr(admin, "cancelar_envio", lambda *_a, **_k: False)

    respuesta = admin.admin_envio_cancelar(77, admin_token="token")

    assert respuesta.status_code == 409
    assert "nota de crédito" in respuesta.body.decode("utf-8")


def test_admin_anula_con_ownership_y_confirma_ocultamiento(
    monkeypatch, admin_autenticado,
):
    llamadas = []
    monkeypatch.setattr(
        admin,
        "cancelar_envio",
        lambda *args, **kwargs: llamadas.append((args, kwargs)) or {
            "id": 77,
            "cliente_id": "WAIMAO",
        },
    )

    respuesta = admin.admin_envio_anular(
        cliente_id="waimao",
        envio_id=77,
        admin_token="token",
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == (
        "/admin/clientes/WAIMAO?ok=envio_anulado"
    )
    assert llamadas == [((77,), {
        "cliente_id": "WAIMAO",
        "actor_tipo": "admin",
        "actor_ref": "admin",
    })]


def test_admin_explica_que_anular_no_borra_la_auditoria():
    plantilla = (
        Path(admin.__file__).resolve().parents[1]
        / "templates" / "admin" / "cliente_detail.html"
    ).read_text(encoding="utf-8")

    assert "Anular prueba" in plantilla
    assert "dejará de mostrarse en el portal del cliente" in plantilla
    assert "seguirá guardado en ADMIN para auditoría" in plantilla
    assert "Oculto del portal" in plantilla


def test_detalle_admin_no_carga_binarios_de_facturas_en_el_listado():
    fuente = Path(admin.__file__).read_text(encoding="utf-8")
    plantilla = (
        Path(admin.__file__).resolve().parents[1]
        / "templates" / "admin" / "cliente_detail.html"
    ).read_text(encoding="utf-8")

    bloque = fuente[
        fuente.index("# Envíos paginados"):
        fuente.index("# Pagos con su imputación")
    ]
    assert "SELECT * FROM envios" not in bloque
    assert "(e.factura_pdf IS NOT NULL) AS tiene_factura_pdf" in bloque
    assert "LEFT JOIN solicitudes_guia s" in bloque
    assert "AS oculto_cliente" in bloque
    assert "e.tiene_factura_pdf" in plantilla


def test_pago_admin_envia_aplicaciones_decimal(monkeypatch, admin_autenticado):
    guardado = []
    monkeypatch.setattr(admin, "registrar_pago", lambda **datos: guardado.append(datos))

    respuesta = asyncio.run(admin.admin_pago_nuevo(
        request=admin_autenticado,
        cliente_id="melcior",
        fecha="2026-08-17",
        monto_ars="100.000",
        idempotency_key=IDEMPOTENCY_KEY,
        metodo="transferencia",
        referencia="TRX-1",
        nota="",
        imputacion="DIVIDIR",
        monto_nacional="40.000",
        monto_internacional="50.000",
        comprobante=None,
        admin_token="token",
    ))

    assert respuesta.status_code == 303
    assert guardado[0]["monto_ars"] == Decimal("100000.00")
    assert guardado[0]["aplicaciones"] == {
        "NACIONAL": Decimal("40000.00"),
        "INTERNACIONAL": Decimal("50000.00"),
    }
    assert guardado[0]["actor_tipo"] == "admin"
    assert guardado[0]["actor_ref"] == "admin"
    assert guardado[0]["idempotency_key"] == IDEMPOTENCY_KEY


def test_admin_exige_clave_idempotencia_y_regenera_si_es_invalida(
    monkeypatch, admin_autenticado,
):
    guardado = []
    monkeypatch.setattr(admin, "registrar_pago", lambda **datos: guardado.append(datos))
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(
        admin.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context
        ),
    )

    respuesta = asyncio.run(admin.admin_pago_nuevo(
        request=admin_autenticado,
        cliente_id="MELCIOR",
        fecha="2026-08-17",
        monto_ars="100",
        idempotency_key="manipulada",
        admin_token="token",
    ))

    assert guardado == []
    assert "no es válida" in respuesta.context["flash_error"]
    assert admin._idempotency_key_form(
        respuesta.context["idempotency_key"]
    ) == respuesta.context["idempotency_key"]


def test_form_admin_anterior_al_deploy_no_devuelve_422(
    monkeypatch, admin_autenticado,
):
    guardado = []
    monkeypatch.setattr(admin, "registrar_envio", lambda **datos: guardado.append(datos))
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(
        admin.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context
        ),
    )

    respuesta = asyncio.run(admin.admin_envio_nuevo(
        request=admin_autenticado,
        cliente_id="MELCIOR",
        fecha="2026-08-17",
        nro_fc="",
        monto_ars="100",
        ambito="NACIONAL",
        admin_token="token",
    ))

    assert respuesta.status_code == 200
    assert "Falta la clave de operación" in respuesta.context["flash_error"]
    assert guardado == []
    assert admin._idempotency_key_form(
        respuesta.context["idempotency_key"]
    ) == respuesta.context["idempotency_key"]


def test_gets_admin_generan_claves_opacas_distintas(monkeypatch, admin_autenticado):
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(
        admin.templates,
        "TemplateResponse",
        lambda *, context, **_kw: SimpleNamespace(context=context),
    )

    cargo = admin.admin_envio_form(admin_autenticado, admin_token="token")
    pago = admin.admin_pago_form(admin_autenticado, admin_token="token")

    clave_cargo = cargo.context["idempotency_key"]
    clave_pago = pago.context["idempotency_key"]
    assert clave_cargo != clave_pago
    assert admin._idempotency_key_form(clave_cargo) == clave_cargo
    assert admin._idempotency_key_form(clave_pago) == clave_pago


@contextmanager
def _conexion_pago(monto):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params=None):
            self.query = query
            self.params = params

        def fetchone(self):
            return {"monto_ars": monto}

    class Conexion:
        def cursor(self):
            return Cursor()

    yield Conexion()


def test_aprobacion_reemplaza_imputacion_en_llamada_atomica(
    monkeypatch, admin_autenticado,
):
    llamadas = []
    monkeypatch.setattr(admin, "get_conn", lambda: _conexion_pago(Decimal("100000.00")))
    monkeypatch.setattr(
        cuenta_corriente,
        "resolver_pago",
        lambda *args, **kwargs: llamadas.append((args, kwargs)) or True,
    )

    respuesta = admin.admin_resolver_pago(
        request=admin_autenticado,
        pago_id=7,
        decision="aprobar",
        imputacion="DIVIDIR",
        monto_nacional="25.000",
        monto_internacional="75.000",
        admin_token="token",
    )

    assert respuesta.status_code == 303
    assert llamadas == [((7,), {
        "aprobar": True,
        "aplicaciones": {
            "NACIONAL": Decimal("25000.00"),
            "INTERNACIONAL": Decimal("75000.00"),
        },
        "actor_tipo": "admin",
        "actor_ref": "admin",
    })]


def test_decision_desconocida_devuelve_400_sin_mutar(monkeypatch, admin_autenticado):
    llamadas = []
    monkeypatch.setattr(
        cuenta_corriente,
        "resolver_pago",
        lambda *args, **kwargs: llamadas.append((args, kwargs)),
    )

    respuesta = admin.admin_resolver_pago(
        request=admin_autenticado,
        pago_id=7,
        decision="archivar",
        admin_token="token",
    )

    assert respuesta.status_code == 400
    assert llamadas == []


@contextmanager
def _conexion_detalle_cliente():
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params=None):
            self.query = " ".join(query.split())
            self.params = params

        def fetchone(self):
            if "SELECT * FROM clientes" in self.query:
                return {"cliente_id": "MELCIOR", "nombre": "Melcior", "email": "m@example.com"}
            if "SELECT COUNT(*)" in self.query:
                return {"n": 1}
            raise AssertionError(self.query)

        def fetchall(self):
            if "FROM envios" in self.query:
                return [{"id": 9, "ambito": "NACIONAL", "tiene_factura_pdf": False}]
            if "FROM pagos p" in self.query:
                return []
            raise AssertionError(self.query)

    class Conexion:
        def cursor(self):
            return Cursor()

    yield Conexion()


def test_detalle_cliente_consume_resumen_canonico_sin_recalcular(
    monkeypatch, admin_autenticado,
):
    resumen = {
        "consolidado": {}, "nacional": {}, "internacional": {},
        "credito_sin_imputar_ars": Decimal("10.00"),
        "cargos_sin_clasificar_ars": Decimal("20.00"),
    }
    consultados = []
    monkeypatch.setattr(admin, "get_conn", _conexion_detalle_cliente)
    monkeypatch.setattr(admin, "describir_pricing", lambda _cliente: "Configurado")
    monkeypatch.setattr(
        cuenta_corriente,
        "resumen_cuenta_por_ambito",
        lambda cliente: consultados.append(cliente) or resumen,
    )
    monkeypatch.setattr(
        admin.templates,
        "TemplateResponse",
        lambda *, context, **_kw: SimpleNamespace(status_code=200, context=context),
    )

    respuesta = admin.admin_cliente_detail(
        request=admin_autenticado,
        cliente_id="melcior",
        admin_token="token",
    )

    assert respuesta.status_code == 200
    assert consultados == ["MELCIOR"]
    assert respuesta.context["cuenta_ambitos"] is resumen
    assert respuesta.context["envios"][0]["ambito"] == "NACIONAL"


def test_clasificar_cargo_pasa_cliente_de_la_ruta_al_servicio(
    monkeypatch, admin_autenticado,
):
    llamadas = []
    monkeypatch.setattr(
        cuenta_corriente,
        "clasificar_cargo_sin_ambito",
        lambda **datos: llamadas.append(datos) or True,
        raising=False,
    )

    respuesta = admin.admin_clasificar_cargo(
        cliente_id="melcior",
        envio_id=17,
        ambito="NACIONAL",
        admin_token="token",
    )

    assert respuesta.status_code == 303
    assert llamadas == [{
        "envio_id": 17,
        "cliente_id": "MELCIOR",
        "ambito": "NACIONAL",
        "actor_tipo": "admin",
        "actor_ref": "admin",
    }]


def test_templates_exponen_ambitos_y_no_mutan_cargos_sin_api():
    raiz = Path(__file__).resolve().parents[1]
    detalle = (raiz / "templates/admin/cliente_detail.html").read_text()
    cargo = (raiz / "templates/admin/envio_form.html").read_text()
    pago = (raiz / "templates/admin/pago_form.html").read_text()
    pendientes = (raiz / "templates/admin/pagos_pendientes.html").read_text()

    assert "Crédito sin imputar" in detalle
    assert "Cargos sin clasificar" in detalle
    assert "SIN CLASIFICAR" in detalle
    assert '"{:,.2f}".format' in detalle
    assert '"{:,.0f}".format' not in detalle
    assert "clasificación segura todavía no está habilitada" in detalle
    assert "/clasificar" in detalle
    assert "p.monto_nacional" in detalle
    assert 'name="ambito" required' in cargo
    assert 'name="idempotency_key" value="{{ idempotency_key }}"' in cargo
    assert "Nota de crédito (NC)" not in cargo
    assert 'name="imputacion" required' in pago
    assert 'name="idempotency_key" value="{{ idempotency_key }}"' in pago
    assert "Sin imputar" in pago
    assert "Aprobar e imputar" in pendientes
