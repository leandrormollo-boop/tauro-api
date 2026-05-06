"""
Dry-run: leer las 4 hojas de RECLAMOS y matchear contra ESTADO DE CUENTA.
NO ESCRIBE NADA. Solo imprime estadísticas y preview.
"""
import re
import gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

RECLAMOS = '1Wnfkx-uzcNEsUoOCSsxDRlHySHsYZT3_IfWiwWB-Up8'
ESTADO = '16KbPY-SsgrlLHmk-tTxx81pVckyDIzJw8GLqogtqXyU'

HOJAS_MOTIVOS = {
    'DESCUENTO NO APLICADO': 'descuento no aplicado',
    'RETORNO ERRONEO': 'retorno erroneo',
    'SOBRECARGO MANEJO ADIC': 'sobrecargo manejo adicional',
    'DIF MEDIDAS RECLAMAR': 'dif de peso',
}


def normalizar_fc(s: str) -> str:
    """Devuelve el numero limpio de FC (sin espacios, sin prefijos raros)."""
    if not s:
        return ''
    s = str(s).strip().upper()
    if not s:
        return ''
    # Sacar comillas, espacios internos
    s = s.replace(' ', '').replace('"', '').replace("'", '')
    return s


print('=' * 70)
print('PASO 1 — Leer estructura de hojas de reclamos')
print('=' * 70)

sh_reclamos = gc.open_by_key(RECLAMOS)

fc_motivo = {}  # fc_normalizada -> motivo
fc_origen = {}  # fc_normalizada -> hoja de origen

for hoja_name, motivo in HOJAS_MOTIVOS.items():
    print(f'\n— {hoja_name} —')
    try:
        ws = sh_reclamos.worksheet(hoja_name)
    except Exception as e:
        print(f'  ERROR abriendo: {e}')
        continue
    valores = ws.get_all_values()
    if len(valores) < 2:
        print('  Vacío')
        continue
    header = valores[0]
    print(f'  Header: {header[:8]}{"..." if len(header) > 8 else ""}')
    # Buscar columna que parezca de FC
    candidatas = []
    for i, h in enumerate(header):
        h_up = (h or '').strip().upper()
        if 'FC' in h_up or 'FACTURA' in h_up or h_up == 'NRO' or 'NUMERO' in h_up:
            candidatas.append((i, h))
    print(f'  Cols candidatas para FC: {candidatas}')
    # Sample 2 filas
    print(f'  Sample fila 2: {valores[1][:8] if len(valores) > 1 else "-"}')
    print(f'  Sample fila 3: {valores[2][:8] if len(valores) > 2 else "-"}')


print('\n' + '=' * 70)
print('PASO 2 — Estructura de ESTADO DE CUENTA')
print('=' * 70)

sh_estado = gc.open_by_key(ESTADO)
ws_estado = sh_estado.worksheets()[0]
print(f'Hoja: {ws_estado.title}')
valores_estado = ws_estado.get_all_values()
print(f'Filas totales: {len(valores_estado)}')
header_estado = valores_estado[0]
print(f'Header completo:')
for i, h in enumerate(header_estado):
    letra = chr(65 + i) if i < 26 else f'col{i}'
    print(f'  {letra} ({i}): {h!r}')
print(f'\nSample fila 2: {valores_estado[1][:14] if len(valores_estado) > 1 else "-"}')
print(f'Sample fila 3: {valores_estado[2][:14] if len(valores_estado) > 2 else "-"}')

# Ver qué hay en col M (idx 12) y N (idx 13) de las primeras 20 filas
print('\nCol M (12) y N (13) de las primeras 20 filas:')
for i, fila in enumerate(valores_estado[:20]):
    m = fila[12] if len(fila) > 12 else ''
    n = fila[13] if len(fila) > 13 else ''
    print(f'  fila {i+1}: M={m!r}  N={n!r}')
