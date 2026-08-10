"""Mis clientes: base privada, prellenado y mutaciones seguras."""
from contextlib import contextmanager
import inspect
from pathlib import Path

import endpoints.portal_cliente as pc
import servicios.direcciones as dd


RAIZ = Path(__file__).resolve().parent.parent


def _request(path="/portal/envios/nuevo"):
    from starlette.requests import Request

    request = Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
        "root_path": "",
    })
    request.state.csp_nonce = "test"
    return request


def _destinatario():
    return {
        "id": 77,
        "tipo": dd.TIPO_DESTINATARIO,
        "alias": "Elle",
        "label": "Elle",
        "nombre": "Elle McGill",
        "documento": "US-TAX-77",
        "email": "elle@example.com",
        "telefono": "+1 305 555 0101",
        "direccion": "1200 Brickell Ave",
        "ciudad": "Miami",
        "estado": "FL",
        "cp": "33131",
        "pais": "US",
        "notas": "Recepción de 9 a 17",
    }


def _preparar_form(monkeypatch, direccion):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kwargs: kwargs)
    monkeypatch.setattr(pc, "obtener_direccion", lambda cliente, did, tipo=None: direccion)
    monkeypatch.setattr(pc, "get_productos", lambda cliente: [])
    monkeypatch.setattr(pc, "_paises_con_nacional", lambda: [("AR", "Argentina"), ("US", "Estados Unidos")])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda cliente: {
        "nombre": "Melcior", "direccion": "Av. Córdoba 1", "ciudad": "CABA",
        "cp": "1000", "pais": "AR",
    })
    monkeypatch.setattr(pc, "listar_direcciones", lambda cliente, tipo: [direccion] if direccion and tipo == dd.TIPO_DESTINATARIO else [])
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda cliente: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda cliente: "FEDEX")


def test_inicio_desde_cliente_propietario_precarga_la_ficha(monkeypatch):
    direccion = _destinatario()
    llamadas = []
    _preparar_form(monkeypatch, direccion)

    def obtener(cliente, did, tipo=None):
        llamadas.append((cliente, did, tipo))
        return direccion

    monkeypatch.setattr(pc, "obtener_direccion", obtener)
    respuesta = pc.envio_nuevo_form(
        _request(), destinatario_id=77, cliente="MELCIOR"
    )
    form = respuesta["context"]["form"]

    assert llamadas == [("MELCIOR", 77, dd.TIPO_DESTINATARIO)]
    assert form["destinatario_id"] == "77"
    assert form["dest_nombre"] == "Elle McGill"
    assert form["destino_pais"] == "US"
    assert respuesta["context"]["error"] is None


def test_id_ajeno_o_inexistente_no_precarga_ni_revela_existencia(monkeypatch):
    _preparar_form(monkeypatch, None)
    respuesta = pc.envio_nuevo_form(
        _request(), destinatario_id=999, cliente="MELCIOR"
    )

    assert "destinatario_id" not in respuesta["context"]["form"]
    assert respuesta["context"]["error"] == "Ese cliente guardado no está disponible en tu cuenta."


def test_edicion_de_mis_clientes_fuerza_tipo_y_propietario(monkeypatch):
    llamadas = []
    monkeypatch.setattr(
        pc, "obtener_direccion",
        lambda cliente, did, tipo=None: _destinatario()
        if (cliente, did, tipo) == ("MELCIOR", 77, dd.TIPO_DESTINATARIO) else None,
    )

    def actualizar(did, **campos):
        llamadas.append((did, campos))
        return _destinatario()

    monkeypatch.setattr(pc, "actualizar_direccion", actualizar)
    respuesta = pc.clientes_add(
        alias="Elle", nombre="Elle McGill", documento="US-TAX-77",
        email="elle@example.com", telefono="+1", direccion="Brickell Ave",
        ciudad="Miami", estado="FL", cp="33131", pais="US", notas="",
        direccion_id="77", cliente="MELCIOR",
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/portal/clientes?ok=1"
    assert llamadas[0][0] == 77
    assert llamadas[0][1]["cliente_id"] == "MELCIOR"
    assert llamadas[0][1]["tipo"] == dd.TIPO_DESTINATARIO
    assert llamadas[0][1]["tipo_actual"] == dd.TIPO_DESTINATARIO


def test_edicion_ajena_no_llega_al_update(monkeypatch):
    monkeypatch.setattr(pc, "obtener_direccion", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pc, "actualizar_direccion",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no debe actualizar")),
    )
    respuesta = pc.clientes_add(
        alias="", nombre="Ajeno", documento="", email="", telefono="",
        direccion="Calle", ciudad="Miami", estado="FL", cp="33131",
        pais="US", notas="", direccion_id="999", cliente="MELCIOR",
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].startswith("/portal/clientes?error=")


def test_post_manipulado_no_guarda_un_pais_fuera_del_catalogo(monkeypatch):
    monkeypatch.setattr(
        pc, "_paises_con_nacional", lambda: [("AR", "Argentina"), ("US", "Estados Unidos")]
    )
    monkeypatch.setattr(
        pc, "crear_direccion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("no debe crear")),
    )

    respuesta = pc.clientes_add(
        alias="", nombre="Cliente", documento="", email="", telefono="",
        direccion="Calle", ciudad="Ciudad", estado="", cp="1000",
        pais="ZZ", notas="", direccion_id="", cliente="MELCIOR",
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].startswith("/portal/clientes?error=")


def test_update_invalido_no_desmarca_la_predeterminada(monkeypatch):
    ejecutadas = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, query, params=None):
            ejecutadas.append((" ".join(query.split()), params))

        def fetchone(self):
            return None

    class Conexion:
        def cursor(self):
            return Cursor()

    @contextmanager
    def conexion():
        yield Conexion()

    monkeypatch.setattr(dd, "get_conn", conexion)
    resultado = dd.actualizar_direccion(
        999, cliente_id="MELCIOR", tipo=dd.TIPO_DESTINATARIO,
        tipo_actual=dd.TIPO_DESTINATARIO, nombre="Ajeno", direccion="Calle",
        ciudad="Miami", cp="33131", pais="US", predeterminada=True,
    )

    assert resultado is None
    assert len(ejecutadas) == 1
    assert "SET predeterminada = FALSE" not in ejecutadas[0][0]


def test_portal_expone_mis_clientes_y_un_pais_canonico():
    base = (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
    clientes = (RAIZ / "templates" / "portal" / "clientes.html").read_text(encoding="utf-8")
    nuevo = (RAIZ / "templates" / "portal" / "envio_nuevo.html").read_text(encoding="utf-8")
    firma_post = inspect.signature(pc.envio_nuevo_post)

    assert 'href="/portal/clientes"' in base
    assert "Mis clientes" in base
    assert "/portal/envios/nuevo?destinatario_id={{ d.id }}" in clientes
    assert 'name="cliente_id"' not in clientes
    assert "Elegir de Mis clientes" in nuevo
    assert 'id="destinatario_id" data-searchable' in nuevo
    assert 'name="dest_pais"' not in nuevo
    assert "dest_pais" not in firma_post.parameters


def test_la_ficha_solo_precarga_y_no_reaparece_un_contacto_borrado():
    fuente = inspect.getsource(pc.envio_nuevo_post)

    assert 'dest_email = dest_email.strip()' in fuente
    assert 'dest_telefono = dest_telefono.strip()' in fuente
    assert 'or (destinatario.get("email")' not in fuente
    assert 'or (destinatario.get("telefono")' not in fuente


def test_error_del_paquete_conserva_remitente_manual_y_vuelve_al_paso_tres(monkeypatch):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kwargs: kwargs)
    monkeypatch.setattr(pc, "get_productos", lambda cliente: [])
    monkeypatch.setattr(
        pc, "_paises_con_nacional",
        lambda: [("AR", "Argentina"), ("CN", "China"), ("US", "Estados Unidos")],
    )
    monkeypatch.setattr(pc, "listar_direcciones", lambda cliente, tipo: [])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda *args, **kwargs: None)
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda cliente: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda cliente: "FEDEX")

    respuesta = pc.envio_nuevo_post(
        _request(), destino_pais="US",
        bulto_producto=[], bulto_cantidad=[], bulto_peso=[], bulto_largo=[],
        bulto_ancho=[], bulto_alto=[], bulto_desc_en=[], bulto_valor_usd=[],
        bulto_hs=[], bulto_pais_fab=[], producto_alias="", cantidad=1,
        intl_courier="dhl", tax_paga="CLIENTE", nac_carrier="", nac_servicio="",
        remitente_id="", rem_nombre="Yiwu Hailu Garment", rem_contacto="Jeff Jang",
        rem_documento="CN-TAX-8", rem_email="jeff@example.cn", rem_telefono="+86 10",
        rem_direccion="88 Fabric Road", rem_ciudad="Yiwu", rem_estado="Zhejiang",
        rem_zip="322000", rem_pais="CN", destinatario_id="",
        dest_nombre="Elle McGill", dest_contacto="Elle", dest_documento="US-TAX-77",
        dest_email="elle@example.com", dest_telefono="+1 305", dest_direccion="Brickell Ave",
        dest_ciudad="Miami", dest_estado="FL", dest_zip="33131", dest_alias="Elle",
        guardar_destinatario=None, precio_cliente_final_ars="", observaciones="Urgente",
        pedido_tienda_id="", cliente="MELCIOR",
    )

    contexto = respuesta["context"]
    assert contexto["error"] == "Agregá al menos una caja al envío."
    assert contexto["form"]["initial_step"] == 3
    assert contexto["form"]["rem_nombre"] == "Yiwu Hailu Garment"
    assert contexto["form"]["rem_contacto"] == "Jeff Jang"
    assert contexto["form"]["rem_pais"] == "CN"
    assert contexto["form"]["dest_contacto"] == "Elle"
    assert contexto["form"]["intl_courier"] == "dhl"
    assert contexto["form"]["tax_paga"] == "CLIENTE"
    assert contexto["remitente"]["direccion"] == "88 Fabric Road"
