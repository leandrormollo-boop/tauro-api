"""Piloto local del portal contra OCA QA. Almacenamiento separado de clientes reales."""

from __future__ import annotations
import hashlib, json, sqlite3, time, uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from servicios.oca_adapter import OCAAdapter, OCAConfig, _shipment_xml
from servicios.carrier_adapter import Package, QuoteRequest, OperationState
from servicios.carrier_contract import Ambito
from datetime import date


class QAPortalError(ValueError):
    pass


class QAPortal:
    def __init__(self, path, config, adapter=None):
        if (
            config.environment != "qa"
            or config.cuit != "30-53625919-4"
            or config.account != "111757/001"
            or config.operation != 64665
            or config.username != "test@oca.com.ar"
        ):
            raise QAPortalError("Este piloto sólo acepta la cuenta de prueba de OCA.")
        if (
            not config.confirm_withdrawal
            or config.cost_center != "2"
            or config.insured_operation
        ):
            raise QAPortalError(
                "El escenario QA requiere PaP sin seguro, centro 2 y confirmación de prueba."
            )
        config.assert_fulfillment_ready()
        self.config = config
        self.adapter = adapter or OCAAdapter(
            config, pricing_loader=lambda *_: {"tipo": "PCT", "valor": "0"}
        )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fingerprint = hashlib.sha256(
            f"{config.environment}|{config.cuit}|{config.account}|{config.operation}|{config.cost_center}".encode()
        ).hexdigest()
        with self.db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS operaciones (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                quote_id TEXT NOT NULL, price TEXT NOT NULL, external_id TEXT NOT NULL DEFAULT '',
                tracking TEXT NOT NULL DEFAULT '', pdf BLOB, error TEXT NOT NULL DEFAULT '',
                events TEXT NOT NULL DEFAULT '[]')""")

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def get(self, ident, owner):
        with self.db() as c:
            r = c.execute(
                "SELECT * FROM operaciones WHERE id=? AND owner=?", (ident, owner)
            ).fetchone()
        if not r or r["fingerprint"] != self.fingerprint:
            raise QAPortalError(
                "Operación no disponible para esta sesión y configuración."
            )
        return dict(r)

    def list(self, owner):
        with self.db() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT id,state,tracking,price FROM operaciones WHERE owner=? AND fingerprint=? ORDER BY created DESC LIMIT 20",
                    (owner, self.fingerprint),
                )
            ]

    def quote(self, owner, values):
        dimensions = []
        for key, maximum in [
            ("weight", 30),
            ("length", 100),
            ("width", 100),
            ("height", 100),
        ]:
            try:
                v = Decimal(str(values[key]).replace(",", "."))
            except Exception:
                raise QAPortalError("Ingresá peso y medidas válidos.") from None
            if not v.is_finite() or not 0 < v <= maximum:
                raise QAPortalError(
                    "Peso o medidas fuera de los límites del piloto QA."
                )
            dimensions.append(v)
        ident = uuid.uuid4().hex
        weight, length, width, height = dimensions
        shipment = {
            "origin": {
                "calle": "VENEZUELA",
                "nro": "3446",
                "cp": "1211",
                "localidad": "CAPITAL FEDERAL",
                "provincia": "CAPITAL FEDERAL",
                "contacto": "PRUEBA TAURO QA",
                "email": "qa@example.invalid",
                "observaciones": "PRUEBA QA - NO RETIRAR",
            },
            "destination": {
                "calle": "BALCARCE",
                "nro": "50",
                "cp": "1214",
                "localidad": "CAPITAL FEDERAL",
                "provincia": "CAPITAL FEDERAL",
                "observaciones": "PRUEBA QA - NO ENTREGAR",
            },
            "recipient": {"first_name": "PRUEBA", "last_name": "TAURO QA"},
            "packages": [
                {
                    "quantity": 1,
                    "weight_kg": str(weight),
                    "length_cm": str(length),
                    "width_cm": str(width),
                    "height_cm": str(height),
                }
            ],
            "declared_value": "1",
        }
        _shipment_xml(shipment, self.config, idempotency_key=ident, today=date.today())
        request = QuoteRequest(
            request_id=ident,
            customer_id=owner,
            scope=Ambito.NACIONAL,
            origin={"pais": "AR", "codigo_postal": "1211"},
            destination={"pais": "AR", "codigo_postal": "1214"},
            packages=(Package(1, weight, length, width, height),),
            declared_value=Decimal("1"),
            declared_currency="ARS",
        )
        result = self.adapter.quote(request)[0]
        if result.state != OperationState.COTIZADO:
            raise QAPortalError("OCA no devolvió una tarifa para esta prueba.")
        with self.db() as c:
            c.execute(
                "INSERT INTO operaciones(id,owner,state,created,fingerprint,payload,quote_id,price) VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    owner,
                    "COTIZADO",
                    time.time(),
                    self.fingerprint,
                    json.dumps(shipment),
                    result.quote_id,
                    str(result.customer_price),
                ),
            )
        return ident

    def claim(self, ident, owner, expected, target):
        row = self.get(ident, owner)
        if expected == "COTIZADO" and time.time() - row["created"] > 900:
            raise QAPortalError("La cotización venció. Volvé a cotizar.")
        with self.db() as c:
            updated = c.execute(
                "UPDATE operaciones SET state=?,error=? WHERE id=? AND owner=? AND state=? AND fingerprint=?",
                (target, "", ident, owner, expected, self.fingerprint),
            ).rowcount
        return row if updated == 1 else None

    def update(self, ident, **values):
        allowed = {"state", "external_id", "tracking", "pdf", "error", "events"}
        if not values.keys() <= allowed:
            raise ValueError("invalid fields")
        with self.db() as c:
            c.execute(
                "UPDATE operaciones SET "
                + ",".join(k + "=?" for k in values)
                + " WHERE id=?",
                (*values.values(), ident),
            )

    def emit(self, ident, owner):
        row = self.claim(ident, owner, "COTIZADO", "EMITIENDO")
        if row is None:
            return self.get(ident, owner)
        try:
            result = self.adapter.create_shipment(
                row["quote_id"], json.loads(row["payload"]), idempotency_key=ident
            )
            if result.state != OperationState.EMITIDO or not result.external_id:
                raise QAPortalError("Respuesta de emisión incompleta.")
            # Persist before a separate, retryable label read.
            self.update(
                ident,
                state="EMITIDO",
                external_id=result.external_id,
                tracking=result.tracking,
            )
        except Exception:
            self.update(
                ident,
                state="INCIERTO",
                error="OCA pudo recibir la orden. No se reintentará; requiere conciliación por remito.",
            )
            return self.get(ident, owner)
        return self.label(ident, owner)

    def label(self, ident, owner):
        row = self.get(ident, owner)
        if row["state"] not in {"EMITIDO", "ETIQUETA_LISTA", "CANCELADO"}:
            raise QAPortalError("La orden todavía no tiene una etiqueta disponible.")
        try:
            pdf = self.adapter.get_label(row["external_id"]).label_pdf
            if not pdf or not pdf.startswith(b"%PDF-"):
                raise QAPortalError("PDF inválido.")
            # Do not overwrite a concurrent cancellation state.
            with self.db() as c:
                c.execute(
                    "UPDATE operaciones SET pdf=?, error='', state=CASE WHEN state='EMITIDO' THEN 'ETIQUETA_LISTA' ELSE state END WHERE id=?",
                    (pdf, ident),
                )
        except Exception:
            self.update(
                ident,
                error="La guía existe, pero falta recuperar el PDF. Reintentar la descarga no genera otra guía.",
            )
        return self.get(ident, owner)

    def cancel(self, ident, owner):
        row = self.get(ident, owner)
        if row["state"] not in {"EMITIDO", "ETIQUETA_LISTA"}:
            return row
        row = self.claim(ident, owner, row["state"], "ANULANDO")
        if row is None:
            return self.get(ident, owner)
        try:
            result = self.adapter.cancel(
                row["external_id"], idempotency_key="cancel-" + ident
            )
            if result != OperationState.CANCELADO:
                raise QAPortalError("Anulación no confirmada.")
            self.update(ident, state="CANCELADO")
        except Exception:
            self.update(
                ident,
                state="ANULACION_INCIERTA",
                error="No se confirmó la anulación; requiere revisión, sin reintentos automáticos.",
            )
        return self.get(ident, owner)

    def track(self, ident, owner):
        row = self.get(ident, owner)
        if not row["tracking"]:
            raise QAPortalError("Todavía no hay número de guía.")
        result = self.adapter.track(row["tracking"])
        self.update(
            ident,
            events=json.dumps(
                [
                    {"code": e.code, "label": e.label, "date": e.occurred_at_iso}
                    for e in result.events
                ]
            ),
        )
        return self.get(ident, owner)
