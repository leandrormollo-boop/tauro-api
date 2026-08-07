"""
Referencia de mercado: lo que Boxfly (revendedor de FedEx) le cobró a TAURO.

Pedido de Leandro (07/08/2026): "hoy mismo podríamos armar un cotizador con
las tarifas de boxfly, para tener referencia de los precios de FedEx".

La fuente NO es un tarifario publicado —el cotizador público de Boxfly es
una pared de captura de datos— sino algo mejor: el historial REAL del portal
de TAURO como cliente, 420 envíos con precio pagado. Es el precio de mercado
demostrado, no el de vidriera.

Los precios se agrupan solos en ESCALONES (los brackets de peso de Boxfly).
Calibración conocida: dos envíos a Perú de 0,5 kg reales salieron ~ARS
41-43k, cobrados por peso REAL (no volumétrico). Los escalones de EE.UU.
todavía no están mapeados a kg — falta abrir un detalle por banda en el
portal de Boxfly o que Leandro los reconozca por producto.

SÓLO PARA EL ADMIN: es inteligencia competitiva. Nunca exponer en el portal
del cliente ni en la web.
"""
from __future__ import annotations

import json
import os

_RUTA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "datos", "referencia_boxfly.json")
_cache = None


def _datos() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_RUTA, encoding="utf-8") as fh:
                _cache = json.load(fh)
        except Exception as e:
            print(f"[referencia] no pude cargar el dataset de Boxfly: {e}")
            _cache = {"escalones_ars": {}, "envios": [], "calibracion": []}
    return _cache


def escalones(pais: str = None) -> dict:
    """Escalones de precio por país: {pais: [{desde, hasta, mediana, envios}]}."""
    esc = _datos().get("escalones_ars", {})
    if pais:
        return {pais: esc.get(pais, [])}
    return esc


def calibracion() -> list:
    return _datos().get("calibracion", [])


def resumen(dolar: float = None) -> list:
    """
    Para la pantalla del admin: una fila por (país, escalón), con USD si hay
    dólar. Ordenado por volumen de envíos del país.
    """
    esc = _datos().get("escalones_ars", {})
    orden = sorted(esc.items(),
                   key=lambda kv: -sum(e["envios"] for e in kv[1]))
    filas = []
    for pais, bandas in orden:
        total_pais = sum(e["envios"] for e in bandas)
        for i, e in enumerate(bandas, start=1):
            filas.append({
                "pais": pais,
                "total_pais": total_pais,
                "escalon": i,
                "desde": e["desde"], "hasta": e["hasta"],
                "mediana": e["mediana"], "envios": e["envios"],
                "mediana_usd": round(e["mediana"] / dolar, 2) if dolar else None,
            })
    return filas


def comparar(pais: str, precio_ars: float) -> dict:
    """
    ¿Dónde cae NUESTRO precio contra lo que Boxfly cobró a ese país?
    Devuelve el escalón más cercano y la diferencia — para saber si con ese
    precio le ganamos o perdemos contra el intermediario.
    """
    bandas = _datos().get("escalones_ars", {}).get(pais) or []
    if not bandas or not precio_ars:
        return {"hay_referencia": False}
    cercano = min(bandas, key=lambda e: abs(e["mediana"] - precio_ars))
    return {
        "hay_referencia": True,
        "escalon": cercano,
        "diferencia_ars": round(precio_ars - cercano["mediana"]),
        "ganamos": precio_ars < cercano["desde"],
    }
