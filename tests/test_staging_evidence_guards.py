from __future__ import annotations

import argparse
import runpy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BURST = runpy.run_path(str(ROOT / "scripts" / "staging_webhook_burst.py"))
CAPTURE = runpy.run_path(str(ROOT / "scripts" / "capture_staging_evidence.py"))


@pytest.mark.parametrize(
    "candidate",
    [
        "https://staging.example.com",
        "https://tauro-api-staging.up.railway.app.example.com",
        "https://user@tauro-api-staging.up.railway.app",
        "https://tauro-api-staging.up.railway.app:443",
        "https://tauro-api-staging.up.railway.app/health",
        "https://tauro-api-staging.up.railway.app?next=production",
        "http://tauro-api-staging.up.railway.app",
    ],
)
def test_burst_rechaza_origenes_que_no_sean_staging_exacto(candidate: str):
    with pytest.raises(argparse.ArgumentTypeError):
        BURST["staging_base_url"](candidate)


def test_burst_acepta_solo_origen_y_tienda_sintetica_exactos():
    assert (
        BURST["staging_base_url"]("https://tauro-api-staging.up.railway.app/")
        == "https://tauro-api-staging.up.railway.app"
    )
    assert BURST["shop_domain"]("TAURO-QA.MYSHOPIFY.COM.") == (
        "tauro-qa.myshopify.com"
    )
    with pytest.raises(argparse.ArgumentTypeError):
        BURST["shop_domain"]("otra-qa.myshopify.com")
    with pytest.raises(argparse.ArgumentTypeError):
        BURST["shop_domain"]("pesca-jacks.myshopify.com")


def test_burst_no_acepta_secreto_por_argv():
    option_strings = {
        option
        for action in BURST["parser"]()._actions
        for option in action.option_strings
    }
    assert "--webhook-secret" not in option_strings


def test_captura_acepta_solo_origen_staging_exacto():
    assert (
        CAPTURE["validate_staging_url"](
            "https://tauro-api-staging.up.railway.app/"
        )
        == "https://tauro-api-staging.up.railway.app"
    )
    with pytest.raises(argparse.ArgumentTypeError):
        CAPTURE["validate_staging_url"]("https://otro-staging.example.com")

