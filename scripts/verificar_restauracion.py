#!/usr/bin/env python3
"""Restaura y verifica un pg_dump custom en una base local de ensayo vacía."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

# Permite ejecutar el archivo directamente desde cualquier directorio sin
# depender de PYTHONPATH ni de la configuración de la aplicación.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.database import _READINESS_CONTABLE_CAMPOS, _verificar_readiness_contable


HOSTS_PERMITIDOS = frozenset({"localhost", "127.0.0.1"})
PREFIJO_BASE = "tauro_restore_test_"
NOMBRE_BASE_RE = re.compile(r"^tauro_restore_test_[a-z0-9_]+$")
TABLAS_CRITICAS = ("envios", "pagos", "solicitudes_guia")
COLUMNAS_DOCUMENTOS = (
    ("envios", "factura_pdf"),
    ("pagos", "comprobante"),
    ("solicitudes_guia", "label_pdf"),
    ("solicitudes_guia", "commercial_invoice_pdf"),
)
PG_RESTORE_DEFAULT = shutil.which("pg_restore") or "/opt/homebrew/bin/pg_restore"


class ErrorVerificacion(RuntimeError):
    """Error seguro para mostrar: nunca debe contener credenciales ni filas."""


def validar_destino(host: str, database: str) -> None:
    if host not in HOSTS_PERMITIDOS:
        raise ErrorVerificacion("El host debe ser localhost o 127.0.0.1.")
    if not NOMBRE_BASE_RE.fullmatch(database):
        raise ErrorVerificacion(
            f"La base debe comenzar con {PREFIJO_BASE} y usar sólo minúsculas, números y guiones bajos."
        )


def _password_entorno() -> str | None:
    return os.environ.get("PGPASSWORD")


def _conectar(*, host: str, port: int, user: str, database: str):
    parametros: dict[str, Any] = {
        "host": host,
        "hostaddr": "127.0.0.1",
        "port": port,
        "user": user,
        "dbname": database,
        "connect_timeout": 5,
        "cursor_factory": psycopg2.extras.RealDictCursor,
    }
    password = _password_entorno()
    if password is not None:
        parametros["password"] = password
    return psycopg2.connect(**parametros)


def _entorno_pg(*, host: str, port: int, user: str, database: str) -> dict[str, str]:
    entorno = dict(os.environ)
    # Evita que una configuración de servicio o URL ajena prevalezca sobre el
    # destino local que ya fue validado. La contraseña, si existe, sólo viaja
    # en el entorno del proceso y jamás en argv ni en el informe.
    password = entorno.get("PGPASSWORD")
    entorno.pop("DATABASE_URL", None)
    for clave in tuple(entorno):
        if clave.startswith("PG"):
            entorno.pop(clave)
    if password is not None:
        entorno["PGPASSWORD"] = password
    entorno.update(
        PGHOST=host,
        PGHOSTADDR="127.0.0.1",
        PGPORT=str(port),
        PGUSER=user,
        PGDATABASE=database,
    )
    return entorno


def exigir_base_vacia(*, host: str, port: int, user: str, database: str) -> None:
    try:
        with _conectar(host=host, port=port, user=user, database=database) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS cantidad
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                       AND n.nspname NOT LIKE 'pg_temp_%'
                       AND n.nspname NOT LIKE 'pg_toast_temp_%'
                       AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                    """
                )
                cantidad = int(cur.fetchone()["cantidad"])
    except psycopg2.Error as exc:
        raise ErrorVerificacion("No se pudo abrir la base local de destino.") from exc
    if cantidad:
        raise ErrorVerificacion(
            "La base de destino no está vacía; el helper no borra ni limpia objetos existentes."
        )


def _ejecutar_pg_restore(
    archive: Path,
    *,
    host: str,
    port: int,
    user: str,
    database: str,
    pg_restore: str,
) -> None:
    entorno = _entorno_pg(host=host, port=port, user=user, database=database)
    try:
        listado = subprocess.run(
            [pg_restore, "--list", str(archive)],
            env=entorno,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise ErrorVerificacion("No se pudo ejecutar pg_restore local.") from exc
    if listado.returncode != 0:
        raise ErrorVerificacion("El archivo no es un pg_dump custom legible por pg_restore.")

    comando = [
        pg_restore,
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        database,
        str(archive),
    ]
    try:
        restauracion = subprocess.run(
            comando,
            env=entorno,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise ErrorVerificacion("No se pudo ejecutar pg_restore local.") from exc
    if restauracion.returncode != 0:
        raise ErrorVerificacion(
            f"pg_restore falló con código {restauracion.returncode}; se omitió su salida para no exponer datos."
        )


def _tabla_y_columna_identificadores(tabla: str, columna: str | None = None) -> None:
    permitidos = set(TABLAS_CRITICAS)
    if tabla not in permitidos:
        raise AssertionError("Identificador interno no permitido")
    if columna is not None and (tabla, columna) not in COLUMNAS_DOCUMENTOS:
        raise AssertionError("Identificador interno no permitido")


def verificar_contenido(*, host: str, port: int, user: str, database: str) -> dict[str, Any]:
    try:
        with _conectar(host=host, port=port, user=user, database=database) as conn:
            with conn.cursor() as cur:
                readiness = _verificar_readiness_contable(cur)
                row_counts: dict[str, int] = {}
                for tabla in TABLAS_CRITICAS:
                    _tabla_y_columna_identificadores(tabla)
                    cur.execute(f'SELECT COUNT(*) AS cantidad FROM "{tabla}"')
                    row_counts[tabla] = int(cur.fetchone()["cantidad"])

                document_bytes: dict[str, int] = {}
                for tabla, columna in COLUMNAS_DOCUMENTOS:
                    _tabla_y_columna_identificadores(tabla, columna)
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.columns
                             WHERE table_schema = CURRENT_SCHEMA()
                               AND table_name = %s AND column_name = %s
                        ) AS existe
                        """,
                        (tabla, columna),
                    )
                    if not cur.fetchone()["existe"]:
                        continue
                    cur.execute(
                        f'SELECT COALESCE(SUM(OCTET_LENGTH("{columna}")), 0) AS bytes '
                        f'FROM "{tabla}"'
                    )
                    document_bytes[f"{tabla}.{columna}"] = int(cur.fetchone()["bytes"])
    except (psycopg2.Error, RuntimeError) as exc:
        raise ErrorVerificacion("La restauración no satisface los contratos de readiness.") from exc

    return {
        "readiness": {campo: bool(readiness[campo]) for campo in _READINESS_CONTABLE_CAMPOS},
        "row_counts": row_counts,
        "document_bytes": document_bytes,
    }


def comparar_manifest(manifest: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    for seccion in ("row_counts", "document_bytes"):
        esperado = manifest.get(seccion, {})
        if not isinstance(esperado, dict):
            raise ErrorVerificacion(f"El manifest tiene una sección {seccion} inválida.")
        for clave, valor in esperado.items():
            if clave not in actual[seccion]:
                raise ErrorVerificacion(f"El manifest contiene una métrica no permitida en {seccion}.")
            if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
                raise ErrorVerificacion(f"El manifest tiene un valor inválido en {seccion}.")
            if actual[seccion][clave] != valor:
                raise ErrorVerificacion(
                    f"No coincide {seccion}.{clave}: esperado {valor}, restaurado {actual[seccion][clave]}."
                )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as archivo:
            for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
                digest.update(bloque)
    except OSError as exc:
        raise ErrorVerificacion("No se pudo leer el archivo pg_dump indicado.") from exc
    return digest.hexdigest()


def _escribir_json_atomico(path: Path, contenido: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporal:
        json.dump(contenido, temporal, ensure_ascii=False, indent=2, sort_keys=True)
        temporal.write("\n")
        temporal_path = Path(temporal.name)
    temporal_path.replace(path)


def ejecutar(
    *,
    archive: Path,
    host: str,
    port: int,
    user: str,
    database: str,
    pg_restore: str = PG_RESTORE_DEFAULT,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    validar_destino(host, database)
    archive = archive.resolve()
    if not archive.is_file():
        raise ErrorVerificacion("El archivo pg_dump indicado no existe o no es regular.")
    try:
        with archive.open("rb") as archivo:
            if archivo.read(5) != b"PGDMP":
                raise ErrorVerificacion("El archivo no tiene formato custom de pg_dump.")
    except OSError as exc:
        raise ErrorVerificacion("No se pudo leer el archivo pg_dump indicado.") from exc
    if not Path(pg_restore).is_file():
        raise ErrorVerificacion("No se encontró pg_restore local.")

    archive_sha256 = _sha256(archive)
    try:
        archive_size = archive.stat().st_size
    except OSError as exc:
        raise ErrorVerificacion("No se pudo leer el archivo pg_dump indicado.") from exc
    exigir_base_vacia(host=host, port=port, user=user, database=database)
    _ejecutar_pg_restore(
        archive,
        host=host,
        port=port,
        user=user,
        database=database,
        pg_restore=pg_restore,
    )
    actual = verificar_contenido(host=host, port=port, user=user, database=database)
    try:
        archive_size_final = archive.stat().st_size
    except OSError as exc:
        raise ErrorVerificacion("No se pudo releer el archivo pg_dump indicado.") from exc
    if archive_size_final != archive_size or _sha256(archive) != archive_sha256:
        raise ErrorVerificacion("El archivo cambió durante el ensayo; el resultado no es verificable.")

    manifest_provisto = manifest_path is not None
    if manifest_path is not None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ErrorVerificacion("No se pudo leer el manifest JSON.") from exc
        if not isinstance(manifest, dict):
            raise ErrorVerificacion("El manifest debe ser un objeto JSON.")
        comparar_manifest(manifest, actual)

    return {
        "version": 1,
        "status": "OK",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive": {"sha256": archive_sha256, "size_bytes": archive_size},
        "destination": {"host": host, "database": database},
        "restore_options": [
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
        ],
        **actual,
        "manifest": {"provided": manifest_provisto, "matched": True if manifest_provisto else None},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restaura un pg_dump custom sólo en una base PostgreSQL local de ensayo vacía."
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--database", required=True)
    parser.add_argument("--host", default=os.environ.get("PGHOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PGPORT", "5432")))
    parser.add_argument("--user", default=os.environ.get("PGUSER", os.environ.get("USER", "postgres")))
    parser.add_argument("--pg-restore", default=PG_RESTORE_DEFAULT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        informe = ejecutar(
            archive=args.archive,
            host=args.host,
            port=args.port,
            user=args.user,
            database=args.database,
            pg_restore=args.pg_restore,
            manifest_path=args.manifest,
        )
        if args.report:
            _escribir_json_atomico(args.report, informe)
        print(json.dumps(informe, ensure_ascii=False, sort_keys=True))
        return 0
    except ErrorVerificacion as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
