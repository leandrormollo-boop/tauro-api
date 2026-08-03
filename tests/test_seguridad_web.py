# ============================================================
# Seguridad de la web: TOTP, api_key hasheada, hosts permitidos
# ============================================================
import base64
import unittest
from unittest import mock


class TestTOTP(unittest.TestCase):
    """Contra los vectores oficiales del RFC 6238 (SHA1, 6 dígitos)."""

    SECRETO = base64.b32encode(b"12345678901234567890").decode()

    def setUp(self):
        from servicios import totp
        totp._ultimo_paso = None

    def test_vectores_rfc_6238(self):
        from servicios.totp import _codigo
        # (momento unix, últimos 6 dígitos del vector de 8 del RFC)
        for momento, esperado in [(59, "287082"), (1111111109, "081804"),
                                  (1234567890, "005924"), (2000000000, "279037")]:
            self.assertEqual(_codigo(self.SECRETO, momento), esperado)

    def test_ventana_tolera_reloj_corrido(self):
        from servicios import totp
        codigo_previo = totp._codigo(self.SECRETO, 59)      # paso anterior
        with mock.patch("time.time", return_value=89):       # 30s después
            self.assertTrue(totp.verificar_codigo(self.SECRETO, codigo_previo))

    def test_replay_mismo_codigo_rechazado(self):
        from servicios import totp
        codigo = totp._codigo(self.SECRETO, 59)
        with mock.patch("time.time", return_value=59):
            self.assertTrue(totp.verificar_codigo(self.SECRETO, codigo))
            self.assertFalse(totp.verificar_codigo(self.SECRETO, codigo),
                             "el mismo código entró dos veces")

    def test_replay_intercalado_rechazado(self):
        # El caso que rompía el anti-replay por string: consumir C_{N+1} y
        # después reenviar C_N (aún dentro de su ventana). Con anti-replay por
        # PASO, C_N cae en un paso <= el ya consumido → rechazado.
        from servicios import totp
        paso = 1234567890 // totp.PASO_SEGUNDOS
        c_n = totp._codigo(self.SECRETO, paso * totp.PASO_SEGUNDOS)
        c_n1 = totp._codigo(self.SECRETO, (paso + 1) * totp.PASO_SEGUNDOS)
        # Admin se loguea en el paso N+1 con C_{N+1}
        with mock.patch("time.time", return_value=(paso + 1) * totp.PASO_SEGUNDOS):
            self.assertTrue(totp.verificar_codigo(self.SECRETO, c_n1))
            # Atacante reenvía C_N (válido como paso previo por la ventana)
            self.assertFalse(totp.verificar_codigo(self.SECRETO, c_n),
                             "un código ya consumido volvió a entrar intercalado")

    def test_paso_valido_no_muta_estado(self):
        # paso_valido() es pura: sirve para verificar sin quemar el código.
        from servicios import totp
        with mock.patch("time.time", return_value=59):
            p1 = totp.paso_valido(self.SECRETO, totp._codigo(self.SECRETO, 59))
            p2 = totp.paso_valido(self.SECRETO, totp._codigo(self.SECRETO, 59))
        self.assertIsNotNone(p1)
        self.assertEqual(p1, p2)
        self.assertIsNone(totp._ultimo_paso, "paso_valido no debe consumir")

    def test_secreto_roto_no_abre_la_puerta(self):
        from servicios.totp import verificar_codigo
        self.assertFalse(verificar_codigo("esto no es base32 !!!", "123456"))

    def test_basura_no_pasa(self):
        from servicios.totp import verificar_codigo
        for basura in ("", "12345", "1234567", "abcdef", None):
            self.assertFalse(verificar_codigo(self.SECRETO, basura))


class TestApiKeyHash(unittest.TestCase):
    def test_hash_deterministico_y_no_reversible(self):
        from servicios.api_b2b import hash_api_key
        h = hash_api_key("tauro_clave-de-prueba")
        self.assertEqual(h, hash_api_key("tauro_clave-de-prueba"))
        self.assertEqual(len(h), 64)                 # sha256 hex
        self.assertNotIn("clave-de-prueba", h)

    def test_clave_generada_tiene_entropia(self):
        # El formato importa: sha256 sin salt sólo es seguro si la clave es
        # impredecible. token_urlsafe(32) ≈ 43 chars de 256 bits.
        import secrets
        clave = f"tauro_{secrets.token_urlsafe(32)}"
        self.assertGreaterEqual(len(clave), 40)
        self.assertTrue(clave.startswith("tauro_"))


class TestHostsPermitidos(unittest.TestCase):
    def test_dominios_tauro_pasan(self):
        from main import _host_permitido
        for h in ("taurosolutions.ar", "www.taurosolutions.ar",
                  "taurosolutions.ar:443", "localhost:8123"):
            self.assertTrue(_host_permitido(h), h)

    def test_hosts_ajenos_rebotan(self):
        from main import _host_permitido
        # `algo.up.railway.app` YA NO pasa: es namespace compartido.
        for h in ("evil.com", "taurosolutions.ar.evil.com",
                  "uprailway.app", "railway.app.evil.io",
                  "atacante.up.railway.app", ""):
            self.assertFalse(_host_permitido(h), h)


class TestClientIP(unittest.TestCase):
    """El rate limit no debe ser evadible spoofeando X-Forwarded-For."""

    def _req(self, headers):
        return type("R", (), {
            "headers": {k.lower(): v for k, v in headers.items()},
            "client": type("C", (), {"host": "10.0.0.1"})(),
        })()

    def test_cf_connecting_ip_manda(self):
        from servicios.rate_limit import client_ip
        r = self._req({"CF-Connecting-IP": "203.0.113.9",
                       "X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
        self.assertEqual(client_ip(r), "203.0.113.9")

    def test_xff_toma_el_de_la_derecha_no_el_spoofeable(self):
        from servicios.rate_limit import client_ip
        # El atacante pone "1.2.3.4" a la izquierda; el proxy real appendea
        # la IP verdadera a la derecha. Debemos tomar la de la derecha.
        r = self._req({"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})
        self.assertEqual(client_ip(r), "198.51.100.7")


if __name__ == "__main__":
    unittest.main()
