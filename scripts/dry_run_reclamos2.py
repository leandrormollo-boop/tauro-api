"""
Dry-run 2: simula los cambios sin escribir.
"""
import re
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

RECLAMOS = '1Wnfkx-uzcNEsUoOCSsxDRlHySHsYZT3_IfWiwWB-Up8'
ESTADO = '16KbPY-SsgrlLHmk-tTxx81pVckyDIzJw8GLqogtqXyU'

HOJAS_MOTIVOS = {
    'DESCUENTO NO APLICADO': ('descuento no aplicado', 6),  # col G
    'RETORNO ERRONEO': ('retorno erroneo', 6),
    'SOBRECARGO MANEJO ADIC': ('sobrecargo manejo adicional', 6),
    'DIF MEDIDAS RECLAMAR': ('dif de peso', 9),  # col J
}


def normalizar_fc(s: str) -> str:
    if not s:
        return ''
    s = str(s).strip().upper()
    s = re.sub(r'\s+', '', s)  # sin espacios
    s = s.replace('-', '')      # sin guiones
    s = s.replace('"', '').replace("'", '')
    # Quedarse solo con dígitos
    s = re.sub(r'[^0-9]', '', s)
    return s


# ─── PASO 1: cargar FC reclamadas con sus motivos ───
sh_reclamos = gc.open_by_key(RECLAMOS)
fc_motivos = defaultdict(set)  # fc_norm -> {motivos}

for hoja_name, (motivo, col_idx) in HOJAS_MOTIVOS.items():
    ws = sh_reclamos.worksheet(hoja_name)
    valores = ws.get_all_values()
    for fila in valores[1:]:
        if len(fila) <= col_idx:
            continue
        fc_norm = normalizar_fc(fila[col_idx])
        if not fc_norm or len(fc_norm) < 6:
            continue
        fc_motivos[fc_norm].add(motivo)

print(f'Total FC reclamadas únicas: {len(fc_motivos)}')

# Distribución por hoja
print('\nDistribución FC por hoja de reclamo:')
for hoja_name, (motivo, _) in HOJAS_MOTIVOS.items():
    n = sum(1 for v in fc_motivos.values() if motivo in v)
    print(f'  {hoja_name}: {n} FC con motivo "{motivo}"')

# FC con múltiples motivos
multi = {fc: sorted(m) for fc, m in fc_motivos.items() if len(m) > 1}
print(f'\nFC con múltiples motivos: {len(multi)}')
for fc, m in list(multi.items())[:5]:
    print(f'  {fc} → {m}')


# ─── PASO 2: leer estado de cuenta ───
sh_estado = gc.open_by_key(ESTADO)
ws_estado = sh_estado.worksheets()[0]
valores_estado = ws_estado.get_all_values()
print(f'\nEstado de cuenta: {len(valores_estado) - 1} filas de datos')

# ─── PASO 3: simular cambios ───
to_update = []  # (row_1based, col_M, col_N, descripcion)
sin_match = 0
ya_completas = 0
ojo_no_reclamo = []  # filas con M = PAGAR / VENCIDA pero estan en reclamos

IDX_REFERENCE = 6  # col G — FC con prefijo 0106
IDX_M = 12          # col M ESTADO FACTURA
IDX_N = 13          # col N MOTIVO

for i, fila in enumerate(valores_estado[1:], start=2):  # row index 1-based
    inv = normalizar_fc(fila[IDX_REFERENCE]) if len(fila) > IDX_REFERENCE else ''
    if not inv:
        continue
    if inv not in fc_motivos:
        sin_match += 1
        continue

    motivo_str = ', '.join(sorted(fc_motivos[inv]))
    m_actual = (fila[IDX_M] if len(fila) > IDX_M else '').strip().upper()
    n_actual = (fila[IDX_N] if len(fila) > IDX_N else '').strip()

    if m_actual == '':
        # M vacío → escribir M=RECLAMO, N=motivo
        to_update.append((i, 'RECLAMO', motivo_str, f'fila {i}: M vacío → RECLAMO + "{motivo_str}"'))
    elif m_actual == 'RECLAMO':
        if not n_actual:
            # M ya RECLAMO, N vacío → solo agregar motivo
            to_update.append((i, None, motivo_str, f'fila {i}: solo agregar motivo "{motivo_str}"'))
        else:
            ya_completas += 1
    else:
        # M dice PAGAR / VENCIDA / otro → no tocar, pero reportar
        ojo_no_reclamo.append((i, inv, m_actual, motivo_str))


print(f'\n══ RESUMEN ══')
print(f'  Filas a actualizar: {len(to_update)}')
print(f'  Ya completas (M=RECLAMO + N tiene texto): {ya_completas}')
print(f'  ⚠️  Filas con M={"PAGAR/VENCIDA/otro"} pero figuran en reclamos: {len(ojo_no_reclamo)}')
print(f'  Sin match (FC del estado no en reclamos): {sin_match}')

print(f'\n══ PREVIEW PRIMERAS 20 ACTUALIZACIONES ══')
for u in to_update[:20]:
    print(f'  {u[3]}')

print(f'\n══ ⚠️  FILAS CON ESTADO RARO (no se tocan, solo reporto) ══')
for o in ojo_no_reclamo[:20]:
    print(f'  fila {o[0]}: FC={o[1]} M actual={o[2]!r} → estaria en reclamos como "{o[3]}"')
if len(ojo_no_reclamo) > 20:
    print(f'  ... y {len(ojo_no_reclamo) - 20} más')
