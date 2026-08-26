from servicios.carrier_contract import (
    Ambito,
    Capacidad,
    CARRIER_SPECS,
    capability_supported,
    carriers_for,
    carrier_spec,
    public_catalog,
)


def test_catalogo_declara_los_operadores_previstos_sin_habilitarlos_por_credenciales():
    ids = {spec.id for spec in CARRIER_SPECS}
    assert ids == {"dhl", "fedex", "ups", "andreani", "oca"}
    assert carrier_spec(" DHL ").id == "dhl"
    assert carrier_spec("oca").pendiente is True


def test_ambitos_no_mezclan_nacionales_e_internacionales():
    assert [spec.id for spec in carriers_for(Ambito.INTERNACIONAL)] == [
        "dhl",
        "fedex",
        "ups",
    ]
    assert [spec.id for spec in carriers_for("nacional")] == ["andreani", "oca"]
    assert carriers_for("desconocido") == ()


def test_capacidades_son_declarativas_y_no_dependen_de_credenciales():
    assert capability_supported("dhl", Capacidad.EMITIR) is True
    assert capability_supported("oca", "tracking") is True
    assert capability_supported("oca", "recolectar") is False
    assert capability_supported("ups", "recolectar") is False
    assert capability_supported("no-existe", Capacidad.COTIZAR) is False
    assert capability_supported("dhl", "inventada") is False


def test_catalogo_publico_no_expone_variables_ni_promete_credenciales():
    catalogo = public_catalog()
    assert {item["id"] for item in catalogo} == {
        "dhl", "fedex", "ups", "andreani", "oca",
    }
    assert next(item for item in catalogo if item["id"] == "dhl")["estado"] == (
        "integracion_preparada"
    )
    assert all("variables_requeridas" not in item for item in catalogo)


def test_matriz_operativa_deriva_estado_y_capacidades_del_contrato():
    from servicios.configuracion_couriers_cliente import COURIERS_CLIENTE

    matriz = {fila["id"]: fila for fila in COURIERS_CLIENTE}
    assert matriz["dhl"]["integracion_implementada"] is True
    assert matriz["dhl"]["permite_recoleccion"] is True
    assert matriz["fedex"]["integracion_implementada"] is False
    assert matriz["ups"]["permite_recoleccion"] is False


def test_catalogo_cliente_solo_marca_disponible_con_permiso_efectivo(monkeypatch):
    from servicios import configuracion_couriers_cliente as config

    monkeypatch.setattr(config, "obtener_matriz", lambda _cliente: {
        "couriers": [{
            "id": "dhl",
            "puede_cotizar": True,
            "integracion_disponible": True,
        }]
    })
    catalogo = config.catalogo_cliente("MELCIOR", Ambito.INTERNACIONAL)
    dhl = next(item for item in catalogo if item["id"] == "dhl")
    assert dhl["estado"] == "disponible_segun_cuenta"
    assert dhl["estado_corto"] == "Disponible"


def test_catalogo_cliente_falla_cerrado_si_faltan_configuracion_o_permiso(monkeypatch):
    from servicios import configuracion_couriers_cliente as config

    monkeypatch.setattr(config, "obtener_matriz", lambda _cliente: {
        "couriers": [{
            "id": "dhl",
            "puede_cotizar": False,
            "integracion_disponible": False,
        }]
    })
    catalogo = config.catalogo_cliente("MELCIOR", Ambito.INTERNACIONAL)
    dhl = next(item for item in catalogo if item["id"] == "dhl")
    assert dhl["estado"] == "configuracion_pendiente"
    assert dhl["estado_corto"] == "Configuración pendiente"
