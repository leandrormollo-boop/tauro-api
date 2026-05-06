============================================================
  09_docs — Documentación
============================================================

QUÉ HAY ACÁ
-----------
Documentación detallada del proyecto.

  ARQUITECTURA_TAURO_API.md   - Arquitectura general (existente)
  ESTRUCTURA_SHEETS.txt       - Estructura de cada hoja (columnas)
  ENDPOINTS.txt               - Detalle de cada endpoint
  ROADMAP.txt                 - Qué falta + orden de prioridades
  DEPLOY.txt                  - Cómo deployar a Render/Railway/VPS
  TROUBLESHOOTING.txt         - Errores comunes + cómo resolverlos

LÓGICA Y DECISIONES
-------------------

POR QUÉ TXT (no MD)
- Los abrís desde cualquier editor (incluso Notepad).
- No requieren render para leerse bien.
- Excepción: ARQUITECTURA porque tiene diagramas mermaid.

ACTUALIZACIÓN
- Cada vez que se cambia un endpoint → actualizar ENDPOINTS.txt.
- Cada vez que se agrega columna a una hoja → ESTRUCTURA_SHEETS.txt.
- Si encontrás un error raro y lo resolvés → anotarlo en TROUBLESHOOTING.

REGLAS
------
- Docs deben ser concretas (no marketing).
- Ejemplos de código reales, no pseudo-código.
- Si un README de carpeta crece mucho, partirlo en archivo de docs.
