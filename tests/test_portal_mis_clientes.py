"""Mis clientes: base privada, prellenado y mutaciones seguras."""
from contextlib import contextmanager
import inspect
from pathlib import Path
import pytest

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


def test_estadisticas_agenda_usa_sesion_y_enlaza_cada_barra(monkeypatch):
    from servicios import estadisticas_contactos
    from test_portal_home_panorama import _action_portal

    portal, request = _action_portal(monkeypatch, [])
    contactos = [_destinatario(), {**_destinatario(), "id": 78, "nombre": "Ana", "alias": "Ana"}]
    llamadas = []
    monkeypatch.setattr(portal, "listar_direcciones", lambda cuenta: contactos if cuenta == "MELCIOR" else [])

    def contar(cuenta, agenda):
        llamadas.append((cuenta, agenda))
        return {77: {"envios_identificados": 0, "porcentaje_relativo": 0},
                78: {"envios_identificados": 12, "porcentaje_relativo": 100}}

    monkeypatch.setattr(estadisticas_contactos, "contar_envios_emitidos_por_contacto", contar)
    respuesta = portal.clientes_view(request('/portal/clientes'), cliente="MELCIOR")
    assert llamadas == [("MELCIOR", contactos)]
    assert [d["id"] for d in respuesta.context["clientes_grafico"]] == [78, 77]
    html = respuesta.body.decode()
    assert 'href="#cliente-78"' in html and 'id="cliente-78"' in html
    assert 'Ver Ana: 12 envíos identificados' in html
    assert 'Ver Elle McGill: 0 envíos identificados' in html
    assert html.count('class="home-bar-fill"') == 1
    assert "envios_identificados" not in contactos[0]


def test_estadisticas_agenda_error_no_inventa_ceros_y_mantiene_contactos(monkeypatch):
    from servicios import estadisticas_contactos
    from test_portal_home_panorama import _action_portal

    portal, request = _action_portal(monkeypatch, [])
    monkeypatch.setattr(portal, "listar_direcciones", lambda _: [_destinatario()])

    def fallar(*_):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(estadisticas_contactos, "contar_envios_emitidos_por_contacto", fallar)
    html = portal.clientes_view(request('/portal/clientes'), cliente="MELCIOR").body.decode()
    assert "No pudimos cargar las estadísticas. Volvé a intentar." in html
    assert 'id="cliente-77"' in html
    assert 'class="home-monthly-bar client-shipment-bar"' not in html


def test_agenda_vacia_no_muestra_grafico_vacio(monkeypatch):
    from test_portal_home_panorama import _action_portal

    portal, request = _action_portal(monkeypatch, [])
    monkeypatch.setattr(portal, "listar_direcciones", lambda _: [])
    html = portal.clientes_view(request('/portal/clientes'), cliente="MELCIOR").body.decode()
    assert "Guardar mi primer contacto" in html
    assert "client-shipment-panel" not in html


def _preparar_form(monkeypatch, direccion):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kwargs: kwargs)
    monkeypatch.setattr(pc, "obtener_direccion", lambda cliente, did, tipo=None: direccion)
    monkeypatch.setattr(pc, "get_productos", lambda cliente: [])
    monkeypatch.setattr(pc, "_paises_con_nacional", lambda: [("AR", "Argentina"), ("US", "Estados Unidos")])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda cliente: {
        "nombre": "Melcior", "direccion": "Av. Córdoba 1", "ciudad": "CABA",
        "cp": "1000", "pais": "AR",
    })
    monkeypatch.setattr(pc, "listar_direcciones", lambda cliente, tipo=None: [direccion] if direccion else [])
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
        _request(), destinatario_id=77, cliente="MELCIOR",
        ambito="internacional",
    )
    form = respuesta["context"]["form"]

    assert llamadas == [("MELCIOR", 77, None)]
    assert form["destinatario_id"] == "77"
    assert form["dest_nombre"] == "Elle McGill"
    assert form["destino_pais"] == "US"
    assert respuesta["context"]["error"] is None


def test_id_ajeno_o_inexistente_no_precarga_ni_revela_existencia(monkeypatch):
    _preparar_form(monkeypatch, None)
    respuesta = pc.envio_nuevo_form(
        _request(), destinatario_id=999, cliente="MELCIOR",
        ambito="internacional",
    )

    assert "destinatario_id" not in respuesta["context"]["form"]
    assert respuesta["context"]["error"] == "Ese cliente guardado no está disponible en tu cuenta."


@pytest.mark.parametrize("rol", [dd.TIPO_REMITENTE, dd.TIPO_DESTINATARIO])
@pytest.mark.parametrize("lado", ["remitente_id", "destinatario_id"])
def test_contacto_guardado_disponible_en_ambos_lados(monkeypatch, rol, lado):
    direccion = {**_destinatario(), "tipo": rol}
    _preparar_form(monkeypatch, direccion)
    llamados = []

    def obtener(cliente, ident, tipo=None):
        llamados.append((cliente, ident, tipo))
        return direccion if (cliente, ident, tipo) == ("MELCIOR", 77, None) else None

    monkeypatch.setattr(pc, "obtener_direccion", obtener)
    respuesta = pc.envio_nuevo_form(_request(), cliente="MELCIOR",
                                     ambito="internacional", **{lado: 77})
    context = respuesta["context"]
    assert context["error"] is None
    assert context["form"][lado] == "77"
    assert context["remitentes"] == context["destinatarios"] == [direccion]
    if lado == "remitente_id":
        assert context["remitente"]["nombre"] == "Elle McGill"
    else:
        assert context["form"]["dest_nombre"] == "Elle McGill"
    assert llamados == [("MELCIOR", 77, None)]


@pytest.mark.parametrize("cliente,ident,esperado", [
    ("MELCIOR", 77, True), ("OTRA-CUENTA", 77, False), ("MELCIOR", 999, False),
])
def test_remitente_explicito_respeta_propietario_sin_fallback(monkeypatch, cliente, ident, esperado):
    direccion = _destinatario()
    monkeypatch.setattr(dd, "obtener_direccion", lambda cuenta, did, tipo=None:
                        direccion if (cuenta, did, tipo) == ("MELCIOR", 77, None) else None)
    monkeypatch.setattr(dd, "listar_direcciones", lambda *_args:
                        pytest.fail("Un id explícito no debe sustituirse por otra ficha"))
    assert dd.obtener_remitente_para_envio(cliente, ident) == (direccion if esperado else None)


def test_elegir_ambito_conserva_ambos_contactos(monkeypatch):
    _preparar_form(monkeypatch, _destinatario())
    response = pc.envio_nuevo_form(_request(), cliente="MELCIOR", remitente_id=77, destinatario_id=88)
    for key in ("nacional_url", "internacional_url"):
        assert "remitente_id=77" in response['context'][key]
        assert "destinatario_id=88" in response['context'][key]
    national = pc.envio_nuevo_form(_request(), cliente="MELCIOR", ambito="nacional",
                                   remitente_id=77, destinatario_id=88)
    assert national.headers['location'] == '/portal/oca/nuevo?remitente_id=77&destinatario_id=88'


def test_edicion_de_mis_clientes_fuerza_tipo_y_propietario(monkeypatch):
    llamadas = []
    monkeypatch.setattr(
        pc, "obtener_direccion",
        lambda cliente, did, tipo=None: _destinatario()
        if (cliente, did) == ("MELCIOR", 77) else None,
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
    assert "Usar un cliente como remitente" in nuevo
    assert "Usar un cliente como destinatario" in nuevo
    assert 'id="destinatario_id" data-searchable' in nuevo
    assert 'name="dest_pais"' not in nuevo
    assert "dest_pais" not in firma_post.parameters


def test_la_ficha_solo_precarga_y_no_reaparece_un_contacto_borrado():
    fuente = inspect.getsource(pc.envio_nuevo_post)

    assert 'dest_email = dest_email.strip()' in fuente
    assert 'dest_telefono = dest_telefono.strip()' in fuente
    assert 'or (destinatario.get("email")' not in fuente
    assert 'or (destinatario.get("telefono")' not in fuente


@pytest.mark.parametrize("contacto_guardado", [False, True])
def test_error_del_paquete_conserva_remitente_manual_y_vuelve_al_paso_tres(monkeypatch, contacto_guardado):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kwargs: kwargs)
    monkeypatch.setattr(pc, "get_productos", lambda cliente: [])
    monkeypatch.setattr(
        pc, "_paises_con_nacional",
        lambda: [("AR", "Argentina"), ("CN", "China"), ("US", "Estados Unidos")],
    )
    monkeypatch.setattr(pc, "listar_direcciones", lambda cliente, tipo=None: [])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda *args, **kwargs: None)
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda cliente: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda cliente: "FEDEX")
    # Un contacto históricamente guardado como remitente también es destino.
    monkeypatch.setattr(pc, "obtener_direccion", lambda cliente, ident, tipo=None:
                        {**_destinatario(), "tipo": dd.TIPO_REMITENTE}
                        if (cliente, ident, tipo) == ("MELCIOR", 77, None) else None)

    respuesta = pc.envio_nuevo_post(
        _request(), destino_pais="US",
        bulto_producto=[], bulto_cantidad=[], bulto_peso=[], bulto_largo=[],
        bulto_ancho=[], bulto_alto=[], bulto_desc_en=[], bulto_valor_usd=[],
        bulto_hs=[], bulto_pais_fab=[], producto_alias="", cantidad=1,
        intl_courier="dhl", tax_paga="CLIENTE",
        remitente_id="", rem_nombre="Yiwu Hailu Garment", rem_contacto="Jeff Jang",
        rem_documento="CN-TAX-8", rem_email="jeff@example.cn", rem_telefono="+86 10",
        rem_direccion="88 Fabric Road", rem_ciudad="Yiwu", rem_estado="Zhejiang",
        rem_zip="322000", rem_pais="CN", destinatario_id="77" if contacto_guardado else "",
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


def test_error_de_invoice_vuelve_directo_al_paso_cuatro(monkeypatch):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kwargs: kwargs)
    monkeypatch.setattr(pc, "get_productos", lambda cliente: [])
    monkeypatch.setattr(
        pc, "_paises_con_nacional",
        lambda: [("AR", "Argentina"), ("CN", "China"), ("US", "Estados Unidos")],
    )
    monkeypatch.setattr(pc, "listar_direcciones", lambda cliente, tipo=None: [])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda *args, **kwargs: None)
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda cliente: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda cliente: "DHL")

    respuesta = pc.envio_nuevo_post(
        _request(), destino_pais="US",
        bulto_producto=[""], bulto_cantidad=["1"], bulto_unidades_aduana=["1"],
        bulto_peso=["2"], bulto_largo=["30"], bulto_ancho=["20"],
        bulto_alto=["10"], bulto_desc_en=[""], bulto_valor_usd=["25"],
        bulto_hs=[""], bulto_pais_fab=["CN"], producto_alias="", cantidad=1,
        intl_courier="dhl", tax_paga="DESTINATARIO",
        remitente_id="", rem_nombre="Yiwu Hailu Garment", rem_contacto="Jeff Jang",
        rem_documento="CN-TAX-8", rem_email="jeff@example.cn", rem_telefono="+86 10",
        rem_direccion="88 Fabric Road", rem_ciudad="Yiwu", rem_estado="Zhejiang",
        rem_zip="322000", rem_pais="CN", destinatario_id="",
        dest_nombre="Elle McGill", dest_contacto="Elle", dest_documento="US-TAX-77",
        dest_email="elle@example.com", dest_telefono="+1 305", dest_direccion="Brickell Ave",
        dest_ciudad="Miami", dest_estado="FL", dest_zip="33131", dest_alias="Elle",
        guardar_destinatario=None, precio_cliente_final_ars="", observaciones="",
        pedido_tienda_id="", cliente="MELCIOR",
    )

    assert "nombre del producto en inglés" in respuesta["context"]["error"]
    assert respuesta["context"]["form"]["initial_step"] == 4
