"""La ficha real del cliente distingue crédito efectivo de historial en revisión."""
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _render_pago(*, en_revision):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
        autoescape=select_autoescape(),
    )
    from servicios.presentacion import registrar_filtros
    registrar_filtros(env)
    return env.get_template("admin/cliente_detail.html").render(
        request=SimpleNamespace(
            url=SimpleNamespace(path="/admin/clientes/DEMO"),
            state=SimpleNamespace(csp_nonce="prueba"),
        ),
        seccion="clientes", vista="pagos",
        cliente={"cliente_id": "DEMO", "nombre": "Demostración", "activo": True},
        pagos=[{
            "id": 99, "fecha": date(2020, 1, 1), "monto_ars": Decimal("100.00"),
            "metodo": "Transferencia", "referencia": "DEMO", "estado": "APROBADO",
            "fecha_revision_requerida": en_revision,
            "monto_nacional": Decimal("100"), "monto_internacional": Decimal("0"),
            "aplicaciones": [{"envio_id": 7, "ambito": "NACIONAL",
                              "monto_ars": Decimal("100"), "estado": "APLICADA"}],
        }],
    )


def test_revision_persiste_en_ficha_aunque_fecha_ya_haya_pasado():
    html = _render_pago(en_revision=True)
    assert "En revisión de fecha" in html
    assert "Sin acreditar" in html
    assert "Imputaciones registradas sin efecto en el saldo." in html
    assert "Revisar fecha</a>" in html
    assert "Cargo #7" in html  # conserva el vínculo al historial
    assert "APROBADO" not in html
    assert "SIN IMPUTAR" not in html
    assert '<span class="badge ok">N $' not in html


def test_pago_valido_conserva_estado_y_ambito_imputado():
    html = _render_pago(en_revision=False)
    assert "En revisión de fecha" not in html
    assert "APROBADO" in html
    assert '<span class="badge ok">N $ 100,00</span>' in html
