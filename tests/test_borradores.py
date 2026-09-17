from uuid import uuid4
from urllib.parse import parse_qs, urlsplit
import pytest
from servicios.borradores import confirmar_borrador


def test_confirmacion_conserva_destino_y_error_y_solo_limpia_token_propio():
    token=str(uuid4())
    url=confirmar_borrador('/portal/envios/40?error=Emisi%C3%B3n+pendiente#detalle',token)
    parts=urlsplit(url)
    assert parts.path=='/portal/envios/40'
    assert parts.fragment=='detalle'
    assert parse_qs(parts.query)=={'error':['Emisión pendiente'],'borrador_listo':[token]}


@pytest.mark.parametrize('token',[None,'','<script>alert(1)</script>','otro-cliente',object()])
def test_token_no_valido_no_cambia_redireccion(token):
    assert confirmar_borrador('/portal/envios?ok=solicitado',token)=='/portal/envios?ok=solicitado'
