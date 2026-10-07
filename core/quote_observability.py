"""Métricas de cotización visibles sin activar logs HTTP ni datos privados."""

import logging


quote_logger = logging.getLogger("tauro.quotes")
if not quote_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[quotes] %(message)s"))
    quote_logger.addHandler(handler)
quote_logger.setLevel(logging.INFO)
quote_logger.propagate = False
