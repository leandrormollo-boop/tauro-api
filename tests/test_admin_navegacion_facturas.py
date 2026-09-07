from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_sidebar_deja_solo_las_tres_areas_principales_visibles():
    html = (ROOT / "templates/admin/base_admin.html").read_text()
    principal = html.split('<nav class="sidebar">', 1)[1].split(
        '<details class="sidebar-more"', 1
    )[0]

    assert principal.count("<a href=") == 4
    assert ">Panel de control</a>" in principal
    assert ">Facturas internacionales</a>" in principal
    assert ">Facturas nacionales</a>" in principal
    assert ">Lista de clientes</a>" in principal
    assert "/admin/clientes/nuevo" not in principal

    clientes = (ROOT / "templates/admin/clientes.html").read_text()
    assert "/admin/clientes/nuevo" in clientes


def test_facturas_internacionales_filtra_dhl_y_fedex(monkeypatch):
    from endpoints import admin
    from servicios import conciliacion_couriers as conciliacion

    llamadas = {}

    def listar_control(**kwargs):
        llamadas["control"] = kwargs
        return {"items": [], "totales": {}, "total": 0}

    def listar_facturas(*, couriers):
        llamadas["facturas"] = couriers
        return []

    monkeypatch.setattr(conciliacion, "listar_control_envios", listar_control)
    monkeypatch.setattr(
        conciliacion, "listar_facturas_courier_control", listar_facturas
    )
    monkeypatch.setattr(
        conciliacion,
        "listar_ajustes_para_revision",
        lambda: [
            {"courier": "DHL", "id": 1},
            {"courier": "OCA", "id": 2},
        ],
    )
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(admin.templates, "TemplateResponse", lambda **kw: kw)

    respuesta = admin._render_facturas_admin(
        request=SimpleNamespace(),
        courier="OCA",
        ambito="INTERNACIONAL",
    )

    assert respuesta["context"]["seccion"] == "facturas_internacionales"
    assert respuesta["context"]["ruta_facturas"] == (
        "/admin/facturas-internacionales"
    )
    assert llamadas["control"]["ambito"] == "INTERNACIONAL"
    assert llamadas["control"]["courier"] == ""
    assert llamadas["facturas"] == ("DHL", "FEDEX")
    assert respuesta["context"]["ajustes"] == [{"courier": "DHL", "id": 1}]


def test_facturas_nacionales_filtra_andreani_y_oca(monkeypatch):
    from endpoints import admin
    from servicios import conciliacion_couriers as conciliacion

    llamadas = {}
    monkeypatch.setattr(
        conciliacion,
        "listar_control_envios",
        lambda **kwargs: llamadas.setdefault(
            "control", {"kwargs": kwargs, "items": [], "totales": {}, "total": 0}
        ),
    )
    monkeypatch.setattr(
        conciliacion,
        "listar_facturas_courier_control",
        lambda *, couriers: llamadas.setdefault("couriers", couriers) and [],
    )
    monkeypatch.setattr(conciliacion, "listar_ajustes_para_revision", lambda: [])
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(admin.templates, "TemplateResponse", lambda **kw: kw)

    respuesta = admin._render_facturas_admin(
        request=SimpleNamespace(),
        courier="OCA",
        ambito="NACIONAL",
    )

    assert respuesta["context"]["seccion"] == "facturas_nacionales"
    assert respuesta["context"]["ruta_facturas"] == "/admin/facturas-nacionales"
    assert llamadas["control"]["kwargs"]["ambito"] == "NACIONAL"
    assert llamadas["control"]["kwargs"]["courier"] == "OCA"
    assert llamadas["couriers"] == ("ANDREANI", "OCA")
