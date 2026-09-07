# Archivos de asociación dominio ↔ app para las tiendas (Play Store / App
# Store). Sin variables configuradas responden vacío y válido; con ellas,
# declaran la app. Se prueban las funciones directo (sin levantar la app).

import json
import os
import unittest
from unittest import mock

import main


def _json(resp):
    return json.loads(resp.body)


class TestAssetlinksAndroid(unittest.TestCase):
    def test_sin_huella_devuelve_lista_vacia(self):
        with mock.patch.dict(os.environ, {"ANDROID_ASSETLINKS_SHA256": ""}, clear=False):
            resp = main.android_assetlinks()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_json(resp), [])

    def test_con_huellas_declara_la_app(self):
        env = {
            "ANDROID_PACKAGE_NAME": "ar.taurosolutions.portal",
            "ANDROID_ASSETLINKS_SHA256": "AA:BB, CC:DD ",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            data = _json(main.android_assetlinks())
        self.assertEqual(len(data), 1)
        target = data[0]["target"]
        self.assertEqual(target["package_name"], "ar.taurosolutions.portal")
        self.assertEqual(target["sha256_cert_fingerprints"], ["AA:BB", "CC:DD"])
        self.assertIn("delegate_permission/common.handle_all_urls", data[0]["relation"])


class TestAppleAppSiteAssociation(unittest.TestCase):
    def test_sin_team_devuelve_vacio_valido(self):
        with mock.patch.dict(os.environ, {"APPLE_TEAM_ID": ""}, clear=False):
            data = _json(main.apple_app_site_association())
        self.assertEqual(data["applinks"]["details"], [])

    def test_con_team_declara_la_app(self):
        env = {"APPLE_TEAM_ID": "TEAM123", "IOS_BUNDLE_ID": "ar.taurosolutions.portal"}
        with mock.patch.dict(os.environ, env, clear=False):
            data = _json(main.apple_app_site_association())
        self.assertEqual(data["applinks"]["details"][0]["appID"],
                         "TEAM123.ar.taurosolutions.portal")


if __name__ == "__main__":
    unittest.main()
