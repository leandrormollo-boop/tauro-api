# Test del import de catálogo Shopify → portal.
# Mapea variantes a filas de catálogo (una por SKU), descarta las sin SKU,
# calcula el peso y arma el data: URI de la miniatura. No toca la base.

import unittest
from unittest import mock

from servicios import shopify_catalogo as sc


class _Resp:
    def __init__(self, products, headers=None, status=200, content=b"", ctype="image/jpeg"):
        self._products = products
        self.headers = headers or {}
        self.status_code = status
        self.content = content
        if content:
            self.headers.setdefault("Content-Type", ctype)

    def json(self):
        return {"products": self._products}


class TestTraerProductos(unittest.TestCase):
    def test_mapea_variantes_y_descarta_sin_sku(self):
        productos = [
            {"title": "Reel Pesca Jacks 200",
             "image": {"src": "https://cdn.shopify.com/reel.jpg"},
             "variants": [
                 {"sku": "REEL-PJ-200", "title": "Default Title", "grams": 1200},
                 {"sku": "", "title": "Sin SKU", "grams": 500},
             ]},
            {"title": "Caña Surf 3.9m",
             "images": [{"src": "https://cdn.shopify.com/cana.jpg"}],
             "variants": [{"sku": "CANA-39", "title": "Negra", "grams": 0}]},
        ]
        with mock.patch.object(sc, "_api", return_value=_Resp(productos)):
            filas = sc.traer_productos("x.myshopify.com", "tok")

        self.assertEqual(len(filas), 2)  # la variante sin SKU se descarta
        por_sku = {f["sku"]: f for f in filas}
        self.assertEqual(por_sku["REEL-PJ-200"]["peso_kg"], 1.2)
        # 'Default Title' no se anexa; otra variante sí
        self.assertEqual(por_sku["REEL-PJ-200"]["nombre"], "Reel Pesca Jacks 200")
        self.assertEqual(por_sku["CANA-39"]["nombre"], "Caña Surf 3.9m — Negra")
        # la segunda cae a images[0] al no tener image principal
        self.assertEqual(por_sku["CANA-39"]["imagen_src"], "https://cdn.shopify.com/cana.jpg")

    def test_api_caido_devuelve_lista_vacia(self):
        with mock.patch.object(sc, "_api", return_value=_Resp([], status=500)):
            self.assertEqual(sc.traer_productos("x.myshopify.com", "tok"), [])


class TestThumb(unittest.TestCase):
    def test_arma_data_uri(self):
        fake = _Resp([], content=b"\xff\xd8\xff\xd9", ctype="image/png")
        with mock.patch.object(sc.requests, "get", return_value=fake):
            uri = sc._thumb_data_uri("https://cdn.shopify.com/reel.jpg")
        self.assertIsNotNone(uri)
        self.assertTrue(uri.startswith("data:image/png;base64,"))

    def test_imagen_gigante_se_omite(self):
        gigante = _Resp([], content=b"x" * (sc._THUMB_MAX_BYTES + 1))
        with mock.patch.object(sc.requests, "get", return_value=gigante):
            self.assertIsNone(sc._thumb_data_uri("https://cdn.shopify.com/reel.jpg"))

    def test_sin_src_devuelve_none(self):
        self.assertIsNone(sc._thumb_data_uri(None))


if __name__ == "__main__":
    unittest.main()
