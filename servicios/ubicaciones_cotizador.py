"""City/postcode quote references, searched locally; never shipment validation."""
from functools import lru_cache
import gzip
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import unicodedata

from servicios.paises import normalizar_iso2
from servicios.provincias import normalizar_provincia

_DATA = Path(__file__).resolve().parents[1] / 'data/postal/postal.sqlite.gz'
_lock = threading.Lock()
_temp = None
_database = None


def normalize(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(text or ''))
                           if not unicodedata.combining(c)).casefold().split())


def _path():
    global _temp, _database
    with _lock:
        if _database is None:
            # Private per-process directory; the shipped bundle stays read-only.
            _temp = tempfile.TemporaryDirectory(prefix='tauro-postal-')
            target = Path(_temp.name) / 'postal.sqlite'
            with gzip.open(_DATA, 'rb') as source, target.open('wb') as out:
                shutil.copyfileobj(source, out)
            _database = target
    return _database


def buscar_ubicaciones(country, query, mode='city', province=''):
    country = normalizar_iso2(country)
    if not country or mode not in {'city', 'postal'}:
        return {'suggestions': [], 'automatic': None}
    query = str(query or '').strip()[:100]
    if len(query) < (2 if mode == 'city' else 3):
        return {'suggestions': [], 'automatic': None}
    return _search(country, query, mode, normalizar_provincia(province) if country == 'AR' else '')


@lru_cache(maxsize=512)
def _search(country, query, mode, province):
    normalized = normalize(query)
    if country == 'AR' and mode == 'city' and normalized in {
        'caba', 'capital', 'capital federal', 'ciudad de buenos aires',
        'ciudad autonoma de buenos aires', 'buenos aires',
    } and (province in {'', 'C'} or normalized != 'buenos aires'):
        option = {'city': 'Buenos Aires', 'postal_code': '1000',
                  'region': 'Ciudad Autónoma de Buenos Aires', 'province': 'C', 'reference': True}
        return {'suggestions': [option], 'automatic': option}
    if mode == 'postal':
        normalized = re.sub('[^A-Z0-9]', '', query.upper())
        if country == 'AR' and re.fullmatch('[A-Z][0-9]{4}([A-Z]{3})?', normalized):
            province = normalized[0]; normalized = normalized[1:5]
    column = 'city_key' if mode == 'city' else 'cp_key'
    conditions = 'country=? AND ' + column + '>=? AND ' + column + '<?'
    params = [country, normalized, normalized + '\uffff']
    if province:
        conditions += ' AND province=?'; params.append(province)
    con = sqlite3.connect(_path().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        rows = con.execute('SELECT city, cp, region, province, city_key, cp_key FROM places WHERE '
                           + conditions + ' ORDER BY ' + column + ', cp LIMIT 80', params).fetchall()
        # Exact matches establish uniqueness across the entire DB, not a truncated result page.
        exact = con.execute('SELECT DISTINCT city_key, region, province FROM places WHERE country=? AND '
                            + column + '=?' + (' AND province=?' if province else '') + ' LIMIT 2',
                            [country, normalized] + ([province] if province else [])).fetchall()
    finally:
        con.close()
    if not rows and province:
        return _search(country, query, mode, '')
    suggestions, seen = [], set()
    for city, cp, region, prov, ck, pk in rows:
        identity = (ck, region, prov) if mode == 'city' else (ck, cp, region, prov)
        if identity in seen: continue
        seen.add(identity)
        suggestions.append({'city': city, 'postal_code': cp, 'region': region,
                            'province': prov if country == 'AR' else '', 'reference': True})
        if len(suggestions) == 8: break
    automatic = None
    if len(exact) == 1:
        automatic = next((s for s in suggestions if normalize(s['city']) == exact[0][0]), None)
    return {'suggestions': suggestions, 'automatic': automatic}
