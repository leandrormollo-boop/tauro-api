============================================================
  08_scripts — Scripts auxiliares
============================================================

QUÉ HAY ACÁ
-----------
Scripts que NO son parte del API en sí — herramientas de soporte.
Se corren a mano cuando hace falta.

  inicializar_hojas.py      - Crea las hojas nuevas en Sheets (1ra vez)
  generar_csv_notas.py      - Generar CSV de notas (ya existente)
  generar_xlsx_notas.py     - Generar XLSX de notas (ya existente)
  read_pdf_scratch.py       - Probar parseo de PDFs (sandbox)
  importar_clientes.py      - Bulk import de clientes a PERFILES
  importar_productos.py     - Bulk import de catálogo a PRODUCTOS_CATALOGO
  test_fedex.py             - Probar conexión FedEx sandbox

LÓGICA Y DECISIONES
-------------------

[inicializar_hojas.py]
- Se corre UNA vez al setear el sistema.
- Crea hojas: PAGOS, SESSIONS, RUTAS_DEFAULT, LOG_REQUESTS,
  LOG_ERRORES, LOG_HEALTH, LOG_JOBS, PRODUCTOS_CATALOGO.
- Si la hoja ya existe, la deja (no la pisa).
- Pone los headers correctos.
- Carga datos iniciales en RUTAS_DEFAULT (AR-US, AR-ES, etc.).

[test_fedex.py]
- Hace una cotización de prueba a FedEx sandbox.
- Útil para verificar que las credenciales OAuth funcionan.
- Usa: ARG (C1000) → Miami (33166), 1kg, 30x20x15.

REGLAS
------
- Scripts NO se importan desde main.py.
- Cada script es independiente — se corre con `python 08_scripts/X.py`.
- Cada uno imprime al final un resumen claro de qué hizo.
- Confirmación antes de operaciones destructivas (input("¿Aplicar? s/n")).
