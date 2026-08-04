# ============================================================
# Páginas legales públicas
# ============================================================
# Shopify exige URLs públicas de privacidad y términos para publicar una
# app en el App Store. Van en código (y no en un CMS) para que existan
# siempre, sin depender de la base ni de nadie que las cargue.
# ============================================================
from __future__ import annotations

CONTACTO = "cotizaciones@taurosolutions.ar"

_LAYOUT = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo} · Tauro Solutions</title>
<link rel="icon" type="image/png" href="/static/img/favicon.png">
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
  p, li {{ color:#b5b0c6; }}
  ul {{ padding-left:20px; }}
  li {{ margin-bottom:8px; }}
  a {{ color:#a78bfa; }}
  .pie {{ margin-top:56px; padding-top:24px; border-top:1px solid #241f38;
         color:#6f6a85; font-size:13px; }}
</style></head>
<body><div class="wrap">
  <a class="marca" href="/web">TAURO <span>SOLUTIONS</span></a>
  <h1>{titulo}</h1>
  <p class="fecha">Última actualización: julio de 2026</p>
  {cuerpo}
  <div class="pie">
    Tauro Solutions · Logística internacional · Buenos Aires, Argentina<br>
    Consultas: <a href="mailto:{contacto}">{contacto}</a>
  </div>
</div></body></html>"""


_PRIVACIDAD = """
<p>Esta política explica qué datos maneja Tauro Solutions cuando usás
nuestra plataforma o instalás nuestra aplicación en tu tienda online.</p>

<h2>Qué datos tomamos</h2>
<ul>
  <li><b>De vos, como cliente:</b> nombre o razón social, email, teléfono,
      dirección y datos fiscales necesarios para emitir envíos y facturar.</li>
  <li><b>De tus ventas:</b> cuando conectás tu tienda, recibimos los datos
      del pedido que hacen falta para despachar — nombre y dirección de
      quien recibe, contacto, y qué productos lleva el paquete.</li>
  <li><b>De uso:</b> registros técnicos de acceso a la plataforma, para
      seguridad y para poder resolver problemas.</li>
</ul>
<p>No pedimos ni almacenamos datos de tarjetas de crédito.</p>

<h2>Para qué los usamos</h2>
<ul>
  <li>Cotizar y emitir envíos con los couriers (FedEx, UPS, DHL).</li>
  <li>Generar la documentación aduanera que exige cada destino.</li>
  <li>Informarte el estado de tus envíos y mantener tu cuenta corriente.</li>
  <li>Dar soporte cuando lo pedís.</li>
</ul>
<p>No vendemos ni cedemos tus datos, ni los de tus compradores, a terceros
con fines comerciales.</p>

<h2>Con quién los compartimos</h2>
<p>Sólo con quien hace falta para que el envío llegue:</p>
<ul>
  <li><b>Couriers</b> (FedEx, UPS, DHL y operadores nacionales): reciben los
      datos del destinatario y del contenido para transportar y despachar
      el paquete.</li>
  <li><b>Aduanas</b> del país de origen y destino, cuando la normativa lo
      exige.</li>
  <li><b>Proveedores de infraestructura</b> que alojan la plataforma, bajo
      acuerdos de confidencialidad.</li>
</ul>

<h2>Cuánto tiempo los guardamos</h2>
<p>Mientras tengas cuenta activa, y después el tiempo que exijan las
obligaciones fiscales y aduaneras (habitualmente 10 años para
documentación de comercio exterior). Los datos personales que ya no son
necesarios para esas obligaciones se eliminan o anonimizan.</p>

<h2>Tus derechos</h2>
<p>Podés pedirnos en cualquier momento acceder a tus datos, corregirlos,
eliminarlos o llevártelos. Escribinos a
<a href="mailto:cotizaciones@taurosolutions.ar">cotizaciones@taurosolutions.ar</a>
y respondemos dentro de los 10 días hábiles.</p>
<p>Si conectaste una tienda, tus compradores pueden ejercer los mismos
derechos a través tuyo: cuando la plataforma de tu tienda nos transmite
un pedido de eliminación, anonimizamos los datos personales de esa
persona conservando únicamente el registro comercial que exige la ley.</p>

<h2>Seguridad</h2>
<p>La plataforma se sirve exclusivamente por HTTPS, las contraseñas se
guardan cifradas (nunca en texto plano) y los accesos a la base están
restringidos. Los secretos de conexión con tu tienda se almacenan
cifrados y nunca se incluyen en exportaciones ni copias de respaldo.</p>

<h2>Cambios</h2>
<p>Si actualizamos esta política, publicamos la nueva versión en esta
misma dirección con su fecha. Si el cambio es significativo, te avisamos
por email.</p>
"""


_TERMINOS = """
<p>Estos términos regulan el uso de la plataforma de Tauro Solutions,
incluida nuestra aplicación para tiendas online.</p>

<h2>Qué hacemos</h2>
<p>Tauro Solutions es un operador logístico: cotizamos, emitimos y
gestionamos envíos internacionales y nacionales a través de couriers
habilitados. No somos el transportista: contratamos el servicio en tu
nombre y respondemos por la gestión.</p>

<h2>Tu cuenta</h2>
<ul>
  <li>Sos responsable de la veracidad de los datos que cargás: un dato mal
      cargado puede demorar o frustrar un envío.</li>
  <li>Sos responsable de resguardar tu contraseña y el acceso a tu cuenta.</li>
  <li>Podemos suspender una cuenta con deuda vencida o uso indebido.</li>
</ul>

<h2>Precios y pago</h2>
<ul>
  <li>Las cotizaciones son estimativas y se calculan con el peso y las
      medidas que declarás. Si el courier verifica en origen medidas o peso
      distintos, se ajusta el precio final.</li>
  <li>Las tarifas pueden variar por cambios de los couriers, recargos de
      combustible, zonas remotas o tipo de cambio.</li>
  <li>Los impuestos, derechos y tasas del país de destino no están
      incluidos salvo que se indique expresamente.</li>
  <li>Operás por cuenta corriente según las condiciones acordadas.</li>
</ul>

<h2>Mercadería y responsabilidad</h2>
<ul>
  <li>Sos responsable de que lo que enviás sea legal en origen y en
      destino, y de declararlo correctamente.</li>
  <li>No se transportan artículos prohibidos por los couriers o por la
      normativa aduanera aplicable.</li>
  <li>Ante pérdida o daño, la responsabilidad se rige por las condiciones
      del courier y el valor declarado del envío. Gestionamos el reclamo
      en tu nombre.</li>
  <li>No respondemos por demoras de aduana, controles oficiales ni casos
      de fuerza mayor.</li>
</ul>

<h2>La aplicación para tu tienda</h2>
<ul>
  <li>La app importa los pedidos con envío para que puedas despacharlos;
      no modifica tus productos, precios ni tus ventas.</li>
  <li>Podés desinstalarla cuando quieras: dejamos de recibir tus pedidos
      de inmediato y borramos los datos de tu tienda según nuestra
      política de privacidad.</li>
  <li>Hacemos todo lo razonable para que el servicio esté siempre
      disponible, pero no garantizamos disponibilidad ininterrumpida.</li>
</ul>

<h2>Ley aplicable</h2>
<p>Estos términos se rigen por la ley de la República Argentina, con
jurisdicción en los tribunales ordinarios de la Ciudad Autónoma de
Buenos Aires.</p>
"""


def pagina_privacidad() -> str:
    return _LAYOUT.format(titulo="Política de privacidad",
                          cuerpo=_PRIVACIDAD, contacto=CONTACTO)


def pagina_terminos() -> str:
    return _LAYOUT.format(titulo="Términos del servicio",
                          cuerpo=_TERMINOS, contacto=CONTACTO)
