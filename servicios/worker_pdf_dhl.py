"""Proceso de lectura con recursos acotados. No recibe credenciales ni rutas PDF.

Se invoca sólo mediante ejecutar_lector_dhl; no es un sandbox de seguridad.
Entrada y salida JSON por pipes, sin PDF temporal, correo ni escritura contable.
"""
import base64
import json
from pathlib import Path
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (10, 12))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from servicios.lector_pdf_dhl import extraer_factura_dhl_pdf
    from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida

    datos = json.loads(sys.stdin.buffer.read(12 * 1024 * 1024 + 1))
    try:
        lectura = extraer_factura_dhl_pdf(
            base64.b64decode(datos['pdf'], validate=True),
            numero_esperado=datos['numero'], cuit_esperado=datos['cuit'],
        )
        salida = {'extraccion': lectura.extraccion,
                  'observaciones': lectura.observaciones}
    except ExtraccionDHLInvalida as exc:
        salida = {'error': str(exc)}
    resultado = json.dumps(salida, default=str, ensure_ascii=True)
    if len(resultado) > 512 * 1024:
        raise ValueError('Salida del lector excesiva')
    sys.stdout.write(resultado)


if __name__ == '__main__':
    main()
