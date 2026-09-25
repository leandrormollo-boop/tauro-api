from dataclasses import replace
import pytest
from servicios import oca_portal as oca
from servicios.oca_adapter import OCAConfig, OCAConfigurationError


def test_produccion_exige_flags_y_rechaza_cuenta_qa(monkeypatch):
    c = OCAConfig.from_env(
        {
            "OCA_ENVIRONMENT": "qa",
            "OCA_CUIT": "30-53625919-4",
            "OCA_CUENTA": "111757/001",
            "OCA_OPERATIVA": "64665",
            "OCA_CENTRO_COSTO": "2",
            "OCA_USUARIO": "test@oca.com.ar",
            "OCA_PASSWORD": "fixture",
            "OCA_ORIGIN_MODE": "domicilio",
            "OCA_DESTINATION_MODE": "domicilio",
            "OCA_OPERATIVA_SEGURO_CONFIRMADO": "true",
            "OCA_CONFIRM_WITHDRAWAL": "true",
            "OCA_ADAPTER_ENABLED": "true",
            "OCA_UAT_APPROVED": "true",
            "OCA_FULFILLMENT_ENABLED": "true",
            "OCA_FULFILLMENT_UAT_APPROVED": "true",
        }
    )
    monkeypatch.setattr(oca.OCAConfig, "from_env", lambda: c)
    with pytest.raises(oca.OCAPortalError):
        oca.config_productiva()
    c = replace(c, environment="production", production_approved=True)
    with pytest.raises(oca.OCAPortalError):
        oca.config_productiva()
    c = replace(
        c,
        account="123456/001",
        username="prod@example.invalid",
        cuit="20-12345678-6",
        operation=472095,
    )
    assert oca.config_productiva() is c
    c = replace(c, production_approved=False)
    with pytest.raises(OCAConfigurationError):
        oca.config_productiva()
