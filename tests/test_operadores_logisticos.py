from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from servicios import operadores_logisticos as op


def documento(**cambios):
    f=dict(total=Decimal('100'),estado='CONCILIADA',tipo_documento='FC',moneda='USD',
        fecha_emision=date(2026,8,1),fecha_vencimiento=date(2026,8,31),aplicado=Decimal(0),verificado=False)
    f.update(cambios)
    return f


def test_conciliada_no_equivale_a_pagada_ni_impaga():
    f=op.estado_documento(documento(),date(2026,9,4))
    assert f['estado_pago']=='SIN_VERIFICAR' and f['dias_mora'] is None
    assert f['saldo']==100 and f['dias_emision']==34
    r=op.resumen_documentos([f])[0]
    assert r['sin_verificar']==1 and r['pendiente']==0 and r['vencido']==0


@pytest.mark.parametrize('hoy,mora,dias',[(date(2026,8,30),0,1),(date(2026,8,31),0,0),(date(2026,9,4),4,-4)])
def test_vencimiento_no_es_antiguedad(hoy,mora,dias):
    f=op.estado_documento(documento(verificado=True),hoy)
    assert f['dias_mora']==mora and f['dias_vencimiento']==dias and f['estado_pago']=='IMPAGA'


def test_cancelada_congela_mora():
    base=documento(aplicado=Decimal(100),ultima_aplicacion=date(2026,9,2))
    for hoy in [date(2026,9,4),date(2027,1,1)]:
        f=op.estado_documento(base,hoy)
        assert f['estado_pago']=='CANCELADA' and f['dias_mora']==2 and f['fecha_cancelacion']==date(2026,9,2)


def test_plazo_no_se_inventa_y_fecha_documental_prevalece():
    f=op.estado_documento(documento(fecha_emision=None,fecha_vencimiento=None,verificado=True))
    assert f['vencimiento'] is None and f['dias_mora'] is None and f['dias_emision'] is None
    f=op.estado_documento(documento(vencimiento_acordado=date(2026,9,20)))
    assert f['vencimiento']==date(2026,8,31)


def test_nc_no_es_deuda_no_se_resta_sin_aplicar_y_no_mezclar_monedas():
    docs=[op.estado_documento(documento(verificado=True)),
          op.estado_documento(documento(tipo_documento='NC',total=Decimal(40),verificado=True)),
          op.estado_documento(documento(moneda='ARS',verificado=True,total=Decimal(1000)))]
    r={v['moneda']:v for v in op.resumen_documentos(docs)}
    assert r['USD']['pendiente']==100 and r['USD']['credito']==40 and r['ARS']['pendiente']==1000
    assert docs[1]['estado_pago']=='CREDITO_DISPONIBLE' and docs[1]['dias_mora'] is None


def test_nc_historica_no_se_supone_disponible():
    f=op.estado_documento(documento(tipo_documento='NC'))
    r=op.resumen_documentos([f])[0]
    assert f['estado_pago']=='CREDITO_SIN_VERIFICAR' and r['credito']==0 and r['sin_verificar']==1


@pytest.mark.parametrize('v',['NaN','Infinity','-1','0',True,'1.00001','1e80',None])
def test_importes_invalidos(v):
    with pytest.raises(op.OperadorError): op.positivo(v)


def test_schema_contiene_migracion_completa():
    root=Path(__file__).resolve().parents[1]
    assert (root/'sql/operadores_logisticos.sql').read_text().strip() in (root/'sql/schema.sql').read_text()
