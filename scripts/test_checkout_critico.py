#!/usr/bin/env python3
"""
Tests del CAMINO CRÍTICO del comprador.
=======================================
Lo que se rompe acá se traduce en ventas perdidas, así que corre contra
producción y falla ruidosamente.

    python3 scripts/test_checkout_critico.py
    python3 scripts/test_checkout_critico.py http://localhost:8123

Qué garantiza:
  1. El checkout SIEMPRE devuelve al menos una opción de envío.
  2. Responde bien por debajo del corte de Shopify (~10s).
  3. Los precios tienen escala razonable (nunca 0, nunca absurdos).
  4. Los webhooks rechazan firmas inválidas.
  5. Las superficies públicas están vivas.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://taurosolutions.ar").rstrip("/")

# Shopify corta cerca de los 10s; nos exigimos la mitad para tener aire.
LIMITE_SEGUNDOS = 5.0

fallos: list[str] = []
avisos: list[str] = []


def _post(path: str, payload: dict, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, None, time.time() - t0


def _get(path: str, timeout: int = 20):
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
            return r.status, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0


def check(ok: bool, descripcion: str, detalle: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {descripcion}{f' — {detalle}' if detalle else ''}")
    if not ok:
        fallos.append(f"{descripcion}{f' — {detalle}' if detalle else ''}")


print(f"\nCAMINO CRÍTICO DEL COMPRADOR · {BASE}\n" + "=" * 60)

# ── 1. El checkout nunca deja al comprador sin opción de envío ──
print("\n[1] Tarifas en el checkout")
CASOS = [
    ("US", 850, 1, 5_000_00, "Estados Unidos, 850g"),
    ("BR", 1200, 2, 8_000_00, "Brasil, 2 unidades"),
    ("ES", 5000, 1, 25_000_00, "España, 5kg"),
    ("MX", 15000, 1, 50_000_00, "México, 15kg"),
    ("CL", 300, 1, 2_000_00, "Chile, liviano"),
    ("US", 90000, 1, 90_000_00, "USA, 90kg (arriba del último escalón)"),
]
for pais, gramos, qty, precio, nombre in CASOS:
    payload = {"rate": {
        "destination": {"country": pais, "city": "Ciudad", "postal_code": "10001"},
        "items": [{"grams": gramos, "quantity": qty, "price": precio, "sku": "TEST-SKU"}],
    }}
    status, data, dur = _post("/shopify/tarifas", payload)
    rates = (data or {}).get("rates", [])
    check(status == 200 and len(rates) >= 1,
          f"{nombre}: hay opción de envío",
          f"HTTP {status}, {len(rates)} opciones")
    check(dur < LIMITE_SEGUNDOS,
          f"{nombre}: responde a tiempo",
          f"{dur:.2f}s (límite {LIMITE_SEGUNDOS}s)")
    for r in rates:
        centavos = int(r.get("total_price", 0))
        check(centavos > 0, f"{nombre}: precio > 0", f"{centavos} centavos")

        # ESCALA: esto FALLA, no avisa. Shopify espera el monto en SUBUNIDADES
        # (centavos); si alguien saca el ×100 de shopify_app.py, el comprador
        # ve el envío 100 veces más barato y la venta se despacha a pérdida.
        # Con un simple `centavos > 0` ese error pasaba en verde.
        ars = centavos / 100
        check(20_000 < ars < 5_000_000,
              f"{nombre}: precio en escala correcta (subunidades)",
              f"ARS {ars:,.0f} — si es ~100x chico, falta el ×100 de total_price; "
              f"si es ~100x grande, está duplicado")

        check((r.get("currency") or "").upper() in ("ARS", "USD"),
              f"{nombre}: moneda declarada", f"currency={r.get('currency')!r}")

# ── 2. Destino nacional no va por esta vía ──
print("\n[2] Reglas de negocio")
status, data, _ = _post("/shopify/tarifas", {"rate": {
    "destination": {"country": "AR", "city": "Buenos Aires", "postal_code": "1043"},
    "items": [{"grams": 500, "quantity": 1, "price": 100000, "sku": "X"}],
}})
check(status == 200 and len((data or {}).get("rates", [])) == 0,
      "Destino nacional (AR) no ofrece tarifa internacional")

# ── 3. Entradas rotas no tumban el checkout ──
print("\n[3] Resistencia a datos basura")
for nombre, payload in [
    ("payload vacío", {}),
    ("sin items", {"rate": {"destination": {"country": "US"}, "items": []}}),
    ("sin destino", {"rate": {"items": [{"grams": 100, "quantity": 1, "price": 1000}]}}),
    ("gramos nulos", {"rate": {"destination": {"country": "US", "postal_code": "33101"},
                               "items": [{"grams": None, "quantity": 1, "price": 1000}]}}),
]:
    status, data, dur = _post("/shopify/tarifas", payload)
    check(status == 200 and isinstance(data, dict) and "rates" in data,
          f"{nombre}: responde 200 con estructura válida", f"HTTP {status}")

# ── 4. Los webhooks rechazan firmas falsas ──
print("\n[4] Seguridad de webhooks")
status, _, _ = _post("/integraciones/shopify/webhook", {"id": 1},
                     headers={"X-Shopify-Shop-Domain": "cualquiera.myshopify.com",
                              "X-Shopify-Hmac-Sha256": "firma-falsa",
                              "X-Shopify-Topic": "orders/create"})
check(status == 401, "Webhook con firma inválida es rechazado", f"HTTP {status}")

status, _, _ = _post("/integraciones/shopify/webhook", {"id": 1},
                     headers={"X-Shopify-Topic": "orders/create"})
check(status == 401, "Webhook sin firma es rechazado", f"HTTP {status}")

# ── 5. Superficies vivas ──
print("\n[5] Superficies públicas")
for path, nombre in [("/salud", "Salud (app + base de datos)"),
                     ("/web", "Web pública"),
                     ("/portal/login", "Login del portal"),
                     ("/admin/login", "Login del admin")]:
    status, dur = _get(path)
    check(status == 200, f"{nombre} responde", f"HTTP {status} en {dur:.2f}s")

# ── Resumen ──
print("\n" + "=" * 60)
if avisos:
    print("\nAVISOS (revisar, no bloquean):")
    for a in avisos:
        print(f"  ! {a}")

if fallos:
    print(f"\n✗ {len(fallos)} CHEQUEOS FALLARON:\n")
    for f in fallos:
        print(f"  · {f}")
    print()
    sys.exit(1)

print("\n✓ Camino crítico OK: el comprador siempre ve una opción de envío.\n")
sys.exit(0)
