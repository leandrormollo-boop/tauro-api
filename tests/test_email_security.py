from __future__ import annotations

import unittest

from core.email_sender import (
    _filename,
    _header,
    _html,
    _mailbox,
    _safe_link,
    generar_pdf_pedido,
)


class EmailAndPdfSecurityTests(unittest.TestCase):
    def test_html_and_header_values_are_sanitized(self):
        self.assertEqual(
            _html('<img src="file:///etc/passwd">'),
            "&lt;img src=&quot;file:///etc/passwd&quot;&gt;",
        )
        self.assertEqual(_header("ok\r\nBcc: attacker@example.com"), "ok Bcc: attacker@example.com")

    def test_mailbox_and_links_reject_header_or_scheme_injection(self):
        self.assertIsNone(_mailbox("victim@example.com\r\nBcc: attacker@example.com"))
        self.assertEqual(_mailbox("victim@example.com"), "victim@example.com")
        self.assertEqual(
            _safe_link(
                "javascript:alert(1)",
                "https://taurosolutions.ar/portal/login",
            ),
            "https://taurosolutions.ar/portal/login",
        )

    def test_attachment_filename_is_restricted(self):
        self.assertEqual(
            _filename("../../guia\r\nmaliciosa.pdf"),
            "guia_maliciosa.pdf",
        )

    def test_pdf_treats_customer_html_as_text(self):
        pdf = generar_pdf_pedido(
            {
                "referencia": '<img src="file:///etc/passwd">',
                "dest_nombre": '<a href="https://attacker.example">X</a>',
            }
        )
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertLess(len(pdf), 1_000_000)


if __name__ == "__main__":
    unittest.main()
