from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import inspect

import pytest

from servicios.carrier_adapter import (
    Ambito,
    OperationState,
    Package,
    QuoteRequest,
    QuoteResult,
)
from servicios.tiendanube_rate_quotes import (
    RateQuoteContractError,
    construir_snapshot,
    referencia_publica,
)


class _ReadinessCursor:
    def __init__(self, ready):
        self.ready = ready
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return {"schema_ready": self.ready}


class _ReadinessConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def test_readiness_rate_quotes_es_solo_lectura(monkeypatch):
    from servicios import tiendanube_rate_quotes

    cursor = _ReadinessCursor(True)
    monkeypatch.setattr(tiendanube_rate_quotes, "_tabla_lista", False)
    monkeypatch.setattr(
        tiendanube_rate_quotes,
        "get_conn",
        lambda: _ReadinessConn(cursor),
    )

    tiendanube_rate_quotes.ensure_rate_quote_storage()

    assert tiendanube_rate_quotes._tabla_lista is True
    assert len(cursor.executed) == 1
    source = inspect.getsource(tiendanube_rate_quotes._ensure_table).upper()
    for ddl in (
        "CREATE TABLE", "ALTER TABLE", "CREATE TRIGGER", "DROP TRIGGER",
        "LOCK TABLE",
    ):
        assert ddl not in source


def test_readiness_rate_quotes_falla_cerrado_si_falta_esquema(monkeypatch):
    from servicios import tiendanube_rate_quotes

    cursor = _ReadinessCursor(False)
    monkeypatch.setattr(tiendanube_rate_quotes, "_tabla_lista", False)
    monkeypatch.setattr(
        tiendanube_rate_quotes,
        "get_conn",
        lambda: _ReadinessConn(cursor),
    )

    with pytest.raises(
        tiendanube_rate_quotes.RateQuoteSnapshotError,
        match="no está migrado",
    ):
        tiendanube_rate_quotes.ensure_rate_quote_storage()

    assert tiendanube_rate_quotes._tabla_lista is False


def _request(**changes):
    values = {
        "request_id": "tn-full-abc",
        "customer_id": "CLIENTE-1",
        "scope": Ambito.NACIONAL,
        "origin": {
            "country": "AR",
            "postal_code": "1425",
            "location_id": "loc-o",
            "direccion": "PII que no debe persistirse",
            "numero": "123",
            "email": "persona@example.com",
        },
        "destination": {
            "pais": "AR",
            "cp": "2000",
            "location_id": "loc-d",
            "direccion": "Otra PII",
            "telefono": "+5491111111111",
            "recipient": "Comprador",
        },
        "packages": (
            Package(
                quantity=1,
                weight_kg=Decimal("1.000"),
                length_cm=Decimal("30"),
                width_cm=Decimal("20"),
                height_cm=Decimal("10"),
            ),
        ),
        "declared_value": Decimal("10000"),
        "declared_currency": "ARS",
        "origin_mode": "domicilio",
        "destination_mode": "domicilio",
        "metadata": {"store_id": "123456", "cart_id": "cart-1"},
    }
    values.update(changes)
    return QuoteRequest(**values)


def _quote(**changes):
    values = {
        "state": OperationState.COTIZADO,
        "carrier_id": "oca",
        "quote_id": "oca-private-quote",
        "service_code": "oca-domicilio",
        "service_name": "OCA domicilio",
        "carrier_cost": Decimal("6000"),
        "carrier_currency": "ARS",
        "customer_price": Decimal("7000"),
        "currency": "ARS",
        "estimated_days": 3,
        "expires_at_iso": "2026-09-18T20:00:00-03:00",
    }
    values.update(changes)
    return QuoteResult(**values)


def _snapshot(
    request=None,
    quote=None,
    buyer_price=Decimal("7000"),
    *,
    pricing_mode="pagado",
    platform_additional_cost=Decimal("0"),
    platform_option_id="88",
):
    return construir_snapshot(
        "123456",
        request or _request(),
        quote or _quote(),
        buyer_price,
        pricing_mode,
        platform_additional_cost=platform_additional_cost,
        platform_option_id=platform_option_id,
        quoted_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )


def test_replay_identico_es_deterministico_aunque_cambie_fecha_de_registro():
    first = _snapshot()
    second = construir_snapshot(
        "123456",
        _request(),
        _quote(),
        Decimal("7000.00"),
        "pagado",
        platform_option_id="88",
        quoted_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
    )

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert first["quoted_at"] != second["quoted_at"]


@pytest.mark.parametrize(
    "changed",
    [
        lambda: _snapshot(quote=_quote(customer_price=Decimal("7100"))),
        lambda: _snapshot(quote=_quote(service_code="oca-sucursal")),
        lambda: _snapshot(
            request=_request(
                packages=(
                    Package(1, Decimal("2"), Decimal("30"), Decimal("20"), Decimal("10")),
                )
            )
        ),
        lambda: _snapshot(request=_request(declared_value=Decimal("11000"))),
        lambda: _snapshot(
            request=_request(destination={"country": "AR", "postal_code": "5000"})
        ),
        lambda: _snapshot(buyer_price=Decimal("0")),
        lambda: _snapshot(platform_additional_cost=Decimal("1500")),
        lambda: _snapshot(platform_option_id="99"),
    ],
)
def test_cambio_comercial_o_logistico_genera_otro_snapshot(changed):
    assert changed()["snapshot_id"] != _snapshot()["snapshot_id"]


def test_snapshot_no_conserva_datos_personales_y_separa_tres_precios():
    snapshot = _snapshot(buyer_price=Decimal("0"))
    serialized = str(snapshot)

    assert snapshot["origin_route"] == {
        "country": "AR",
        "postal_code": "1425",
        "location_id": "loc-o",
    }
    assert snapshot["destination_route"] == {
        "country": "AR",
        "postal_code": "2000",
        "location_id": "loc-d",
    }
    assert "persona@example.com" not in serialized
    assert "+549" not in serialized
    assert "Comprador" not in serialized
    assert snapshot["carrier_cost"] == "6000"
    assert snapshot["tauro_price"] == "7000"
    assert snapshot["buyer_price"] == "0"
    assert snapshot["platform_additional_cost"] == "0"
    assert snapshot["expected_consumer_price"] == "0"
    assert snapshot["platform_option_id"] == "88"


def test_snapshot_congela_ajuste_de_plataforma_y_precio_final_esperado():
    snapshot = _snapshot(platform_additional_cost=Decimal("1500"))

    assert snapshot["buyer_price"] == "7000"
    assert snapshot["platform_additional_cost"] == "1500"
    assert snapshot["expected_consumer_price"] == "8500"


def test_snapshot_gratis_total_conserva_tarifa_y_espera_cero_del_comprador():
    snapshot = _snapshot(
        buyer_price=Decimal("7000"),
        pricing_mode="gratis_total",
        platform_additional_cost=Decimal("1500"),
    )

    assert snapshot["buyer_price"] == "7000"
    assert snapshot["platform_additional_cost"] == "1500"
    assert snapshot["expected_consumer_price"] == "0"


def test_referencia_publica_no_expone_quote_id_del_operador():
    snapshot = _snapshot()
    reference = referencia_publica(snapshot)

    assert reference == f"tauro:oca:{snapshot['snapshot_id']}"
    assert "oca-private-quote" not in reference


def test_rechaza_snapshot_de_otra_tienda_en_metadata():
    with pytest.raises(RateQuoteContractError, match="otra tienda"):
        _snapshot(request=_request(metadata={"store_id": "999999"}))
