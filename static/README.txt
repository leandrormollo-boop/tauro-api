============================================================
  07_static — Archivos estáticos (CSS, JS, imágenes)
============================================================

QUÉ HAY ACÁ
-----------
Servidos por FastAPI en /static/*

  css/
    tauro.css         - Estilos del portal y admin
    reset.css         - Reset estándar
  js/
    cotizar.js        - Cálculo de peso volumétrico en vivo
    catalogo.js       - Auto-llenar medidas al elegir producto
    htmx.min.js       - HTMX para interacciones sin reload
  img/
    logo.png          - Logo Tauro
    favicon.ico

LÓGICA Y DECISIONES
-------------------

CSS
- Una sola hoja tauro.css — todo el sistema de diseño.
- Variables CSS para colores (--color-primary, --color-accent, etc.).
- Responsive con media queries (mobile-first).
- Sin frameworks (no Bootstrap, no Tailwind) — control total.

JS
- Vanilla JS donde sea posible.
- HTMX para AJAX declarativo (atributos hx-get, hx-post).
- Sin build step (sin webpack, sin npm install).

IMÁGENES
- PNG para logo (transparencia).
- SVG donde se pueda (escalable).
- Optimizadas con TinyPNG antes de subir.

REGLAS
------
- NUNCA inline-styles en templates (todo va a tauro.css).
- NUNCA scripts inline largos (más de 5 líneas → archivo aparte).
- Versionar con ?v=1.2 al cambiar (cache busting simple).
