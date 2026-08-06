"""
El JavaScript de cada template tiene que PARSEAR.

Existe por un bug concreto (05/08/2026, commit 1480078): una edición dejó
colgadas dos líneas huérfanas con el cierre de una versión vieja de
`leerBultos()` en templates/portal/envio_nuevo.html:

      }).filter(function (b) { return b.producto; });
    }

Eso es un `SyntaxError: Unexpected token '}'`, y un error de sintaxis NO rompe
una línea: el navegador descarta el `<script>` ENTERO sin ejecutar nada. El
formulario de Nuevo envío mostraba Remitente, Destinatario y Paquete apilados
en vez de un paso por pantalla, la barra de "Crear solicitud" aparecía desde el
paso 1, y tampoco andaban el precio en vivo, la precarga desde la libreta ni el
parser de mails. Todo eso ya estaba escrito: no se ejecutaba.

**Por qué se nos pasó**: el servidor devuelve 200 y la página se ve entera. El
error vive sólo en la consola del navegador, así que ni el smoke test de
producción ni una mirada por encima lo detectan. Un bug mudo es peor que un
crash.

Los `{{ ... }}` de Jinja se reemplazan por un literal inocuo antes de parsear —
acá se valida la SINTAXIS del JS, no lo que rinde el template.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = RAIZ / "templates"

# Jinja de expresión -> `0`. Tiene que ser un token que parsee TANTO suelto
# (`var x = {{ y|tojson }};`) como adentro de comillas (`var x = "{{ y }}";`),
# y `0` cumple las dos: un string con comillas rompería el segundo caso.
# Los de bloque ({% if %}) se dejan tal cual: si alguien mete lógica de Jinja
# adentro del JS y rompe el parseo, queremos enterarnos acá y no en la consola
# de un cliente.
_EXPR_JINJA = re.compile(r"\{\{.*?\}\}", re.S)
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)


def _bloques_js(html: str):
    """Los <script> con código propio. Los que sólo tienen src no traen JS."""
    for cuerpo in _SCRIPT.findall(html):
        if cuerpo.strip():
            yield cuerpo


def _templates_con_js():
    for ruta in sorted(TEMPLATES.rglob("*.html")):
        html = ruta.read_text(encoding="utf-8")
        if any(_bloques_js(html)):
            yield ruta


TEMPLATES_CON_JS = list(_templates_con_js())

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node no está instalado; el chequeo de sintaxis JS se saltea",
)


def test_hay_templates_para_revisar():
    """Si el descubrimiento se rompe, el resto pasa en verde sin revisar nada."""
    assert TEMPLATES_CON_JS, f"no encontré ningún template con <script> en {TEMPLATES}"


@pytest.mark.parametrize("ruta", TEMPLATES_CON_JS, ids=lambda r: r.name)
def test_el_javascript_del_template_parsea(ruta):
    html = ruta.read_text(encoding="utf-8")

    for i, cuerpo in enumerate(_bloques_js(html), start=1):
        js = _EXPR_JINJA.sub("0", cuerpo)

        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                         delete=False) as tmp:
            tmp.write(js)
            tmp_path = tmp.name
        try:
            r = subprocess.run(["node", "--check", tmp_path],
                               capture_output=True, text=True, timeout=30)
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

        assert r.returncode == 0, (
            f"\n{ruta.relative_to(RAIZ)} — bloque <script> #{i}: el JS NO PARSEA.\n"
            f"El navegador va a descartar el script ENTERO y la página va a\n"
            f"quedar sin ninguna interacción, devolviendo 200 igual.\n\n"
            f"{r.stderr.replace(tmp_path, str(ruta.relative_to(RAIZ)))}"
        )
