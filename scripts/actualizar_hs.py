"""Construye un índice HS6 desde una edición USITC de dominio público.

Uso: python scripts/actualizar_hs.py --archivo /ruta/hts.json --url URL --edicion EDICION
No descarga durante el arranque ni actualiza producción automáticamente. Revisar
diff/cobertura y ejecutar tests antes de publicar cada nueva edición.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def construir(raw, url, edicion):
    if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != 'www.usitc.gov':
        raise ValueError('La fuente debe ser una descarga oficial HTTPS de USITC.')
    rows = json.loads(raw)
    stack, groups = [], defaultdict(list)
    for row in rows:
        code = row['htsno'].replace('.', '')
        level = int(row['indent'])
        description = html.unescape(re.sub(r'<[^>]+>', '', row['description'])).strip().rstrip(':')
        if code and len(code) == 4:
            stack = []
        while stack and stack[-1][0] >= level:
            stack.pop()
        path = [text for _, text in stack] + [description]
        if re.fullmatch(r'[0-9]{6}(?:[0-9]{2}){0,2}', code) and int(code[:2]) < 98:
            groups[code[:6]].append((len(code), path))
        stack.append((level, description))
    entries = []
    for code, variants in sorted(groups.items()):
        # Si hay nodo HS6, conservar su texto. Cuando USITC lo presenta unido
        # a una extensión nacional, tomar sólo el ancestro común de TODAS sus
        # ramas, nunca el texto de una rama nacional elegida arbitrariamente.
        depth = min(n for n, _ in variants)
        paths = [p for n, p in variants if n == depth]
        common = []
        for parts in zip(*paths):
            if len(set(parts)) != 1:
                break
            common.append(parts[0])
        if not common:
            raise ValueError(f'Sin contexto jerárquico para {code}')
        entries.append(dict(code=code, description='; '.join(common), path=common, parent=code[:4]))
    if len(entries) != 5612:
        raise ValueError(f'Cobertura HS2022 inesperada: {len(entries)}. Revisar manualmente.')
    return dict(edition='HS2022', source='USITC — nomenclatura HS6 extraída del HTS',
        source_url=url, source_index='https://www.usitc.gov/harmonized_tariff_information',
        source_edition=edicion, source_license='http://www.usa.gov/publicdomain/label/1.0/',
        license_evidence='https://www.usitc.gov/data.json',
        source_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=datetime.now(timezone.utc).isoformat(),
        scope='HS6 lookup only; no national suffixes, duty rates, legal notes or binding classification', entries=entries)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archivo', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--edicion', required=True)
    parser.add_argument('--salida', default=str(Path(__file__).resolve().parents[1] / 'datos/hs2022.json'))
    args = parser.parse_args()
    raw = Path(args.archivo).read_bytes()
    if len(raw) > 20_000_000:
        raise ValueError('Archivo demasiado grande')
    data = construir(raw, args.url, args.edicion)
    target = Path(args.salida)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    temporary.replace(target)
    print(f"HS2022: {len(data['entries'])} códigos; SHA256 {data['source_sha256']}")
