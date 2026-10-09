from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cuenta_mantiene_saldo_exportacion_e_historial_paginado():
    cuenta = (ROOT / "templates" / "portal" / "cuenta.html").read_text(
        encoding="utf-8"
    )
    pagos = (ROOT / "templates" / "portal" / "cuenta_pagos.html").read_text(
        encoding="utf-8"
    )

    assert "account-balance-value" in cuenta
    assert "Cómo se compone tu saldo" in cuenta
    assert "data-account-export" in cuenta
    assert "Mostrando {{ pp.pagina_desde }}–{{ pp.pagina_hasta }} de {{ pp.total }}" in pagos
    assert "pp.total_paginas > 1" in pagos
    assert "pagina_pagos=pp.pagina_actual+1" in pagos
    assert "pago.monto_ars" in pagos
    assert "Comprobante de pago" in pagos


def test_contactos_ya_distinguen_alias_localidad_y_documento():
    clientes = (ROOT / "templates" / "portal" / "clientes.html").read_text(
        encoding="utf-8"
    )

    assert 'class="client-alias"' in clientes
    assert "{{ d.ciudad }}" in clientes
    assert "{{ d.documento }}" in clientes
    assert "deduplic" not in clientes.casefold()
