// Railway sirve estos archivos ya construidos. Ejecutar al actualizar pdfjs-dist.
import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
const source = new URL('../node_modules/pdfjs-dist/', import.meta.url);
const dest = new URL('../static/vendor/pdfjs/', import.meta.url);
await mkdir(dest, { recursive: true });
for (const file of ['build/pdf.min.mjs', 'build/pdf.worker.min.mjs', 'cmaps', 'standard_fonts', 'wasm', 'LICENSE']) {
  await cp(new URL(file, source), new URL(file.replace('build/', '').replace('.min.mjs', '.mjs'), dest), { recursive: true });
}
const { version } = JSON.parse(await readFile(new URL('package.json', source), 'utf8'));
await writeFile(new URL('VERSION', dest), version + '\n');
