import os
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WEB_HTML = ROOT / "web" / "Tauro Solutions.html"
DOMAIN_TAG = (
    '<meta name="facebook-domain-verification" '
    'content="bz5798tfqs6ts9cxf8g545bs6kikkk" />'
)


class TestMetaAdsFoundation(unittest.TestCase):
    def test_verificacion_de_dominio_esta_en_head(self):
        html = WEB_HTML.read_text(encoding="utf-8")
        head = html.split("<head>", 1)[1].split("</head>", 1)[0]
        self.assertIn(DOMAIN_TAG, head)
        self.assertEqual(html.count(DOMAIN_TAG), 1)

    def test_pixel_esta_apagado_por_default(self):
        from servicios.meta_ads import inyectar_meta_pixel, obtener_meta_pixel_id

        html = WEB_HTML.read_text(encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("META_PIXEL_ID", None)
            self.assertIsNone(obtener_meta_pixel_id())
            render = inyectar_meta_pixel(html, obtener_meta_pixel_id())

        self.assertNotIn('/meta-pixel.js', render)
        self.assertNotIn("connect.facebook.net", render)

    def test_id_invalido_no_habilita_pixel(self):
        from servicios.meta_ads import obtener_meta_pixel_id

        for valor in ("abc", "123<script>", "１２３４５", "1234", "1" * 33):
            with self.subTest(valor=valor), mock.patch.dict(
                os.environ, {"META_PIXEL_ID": valor}
            ):
                self.assertIsNone(obtener_meta_pixel_id())

    def test_id_valido_habilita_solo_la_home(self):
        from servicios.meta_ads import meta_pixel_habilitado, obtener_meta_pixel_id

        with mock.patch.dict(os.environ, {"META_PIXEL_ID": "123456789012345"}):
            self.assertEqual(obtener_meta_pixel_id(), "123456789012345")
            self.assertTrue(meta_pixel_habilitado("/web"))
            for path in ("/", "/portal/home", "/admin", "/shopify/app"):
                self.assertFalse(meta_pixel_habilitado(path), path)

    def test_csp_sin_pixel_no_abre_meta(self):
        from servicios.meta_ads import construir_content_security_policy

        csp = construir_content_security_policy("nonce-test", pixel_habilitado=False)
        self.assertNotIn("facebook", csp)
        self.assertIn("script-src 'self' 'nonce-nonce-test'", csp)
        self.assertIn("connect-src 'self'", csp)

    def test_csp_con_pixel_abre_solo_origenes_necesarios(self):
        from servicios.meta_ads import construir_content_security_policy

        csp = construir_content_security_policy("nonce-test", pixel_habilitado=True)
        self.assertIn("script-src 'self' 'nonce-nonce-test' https://connect.facebook.net", csp)
        self.assertIn("https://cdn.shopify.com", csp)
        self.assertIn("https://*.shopifycdn.com", csp)
        self.assertIn("https://www.facebook.com", csp)
        self.assertIn(
            "connect-src 'self' https://connect.facebook.net https://www.facebook.com",
            csp,
        )
        self.assertNotIn("*.facebook.com", csp)
        self.assertNotIn("unsafe-eval", csp)

    def test_loader_solo_emite_pageview_sin_pii(self):
        from servicios.meta_ads import javascript_meta_pixel

        script = javascript_meta_pixel("123456789012345")
        self.assertIn('fbq("set", "autoConfig", false, pixelId)', script)
        self.assertIn('fbq("init", pixelId)', script)
        self.assertEqual(script.count('fbq("track", "PageView")'), 1)
        for dato_sensible in (
            "email",
            "phone",
            "telefono",
            "advanced_matching",
            "Purchase",
            "Lead",
            "InitiateCheckout",
        ):
            self.assertNotIn(dato_sensible, script)


if __name__ == "__main__":
    unittest.main()
