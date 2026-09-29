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
    ) == "RECONCILIAR"
    assert len(llamadas) == 1


def test_fulfillment_worker_apagado_por_default_no_claim(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.delenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", raising=False)
    monkeypatch.setattr(
        ecommerce_outbox, "_claim_fulfillment",
        lambda: (_ for _ in ()).throw(AssertionError("no debe reclamar")),
    )
    assert ecommerce_outbox.procesar_fulfillments()["disabled"] is True


def test_fulfillment_allowlist_vacia_falla_cerrada(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.setenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", "true")
    monkeypatch.delenv("ECOMMERCE_FULFILLMENT_ALLOWLIST", raising=False)
    monkeypatch.setattr(
        ecommerce_outbox,
        "_claim_fulfillment",
        lambda *_: (_ for _ in ()).throw(AssertionError("no debe reclamar")),
    )
    monkeypatch.setattr(
        ecommerce_outbox,
        "_excluded_fulfillment_domains",
        lambda *_: (_ for _ in ()).throw(AssertionError("no debe consultar")),
    )

    resultado = ecommerce_outbox.procesar_fulfillments()

    assert resultado == {
        "procesados": 0,
        "errores": 0,
        "manuales": 0,
        "allowlist_empty": True,
    }


def test_fulfillment_allowlist_se_relee_y_normaliza_en_cada_ciclo(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.setenv(
        "ECOMMERCE_FULFILLMENT_ALLOWLIST", " Tienda-A.MyShopify.com ",
    )
    assert ecommerce_outbox._fulfillment_allowlist() == frozenset({
        "tienda-a.myshopify.com",
    })

    monkeypatch.setenv(
        "ECOMMERCE_FULFILLMENT_ALLOWLIST", "tienda-b.myshopify.com",
    )
    assert ecommerce_outbox._fulfillment_allowlist() == frozenset({
        "tienda-b.myshopify.com",
    })


def test_fulfillment_allowlist_incluida_procesa_la_tienda(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.setenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "ECOMMERCE_FULFILLMENT_ALLOWLIST",
        " Piloto.MyShopify.com, piloto.myshopify.com ",
    )
    vistos = []
    trabajos = iter([
        {"id": 7, "claim_id": "claim", "intentos": 1},
        None,
    ])

    def claim(allowed_domains):
        vistos.append(allowed_domains)
        return next(trabajos)

    monkeypatch.setattr(
        ecommerce_outbox, "_excluded_fulfillment_domains", lambda _allowed: [],
    )
    monkeypatch.setattr(ecommerce_outbox, "_claim_fulfillment", claim)
    monkeypatch.setattr(
        ecommerce_outbox, "_ejecutar_fulfillment_bajo_lock", lambda _job: "COMPLETADO",
    )
    finales = []
    monkeypatch.setattr(
        ecommerce_outbox,
        "_finish_fulfillment",
        lambda _job, estado, *args, **kwargs: finales.append(estado),
    )

    resultado = ecommerce_outbox.procesar_fulfillments()

    assert vistos == [
        frozenset({"piloto.myshopify.com"}),
        frozenset({"piloto.myshopify.com"}),
    ]
    assert finales == ["COMPLETADO"]
    assert resultado["procesados"] == 1


def test_fulfillment_allowlist_excluida_no_reclama_y_deja_log(
    monkeypatch, capsys,
):
    from servicios import ecommerce_outbox

    monkeypatch.setenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "ECOMMERCE_FULFILLMENT_ALLOWLIST", "permitida.myshopify.com",
    )
    monkeypatch.setattr(
        ecommerce_outbox,
        "_excluded_fulfillment_domains",
        lambda _allowed: ["prueba.myshopify.com"],
    )
    monkeypatch.setattr(ecommerce_outbox, "_claim_fulfillment", lambda _allowed: None)
    monkeypatch.setattr(
        ecommerce_outbox,
        "_finish_fulfillment",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no debe finalizar")),
    )

    resultado = ecommerce_outbox.procesar_fulfillments()

    assert resultado == {"procesados": 0, "errores": 0, "manuales": 0}
    assert capsys.readouterr().out == (
        "[fulfillment] tienda fuera de allowlist: prueba.myshopify.com\n"
    )


def test_fulfillment_excepcion_final_va_a_revision_manual(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.setenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "ECOMMERCE_FULFILLMENT_ALLOWLIST", "piloto.myshopify.com",
    )
    trabajos = iter([{"id": 7, "claim_id": "claim", "intentos": 5}, None])
    monkeypatch.setattr(
        ecommerce_outbox, "_excluded_fulfillment_domains", lambda _allowed: [],
    )
    monkeypatch.setattr(
        ecommerce_outbox, "_claim_fulfillment", lambda _allowed: next(trabajos),
    )
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

    assert resultado == "RECONCILIAR"
    assert conciliaciones == [True]


def test_resultado_ambiguo_nunca_regresa_al_camino_de_mutacion(monkeypatch):
    from servicios import ecommerce_outbox

    monkeypatch.setenv("ECOMMERCE_FULFILLMENT_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "ECOMMERCE_FULFILLMENT_ALLOWLIST", "piloto.myshopify.com",
    )
    trabajos = iter([
        {"id": 7, "claim_id": "c1", "intentos": 1},
        {"id": 7, "claim_id": "c2", "intentos": 2},
        {"id": 7, "claim_id": "c3", "intentos": 5},
        None,
    ])
    monkeypatch.setattr(
        ecommerce_outbox, "_excluded_fulfillment_domains", lambda _allowed: [],
    )
    monkeypatch.setattr(
        ecommerce_outbox, "_claim_fulfillment", lambda _allowed: next(trabajos),
    )
    monkeypatch.setattr(
        ecommerce_outbox,
        "_ejecutar_fulfillment_bajo_lock",
        lambda _job: "RECONCILIAR",
    )
    finales = []
    monkeypatch.setattr(
        ecommerce_outbox,
        "_finish_fulfillment",
        lambda job, estado, codigo="", detalle="", remote_reference="":
            finales.append((job["intentos"], estado, codigo)),
    )

    resultado = ecommerce_outbox.procesar_fulfillments(limite=10)

    assert finales == [
        (1, "RECONCILIAR", "RESULTADO_AMBIGUO"),
        (2, "RECONCILIAR", "RESULTADO_AMBIGUO"),
        (5, "MANUAL_REVIEW", "FULFILLMENT_NO_SEGURO"),
    ]
    assert resultado["manuales"] == 1
