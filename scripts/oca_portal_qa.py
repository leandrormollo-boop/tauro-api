"""Servidor local QA: python scripts/oca_portal_qa.py. Credenciales por variables de entorno."""

import os, sys, secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from servicios.oca_adapter import OCAConfig
from servicios.oca_portal_qa import QAPortal
from endpoints.portal_oca_qa import router


def create_app(service):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.oca_qa = service
    app.state.oca_csrf = secrets.token_urlsafe(32)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    app.include_router(router)

    @app.middleware("http")
    async def safe_headers(request, call_next):
        request.state.csp_nonce = secrets.token_urlsafe(16)
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0")) > 16000
        ):
            from fastapi.responses import Response

            return Response(status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index():
        return RedirectResponse("/portal/nacional/oca")

    return app


if __name__ == "__main__":
    import uvicorn

    # No dotenv and no DATABASE_URL: this pilot never opens production storage.
    cfg = OCAConfig.from_env(
        {
            "OCA_ENVIRONMENT": "qa",
            "OCA_CUIT": "30-53625919-4",
            "OCA_CUENTA": "111757/001",
            "OCA_OPERATIVA": "64665",
            "OCA_CENTRO_COSTO": "2",
            "OCA_USUARIO": os.environ["OCA_TEST_USER"],
            "OCA_PASSWORD": os.environ["OCA_TEST_PASSWORD"],
            "OCA_ORIGIN_MODE": "domicilio",
            "OCA_DESTINATION_MODE": "domicilio",
            "OCA_OPERATIVA_SEGURO_CONFIRMADO": "true",
            "OCA_CONFIRM_WITHDRAWAL": "true",
            "OCA_ADAPTER_ENABLED": "true",
            "OCA_UAT_APPROVED": "true",
            "OCA_FULFILLMENT_ENABLED": "true",
            "OCA_FULFILLMENT_UAT_APPROVED": "true",
            "OCA_LABEL_FORMAT": "10x15",
        }
    )
    pilot = QAPortal(os.environ["OCA_QA_DB_PATH"], cfg)
    uvicorn.run(create_app(pilot), host="127.0.0.1", port=8787)
