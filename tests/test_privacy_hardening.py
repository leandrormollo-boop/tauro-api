from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import main
from core import database, sheets_client
from jobs import sync_sheet_tauro
from servicios import integraciones_tienda, solicitudes_guia
from starlette.requests import Request
from starlette.responses import JSONResponse


ROOT = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self, filas=None, rowcount=0):
        self.filas = filas or []
        self.rowcount = rowcount
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.query += str(query)

    def fetchall(self):
        return self.filas


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


class _Hoja:
    def __init__(self):
        self.filas = None
        self.rango_formato = None
        self.cleared = False

    def clear(self):
        self.cleared = True

    def update(self, filas, rango):
        assert rango == "A1"
        self.filas = filas

    def format(self, rango, formato):
        self.rango_formato = rango


class _Documento:
    def __init__(self, hoja):
        self.hoja = hoja
        self.pestana_pedida = None

    def worksheet(self, nombre):
        self.pestana_pedida = nombre
        return self.hoja


class _Sheets:
    def __init__(self, documento):
        self.documento = documento

    def open_by_key(self, _):
        return self.documento


def test_sheet_operativo_no_copia_identificadores_del_comprador(monkeypatch):
    fila = {
        "id": 91,
        "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        "cliente_id": "MELCIOR",
        "estado": "GUIA_LISTA",
        "ambito": "INTERNACIONAL",
        "remitente_pais": "AR",
        "courier": "DHL",
        "producto_alias": "LANA",
        "cantidad": 2,
        "destino_pais": "US",
        "peso_kg": 1.2,
        "valor_declarado_usd": 40,
        "precio_tauro_ars": 100000,
        "precio_tauro_usd": 100,
        # Si el job vuelve a leer estas claves por accidente, el assertion
        # final detecta la filtración al Sheet.
        "dest_nombre": "NOMBRE COMPRADOR SECRETO",
        "dest_ciudad": "CIUDAD COMPRADOR SECRETA",
        "tracking": "TRACKING-SECRETO",
    }
    cursor = _Cursor([fila])
    conn = _Conn(cursor)
    hoja = _Hoja()
    documento = _Documento(hoja)

    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "configurada")
    monkeypatch.setattr(database, "get_conn", lambda: conn)
    monkeypatch.setattr(
        sheets_client, "get_cliente_sheets", lambda: _Sheets(documento),
    )

    sync_sheet_tauro.sincronizar()

    assert documento.pestana_pedida == "PLATAFORMA_SIN_PII"
    assert hoja.cleared is True
    assert hoja.filas[0] == sync_sheet_tauro.ENCABEZADOS
    serializado = repr(hoja.filas)
    for secreto in ("NOMBRE COMPRADOR SECRETO", "CIUDAD COMPRADOR SECRETA", "TRACKING-SECRETO"):
        assert secreto not in serializado
    for columna in ("dest_nombre", "dest_ciudad", "tracking"):
        assert columna not in cursor.query


def test_poda_global_de_huerfanos_es_independiente_de_nuevos_webhooks(monkeypatch):
    cursor = _Cursor(rowcount=7)
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    eliminados = integraciones_tienda.limpiar_pedidos_huerfanos_vencidos()

    assert eliminados == 14
    assert "DELETE FROM pedidos_huerfanos" in cursor.query
    assert "DELETE FROM shopify_huerfanos_cancelados" in cursor.query
    assert "INTERVAL '90 days'" in cursor.query
    assert conn.commits == 1


def test_scheduler_registra_la_poda_diaria_de_huerfanos():
    jobs = main.scheduler.get_jobs()
    assert any(
        getattr(job.func, "__name__", "") == "job_limpiar_pedidos_huerfanos"
        for job in jobs
    )


def test_descarga_b2b_de_guia_es_privada_y_no_cacheable(monkeypatch):
    monkeypatch.setattr(main, "autenticar", lambda _: {"cliente_id": "MELCIOR"})
    monkeypatch.setattr(
        solicitudes_guia,
        "obtener_label_de_cliente",
        lambda solicitud_id, cliente_id: b"%PDF-1.4\nprivado",
    )

    respuesta = main.descargar_guia_api(91, x_api_key="tauro_test")

    assert respuesta.headers["cache-control"] == "private, no-store"


def test_json_b2b_privado_no_se_cachea_ni_mezcla_api_keys():
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/stock",
        "headers": [(b"host", b"testserver"), (b"x-api-key", b"tauro_test")],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    })

    async def continuar(_request):
        return JSONResponse({"ok": True})

    respuesta = asyncio.run(main.headers_de_seguridad(request, continuar))

    assert respuesta.status_code == 200
    assert "private" in respuesta.headers["cache-control"]
    assert "no-store" in respuesta.headers["cache-control"]
    assert "X-API-Key" in respuesta.headers["vary"]


def test_todas_las_descargas_privadas_declaran_no_store():
    """Evita que una nueva descarga con Content-Disposition olvide el header."""
    for ruta in (ROOT / "main.py", ROOT / "endpoints/admin.py", ROOT / "endpoints/portal_cliente.py"):
        lineas = ruta.read_text(encoding="utf-8").splitlines()
        for indice, linea in enumerate(lineas):
            if "Content-Disposition" not in linea:
                continue
            bloque = "\n".join(lineas[indice:indice + 7])
            assert '"Cache-Control": "private, no-store"' in bloque, (
                f"{ruta.name}:{indice + 1} descarga sin private, no-store"
            )


def test_adaptadores_no_referencian_el_body_crudo_de_respuestas():
    for relativo in ("core/fedex_client.py", "core/dhl_client.py", "core/ups_client.py"):
        fuente = (ROOT / relativo).read_text(encoding="utf-8")
        assert ".text" not in fuente, f"{relativo} vuelve a leer/loguear un body crudo"


def test_admin_nunca_imprime_una_contrasena_temporal():
    fuente = (ROOT / "endpoints/admin.py").read_text(encoding="utf-8")
    assert "Contraseña temporal" not in fuente
    assert "ADMIN_PASSWORD}" not in fuente
    assert "login por contraseña deshabilitado" in fuente


def test_runbook_no_declara_aplicados_controles_externos_pendientes():
    runbook = (ROOT / "docs/PRIVACIDAD_SHOPIFY_OPERACION.md").read_text(encoding="utf-8")
    assert "no la modifica ni la borra" in runbook
    assert "deuda operativa conocida" in runbook
    assert "bloqueada" in runbook
    assert "No se considera aplicado" in runbook
