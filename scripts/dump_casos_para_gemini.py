"""Genera un resumen de casos ambiguos para que Gemini los audite."""
import re
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

MENDEZ_2025 = '1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0'
MENDEZ_2026 = '1cI2uBWvcqAw5zHwNsV7-6uzQJPz1nxx9nBgWPPgh1CU'


def normalizar_fc(s):
    if not s:
        return ''
    return re.sub(r'[^0-9]', '', str(s).strip())


def parse_monto(s):
    if not s:
        return 0.0
    s = str(s).strip().replace('$', '').replace(' ', '').replace('\xa0', '')
    if ',' in s and '.' in s:
        s = s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def normalizar_concepto(s):
    s = str(s).strip().upper()
    if 'FLETE' in s:
        return 'FLETE'
    if 'TAX' in s:
        return 'TAX'
    return ''


sh25 = gc.open_by_key(MENDEZ_2025)
sh26 = gc.open_by_key(MENDEZ_2026)

# Indexar FC TAURO: tracking → list of (concepto, NRO FC, monto)
fc_por_tracking = defaultdict(list)

v = sh25.worksheet('FC TAURO 2025').get_all_values()
for fila in v[1:]:
    if len(fila) < 9:
        continue
    tracking = str(fila[6]).strip()
    concepto = normalizar_concepto(fila[5])
    nro_fc = str(fila[7]).strip()
    monto = parse_monto(fila[8])
    if tracking and concepto and nro_fc:
        fc_por_tracking[tracking].append((concepto, nro_fc, monto, '2025'))

v = sh26.worksheet('FC TAURO 2026').get_all_values()
for fila in v[1:]:
    if len(fila) < 11:
        continue
    tracking = str(fila[8]).strip()
    concepto = normalizar_concepto(fila[7])
    nro_fc = str(fila[9]).strip()
    monto = parse_monto(fila[10])
    if tracking and concepto and nro_fc:
        fc_por_tracking[tracking].append((concepto, nro_fc, monto, '2026'))

# ¿Cuántos trackings tienen >1 FLETE o >1 TAX en FC TAURO?
multi_flete = 0
multi_tax = 0
ejemplos_multi = []
for t, lista in fc_por_tracking.items():
    fletes = [x for x in lista if x[0] == 'FLETE']
    taxes = [x for x in lista if x[0] == 'TAX']
    if len(fletes) > 1:
        multi_flete += 1
        if len(ejemplos_multi) < 10:
            ejemplos_multi.append((t, fletes, taxes))
    if len(taxes) > 1:
        multi_tax += 1

print(f'═══ Análisis de FC TAURO ═══')
print(f'Total trackings: {len(fc_por_tracking)}')
print(f'Trackings con >1 FLETE: {multi_flete}')
print(f'Trackings con >1 TAX:   {multi_tax}')
print(f'\n══ Ejemplos trackings con múltiples FLETE ══')
for t, fletes, taxes in ejemplos_multi[:5]:
    print(f'  {t}:')
    for c, nro, m, src in fletes:
        print(f'    FLETE: {nro} ${m:,.2f} (src {src})')
    for c, nro, m, src in taxes:
        print(f'    TAX:   {nro} ${m:,.2f} (src {src})')

# Casos de error tipo "Solo NRO FC mal" — sample 10
# (recorriendo Mendez con la misma lógica que el script anterior)
print(f'\n══ Sample de casos "etiqueta OK pero NRO FC mal" ══')

casos_solo_fc = []
for hoja in ['MAYO', 'AGOSTO', 'SEPTIEMBRE', 'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']:
    ws = sh25.worksheet(hoja)
    valores = ws.get_all_values()
    for i, fila in enumerate(valores[5:], start=6):
        if len(fila) <= 13:
            continue
        tracking = str(fila[6]).strip()
        if not tracking or tracking not in fc_por_tracking:
            continue
        monto = parse_monto(fila[7])
        etiq = normalizar_concepto(fila[12])
        nro_fc_actual = str(fila[13]).strip()
        nro_fc_actual_norm = normalizar_fc(nro_fc_actual)
        info = fc_por_tracking[tracking]
        # Determinar concepto real por monto
        fletes = [x for x in info if x[0] == 'FLETE']
        taxes = [x for x in info if x[0] == 'TAX']
        if fletes and taxes:
            avg_flete = sum(x[2] for x in fletes) / len(fletes)
            avg_tax = sum(x[2] for x in taxes) / len(taxes)
            concepto_real = 'FLETE' if abs(monto - avg_flete) <= abs(monto - avg_tax) else 'TAX'
        elif fletes:
            concepto_real = 'FLETE'
        else:
            concepto_real = 'TAX'

        # ¿La NRO FC actual existe en FC TAURO?
        candidatos = [x for x in info if normalizar_fc(x[1]) == nro_fc_actual_norm]
        if not candidatos:
            continue  # NRO FC no está en FC TAURO — caso distinto
        concepto_de_la_fc = candidatos[0][0]

        # CASO: etiqueta coincide con concepto_real PERO NRO FC pertenece a otro concepto
        if etiq == concepto_real and concepto_de_la_fc != concepto_real:
            casos_solo_fc.append({
                'hoja': hoja, 'fila': i, 'tracking': tracking, 'monto': monto,
                'etiq': etiq, 'fc_actual': nro_fc_actual, 'fc_es_de': concepto_de_la_fc,
                'fletes': [(x[1], x[2]) for x in fletes],
                'taxes': [(x[1], x[2]) for x in taxes],
            })
        if len(casos_solo_fc) >= 8:
            break
    if len(casos_solo_fc) >= 8:
        break

for c in casos_solo_fc:
    print(f'\n  {c["hoja"]} fila {c["fila"]}: tracking {c["tracking"]} monto ${c["monto"]:,.2f}')
    print(f'    Etiqueta: {c["etiq"]} (correcta según monto)')
    print(f'    NRO FC actual: {c["fc_actual"]} (¡pero esa FC en realidad es {c["fc_es_de"]}!)')
    print(f'    FC TAURO tiene para este tracking:')
    for nro, m in c['fletes']:
        print(f'      FLETE: {nro} ${m:,.2f}')
    for nro, m in c['taxes']:
        print(f'      TAX:   {nro} ${m:,.2f}')
