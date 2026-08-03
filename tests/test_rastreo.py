# ============================================================
# Rastreo público: detección de courier por formato
# ============================================================
import unittest

from servicios.couriers_urls import (
    detectar_courier, normalizar_tracking, nombre_courier, url_tracking,
)


class TestDetectarCourier(unittest.TestCase):
    def test_ups_inequivoco(self):
        self.assertEqual(detectar_courier("1Z999AA10123456784"), "UPS")
        # Con espacios/guiones como los copia la gente:
        self.assertEqual(detectar_courier("1Z 999 AA1 01 2345 6784"), "UPS")

    def test_dhl(self):
        self.assertEqual(detectar_courier("1234567890"), "DHL")      # 10 díg
        self.assertEqual(detectar_courier("JJD0099998877"), "DHL")

    def test_correo_argentino_postal(self):
        self.assertEqual(detectar_courier("CX123456789AR"), "CORREO")

    def test_andreani(self):
        self.assertEqual(detectar_courier("360000123456789"), "ANDREANI")

    def test_fedex_es_el_default_de_digitos(self):
        self.assertEqual(detectar_courier("123456789012"), "FEDEX")     # 12
        self.assertEqual(detectar_courier("123456789012345"), "FEDEX")  # 15

    def test_no_concluyente_devuelve_none(self):
        for basura in ("", "abc", "12345", "hola mundo", None):
            self.assertIsNone(detectar_courier(basura), basura)

    def test_normalizar(self):
        self.assertEqual(normalizar_tracking("  1z-999 aa1  "), "1Z999AA1")

    def test_nombre_y_url(self):
        self.assertEqual(nombre_courier("FEDEX"), "FedEx")
        self.assertEqual(nombre_courier("CORREO"), "Correo Argentino")
        self.assertIn("fedextrack", url_tracking("FEDEX", "771"))
        self.assertIn("ups.com", url_tracking("UPS", "1Z"))


if __name__ == "__main__":
    unittest.main()
