"""Contratos de alcance para la imagen panorámica del inicio del cliente."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "css" / "tauro.css").read_text(encoding="utf-8")
BASE = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
HOME = (ROOT / "templates" / "portal" / "home.html").read_text(encoding="utf-8")


def test_panorama_se_limita_al_inicio_del_portal():
    selector = ".main:has(> .main-inner > .home-hero)"
    assert selector in CSS
    assert '<section class="home-hero">' in HOME
    assert '<section class="home-hero">' not in BASE
    assert ".main::before" not in CSS


def test_panorama_usa_las_imagenes_existentes_sin_filtros():
    start = CSS.index("INICIO DEL CLIENTE — escena logística panorámica")
    panorama = CSS[start:]
    assert 'url("/static/img/escenas/avion-hero.jpg")' in panorama
    assert 'url("/static/img/escenas/avion-hero-mob.jpg")' in panorama
    assert "pointer-events: none;" in panorama
    assert "filter:" not in panorama
    assert "opacity:" not in panorama


def test_hero_conserva_el_contenido_y_deja_de_encerrar_la_foto():
    for text in ("Nuevo envío", "Cotizar envío", "Cuenta corriente", "Seguimiento"):
        assert text in HOME
    start = CSS.index("INICIO DEL CLIENTE — escena logística panorámica")
    panorama = CSS[start:]
    assert ".home-hero {" in panorama
    assert "border: 0;" in panorama
    assert "background: none;" in panorama
    assert "box-shadow: none;" in panorama
    assert "tauro.css?v=42" in BASE
