from pathlib import Path

from servicios.tiendanube_preflight import evaluate_preflight


def test_preflight_bloquea_release_sin_credenciales_ni_adapter(tmp_path):
    result = evaluate_preflight({}, repository_root=tmp_path)

    assert result["ready_for_release"] is False
    assert "oauth_credentials" in result["blockers"]
    assert "token_encryption_key" in result["blockers"]
    assert "national_adapter_code" not in result["blockers"]
    assert "oca_adapter_enabled" in result["blockers"]
    assert "nube_sdk_bundle" in result["blockers"]


def test_preflight_no_expone_valores_de_secretos(monkeypatch, tmp_path):
    bundle = tmp_path / "tiendanube_nube_app" / "dist" / "main.min.js"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("export{}", encoding="utf-8")
    env = {
        "BASE_URL": "https://taurosolutions.ar",
        "TIENDANUBE_CLIENT_ID": "id-super-secreto",
        "TIENDANUBE_CLIENT_SECRET": "secret-super-secreto",
    }

    result = evaluate_preflight(env, repository_root=tmp_path)
    serialized = str(result)

    assert "id-super-secreto" not in serialized
    assert "secret-super-secreto" not in serialized
    assert next(c for c in result["checks"] if c["code"] == "oauth_credentials")["ok"]


def test_preflight_acepta_oca_qa_solo_con_configuracion_y_uat_completos(tmp_path):
    bundle = tmp_path / "tiendanube_nube_app" / "dist" / "main.min.js"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("export{}", encoding="utf-8")
    env = {
        "BASE_URL": "https://taurosolutions.ar",
        "TIENDANUBE_CLIENT_ID": "id",
        "TIENDANUBE_CLIENT_SECRET": "secret",
        "TIENDANUBE_TOKEN_ENCRYPTION_KEY": "encryption-key",
        "TIENDANUBE_SHIPPING_ACCESS_APPROVED": "true",
        "TIENDANUBE_DEMO_STORE_ID": "123",
        "TIENDANUBE_SHIPPING_ENABLED": "true",
        "TAURO_NACIONAL_RATES_READY": "true",
        "TIENDANUBE_HOMOLOGATION_APPROVED": "true",
        "OCA_ADAPTER_ENABLED": "true",
        "OCA_UAT_APPROVED": "true",
        "OCA_ENVIRONMENT": "qa",
        "OCA_CUIT": "20-12345678-6",
        "OCA_CUENTA": "123456/001",
        "OCA_OPERATIVA": "123456",
        "OCA_USUARIO": "usuario",
        "OCA_PASSWORD": "password",
        "OCA_ORIGIN_MODE": "domicilio",
        "OCA_DESTINATION_MODE": "domicilio",
    }

    result = evaluate_preflight(env, repository_root=tmp_path)

    assert result["ready_for_release"] is True
    assert result["blockers"] == []
