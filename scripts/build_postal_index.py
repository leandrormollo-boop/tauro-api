"""Build a compact, offline quote-reference index from the GeoNames postal dump.

Usage: python scripts/build_postal_index.py /path/to/allCountries.zip
No customer data or network access. The original archive is not committed.
"""
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unicodedata
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from servicios.paises import referencias_formulario


def key(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', text)
                           if not unicodedata.combining(c)).casefold().split())


def build(archive, output):
    # GeoNames explicitly documents truncated codes for these countries.
    # AE/HK/MO have no standard postal code for delivery. Never synthesize one.
    excluded = {'CA', 'NL', 'GB', 'CL', 'IE', 'MT', 'AE', 'HK', 'MO'}
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / 'postal.sqlite'
        con = sqlite3.connect(db)
        con.execute('PRAGMA journal_mode=OFF')
        con.execute('CREATE TABLE places(country TEXT, cp TEXT, city TEXT, region TEXT, province TEXT, city_key TEXT, cp_key TEXT)')
        def row(country, cp, city, region, province):
            return country, cp, city, region, province, key(city), re.sub(r'[^A-Z0-9]', '', cp.upper())
        with zipfile.ZipFile(archive) as z, z.open('allCountries.txt') as f:
            batch = []
            for line in f:
                r = line.decode('utf-8').rstrip('\n').split('\t')
                country, cp, city, region, province = r[:5]
                if country in excluded or not cp or not city or not re.fullmatch('[A-Z]{2}', country):
                    continue
                if country == 'AR':
                    cp = cp[1:5] if re.fullmatch('[A-Z][0-9]{4}', cp) else cp
                    if not re.fullmatch('[0-9]{4}', cp): continue
                    city = city.title()
                batch.append(row(country, cp, city, region, province))
                if len(batch) >= 10000:
                    con.executemany('INSERT INTO places VALUES (?,?,?,?,?,?,?)', batch); batch.clear()
            con.executemany('INSERT INTO places VALUES (?,?,?,?,?,?,?)', batch)
        # Existing TAURO city references cover a few gaps without fabricating codes.
        for country, ref in referencias_formulario().items():
            if country in excluded and ref['postal_code']:
                con.execute('INSERT INTO places VALUES (?,?,?,?,?,?,?)', row(country, ref['postal_code'], ref['city'].title(), '', ''))
        con.execute('INSERT INTO places VALUES (?,?,?,?,?,?,?)', row('AR', '1000', 'Buenos Aires', 'Ciudad Autónoma de Buenos Aires', 'C'))
        con.execute('CREATE TABLE clean AS SELECT DISTINCT * FROM places')
        con.execute('DROP TABLE places'); con.execute('ALTER TABLE clean RENAME TO places')
        con.execute('CREATE INDEX by_city ON places(country, city_key)')
        con.execute('CREATE INDEX by_cp ON places(country, cp_key)')
        countries = dict(con.execute('SELECT country, COUNT(*) FROM places GROUP BY country'))
        con.commit(); con.execute('VACUUM'); con.close()
        target = output / 'postal.sqlite.gz'
        with target.open('wb') as dest, gzip.GzipFile(fileobj=dest, mode='wb', mtime=0) as g, db.open('rb') as f:
            import shutil
            shutil.copyfileobj(f, g)
        (output / 'manifest.json').write_text(json.dumps({
            'source': 'https://download.geonames.org/export/zip/allCountries.zip',
            'license': 'CC BY 4.0', 'built_on': datetime.now(timezone.utc).date().isoformat(),
            'source_sha256': hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
            'index_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'countries': countries, 'excluded_partial_or_nonpostal': sorted(excluded),
            'purpose': 'Referencias de cotización; no validación de domicilios.'
        }, indent=2, ensure_ascii=False) + '\n')
        print(f'{sum(countries.values())} referencias · {len(countries)} países · {target.stat().st_size / 1048576:.1f} MB comprimidos')


if __name__ == '__main__':
    build(Path(sys.argv[1]), ROOT / 'data/postal')
