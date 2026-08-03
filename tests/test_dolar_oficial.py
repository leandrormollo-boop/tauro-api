# ============================================================
# Dólar oficial automático — el que mueve todos los precios
# ============================================================
import unittest
from unittest import mock


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
    def json(self):
        return self._data


class TestConsultar(unittest.TestCase):
    def test_toma_la_venta(self):
        from servicios import dolar_oficial as d
        with mock.patch.object(d, "TIPO", "venta"), \
             mock.patch("requests.get", return_value=_Resp({"compra": 1465, "venta": 1515})):
            self.assertEqual(d.consultar_dolar_oficial(), 1515.0)

    def test_valor_fuera_de_rango_se_ignora(self):
        from servicios import dolar_oficial as d
        # API devuelve basura (15 en vez de 1515): no se acepta.
        with mock.patch("requests.get", return_value=_Resp({"venta": 15})):
            self.assertIsNone(d.consultar_dolar_oficial())
        with mock.patch("requests.get", return_value=_Resp({"venta": 999999})):
            self.assertIsNone(d.consultar_dolar_oficial())

    def test_error_de_red_devuelve_none(self):
        from servicios import dolar_oficial as d
        with mock.patch("requests.get", side_effect=Exception("timeout")):
            self.assertIsNone(d.consultar_dolar_oficial())

    def test_status_no_200(self):
        from servicios import dolar_oficial as d
        with mock.patch("requests.get", return_value=_Resp({}, status=503)):
            self.assertIsNone(d.consultar_dolar_oficial())


class TestActualizar(unittest.TestCase):
    def test_apagado_no_hace_nada(self):
        from servicios import dolar_oficial as d
        with mock.patch.object(d, "auto_activo", return_value=False):
            r = d.actualizar_dolar_auto()
        self.assertFalse(r["ok"])
        self.assertEqual(r["motivo"], "dolar_auto_apagado")

    def test_salto_excesivo_no_pisa_y_avisa(self):
        from servicios import dolar_oficial as d
        # actual 1500, la fuente devuelve 5000 (>50% de salto): NO se actualiza
        # y dispara la alerta por mail.
        with mock.patch.object(d, "auto_activo", return_value=True), \
             mock.patch.object(d, "consultar_dolar_oficial", return_value=5000.0), \
             mock.patch.object(d, "_valor_actual", return_value=1500.0), \
             mock.patch.object(d, "_avisar_salto") as avisar:
            r = d.actualizar_dolar_auto()
        self.assertFalse(r["ok"])
        self.assertEqual(r["motivo"], "salto_excesivo")
        avisar.assert_called_once()

    def test_sin_valor_previo_usa_fallback_como_referencia(self):
        from servicios import dolar_oficial as d
        # DB sin fila (actual None): la guarda de salto NO se saltea, usa el
        # fallback env como referencia. 15150 vs ~1450 = salto enorme → rechazo.
        with mock.patch.object(d, "auto_activo", return_value=True), \
             mock.patch.object(d, "consultar_dolar_oficial", return_value=15150.0), \
             mock.patch.object(d, "_valor_actual", return_value=None), \
             mock.patch.dict("os.environ", {"COTIZACION_DOLAR_ARS": "1450"}), \
             mock.patch.object(d, "_avisar_salto"):
            r = d.actualizar_dolar_auto()
        self.assertFalse(r["ok"])
        self.assertEqual(r["motivo"], "salto_excesivo")

    def test_sin_dato_valido_no_pisa(self):
        from servicios import dolar_oficial as d
        with mock.patch.object(d, "auto_activo", return_value=True), \
             mock.patch.object(d, "consultar_dolar_oficial", return_value=None):
            r = d.actualizar_dolar_auto()
        self.assertFalse(r["ok"])
        self.assertEqual(r["motivo"], "sin_dato_valido")

    def test_actualiza_cuando_es_sano(self):
        from servicios import dolar_oficial as d
        escrito = {}
        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                if params and "COTIZACION_DOLAR_ARS" in str(sql):
                    escrito["valor"] = params[0]
            def fetchone(self): return None
        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cur()
        with mock.patch.object(d, "auto_activo", return_value=True), \
             mock.patch.object(d, "consultar_dolar_oficial", return_value=1560.0), \
             mock.patch.object(d, "_valor_actual", return_value=1500.0), \
             mock.patch.object(d, "get_conn", return_value=_Conn()):
            r = d.actualizar_dolar_auto()
        self.assertTrue(r["ok"])
        self.assertEqual(escrito.get("valor"), "1560")


if __name__ == "__main__":
    unittest.main()
