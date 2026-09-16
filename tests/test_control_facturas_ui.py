from datetime import date
from urllib.parse import parse_qs, urlsplit

from servicios.control_facturas_ui import preparar_presentacion, retorno_control
from servicios.periodos_envios import normalizar_periodo


def test_paginacion_de_documentos_conserva_busqueda_y_no_oculta_historico():
    facturas = [{'numero': f'FC-{i:04}', 'id': i} for i in range(135)]
    def presentar(**kw):
        return preparar_presentacion(control={'items': [], 'total': 0},
            facturas=facturas, ajustes=[], ruta='/admin/facturas-internacionales',
            vista='facturas', **kw)
    p = presentar(filtros={'courier': 'DHL', 'buscar': 'FC-'}, pagina=6)
    assert [f['id'] for f in p['filas_visibles']] == list(range(125, 135))
    assert p['ventana'] == dict(pagina=6, paginas=6, desde=126, hasta=135, total=135)
    url = p['enlace'](pagina=5)
    assert parse_qs(urlsplit(url).query) == {'vista':['facturas'], 'courier':['DHL'], 'buscar':['FC-'], 'pagina':['5']}
    p = presentar(filtros={'buscar': 'fc-0134'}, pagina=999)
    assert p['filas_visibles'] == [facturas[134]]
    assert p['ventana']['pagina'] == 1


def test_cambiar_vista_descarta_filtros_que_no_corresponden_y_codifica_cliente():
    p = preparar_presentacion(control={'items': [], 'total': 0}, facturas=[], ajustes=[],
        filtros={'cliente':'CASA & SUR', 'courier':'DHL', 'estado':'BASE_PENDIENTE', 'buscar':'123'},
        ruta='/admin/facturas-internacionales', vista='conciliacion', pagina='no-numero')
    assert parse_qs(urlsplit(p['enlace'](vista='facturas')).query) == {'vista':['facturas'], 'courier':['DHL']}
    assert parse_qs(urlsplit(p['enlace'](vista='diferencias')).query)['cliente'] == ['CASA & SUR']
    assert p['ventana']['total'] == 0


def test_historial_completo_no_ofrece_anio_cero():
    p = normalizar_periodo('', '', '', [(2025,12)], hoy=date(2026,9,8))
    assert p['anios'] == [2026,2025]
    assert p['desde'] is None and p['hasta'] is None


def test_alerta_guias_sin_match_muestra_solo_documentos_afectados_y_conserva_busqueda():
    facturas = [
        {'id': 1, 'numero': 'DHL-01', 'estado': 'PARCIAL', 'guias': 5, 'con_match': 3},
        {'id': 2, 'numero': 'DHL-02', 'estado': 'CONCILIADA', 'guias': 4, 'con_match': 4},
        {'id': 3, 'numero': 'DHL-03', 'estado': 'ABIERTA', 'guias': 2, 'con_match': 0},
    ]
    p = preparar_presentacion(control={'items': [], 'total': 0}, facturas=facturas,
        ajustes=[], filtros={'estado': 'GUIAS_SIN_MATCH', 'buscar': '01'},
        ruta='/admin/facturas-internacionales', vista='facturas', pagina=1)
    assert [f['id'] for f in p['filas_visibles']] == [1]
    assert p['ventana']['total'] == 1
    assert 'estado=GUIAS_SIN_MATCH' in p['enlace'](pagina=2)


def test_volver_al_control_conserva_contexto_sin_aceptar_destinos_externos():
    fallback = '/admin/facturas-internacionales'
    ruta = fallback + '?vista=conciliacion&cliente=CASA+%26+SUR&pagina=3&estado=REQUIERE_ACCION'
    assert retorno_control(ruta, fallback) == ruta
    for invalido in ('https://otra.example', '//otra.example', '//[', 'javascript:alert(1)', '/admin/logout', None):
        assert retorno_control(invalido, fallback) == fallback
    assert retorno_control(fallback + '?error=texto&pagina=2', fallback) == fallback + '?pagina=2'
