"""
v2: Considera la lógica FLETE/TAX.
Solo cuenta como DUPLICADO REAL si dos filas tienen el mismo tracking Y el mismo concepto (FLETE o TAX).
Si una es FLETE y la otra TAX, es legítimo.
"""
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

MENDEZ_2025 = '1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0'
MENDEZ_2026 = '1cI2uBWvcqAw5zHwNsV7-6uzQJPz1nxx9nBgWPPgh1CU'

# (sid, hoja, etiqueta, idx_tracking, idx_flete_tax, idx_facturado, idx_fc)
FUENTES = [
    (MENDEZ_2025, 'NOVIEMBRE', '2025/NOV', 6, 12, 7, 13),
    (MENDEZ_2025, 'DICIEMBRE', '2025/DIC', 6, 12, 7, 13),
    (MENDEZ_2026, 'pendientes 2025', '2026/pend', 8, 7, 10, 9),
    (MENDEZ_2026, 'ENERO', '2026/ENE', 8, 7, 10, 9),
]


def normalizar(t):
    if not t:
        return ''
    return str(t).strip().replace(' ', '').replace('-', '')


def normalizar_concepto(s):
    s = str(s).strip().upper()
    if 'FLETE' in s:
        return 'FLETE'
    if 'TAX' in s:
        return 'TAX'
    return s or '?'


# tracking -> [(etiqueta, fila, concepto, monto, fc), ...]
mapa = defaultdict(list)
for sid, hoja, etiq, idx_t, idx_ft, idx_f, idx_fc in FUENTES:
    sh = gc.open_by_key(sid)
    ws = sh.worksheet(hoja)
    v = ws.get_all_values()
    for i, fila in enumerate(v[5:], start=6):
        if len(fila) <= idx_t:
            continue
        t = normalizar(fila[idx_t])
        if not t or len(t) < 8:
            continue
        concepto = normalizar_concepto(fila[idx_ft]) if len(fila) > idx_ft else '?'
        monto = fila[idx_f] if len(fila) > idx_f else ''
        fc = fila[idx_fc] if len(fila) > idx_fc else ''
        mapa[t].append({
            'etiq': etiq, 'fila': i, 'concepto': concepto,
            'monto': monto, 'fc': fc,
        })

# Para cada tracking con >1 ref: agrupar por concepto y ver si hay duplicado real
duplicados_reales = []      # mismo tracking + mismo concepto en >= 2 hojas/filas
fletes_y_tax_legitimo = []  # mismo tracking, FLETE+TAX → legítimo

for t, refs in mapa.items():
    if len(refs) < 2:
        continue
    # agrupar por concepto
    por_concepto = defaultdict(list)
    for r in refs:
        por_concepto[r['concepto']].append(r)
    for concepto, lista in por_concepto.items():
        if len(lista) > 1:
            duplicados_reales.append((t, concepto, lista))
    # ¿es FLETE+TAX legítimo?
    conceptos = set(por_concepto.keys())
    if conceptos == {'FLETE', 'TAX'} and all(len(l) == 1 for l in por_concepto.values()):
        fletes_y_tax_legitimo.append((t, refs))

print(f'TRACKINGS con múltiples referencias: {sum(1 for r in mapa.values() if len(r) > 1)}')
print(f'  → de los cuales son LEGÍTIMOS (FLETE+TAX exacto, 1 c/u): {len(fletes_y_tax_legitimo)}')
print(f'  → DUPLICADOS REALES (mismo concepto repetido): {len(duplicados_reales)}\n')

print(f'══ DUPLICADOS REALES ══')
print(f'{"TRACKING":<14} {"CONCEPTO":<8} {"DONDE":<60} {"MONTOS":<30}')
print('-' * 120)
# Agrupar para reporte
from collections import Counter
pares_real = Counter()
for t, concepto, lista in duplicados_reales:
    where = ', '.join(f'{r["etiq"]}#{r["fila"]}' for r in lista)
    montos = ' / '.join(str(r['monto']) for r in lista)
    print(f'{t:<14} {concepto:<8} {where:<60} {montos:<30}')
    etiqs = sorted(set(r['etiq'] for r in lista))
    if len(etiqs) >= 2:
        for i in range(len(etiqs)):
            for j in range(i+1, len(etiqs)):
                pares_real[(etiqs[i], etiqs[j], concepto)] += 1
    elif len(etiqs) == 1:
        pares_real[(etiqs[0], etiqs[0], concepto)] += 1

print(f'\n══ RESUMEN duplicados REALES por par/concepto ══')
for (a, b, c), n in sorted(pares_real.items(), key=lambda x: -x[1]):
    if a == b:
        print(f'  Internos en {a} ({c}): {n}')
    else:
        print(f'  {a} ↔ {b} ({c}): {n}')
