"""El backup descargable nunca debe transportar credenciales vivas."""

from servicios.backup import COLUMNAS_SENSIBLES


def test_backup_excluye_par_completo_de_tokens_shopify():
    sensibles = COLUMNAS_SENSIBLES["shopify_instalaciones"]

    assert {"access_token", "refresh_token"} <= sensibles
