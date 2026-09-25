# Ubicaciones de referencia para cotizar

Fuente: [GeoNames Postal Code Dataset](https://download.geonames.org/export/zip/), licencia [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Se conserva atribución visible en el autocompletado. Los datos no garantizan cobertura logística ni validan domicilios.

El índice contiene país, código postal, localidad y provincia/estado, con claves de búsqueda normalizadas. No contiene domicilios ni datos de clientes. Se deduplicaron filas y se omitieron coordenadas y divisiones administrativas secundarias. El archivo `manifest.json` registra fuente, hashes y cantidad de filas por país.

Los países con códigos incompletos según el [readme de GeoNames](https://download.geonames.org/export/zip/readme.txt) —Canadá, Países Bajos, Reino Unido, Chile, Irlanda y Malta— no se importan del dump general. Sólo conservan las referencias puntuales de ciudad y código completo que ya tenía TAURO, si existen. Tampoco se importan códigos para Emiratos Árabes Unidos, Hong Kong o Macao. Brasil incluye códigos principales y China referencias terminadas en 00: sirven para estimar, no para definir el domicilio de emisión.

Para Argentina se usa el CP numérico de cuatro dígitos que acepta la cotización OCA. La relación CABA/Buenos Aires → 1000 se agregó expresamente a pedido del usuario. Al emitir debe completarse el domicilio y código exactos. Si un código tiene varias localidades, se ofrecen opciones. No se selecciona una ciudad ambigua automáticamente.

## Regenerar

Descargar `allCountries.zip` de la fuente indicada y ejecutar:

```sh
python scripts/build_postal_index.py /ruta/allCountries.zip
```

Revisar los cambios de cobertura y los tests antes de publicar. La aplicación no descarga datos en ejecución: descomprime el índice una vez por proceso en un directorio temporal privado y usa SQLite de solo lectura, con índices por país/ciudad y país/CP. El navegador recibe hasta ocho sugerencias, nunca esta base completa.
