"""La cotización del correo debe ser el snapshot exacto del servidor."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import main
from core.email_transport import EmailDeliveryResult
from servicios import cotizador, leads


class _Cursor:
    def __init__(self, respuestas=(), rowcounts=()):
        self.respuestas = iter(respuestas)
        self.rowcounts = iter(rowcounts)
        self.ejecutadas = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.ejecutadas.append((sql, tuple(params or ())))
        self.rowcount = next(self.rowcounts, 1)

    def fetchone(self):
        return next(self.respuestas)

    def fetchall(self):
        return next(self.respuestas)


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _conexion(cursor):
    @contextmanager
    def get_conn():
        yield _Conn(cursor)

    return get_conn


def _cotizacion(nombre="DHL Express", servicio="Worldwide"):
    ahora = datetime.now(timezone.utc)
    return {
        "id": 42,
        "quote_id": "Q-abcdefghijklmnopqrstuvwxyz123456",
        "referencia": "TW-20260818-ABC123",
        "origen": "CN",
        "destino": "IN",
        "origen_nombre": "China",
        "destino_nombre": "India",
        "peso_texto": "5,5",
        "medidas_texto": "30 × 20 × 10",
        "valor_texto": "100,00",
        "opciones": [{
            "id": "dhl",
            "nombre": nombre,
            "servicio": servicio,
            "dias_texto": "3-5 días",
            "precio_ars_texto": "100.000,00",
            "precio_usd_texto": "80,00",
            "recomendada": True,
        }],
        "emitida_texto": "18/08/2026 12:00",
        "vigente_texto": "19/08/2026 12:00",
        "emitida_en": ahora.isoformat(),
        "vigente_hasta": (ahora + timedelta(days=1)).isoformat(),
        "expirada": False,
        "url": "https://taurosolutions.ar/cotizacion/Q-abcdefghijklmnopqrstuvwxyz123456",
    }


def test_cotizador_rapido_canoniza_nombres_antes_de_decidir_ambito(monkeypatch):
    def no_llamar(*_args, **_kwargs):
        raise AssertionError("AR→AR no debe consultar ningún courier")

    monkeypatch.setattr("servicios.carriers.cotizar_carriers_cliente", no_llamar)
    with pytest.raises(ValueError, match="Andreani y OCA"):
        cotizador.cotizar_referencia_couriers(
            "TEST", "Argentina", "argentina", 1, 10, 10, 10, 100,
        )


def test_snapshot_persiste_tarifa_del_servidor_y_devuelve_referencia(monkeypatch):
    ahora = datetime.now(timezone.utc)
    cursor = _Cursor([{
        "id": 9,
        "created_at": ahora,
        "vigente_hasta": ahora + timedelta(hours=24),
    }])
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    resultado = leads.guardar_cotizacion(
        origen="China", destino="India", peso_kg="5.5",
        largo_cm=30, ancho_cm=20, alto_cm=10, valor_declarado_usd=100,
        carriers=[{
            "id": "dhl", "nombre": "DHL Express", "estado": "cotizado",
            "servicio": "Worldwide", "dias_estimados": "3-5",
            "precio_ars": 100000, "precio_usd": 80,
        }],
        recomendado="dhl",
    )

    sql, params = cursor.ejecutadas[-1]
    assert "INSERT INTO cotizaciones_web" in sql
    assert params[2:4] == ("CN", "IN")
    resumen = json.loads(params[10])
    assert resumen == [{
        "id": "dhl", "nombre": "DHL Express", "servicio": "Worldwide",
        "dias_estimados": "3-5", "precio_ars": "100000.00",
        "precio_usd": "80.00", "recomendada": True,
    }]
    assert resultado["quote_id"].startswith("Q-")
    assert resultado["referencia"].startswith("TW-")


def test_email_escapa_html_y_solo_confirma_si_smtp_acepta(monkeypatch):
    cotizacion = _cotizacion("<img src=x onerror=alert(1)>", "<b>Express</b>")
    cursor = _Cursor()
    capturado = {}
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *_a, **_k: cotizacion)
    monkeypatch.setattr(
        leads,
        "_reclamar_entrega",
        lambda *_a, **_k: leads._ReclamoEntrega("reclamado", 7, "claim"),
    )
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    def enviar(**kwargs):
        capturado.update(kwargs)
        return EmailDeliveryResult(True, "ACCEPTED", message_id="<m@tauro>")

    monkeypatch.setattr(leads, "send_transactional_email", enviar)
    resultado = leads.guardar_lead(
        "cliente@example.com", "Q-abcdefghijklmnopqrstuvwxyz123456"
    )

    assert resultado == {
        "ok": True, "estado": "enviado", "referencia": "TW-20260818-ABC123",
    }
    assert "<img src=x" not in capturado["html_body"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in capturado["html_body"]
    assert "<b>Express</b>" not in capturado["html_body"]
    assert "&lt;b&gt;Express&lt;/b&gt;" in capturado["html_body"]
    assert "TW-20260818-ABC123" in capturado["text_body"]
    assert cursor.ejecutadas[-1][1][0] == "ENVIADO"


def test_smtp_fallido_jamas_devuelve_enviado_y_permita_reintentar(monkeypatch):
    cursor = _Cursor()
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *_a, **_k: _cotizacion())
    monkeypatch.setattr(
        leads,
        "_reclamar_entrega",
        lambda *_a, **_k: leads._ReclamoEntrega("reclamado", 7, "claim"),
    )
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))
    monkeypatch.setattr(
        leads,
        "send_transactional_email",
        lambda **_kwargs: EmailDeliveryResult(False, "SMTP_NOT_CONFIGURED"),
    )

    resultado = leads.guardar_lead(
        "cliente@example.com", "Q-abcdefghijklmnopqrstuvwxyz123456"
    )
    assert resultado["ok"] is False
    assert resultado["estado"] == "fallido"
    assert cursor.ejecutadas[-1][1][0] == "FALLIDO"
    assert cursor.ejecutadas[-1][1][1] == "SMTP_NOT_CONFIGURED"


def test_un_snapshot_ya_enviado_no_manda_un_segundo_mail(monkeypatch):
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *_a, **_k: _cotizacion())
    monkeypatch.setattr(
        leads,
        "_reclamar_entrega",
        lambda *_a, **_k: leads._ReclamoEntrega("enviado", 7),
    )
    monkeypatch.setattr(
        leads,
        "send_transactional_email",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no reenviar")),
    )
    assert leads.guardar_lead(
        "cliente@example.com", "Q-abcdefghijklmnopqrstuvwxyz123456"
    )["estado"] == "enviado"


def test_claim_es_atomico_por_destinatario_y_limita_24h_entre_quotes(monkeypatch):
    cursor = _Cursor([None, None, {"total": 0}, {"id": 91}])
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    reclamo = leads._reclamar_entrega("Cliente@Example.com", _cotizacion())

    assert reclamo.estado == "reclamado"
    assert reclamo.lead_id == 91 and reclamo.claim
    sql = "\n".join(query for query, _params in cursor.ejecutadas)
    assert "pg_advisory_xact_lock" in sql
    assert "hashtextextended(LOWER(BTRIM(%s))" in sql
    assert "NOW() - INTERVAL '24 hours'" in sql
    assert "cotizacion_id = %s AND LOWER(email) = LOWER(%s)" in sql
    assert "email_estado IN ('PENDIENTE', 'FALLIDO')" in sql
    assert "tauro:cotizaciones-email:global" in sql
    assert "created_at > NOW() - INTERVAL '1 hour'" in sql
    assert "INSERT INTO leads_cotizacion" in sql
    lock_pos = next(i for i, (q, _p) in enumerate(cursor.ejecutadas) if "advisory" in q)
    limite_pos = next(
        i for i, (q, _p) in enumerate(cursor.ejecutadas)
        if "INTERVAL '24 hours'" in q
    )
    assert lock_pos < limite_pos


def test_tope_global_durable_no_depende_de_headers_ip(monkeypatch):
    monkeypatch.setenv("EMAIL_COTIZACIONES_MAX_HORA", "100")
    cursor = _Cursor([None, None, {"total": 100}])
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    reclamo = leads._reclamar_entrega("nuevo@example.com", _cotizacion())

    assert reclamo == leads._ReclamoEntrega("limitado")
    assert not any("INSERT INTO leads_cotizacion" in q for q, _p in cursor.ejecutadas)
    assert any("tauro:cotizaciones-email:global" in q for q, _p in cursor.ejecutadas)


def test_limite_por_email_no_depende_del_quote_id_y_no_crea_otro_lead(monkeypatch):
    cursor = _Cursor([None, {"id": 88}])
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    reclamo = leads._reclamar_entrega("cliente@example.com", _cotizacion())

    assert reclamo == leads._ReclamoEntrega("limitado")
    assert not any("INSERT INTO leads_cotizacion" in q for q, _p in cursor.ejecutadas)
    consulta_limite = next(q for q, _p in cursor.ejecutadas if "24 hours" in q)
    assert "LOWER(email) = LOWER(%s)" in consulta_limite
    assert "'PENDIENTE', 'ENVIANDO', 'FALLIDO'" in consulta_limite
    assert "AND id <> %s" in consulta_limite
    assert "cotizacion_id" not in consulta_limite


def test_enviando_stale_pasa_a_verificar_y_no_se_reclama(monkeypatch):
    cursor = _Cursor([{"id": 7, "email_estado": "VERIFICAR_EMAIL"}])
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    reclamo = leads._reclamar_entrega("cliente@example.com", _cotizacion())

    assert reclamo.estado == "verificar"
    sql = "\n".join(q for q, _p in cursor.ejecutadas)
    assert "email_estado = 'VERIFICAR_EMAIL'" in sql
    assert "email_error_codigo = 'SMTP_OUTCOME_UNKNOWN'" in sql
    assert "email_estado = 'ENVIANDO'" in sql
    assert "INTERVAL '5 minutes'" in sql
    assert not any("SET email_estado = 'ENVIANDO'" in q for q, _p in cursor.ejecutadas)


def test_resultado_smtp_no_confirmado_por_claim_jamas_declara_exito(monkeypatch):
    cursor = _Cursor(rowcounts=[0])
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *_a, **_k: _cotizacion())
    monkeypatch.setattr(
        leads,
        "_reclamar_entrega",
        lambda *_a, **_k: leads._ReclamoEntrega("reclamado", 7, "claim"),
    )
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))
    monkeypatch.setattr(
        leads,
        "send_transactional_email",
        lambda **_kwargs: EmailDeliveryResult(True, "ACCEPTED", message_id="<m@tauro>"),
    )

    resultado = leads.guardar_lead(
        "cliente@example.com", "Q-abcdefghijklmnopqrstuvwxyz123456"
    )

    assert resultado["ok"] is False
    assert resultado["estado"] == "verificar"
    sql = cursor.ejecutadas[-1][0]
    assert "email_claim = %s" in sql
    assert "email_estado = 'ENVIANDO'" in sql


@pytest.mark.parametrize("codigo", ["SMTP_TIMEOUT", "SMTP_NETWORK", "SMTP_ERROR"])
def test_fallo_smtp_incierto_se_aísla_y_no_queda_reintentable(monkeypatch, codigo):
    cursor = _Cursor()
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *_a, **_k: _cotizacion())
    monkeypatch.setattr(
        leads,
        "_reclamar_entrega",
        lambda *_a, **_k: leads._ReclamoEntrega("reclamado", 7, "claim"),
    )
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))
    monkeypatch.setattr(
        leads,
        "send_transactional_email",
        lambda **_kwargs: EmailDeliveryResult(False, codigo, retryable=True),
    )

    resultado = leads.guardar_lead(
        "cliente@example.com", "Q-abcdefghijklmnopqrstuvwxyz123456"
    )

    assert resultado["ok"] is False
    assert resultado["estado"] == "verificar"
    assert cursor.ejecutadas[-1][1][0] == "VERIFICAR_EMAIL"
    assert cursor.ejecutadas[-1][1][1] == codigo


def test_contrato_publico_no_acepta_carriers_ni_precios_del_navegador():
    with pytest.raises(ValidationError):
        main.LeadCotizacionRequest(
            email="cliente@example.com",
            quote_id="Q-abcdefghijklmnopqrstuvwxyz123456",
            carriers=[{"nombre": "DHL", "precio_ars": 1}],
        )
    with pytest.raises(ValidationError):
        main.LeadCotizacionRequest(email="cliente@example.com")


def test_widget_y_schema_transportan_solo_el_quote_id():
    raiz = Path(__file__).resolve().parent.parent
    widget = (raiz / "web" / "components" / "02-quote-widget.jsx").read_text(
        encoding="utf-8"
    )
    schema = (raiz / "sql" / "schema.sql").read_text(encoding="utf-8")

    assert "quoteId={result.quote_id}" in widget
    assert "quote_id: quoteId" in widget
    assert "carriers: (carriers || [])" not in widget
    assert "CREATE TABLE IF NOT EXISTS cotizaciones_web" in schema
    assert "uq_lead_cotizacion_email" in schema


def test_servicio_de_leads_no_ejecuta_ddl_en_requests():
    fuente = Path(leads.__file__).read_text(encoding="utf-8")
    assert "CREATE TABLE" not in fuente
    assert "ALTER TABLE" not in fuente
    assert "CREATE INDEX" not in fuente
    assert "def _ensure_tabla" not in fuente


def test_reintento_automatico_solo_procesa_fallos_transitorios(monkeypatch):
    cursor = _Cursor([[
        {"email": "uno@example.com", "public_id": "Q-uno"},
        {"email": "dos@example.com", "public_id": "Q-dos"},
    ]])
    procesados = []
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))
    monkeypatch.setattr(
        "core.email_transport.email_config_status",
        lambda: {"configured": True},
    )
    monkeypatch.setattr(
        leads, "guardar_lead",
        lambda email, quote: (procesados.append((email, quote)) or {"ok": True}),
    )

    resultado = leads.procesar_reintentos_email()

    sql = "\n".join(item[0] for item in cursor.ejecutadas)
    assert "email_error_codigo = 'SMTP_TEMPORARY'" in sql
    assert "SMTP_TIMEOUT" not in sql and "SMTP_NETWORK" not in sql
    assert "email_estado = 'VERIFICAR_EMAIL'" in sql
    assert "email_estado = 'ENVIANDO'" in sql
    assert resultado == {"procesados": 2, "enviados": 2}
    assert procesados == [
        ("uno@example.com", "Q-uno"),
        ("dos@example.com", "Q-dos"),
    ]


def test_limpieza_respeta_retencion_y_quotes_referenciadas(monkeypatch):
    cursor = _Cursor(rowcounts=[2, 3])
    monkeypatch.setattr(leads, "get_conn", _conexion(cursor))

    resultado = leads.limpiar_retencion_cotizaciones(
        leads_dias=365, cotizaciones_dias=30
    )

    assert resultado == {"leads_eliminados": 2, "cotizaciones_eliminadas": 3}
    assert cursor.ejecutadas[0][1] == (365,)
    assert "email_estado <> 'ENVIANDO'" in cursor.ejecutadas[0][0]
    assert cursor.ejecutadas[1][1] == (30,)
    assert "NOT EXISTS" in cursor.ejecutadas[1][0]
    assert "l.cotizacion_id = q.id" in cursor.ejecutadas[1][0]


def test_copia_web_es_privada_imprimible_y_marca_vencimiento(monkeypatch):
    cotizacion = _cotizacion()
    cotizacion["expirada"] = True
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *_a, **_k: cotizacion)

    respuesta = main.cotizacion_publica("Q-abcdefghijklmnopqrstuvwxyz123456")

    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert respuesta.headers["x-robots-tag"] == "noindex, nofollow"
    assert b"ESTIMACI" in respuesta.body and b"VENCIDA" in respuesta.body
    assert b"Cotizar nuevamente" in respuesta.body
