from __future__ import annotations


def _shopify_order(fos, *, fulfillments=None):
    return {
        "order": {
            "fulfillments": fulfillments or [],
            "fulfillmentOrders": {"nodes": fos},
        }
    }


def test_shopify_multi_fo_va_a_manual_sin_mutacion(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token",
    })

    def graphql(_dominio, _token, query, variables=None):
        llamadas.append((query, variables))
        return _shopify_order([
            {"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN"},
            {"id": "gid://shopify/FulfillmentOrder/2", "status": "IN_PROGRESS"},
        ])

    monkeypatch.setattr(shopify_app, "_graphql", graphql)
    assert shopify_app.marcar_enviado_resultado(
        "piloto.myshopify.com", "123", "TRACK-1",
    ) == "MANUAL_REVIEW"
    assert len(llamadas) == 1


def test_shopify_timeout_post_write_exige_reconciliar(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token",
    })

    def graphql(_dominio, _token, query, variables=None):
        llamadas.append((query, variables))
        if "TauroFulfillmentOrders" in query:
            return _shopify_order([
                {"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN"},
            ])
        return None

    monkeypatch.setattr(shopify_app, "_graphql", graphql)
    assert shopify_app.marcar_enviado_resultado(
        "piloto.myshopify.com", "123", "TRACK-1",
    ) == "RECONCILIAR"
    assert len(llamadas) == 2


def test_shopify_ciclo_reconciliacion_es_solo_lectura(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token",
    })
    monkeypatch.setattr(
        shopify_app,
        "_graphql",
        lambda _dominio, _token, query, variables=None: (
            llamadas.append((query, variables))
            or _shopify_order([
                {"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN"},
            ])
        ),
    )
    assert shopify_app.marcar_enviado_resultado(
        "piloto.myshopify.com", "123", "TRACK-1", solo_reconciliar=True,
    ) == "REINTENTAR"
    assert len(llamadas) == 1


def test_fulfillment_worker_apagado_por_default_no_claim(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.delenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", raising=False)
    monkeypatch.setattr(
        ecommerce_outbox, "_claim_fulfillment",
        lambda: (_ for _ in ()).throw(AssertionError("no debe reclamar")),
    )
    assert ecommerce_outbox.procesar_fulfillments()["disabled"] is True


def test_fulfillment_excepcion_final_va_a_revision_manual(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.setenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", "true")
    trabajos = iter([{"id": 7, "claim_id": "claim", "intentos": 5}, None])
    monkeypatch.setattr(ecommerce_outbox, "_claim_fulfillment", lambda: next(trabajos))
    monkeypatch.setattr(
        ecommerce_outbox,
        "_ejecutar_fulfillment_bajo_lock",
        lambda _job: (_ for _ in ()).throw(TimeoutError("ambiguo")),
    )
    finales = []
    monkeypatch.setattr(
        ecommerce_outbox,
        "_finish_fulfillment",
        lambda _job, estado, codigo="", detalle="", remote_reference="":
            finales.append((estado, codigo, detalle)),
    )

    resultado = ecommerce_outbox.procesar_fulfillments()
    assert finales == [("MANUAL_REVIEW", "TimeoutError", "ambiguo")]
    assert resultado["manuales"] == 1


def test_tiendanube_reconciliar_se_propaga_como_ciclo_solo_lectura(monkeypatch):
    from servicios import ecommerce_outbox, integraciones_tienda, tiendanube_app

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return {"estado": "CONVERTIDO", "automatismos_bloqueados": False}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(ecommerce_outbox, "get_conn", lambda: Conn())
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_tiendanube", lambda *_args: None,
    )
    conciliaciones = []
    monkeypatch.setattr(
        tiendanube_app,
        "marcar_enviado",
        lambda *_args, **kwargs: (
            conciliaciones.append(kwargs.get("solo_reconciliar")) or False
        ),
    )

    resultado = ecommerce_outbox._ejecutar_fulfillment_bajo_lock({
        "plataforma": "tiendanube",
        "estado_anterior": "RECONCILIAR",
        "dominio": "123.tiendanube",
        "pedido_id": 7,
        "pedido_externo_id": "pedido-1",
        "tracking": "TRACK-1",
    })

    assert resultado == "REINTENTAR"
    assert conciliaciones == [True]
