"""La búsqueda está siempre visible, es global y funciona sin JavaScript."""
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment


ROOT = Path(__file__).resolve().parent.parent


def render_search(query=""):
    source = (ROOT / "templates/portal/envios.html").read_text(encoding="utf-8")
    start = source.index('<form method="GET" action="/portal/envios" class="envios-search"')
    end = source.index("</form>", start) + len("</form>")
    return Environment(autoescape=True).from_string(source[start:end]).render(busqueda_filtro=query)


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def first(self, tag):
        return next(attrs for name, attrs in self.tags if name == tag)


def test_busqueda_visible_por_defecto_y_control_nativo_accesible():
    html = render_search()
    parsed = Elements(html)
    assert not any(tag == "details" for tag, _ in parsed.tags)
    assert "Encontrá tu envío" in html
    assert parsed.first("label")["for"] == parsed.first("input")["id"]
    assert parsed.first("input")["value"] == ""
    assert not any(tag in ("script", "svg") for tag, _ in parsed.tags)


def test_el_boton_no_depende_de_la_bandera_auxiliar_de_historial():
    source = (ROOT / "templates/portal/envios.html").read_text(encoding="utf-8")
    before = source[:source.index('<form method="GET" action="/portal/envios" class="envios-search"')]
    assert before.rfind("{% if tiene_historial %}") < before.rfind("{% endif %}")


def test_filtro_activo_conserva_busqueda_y_permite_limpiar():
    html = render_search("888244412640")
    parsed = Elements(html)
    assert not any(tag == "details" for tag, _ in parsed.tags)
    assert parsed.first("input")["value"] == "888244412640"
    assert parsed.first("a")["href"] == "/portal/envios"
    assert parsed.first("a")["aria-label"] == "Limpiar búsqueda"


def test_desplegar_no_cambia_la_busqueda_global_ni_paginacion():
    parsed = Elements(render_search())
    assert parsed.first("form")["method"] == "GET"
    assert parsed.first("form")["action"] == "/portal/envios"
    assert parsed.first("form")["role"] == "search"
    assert [attrs["name"] for tag, attrs in parsed.tags if tag == "input"] == ["buscar"]
    assert parsed.first("button")["type"] == "submit"


def test_tracking_se_escapa_en_el_atributo_del_input():
    query = '\"><script>alert(1)</script>'
    html = render_search(query)
    parsed = Elements(html)
    assert parsed.first("input")["value"] == query
    assert not any(tag == "script" for tag, _ in parsed.tags)
