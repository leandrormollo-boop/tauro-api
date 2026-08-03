# ============================================================
# Guías por país — contenido público SEO
# ============================================================
# "Cómo exportar a X desde Argentina": responde lo que el cliente googlea
# antes de animarse a exportar. Casi no existe contenido actualizado en
# español rioplatense — los blogs locales siguen citando reglas VENCIDAS
# (ej: el mínimo libre de USD 800 de EEUU, eliminado el 29/08/2025).
#
# REGLA DE ORO DEL CONTENIDO: sólo datos verificados a la fecha de
# actualización, con disclaimer visible. Un dato aduanero viejo quema la
# credibilidad de toda la empresa. Si un número no está verificado, se
# escribe en general ("según el producto") y se remite a la cotización.
#
# Va en código como las páginas legales: existe siempre, sin CMS ni base.
# ============================================================
from __future__ import annotations

CONTACTO = "taurosolutionsar@gmail.com"
ACTUALIZADO = "agosto de 2026"

_LAYOUT = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo_seo}</title>
<meta name="description" content="{descripcion_seo}">
<link rel="icon" type="image/png" href="/static/img/favicon.png">
<link rel="canonical" href="https://taurosolutions.ar{canonical}">
<style>
  body {{ margin:0; background:#0c0a14; color:#d7d3e4;
         font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;
         line-height:1.75; font-size:15.5px; }}
  .wrap {{ max-width:720px; margin:0 auto; padding:56px 24px 80px; }}
  .marca {{ font-size:19px; font-weight:700; letter-spacing:.08em;
           color:#fff; text-decoration:none; display:inline-block;
           margin-bottom:44px; }}
  .marca span {{ color:#a78bfa; }}
  h1 {{ color:#fff; font-size:32px; line-height:1.2; margin:0 0 8px; }}
  .fecha {{ color:#6f6a85; font-size:13px; margin:0 0 40px; }}
  h2 {{ color:#fff; font-size:19px; margin:38px 0 12px; }}
  h3 {{ color:#fff; font-size:16px; margin:26px 0 8px; }}
  p, li {{ color:#b5b0c6; }}
  ul {{ padding-left:20px; }}
  li {{ margin-bottom:8px; }}
  a {{ color:#a78bfa; }}
  .destacado {{ background:rgba(167,139,250,.08); border-left:3px solid #7c5cf6;
               padding:14px 18px; border-radius:0 8px 8px 0; margin:18px 0; }}
  .cta {{ display:inline-block; background:#7c5cf6; color:#fff; padding:13px 30px;
         border-radius:999px; text-decoration:none; font-weight:600; margin-top:8px; }}
  .aviso {{ color:#6f6a85; font-size:13px; margin-top:34px; padding:14px 16px;
           background:#131020; border-radius:8px; }}
  .pie {{ margin-top:56px; padding-top:24px; border-top:1px solid #241f38;
         color:#6f6a85; font-size:13px; }}
  .grid-paises {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
                 gap:12px; margin-top:20px; }}
  .pais-card {{ background:#131020; border:1px solid #241f38; border-radius:12px;
               padding:18px; text-decoration:none; display:block; }}
  .pais-card:hover {{ border-color:#7c5cf6; }}
  .pais-card b {{ color:#fff; font-size:16px; }}
  .pais-card small {{ color:#6f6a85; display:block; margin-top:4px; }}
</style></head>
<body><div class="wrap">
  <a class="marca" href="/web">TAURO <span>SOLUTIONS</span></a>
  {cuerpo}
  <div class="aviso">⚠️ Información orientativa, actualizada a {actualizado}.
  Las reglas aduaneras cambian seguido; antes de cada envío te confirmamos
  los valores exactos para tu producto y destino.</div>
  <div class="pie">
    Tauro Solutions · Logística internacional puerta a puerta · Buenos Aires, Argentina<br>
    <a href="/guias">Todas las guías</a> · <a href="/web">Cotizá tu envío</a> ·
    Consultas: <a href="mailto:{contacto}">{contacto}</a>
  </div>
</div></body></html>"""


def _pagina(titulo_seo: str, descripcion_seo: str, canonical: str, cuerpo: str) -> str:
    return _LAYOUT.format(
        titulo_seo=titulo_seo, descripcion_seo=descripcion_seo,
        canonical=canonical, cuerpo=cuerpo,
        actualizado=ACTUALIZADO, contacto=CONTACTO,
    )


_CTA = """<p><a class="cta" href="/web#cotizador">Cotizá tu envío ahora →</a></p>"""


# ── Contenido por país ───────────────────────────────────────
# Cada guía: tiempos, impuestos (SÓLO datos verificados), documentación,
# restricciones frecuentes, y el cierre comercial.

GUIAS = {
    "estados-unidos": {
        "nombre": "Estados Unidos",
        "emoji": "🇺🇸",
        "resumen": "El destino nº1 del e-commerce argentino. Ojo: las reglas cambiaron fuerte en 2025.",
        "titulo_seo": "Exportar a Estados Unidos desde Argentina (2026): aduana, impuestos y tiempos · TAURO Solutions",
        "descripcion_seo": "Guía actualizada 2026 para enviar productos de Argentina a EEUU: fin del mínimo libre de USD 800, aranceles vigentes, documentación, tiempos puerta a puerta y qué no se puede mandar.",
        "cuerpo": f"""
<h1>Exportar a Estados Unidos desde Argentina</h1>
<p class="fecha">Guía actualizada · {ACTUALIZADO}</p>

<div class="destacado">
<b>Lo que cambió y muchos todavía no saben:</b> desde el 29 de agosto de 2025,
EEUU <b>eliminó el mínimo libre de impuestos de USD 800</b> (el famoso
"de minimis"). Hoy <b>todo envío comercial paga aranceles</b>, sin importar el
valor. Si leíste en un blog que "hasta 800 dólares no paga nada", esa
información está vencida.
</div>

<h2>Tiempos</h2>
<p>Puerta a puerta por courier: <b>3 a 5 días hábiles</b> a las principales
ciudades. El despacho de aduana está incluido en el servicio.</p>

<h2>Impuestos y aranceles (2026)</h2>
<ul>
  <li>Todo envío comercial paga aranceles según el <b>tipo de producto</b>
      (su código HS) y el país de origen.</li>
  <li>El porcentaje varía mucho por rubro: no es lo mismo indumentaria de
      cuero que artículos de pesca. Por eso cotizamos <b>por producto</b>.</li>
  <li>Podés elegir quién paga los impuestos de destino: tu comprador al
      recibir, o vos de antemano (DDP) para que a tu cliente no le toquen
      el timbre con una factura sorpresa.</li>
</ul>

<h2>Documentación que preparamos por vos</h2>
<ul>
  <li><b>Invoice comercial en inglés</b> con la descripción del producto.</li>
  <li><b>Código HS</b> (clasificación aduanera) correcto — el error más caro
      de los exportadores nuevos. Si no lo sabés, lo determinamos juntos.</li>
  <li>Guía aérea con valor declarado.</li>
</ul>

<h2>Restricciones frecuentes</h2>
<ul>
  <li>Alimentos, suplementos y cosméticos: requieren registro FDA — consultanos antes.</li>
  <li>Productos de origen animal o vegetal (cueros exóticos, semillas, plumas):
      pueden requerir permisos CITES/USDA.</li>
  <li>Baterías de litio sueltas: restricciones de transporte aéreo.</li>
</ul>

<h2>¿Cuánto sale?</h2>
<p>Cotizás online en segundos, sin registrarte, con el precio final puerta a
puerta — no una "tarifa base" que después crece.</p>
{_CTA}
""",
    },
    "brasil": {
        "nombre": "Brasil",
        "emoji": "🇧🇷",
        "resumen": "El gigante de al lado, con reglas nuevas desde mayo 2026 que lo hicieron más accesible.",
        "titulo_seo": "Exportar a Brasil desde Argentina (2026): Remessa Conforme, impuestos y tiempos · TAURO Solutions",
        "descripcion_seo": "Guía actualizada 2026 para vender de Argentina a Brasil: nuevas reglas de mayo 2026, impuesto 0% hasta USD 50, ICMS, tiempos y documentación.",
        "cuerpo": f"""
<h1>Exportar a Brasil desde Argentina</h1>
<p class="fecha">Guía actualizada · {ACTUALIZADO}</p>

<div class="destacado">
<b>Buena noticia (mayo 2026):</b> Brasil eliminó la "taxa das blusinhas".
Los envíos de hasta <b>USD 50 pagan 0% de impuesto federal</b> de importación
(sigue el ICMS estadual). Arriba de USD 50: 60% de impuesto de importación
<b>con un descuento fijo de USD 30</b>, más ICMS.
</div>

<h2>Impuestos (2026)</h2>
<ul>
  <li><b>Hasta USD 50:</b> 0% de impuesto federal + ICMS del estado de
      destino (entre 17% y 20% según el estado).</li>
  <li><b>Más de USD 50:</b> 60% de impuesto de importación con descuento
      fijo de USD 30, + ICMS.</li>
  <li>La aduana brasileña es de las más estrictas de la región: la
      documentación prolija no es opcional.</li>
</ul>

<h2>Tiempos</h2>
<p>Por courier puerta a puerta: <b>3 a 6 días hábiles</b> a las principales
ciudades, despacho incluido.</p>

<h2>Claves para que no se trabe</h2>
<ul>
  <li>Descripción del producto <b>precisa y honesta</b> — subvaluar es la
      forma más rápida de perder el paquete y el cliente.</li>
  <li>Código HS correcto por producto.</li>
  <li>Datos completos del destinatario, <b>CPF incluido</b> — sin CPF la
      aduana brasileña no libera.</li>
</ul>
{_CTA}
""",
    },
    "chile": {
        "nombre": "Chile",
        "emoji": "🇨🇱",
        "resumen": "Cruzar la cordillera en 2-4 días, con una aduana ágil para e-commerce.",
        "titulo_seo": "Exportar a Chile desde Argentina (2026): impuestos, tiempos y documentación · TAURO Solutions",
        "descripcion_seo": "Guía para enviar productos de Argentina a Chile por courier: IVA, aranceles según producto, tiempos puerta a puerta y qué documentación hace falta.",
        "cuerpo": f"""
<h1>Exportar a Chile desde Argentina</h1>
<p class="fecha">Guía actualizada · {ACTUALIZADO}</p>

<h2>Tiempos</h2>
<p>Por courier puerta a puerta: <b>2 a 4 días hábiles</b> a Santiago y
principales ciudades.</p>

<h2>Impuestos</h2>
<ul>
  <li>Los envíos comerciales pagan <b>IVA (19%)</b> y aranceles según el
      tipo de producto y su valor.</li>
  <li>Por el acuerdo Mercosur-Chile, muchos productos de origen argentino
      tienen <b>preferencias arancelarias</b> — con el certificado de origen
      correcto, el arancel puede bajar mucho. Te decimos si tu producto
      califica.</li>
</ul>

<h2>Documentación</h2>
<ul>
  <li>Invoice comercial con descripción y valor real.</li>
  <li>Código HS del producto.</li>
  <li>RUT del destinatario para el despacho.</li>
</ul>
{_CTA}
""",
    },
    "uruguay": {
        "nombre": "Uruguay",
        "emoji": "🇺🇾",
        "resumen": "El destino más rápido: del depósito a Montevideo en 1-3 días.",
        "titulo_seo": "Exportar a Uruguay desde Argentina (2026): tiempos, impuestos y cómo empezar · TAURO Solutions",
        "descripcion_seo": "Guía para enviar de Argentina a Uruguay por courier: el destino más rápido para probar tu producto afuera. Tiempos, impuestos y documentación.",
        "cuerpo": f"""
<h1>Exportar a Uruguay desde Argentina</h1>
<p class="fecha">Guía actualizada · {ACTUALIZADO}</p>

<div class="destacado">
<b>El mejor destino para tu primera exportación:</b> cerca, rápido, en tu
idioma y con un comprador acostumbrado a comprar afuera. Ideal para probar
producto y proceso antes de saltar a EEUU o Brasil.
</div>

<h2>Tiempos</h2>
<p>Por courier puerta a puerta: <b>1 a 3 días hábiles</b> a Montevideo.</p>

<h2>Impuestos</h2>
<ul>
  <li>Los envíos comerciales pagan IVA y aranceles según el producto y el
      régimen de importación del comprador.</li>
  <li>Uruguay tiene regímenes simplificados para compras de bajo valor —
      el detalle exacto depende del monto y la frecuencia: lo vemos por
      envío al cotizar.</li>
</ul>

<h2>Documentación</h2>
<ul>
  <li>Invoice comercial + código HS.</li>
  <li>Cédula o RUT del destinatario.</li>
</ul>
{_CTA}
""",
    },
    "mexico": {
        "nombre": "México",
        "emoji": "🇲🇽",
        "resumen": "Mercado enorme y aduana exigente — con la documentación bien hecha, entra.",
        "titulo_seo": "Exportar a México desde Argentina (2026): aduana, impuestos y tiempos · TAURO Solutions",
        "descripcion_seo": "Guía para enviar productos de Argentina a México por courier: régimen simplificado, IVA, restricciones (textiles y calzado) y tiempos puerta a puerta.",
        "cuerpo": f"""
<h1>Exportar a México desde Argentina</h1>
<p class="fecha">Guía actualizada · {ACTUALIZADO}</p>

<h2>Tiempos</h2>
<p>Por courier puerta a puerta: <b>3 a 6 días hábiles</b> a CDMX y
principales ciudades.</p>

<h2>Impuestos</h2>
<ul>
  <li>Los envíos por courier suelen despacharse por el <b>régimen
      simplificado</b>, con una tasa global que combina arancel e IVA según
      el valor del envío.</li>
  <li>El IVA mexicano es del 16%; la tasa final depende del producto y el
      valor — lo calculamos al cotizar.</li>
</ul>

<h2>Ojo con estos rubros</h2>
<ul>
  <li><b>Textiles, indumentaria y calzado</b>: México los controla fuerte
      (padrones y permisos según volumen). Para venta ocasional por courier
      suele haber camino — consultanos ANTES de prometerle fecha a tu
      cliente.</li>
  <li>Suplementos, cosméticos y juguetes pueden requerir normas (NOM).</li>
</ul>

<h2>Documentación</h2>
<ul>
  <li>Invoice comercial + código HS.</li>
  <li>RFC del destinatario si es empresa.</li>
</ul>
{_CTA}
""",
    },
    "espana": {
        "nombre": "España",
        "emoji": "🇪🇸",
        "resumen": "La puerta de entrada a Europa: reglas claras y parejas para toda la UE.",
        "titulo_seo": "Exportar a España desde Argentina (2026): IVA, aranceles UE y tiempos · TAURO Solutions",
        "descripcion_seo": "Guía para enviar de Argentina a España: IVA 21% desde el primer euro, arancel solo arriba de 150 EUR, tiempos puerta a puerta y documentación.",
        "cuerpo": f"""
<h1>Exportar a España desde Argentina</h1>
<p class="fecha">Guía actualizada · {ACTUALIZADO}</p>

<h2>Impuestos (reglas de la Unión Europea)</h2>
<ul>
  <li><b>IVA (21% en España): se paga siempre</b>, desde el primer euro —
      la UE eliminó el mínimo libre de IVA en 2021.</li>
  <li><b>Arancel: solo si el envío supera los 150 EUR</b> de valor. Por
      debajo de eso, pagás IVA pero no arancel.</li>
  <li>El porcentaje de arancel arriba de 150 EUR depende del producto
      (su código HS).</li>
</ul>

<h2>Tiempos</h2>
<p>Por courier puerta a puerta: <b>3 a 6 días hábiles</b> a Madrid,
Barcelona y principales ciudades.</p>

<h2>Documentación</h2>
<ul>
  <li>Invoice comercial + código HS.</li>
  <li>NIF/DNI del destinatario para el despacho.</li>
</ul>

<div class="destacado">
Una vez dentro de España, tu producto está dentro de la UE: las mismas
reglas aplican para mandar a Francia, Italia, Alemania o Portugal.
</div>
{_CTA}
""",
    },
}


def pagina_guia(slug: str) -> str | None:
    g = GUIAS.get(slug)
    if not g:
        return None
    return _pagina(g["titulo_seo"], g["descripcion_seo"], f"/guias/{slug}", g["cuerpo"])


def pagina_indice() -> str:
    cards = "".join(
        f'<a class="pais-card" href="/guias/{slug}"><b>{g["emoji"]} {g["nombre"]}</b>'
        f'<small>{g["resumen"]}</small></a>'
        for slug, g in GUIAS.items()
    )
    cuerpo = f"""
<h1>Guías para exportar desde Argentina</h1>
<p class="fecha">Actualizadas · {ACTUALIZADO}</p>
<p>Aduana, impuestos, tiempos y documentación de cada destino — explicado
para pymes, sin jerga y con las reglas VIGENTES (varias cambiaron fuerte
entre 2025 y 2026, y buena parte de lo que se lee por ahí está vencido).</p>
<div class="grid-paises">{cards}</div>

<h2>Herramientas gratis</h2>
<div class="grid-paises">
  <a class="pais-card" href="/calculadora-volumetrica"><b>📦 Calculadora de peso volumétrico</b>
  <small>Sabé cuántos kilos te factura el courier antes de despachar.</small></a>
</div>
{_CTA}
"""
    return _pagina(
        "Guías para exportar desde Argentina (2026): aduana, impuestos y tiempos por país · TAURO Solutions",
        "Cómo exportar desde Argentina a EEUU, Brasil, Chile, Uruguay, México y España: impuestos vigentes 2026, tiempos puerta a puerta, documentación y restricciones, país por país.",
        "/guias", cuerpo)


def pagina_calculadora() -> str:
    """
    Calculadora pública de peso volumétrico. Imán SEO clásico del rubro
    (patrón Easyship/DHL Discover): responde "¿por qué me cobran más kilos
    de los que pesa?" y termina en el cotizador. Todo client-side: una
    fórmula, cero backend.
    """
    cuerpo = """
<h1>Calculadora de peso volumétrico</h1>
<p class="fecha">La fórmula que usan los couriers aéreos · """ + ACTUALIZADO + """</p>

<p>Los couriers internacionales no cobran solo por lo que pesa tu paquete,
sino por el espacio que ocupa en el avión. Facturan <b>el mayor</b> entre el
peso real y el peso volumétrico:</p>

<div class="destacado" style="font-family:monospace;font-size:16px;">
Peso volumétrico = largo × ancho × alto (cm) ÷ 5000
</div>

<div style="background:#131020;border:1px solid #241f38;border-radius:12px;padding:22px;margin:22px 0;">
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">
    <label style="font-size:12px;color:#6f6a85;">LARGO (cm)
      <input id="cv-l" type="number" min="0" step="0.5" placeholder="60" style="width:100%;margin-top:5px;padding:10px;background:#0c0a14;border:1px solid #241f38;border-radius:8px;color:#fff;font-size:15px;box-sizing:border-box;"></label>
    <label style="font-size:12px;color:#6f6a85;">ANCHO (cm)
      <input id="cv-a" type="number" min="0" step="0.5" placeholder="40" style="width:100%;margin-top:5px;padding:10px;background:#0c0a14;border:1px solid #241f38;border-radius:8px;color:#fff;font-size:15px;box-sizing:border-box;"></label>
    <label style="font-size:12px;color:#6f6a85;">ALTO (cm)
      <input id="cv-h" type="number" min="0" step="0.5" placeholder="40" style="width:100%;margin-top:5px;padding:10px;background:#0c0a14;border:1px solid #241f38;border-radius:8px;color:#fff;font-size:15px;box-sizing:border-box;"></label>
    <label style="font-size:12px;color:#6f6a85;">PESO REAL (kg)
      <input id="cv-p" type="number" min="0" step="0.1" placeholder="10" style="width:100%;margin-top:5px;padding:10px;background:#0c0a14;border:1px solid #241f38;border-radius:8px;color:#fff;font-size:15px;box-sizing:border-box;"></label>
  </div>
  <div id="cv-out" style="margin-top:18px;font-size:15px;color:#6f6a85;">
    Completá las medidas y te mostramos el peso que te van a facturar.
  </div>
</div>

<h2>¿Por qué 5000?</h2>
<p>Es el divisor estándar de los couriers aéreos internacionales (FedEx, UPS,
DHL) para medidas en centímetros. Un paquete liviano pero voluminoso — una
caja de 60×40×40 con almohadones — "pesa" 19,2 kg para el courier aunque la
balanza diga 3.</p>

<h2>Cómo pagar menos</h2>
<ul>
  <li><b>Achicá la caja al mínimo</b>: el aire se factura como si fuera producto.</li>
  <li><b>Repartí en más de una caja</b> si el producto lo permite: dos cajas
      chicas pueden facturar menos que una grande medio vacía.</li>
  <li><b>Cargá las medidas reales de tus productos</b> en tu catálogo TAURO:
      cotizamos con el peso facturable exacto, sin sorpresas al despachar.</li>
</ul>
""" + _CTA + """

<!-- El JS vive en /static (no inline): la CSP global es script-src 'self'. -->
<script src="/static/js/calculadora-volumetrica.js?v=1"></script>
"""
    return _pagina(
        "Calculadora de peso volumétrico para envíos internacionales · TAURO Solutions",
        "Calculá gratis el peso volumétrico de tu paquete (largo × ancho × alto ÷ 5000) y sabé cuántos kilos te va a facturar el courier antes de despachar.",
        "/calculadora-volumetrica", cuerpo)


def sitemap_xml() -> str:
    """Sitemap para que Google encuentre las guías sin esperar a los links."""
    urls = ["/web", "/guias", "/calculadora-volumetrica", "/estado",
            "/privacidad", "/terminos"] + \
           [f"/guias/{slug}" for slug in GUIAS]
    cuerpo = "".join(
        f"<url><loc>https://taurosolutions.ar{u}</loc></url>" for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{cuerpo}</urlset>")
