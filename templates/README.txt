============================================================
  06_templates — HTML del portal (Jinja2)
============================================================

QUÉ HAY ACÁ
-----------
Templates HTML que renderiza FastAPI con Jinja2.

  base.html               - Layout común (header, footer, nav)
  portal/
    login.html            - Form de email para login mágico
    home.html             - Saldo + últimos envíos
    envios.html           - Tabla completa filtrable
    cuenta.html           - Cuenta corriente con timeline
    cotizar.html          - Form de cotización con dropdowns
    catalogo.html         - Productos del cliente + form alta
    guia.html             - (Phase 2) Generar guía
  admin/
    home.html             - Panel principal con health
    cotizaciones.html     - Tabla últimas cotizaciones
    errores.html          - Log de errores con replay
    clientes.html         - Lista de PERFILES editable

LÓGICA Y DECISIONES
-------------------

STACK
- Jinja2 (incluido con FastAPI/starlette).
- Sin frameworks JS pesados — HTML + un poco de JS vanilla.
- HTMX para interacciones (sin recargar página).
- CSS propio en 07_static/css/tauro.css (ver paleta abajo).

PALETA DE COLORES
- Primario:  #1a1a1a (negro Tauro)
- Acento:    #d4af37 (dorado)
- Fondo:     #fafafa
- Texto:     #2c2c2c
- Éxito:     #22c55e
- Error:     #ef4444
- Warning:   #f59e0b

ESTRUCTURA TIPO
- Cada template extiende base.html.
- base.html tiene bloques: {% block title %}, {% block content %}.
- Variables comunes: {{ cliente }}, {{ saldo }}, {{ ahora }}.

UX
- Mobile-first (clientes consultan saldo desde el cel).
- Sin animaciones excesivas — es herramienta de trabajo.
- Tablas con sticky header + filtro arriba.
- Confirmación antes de cualquier POST destructivo.

REGLAS
------
- Lógica mínima en templates (solo presentación).
- Datos preprocesados en los servicios.
- Strings en español (audiencia AR).
- Fechas en formato DD/MM/YYYY.
- Montos con separador de miles AR (1.234.567,89).
