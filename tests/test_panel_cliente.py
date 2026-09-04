"""
Tests del escritorio del portal (servicios/panel_cliente.py).

Existen por un bug concreto: el embudo mapeaba los estados a mano y se comía
3 de cada 5 envíos (EN_PROCESO y DESPACHADO no caían en ninguna rama, y
ENTREGADO no existe en la tabla). Como todo el módulo falla en vacío para no
tumbar el escritorio, el error no daba excepción: dejaba los contadores en
cero y parecía un cliente sin envíos.

El test que importa es el primero: recorre ESTADOS_VALIDOS de verdad, así que
el día que alguien agregue un estado nuevo y no lo mapee, falla acá en vez de
descubrirse por un cliente que no entiende por qué sus envíos desaparecieron.
"""
import os
import sys
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servicios.panel_cliente as pc  # noqa: E402
from servicios.solicitudes_guia import ESTADOS_VALIDOS  # noqa: E402


def _conn_falsa(filas=None, n=0):
    """get_conn() de mentira: devuelve lo que le pidas, sin base."""

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, q, p=None):
            pass

        def fetchall(self):
            return filas or []

        def fetchone(self):
            return {"n": n}

    @contextmanager
    def _fake():
        class C:
            def cursor(self):
                return Cur()

        yield C()

    return _fake


# ── Embudo ───────────────────────────────────────────────────

def test_ningun_estado_se_pierde_en_el_embudo():
    """
    Con un envío en cada estado válido, los contadores tienen que incluirlos
    todos. CANCELADO y REEMPLAZADO viven en la pestaña Canceladas.
    """
    filas = [{"estado": e, "n": 1} for e in ESTADOS_VALIDOS]
    with mock.patch.object(pc, "get_conn", _conn_falsa(filas, n=0)):
        pasos = pc.embudo_envios("TEST")

    contados = sum(p["cantidad"] for p in pasos)
    esperados = len(ESTADOS_VALIDOS)
    assert contados == esperados, (
        f"se pierden {esperados - contados} envío(s): "
        f"algún estado de {ESTADOS_VALIDOS} no está mapeado en embudo_envios"
    )


def test_cancelado_se_muestra_en_su_pestana():
    filas = [{"estado": "CANCELADO", "n": 7}]
    with mock.patch.object(pc, "get_conn", _conn_falsa(filas, n=0)):
        pasos = pc.embudo_envios("TEST")
    canceladas = next(p for p in pasos if p["clave"] == "canceladas")
    assert canceladas["cantidad"] == 7


def test_el_estado_en_proceso_espera_a_tauro():
    """EN_PROCESO es 'lo estamos trabajando': va en la tarjeta de Tauro."""
    filas = [{"estado": "EN_PROCESO", "n": 2}]
    with mock.patch.object(pc, "get_conn", _conn_falsa(filas, n=0)):
        pasos = {p["clave"]: p for p in pc.embudo_envios("TEST")}
    assert pasos["esperando_guia"]["cantidad"] == 2
    assert pasos["esperando_guia"]["accion_de"] == "tauro"


def test_despachado_se_presenta_como_recolectado():
    filas = [{"estado": "DESPACHADO", "n": 2}]
    with mock.patch.object(pc, "get_conn", _conn_falsa(filas, n=0)):
        pasos = {p["clave"]: p for p in pc.embudo_envios("TEST")}

    assert pasos["despachados"]["titulo"] == "Recolectados"
    assert pasos["despachados"]["cantidad"] == 2


def test_embudo_vacio_si_la_base_falla():
    """Nunca tumbar el escritorio: sin base, el bloque no se dibuja."""

    @contextmanager
    def rota():
        raise RuntimeError("sin base")
        yield  # pragma: no cover

    with mock.patch.object(pc, "get_conn", rota):
        assert pc.embudo_envios("TEST") == []


# ── Checklist ────────────────────────────────────────────────

def _checklist(productos, destinatarios, envios, activas, inactivas=0, aprobados=None):
    """
    `aprobados` simula el filtro activo=TRUE de la base: son los productos que
    Tauro ya validó. Por defecto están todos aprobados; pasando aprobados=0 se
    reproduce el cliente que acaba de cargar y todavía no lo revisó nadie.
    """
    aprobados = productos if aprobados is None else aprobados
    tiendas = [{"activa": True}] * activas + [{"activa": False}] * inactivas
    with mock.patch.object(pc, "get_conn", _conn_falsa(n=envios)), mock.patch(
        "servicios.catalogo.get_productos",
        lambda c, solo_activos=True: [1] * (aprobados if solo_activos else productos),
    ), mock.patch(
        "servicios.direcciones.contar_direcciones",
        lambda c: {"DESTINATARIO": destinatarios, "REMITENTE": 1},
    ), mock.patch(
        "servicios.integraciones_tienda.listar_tiendas", lambda c: tiendas
    ):
        return pc.checklist_arranque("TEST")


def test_el_producto_cuenta_aunque_tauro_no_lo_haya_aprobado():
    """
    get_productos() filtra por activo=TRUE por defecto. El paso mide una
    acción del cliente, no la validación del admin: si esperamos la
    aprobación, el cliente carga el producto y el tilde no aparece por días.
    """
    r = _checklist(productos=1, aprobados=0, destinatarios=0, envios=0, activas=0)
    assert r["pasos"][0]["hecho"]


def test_un_revendedor_no_necesita_catalogo_para_completar_el_arranque():
    """El catálogo acelera e integra, pero nunca bloquea una carga manual."""
    r = _checklist(productos=0, destinatarios=1, envios=1, activas=0)
    producto = next(p for p in r["pasos"] if p["clave"] == "producto")
    assert producto["opcional"]
    assert r["completo"]


def test_el_que_ya_despacha_no_ve_el_cartel_de_primeros_pasos():
    """
    Caso Pesca Jacks: vende por Shopify, el destinatario se lo trae el
    checkout y nunca usó la libreta. Sin esto el cartel de arranque le
    queda pegado arriba del escritorio para siempre.
    """
    r = _checklist(productos=12, destinatarios=0, envios=40, activas=1)
    assert r["completo"], "un cliente que ya opera sigue viendo 'Primeros pasos'"


def test_la_tienda_desconectada_no_cuenta():
    r = _checklist(productos=1, destinatarios=1, envios=1, activas=0, inactivas=1)
    assert not r["pasos"][3]["hecho"]


def test_la_barra_y_el_texto_miden_lo_mismo():
    """
    El % sale de los pasos obligatorios; el 'x de y' de al lado tiene que
    salir de los mismos, si no queda '67%' arriba de '3 de 4'.
    """
    casos = [
        dict(productos=0, destinatarios=0, envios=0, activas=0),
        dict(productos=1, destinatarios=0, envios=0, activas=0),
        dict(productos=0, destinatarios=0, envios=0, activas=1),
        dict(productos=3, destinatarios=1, envios=0, activas=1),
    ]
    for kw in casos:
        r = _checklist(**kw)
        esperado = round(r["hechos_oblig"] * 100 / r["total_oblig"])
        assert r["pct"] == esperado, (
            f"barra {r['pct']}% vs texto "
            f"{r['hechos_oblig']} de {r['total_oblig']} — con {kw}"
        )


def test_catalogo_compacta_la_barra_de_paginas():
    """Un catálogo grande no puede imprimir cien botones y forzar scroll."""
    fuente = open("templates/portal/catalogo.html", encoding="utf-8").read()
    assert "n >= pagina - 2 and n <= pagina + 2" in fuente
    assert 'class="ellipsis"' in fuente
    assert "n == total_paginas" in fuente
