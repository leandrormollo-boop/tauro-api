"""
Compara trackings entre:
  - Mendez 2025: NOVIEMBRE, DICIEMBRE (TRACKING en col G, idx 6)
  - Mendez 2026: pendientes 2025, ENERO (TRACKING en col I, idx 8)

Reporta los duplicados (mismo tracking en >= 2 hojas).
NO ESCRIBE NADA.
"""
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

MENDEZ_2025 = '1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0'
MENDEZ_2026 = '1cI2uBWvcqAw5zHwNsV7-6uzQJPz1nxx9nBgWPPgh1CU'

# (sheet_id, hoja, etiqueta, idx_tracking)
FUENTES = [
    (MENDEZ_2025, 'NOVIEMBRE', '2025/NOV', 6),
    (MENDEZ_2025, 'DICIEMBRE', '2025/DIC', 6),
    (MENDEZ_2026, 'pendientes 2025', '2026/pend', 8),
    (MENDEZ_2026, 'ENERO', '2026/ENE', 8),
]


def normalizar(t):
    if not t:
        return ''
    return str(t).strip().replace(' ', '').replace('-', '')


# tracking_normalizado -> [(etiqueta, fila, datos_extra), ...]
mapa = defaultdict(list)

for sid, hoja, etiq, idx in FUENTES:
    sh = gc.open_by_key(sid)
    ws = sh.worksheet(hoja)
    v = ws.get_all_values()
    cont = 0
    for i, fila in enumerate(v[5:], start=6):  # datos desde fila 6
        if len(fila) <= idx:
            continue
        t = normalizar(fila[idx])
        if not t or len(t) < 8:
            continue
        # Datos extra: fecha, destinatario, FC
        fecha = fila[1] if len(fila) > 1 else ''
        if etiq.startswith('2025'):
            dest = fila[2] if len(fila) > 2 else ''
            fc = fila[13] if len(fila) > 13 else ''
            facturado = fila[7] if len(fila) > 7 else ''
        else:
            dest = fila[3] if len(fila) > 3 else ''
            fc = fila[9] if len(fila) > 9 else ''
            facturado = fila[10] if len(fila) > 10 else ''
        mapa[t].append({
            'etiq': etiq, 'fila': i, 'fecha': fecha,
            'dest': dest[:18], 'fc': fc, 'facturado': facturado,
        })
        cont += 1
    print(f'  {etiq}: {cont} trackings cargados')

print(f'\nTotal trackings únicos: {len(mapa)}')
duplicados = {t: refs for t, refs in mapa.items() if len(refs) > 1}
print(f'Trackings duplicados (en >= 2 hojas): {len(duplicados)}\n')

# Agrupar por tipo de duplicado
print(f'══ DUPLICADOS ══')
print(f'{"TRACKING":<14} {"DONDE APARECE":<60} {"FACTURADO":<14}')
print('-' * 100)
for t, refs in sorted(duplicados.items(), key=lambda x: x[1][0]['etiq']):
    where = ', '.join(f'{r["etiq"]}#{r["fila"]}' for r in refs)
    facts = ' / '.join(str(r['facturado']) for r in refs)
    print(f'{t:<14} {where:<60} {facts}')

# Resumen por par de hojas
print(f'\n══ RESUMEN: cantidad de duplicados por par de hojas ══')
pares = defaultdict(int)
for refs in duplicados.values():
    etiqs = sorted(set(r['etiq'] for r in refs))
    if len(etiqs) >= 2:
        for i in range(len(etiqs)):
            for j in range(i+1, len(etiqs)):
                pares[(etiqs[i], etiqs[j])] += 1
for (a, b), n in sorted(pares.items(), key=lambda x: -x[1]):
    print(f'  {a} ↔ {b}: {n}')
