from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mi_tienda_explicita_disponibilidad_sin_jerga_interna():
    plantilla = (ROOT / "templates" / "portal" / "tienda.html").read_text(
        encoding="utf-8"
    )

    assert "todavía no se puede instalar" in plantilla
    assert "Consultar por el piloto" in plantilla
    for termino_interno in ("Shipping", "UAT", "homologación"):
        assert termino_interno not in plantilla


def test_embalajes_vuelve_a_mi_tienda_y_conserva_limite_shopify():
    plantilla = (ROOT / "templates" / "portal" / "paquetes.html").read_text(
        encoding="utf-8"
    )

    assert 'href="/portal/tienda">Volver a Mi tienda' in plantilla
    assert "Ir a mis ventas" not in plantilla
    assert "TAURO no publica tarifas en el checkout de Shopify" in plantilla


def test_avisos_de_tiendanube_no_exponen_controles_internos():
    avisos = "\n".join(
        (
            (ROOT / "endpoints" / "portal_cliente.py").read_text(encoding="utf-8"),
            (ROOT / "endpoints" / "integraciones.py").read_text(encoding="utf-8"),
        )
    )

    assert "Escribinos si querés probarla cuando esté disponible" in avisos
    assert "Falta completar Shipping" not in avisos
    assert "completó Shipping, tarifas, UAT y homologación" not in avisos
