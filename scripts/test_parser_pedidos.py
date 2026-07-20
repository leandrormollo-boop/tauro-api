# ────────────────────────────────────────────────────────────
# Tests del parser de pedidos por email — Tauro Solutions
# ────────────────────────────────────────────────────────────
# Ejecutar directo (sin pytest):
#   .venv-codex/bin/python scripts/test_parser_pedidos.py
#
# Usa asserts y prints. Sale con código 0 si todo pasa.

import os
import sys

# Agregar la raíz del proyecto al path para importar servicios/
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from servicios.parser_pedidos import parsear_pedido  # noqa: E402


# ────────────────────────────────────────────────────────────
# Fixtures: mails de ejemplo
# ────────────────────────────────────────────────────────────

MAIL_REAL = """\
1 cajas 40x40x15cm
Wool top
 HS 5105290000 wool tops combed
Use for hand knitting blankets
Peso 5kg c/u
Precio USD 20c/u

Quien envía

Juan Pablo melcior
20-23780019-3
pampa 2497 piso 7
Belgrano
Cap federal
Cp 1428

Quien recibe

Jessica Thomas
288 lost valley rd
AMSTERDAM, NY 12010
United States
Panamax08@gmail.com

1 cajas 40x40x30cm
Wool top
 HS 5105290000 wool tops combed
Use for hand knitting blankets
Peso 8kg c/u
Precio USD 20c/u
"""

MAIL_VARIANTES = """\
2 bultos 40 x 40 x 15 cm
Yerba mate
peso: 5,5 kg
valor 15 usd

Quién envía

María Pérez
27112223334
Av. Siempreviva 742
Rosario
CP: C1043AAZ

Quién recibe

John Doe
1 Main St apt 4
MADRID
28001
España
"""

MAIL_SIN_REMITENTE = """\
1 caja 30x20x10
Libros usados
Peso 2kg
u$s 50

Quien recibe

Jane Smith
50 Oak Ave
BOSTON, MA 02101
USA
"""

MAIL_CON_TELEFONO = """\
1 caja 30x20x10
Libros
Peso 2kg
USD 50

Quien envia

Carlos Gomez
20-11111111-1
Calle Falsa 123
CABA
Cp 1425

Quien recibe

Jane Smith
50 Oak Ave
BOSTON, MA 02101
United States
jane@example.com
+1 617 555 0134
"""


# ────────────────────────────────────────────────────────────
# Test 1 — Mail real completo (2 paquetes, remitente, destinatario)
# ────────────────────────────────────────────────────────────

def test_mail_real():
    r = parsear_pedido(MAIL_REAL)

    # Paquetes
    assert len(r["paquetes"]) == 2, "esperaba 2 paquetes, hay %d" % len(r["paquetes"])
    p1, p2 = r["paquetes"]
    assert p1["cantidad"] == 1, p1
    assert (p1["largo_cm"], p1["ancho_cm"], p1["alto_cm"]) == (40.0, 40.0, 15.0), p1
    assert p1["peso_kg"] == 5.0, p1
    assert p1["hs_code"] == "5105290000", p1
    assert p1["descripcion_en"] == "wool tops combed", p1
    assert p1["valor_unitario_usd"] == 20.0, p1
    assert p1["descripcion"] and "Wool top" in p1["descripcion"], p1
    assert (p2["largo_cm"], p2["ancho_cm"], p2["alto_cm"]) == (40.0, 40.0, 30.0), p2
    assert p2["peso_kg"] == 8.0, p2
    assert p2["hs_code"] == "5105290000", p2

    # Remitente
    rem = r["remitente"]
    assert rem["nombre"] == "Juan Pablo melcior", rem
    assert rem["documento"] == "20-23780019-3", rem
    assert rem["direccion"] == "pampa 2497 piso 7", rem
    assert rem["cp"] == "1428", rem

    # Destinatario
    d = r["destinatario"]
    assert d["nombre"] == "Jessica Thomas", d
    assert d["direccion"] == "288 lost valley rd", d
    assert d["ciudad"] == "AMSTERDAM", d
    assert d["estado"] == "NY", d
    assert d["cp"] == "12010", d
    assert d["pais_iso"] == "US", d
    assert d["email"] == "Panamax08@gmail.com", d

    # Confianza: los 4 campos clave presentes
    assert r["confianza"] == 1.0, r["confianza"]
    print("OK  test_mail_real — 2 paquetes, remitente y destinatario completos")


# ────────────────────────────────────────────────────────────
# Test 2 — Variantes: dims con espacios, coma decimal, tildes,
#          sin remitente, y texto basura
# ────────────────────────────────────────────────────────────

def test_variantes():
    r = parsear_pedido(MAIL_VARIANTES)
    assert len(r["paquetes"]) == 1, r["paquetes"]
    p = r["paquetes"][0]
    assert p["cantidad"] == 2, p
    assert (p["largo_cm"], p["ancho_cm"], p["alto_cm"]) == (40.0, 40.0, 15.0), p
    assert p["peso_kg"] == 5.5, p  # coma decimal argentina
    assert p["valor_unitario_usd"] == 15.0, p  # "valor 15 usd"
    # "Quién envía" con tilde detectado
    rem = r["remitente"]
    assert rem["nombre"] == "María Pérez", rem
    assert rem["documento"] == "27112223334", rem  # CUIT sin guiones
    assert rem["cp"] == "C1043AAZ", rem  # CP alfanumérico
    # Destino España
    d = r["destinatario"]
    assert d["nombre"] == "John Doe", d
    assert d["pais_iso"] == "ES", d
    print("OK  test_variantes — dims con espacios, coma decimal, tildes, ES")


def test_sin_remitente():
    r = parsear_pedido(MAIL_SIN_REMITENTE)
    assert len(r["paquetes"]) == 1, r["paquetes"]
    p = r["paquetes"][0]
    assert (p["largo_cm"], p["ancho_cm"], p["alto_cm"]) == (30.0, 20.0, 10.0), p
    assert p["valor_unitario_usd"] == 50.0, p  # "u$s 50"
    assert r["remitente"]["nombre"] is None, r["remitente"]
    d = r["destinatario"]
    assert d["nombre"] == "Jane Smith", d
    assert d["pais_iso"] == "US", d  # "USA"
    # 3 de 4 campos clave (falta remitente)
    assert r["confianza"] == 0.75, r["confianza"]
    print("OK  test_sin_remitente — no rompe, confianza 0.75")


def test_basura():
    r = parsear_pedido("hola como estas")
    assert r["paquetes"] == [], r["paquetes"]
    assert r["confianza"] == 0.0, r["confianza"]
    assert r["remitente"]["nombre"] is None
    assert r["destinatario"]["nombre"] is None
    # Otros inputs degenerados: tampoco pueden lanzar excepción
    for basura in ["", "   \n\n  ", None, 12345, "x" * 10000]:
        rr = parsear_pedido(basura)
        assert isinstance(rr, dict) and "paquetes" in rr, type(rr)
    print("OK  test_basura — sin excepción, paquetes=[], confianza 0.0")


# ────────────────────────────────────────────────────────────
# Test 3 — Destinatario con teléfono
# ────────────────────────────────────────────────────────────

def test_telefono():
    r = parsear_pedido(MAIL_CON_TELEFONO)
    d = r["destinatario"]
    assert d["nombre"] == "Jane Smith", d
    assert d["email"] == "jane@example.com", d
    assert d["telefono"] == "+1 617 555 0134", d
    assert d["ciudad"] == "BOSTON" and d["estado"] == "MA" and d["cp"] == "02101", d
    # El CUIT del remitente NO tiene que colarse como teléfono
    assert r["remitente"]["documento"] == "20-11111111-1", r["remitente"]
    assert r["confianza"] == 1.0, r["confianza"]
    print("OK  test_telefono — teléfono capturado, CUIT y CP no confundidos")


# ────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_mail_real()
    test_variantes()
    test_sin_remitente()
    test_basura()
    test_telefono()
    print("\nTODOS LOS TESTS PASARON (5/5)")
