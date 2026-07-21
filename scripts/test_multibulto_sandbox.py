# ============================================================
# Test multi-bulto contra FedEx SANDBOX (Rate + Ship API)
# Corre con: .venv-codex/bin/python scripts/test_multibulto_sandbox.py
# No toca la base de datos: solo FedEx sandbox.
# ============================================================

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from core.fedex_client import FedExClient  # noqa: E402

fedex = FedExClient()
print(f"Entorno: {fedex.environment} ({fedex.base_url})")
assert fedex.environment == "sandbox", "Este test es SOLO para sandbox"

ORIGEN = {"street": "Av. Corrientes 1234", "city": "BUENOS AIRES",
          "state": "B", "postal_code": "1043", "country": "AR"}
DESTINO = {"street": "120 SW 8th St", "city": "Miami",
           "state": "FL", "postal_code": "33130", "country": "US"}

# 2 productos distintos, 3 cajas en total (2 carteras + 1 caja de vino)
BULTOS = [
    {"peso_kg": 1.2, "largo": 30, "ancho": 20, "alto": 12,
     "valor_unitario_usd": 120, "unidades": 2,
     "hs_code": "4202.21.00", "descripcion_en": "Leather handbag"},
    {"peso_kg": 9.0, "largo": 40, "ancho": 30, "alto": 32,
     "valor_unitario_usd": 90, "unidades": 1,
     "hs_code": "2204.21.00", "descripcion_en": "Red wine bottles 6x750ml"},
]

# ── 1. Rate multi-pieza ──────────────────────────────────────
print("\n[1/3] Rate API multi-pieza (3 cajas, 2 productos)…")
rates = fedex.get_rates(origen=ORIGEN, destino=DESTINO, paquetes=BULTOS)
assert rates.get("encontrado"), f"Rate falló: {rates.get('error')}"
print(f"  OK — {rates['servicio']}: {rates['costo']} {rates['moneda']}"
      f" (lista: {rates.get('costo_lista')})")

# Referencia: una sola caja de 1.2kg para comparar magnitudes
solo = fedex.get_rates(origen=ORIGEN, destino=DESTINO, paquete={
    "peso_kg": 1.2, "largo": 30, "ancho": 20, "alto": 12,
    "valor_declarado_usd": 120, "unidades": 1,
})
if solo.get("encontrado"):
    print(f"  Referencia 1 caja: {solo['costo']} {solo['moneda']}"
          f" → multi es {'MÁS caro (esperado)' if rates['costo'] > solo['costo'] else 'RARO: no subió'}")

# ── 2. Ship multi-pieza ──────────────────────────────────────
print("\n[2/3] Ship API multi-pieza (emisión de guía con 3 labels)…")
envio = fedex.create_shipment({
    "shipper": {"nombre": "Tauro Solutions", "empresa": "Tauro Solutions",
                "telefono": "1132986540", "calle": "Av. Corrientes 1234",
                "ciudad": "Buenos Aires", "estado": "B", "zip": "1043", "pais": "AR"},
    "recipient": {"nombre": "John Test", "telefono": "3055551212",
                  "calle": "120 SW 8th St", "ciudad": "Miami",
                  "estado": "FL", "zip": "33130", "pais": "US"},
    "bultos": BULTOS,
})
assert envio.get("encontrado"), f"Ship falló: {envio.get('error')}"
print(f"  OK — tracking {envio['tracking']} · piezas con label: {envio.get('piezas')}")
assert envio.get("piezas") == 3, f"Esperaba 3 labels, vinieron {envio.get('piezas')}"

# ── 3. PDF unido ─────────────────────────────────────────────
print("\n[3/3] Label PDF unido…")
pdf = envio.get("label_pdf")
assert pdf and pdf[:4] == b"%PDF", "El label no es un PDF válido"
import io
from pypdf import PdfReader
paginas = len(PdfReader(io.BytesIO(pdf)).pages)
print(f"  OK — PDF de {paginas} páginas, {len(pdf)} bytes")
assert paginas >= 3, f"Esperaba >=3 páginas (una por caja), hay {paginas}"

out = pathlib.Path(__file__).parent / "label_multibulto_test.pdf"
out.write_bytes(pdf)
print(f"\n✅ TODO OK — label guardado en {out}")
