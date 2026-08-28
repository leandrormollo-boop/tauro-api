#!/usr/bin/env python3
"""Genera evidencia reproducible para la certificación sandbox de MyDHL.

Usa el mismo ``DHLClient.create_shipment`` que producción y exporta el JSON
enviado, la respuesta técnica sin base64, la guía, la factura comercial y
sus hashes. El guardarraíl de ambiente impide ejecutar este comando contra el
endpoint productivo.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.dhl_client import DHLClient  # noqa: E402


def _json_sin_base64(data: object) -> object:
    """Copia una respuesta DHL y reemplaza documentos por metadatos seguros."""
    salida = copy.deepcopy(data)
    if not isinstance(salida, dict):
        return salida
    for documento in salida.get("documents") or []:
        if not isinstance(documento, dict) or "content" not in documento:
            continue
        contenido = str(documento.get("content") or "")
        documento["content"] = {
            "omitido": True,
            "caracteres_base64": len(contenido),
            "sha256_base64": hashlib.sha256(
                contenido.encode("ascii", "ignore")
            ).hexdigest(),
        }
    return salida


def _escribir_privado(path: Path, contenido: bytes) -> None:
    path.write_bytes(contenido)
    path.chmod(0o600)


def _json_privado(path: Path, contenido: object) -> None:
    _escribir_privado(
        path,
        (json.dumps(contenido, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def generar_paquete(datos: dict, carpeta_base: Path) -> Path:
    cliente = DHLClient()
    if cliente.environment != "sandbox":
        raise RuntimeError(
            "Certificación bloqueada: DHL_ENVIRONMENT debe ser sandbox; "
            "este script nunca emite en producción."
        )

    marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destino = carpeta_base / f"dhl_certificacion_{marca}"
    destino.mkdir(parents=True, exist_ok=False, mode=0o700)

    captura: dict[str, object] = {}
    sesion = requests.Session()

    def _post_real(url, json=None, **kwargs):
        captura["endpoint"] = url
        captura["request"] = copy.deepcopy(json)
        captura["headers"] = {
            clave: valor
            for clave, valor in (kwargs.get("headers") or {}).items()
            if clave.lower() != "authorization"
        }
        respuesta = sesion.post(url, json=json, **kwargs)
        captura["status_code"] = respuesta.status_code
        try:
            captura["response"] = respuesta.json()
        except Exception:
            captura["response"] = {"body": respuesta.text[:2000]}
        return respuesta

    with mock.patch("core.dhl_client.requests.post", side_effect=_post_real):
        resultado = cliente.create_shipment(datos)

    _json_privado(
        destino / "01_codigo_enviado.json",
        {
            "endpoint": captura.get("endpoint"),
            "method": "POST",
            "headers_sin_authorization": captura.get("headers") or {},
            "body": captura.get("request") or {},
        },
    )
    _json_privado(
        destino / "02_respuesta_dhl.json",
        {
            "http_status": captura.get("status_code"),
            "body": _json_sin_base64(captura.get("response") or {}),
        },
    )

    if not resultado.get("encontrado"):
        raise RuntimeError(
            f"DHL no confirmó la guía: {resultado.get('error') or 'error desconocido'}. "
            f"La evidencia técnica quedó en {destino}."
        )

    guia = resultado.get("label_pdf")
    factura = resultado.get("invoice_pdf")
    if not guia or not bytes(guia).startswith(b"%PDF"):
        raise RuntimeError(
            f"DHL emitió tracking pero no devolvió la guía PDF. Revisar {destino}."
        )
    if not factura or not bytes(factura).startswith(b"%PDF"):
        raise RuntimeError(
            "DHL emitió tracking pero no devolvió la factura comercial PDF. "
            f"Revisar {destino}."
        )

    guia_path = destino / "03_guia_dhl.pdf"
    factura_path = destino / "04_factura_comercial_dhl.pdf"
    _escribir_privado(guia_path, bytes(guia))
    _escribir_privado(factura_path, bytes(factura))
    _json_privado(
        destino / "05_resumen.json",
        {
            "ambiente": "sandbox",
            "tracking": resultado.get("tracking"),
            "message_reference": resultado.get("message_reference"),
            "http_status": captura.get("status_code"),
            "sha256_guia_pdf": hashlib.sha256(bytes(guia)).hexdigest(),
            "sha256_factura_pdf": hashlib.sha256(bytes(factura)).hexdigest(),
        },
    )

    zip_path = destino.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as paquete:
        for archivo in sorted(destino.iterdir()):
            paquete.write(archivo, arcname=archivo.name)
    zip_path.chmod(0o600)
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "var" / "dhl_certificacion",
    )
    args = parser.parse_args()
    try:
        datos = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("el JSON raíz debe ser un objeto")
        paquete = generar_paquete(datos, args.output_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Paquete de certificación listo: {paquete}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
