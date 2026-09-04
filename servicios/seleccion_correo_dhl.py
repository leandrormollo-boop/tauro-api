"""Preselección local de adjuntos DHL; no conecta Gmail ni importa facturas.

Un candidato NO es un remitente autenticado ni un documento aprobado. Antes
de automatizar cargas faltan validar autenticidad, PDF y revisión financiera.
Los parámetros esperados provienen de configuración, nunca del correo/modelo.
"""
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Mapping, Any


class CorreoDHLInvalido(ValueError):
    pass


@dataclass(frozen=True)
class AdjuntoCandidatoDHL:
    mensaje_id: str
    parte_id: str
    adjunto_id: str | None
    archivo_nombre: str
    numero_documento: str
    requiere_revision: bool = True


def seleccionar_adjunto_dhl(
    mensaje: Mapping[str, Any], *, cuenta_autenticada: str,
    cuenta_esperada: str, cuit_esperado: str,
) -> AdjuntoCandidatoDHL | None:
    """Recibe ``users.messages.get(format=full)``; no realiza I/O.

    Devuelve None para mensajes/adjuntos fuera del patrón inicial verificado.
    Rechaza ambigüedad en lugar de elegir una factura arbitrariamente.
    ``cuenta_autenticada`` debe obtenerse del perfil del conector real.
    No inspecciona cuerpo, enlaces de pago, HTML ni mensajes reenviados anidados.
    """
    if (not isinstance(cuenta_esperada, str) or not cuenta_esperada.strip()
            or not isinstance(cuenta_autenticada, str)
            or cuenta_autenticada.strip().lower() != cuenta_esperada.strip().lower()):
        raise CorreoDHLInvalido('La cuenta conectada no es el buzón autorizado.')
    if not isinstance(cuit_esperado, str) or not re.fullmatch(r'[0-9]{11}', cuit_esperado):
        raise CorreoDHLInvalido('Falta configurar el CUIT documental esperado.')
    if not isinstance(mensaje, Mapping):
        raise CorreoDHLInvalido('Mensaje inválido.')
    mensaje_id = mensaje.get('id')
    if not isinstance(mensaje_id, str) or not mensaje_id.strip():
        raise CorreoDHLInvalido('Falta el identificador inmutable de Gmail.')
    etiquetas = mensaje.get('labelIds', [])
    if not isinstance(etiquetas, list) or not all(isinstance(v, str) for v in etiquetas):
        raise CorreoDHLInvalido('Etiquetas inválidas.')
    if set(etiquetas) & {'SENT', 'DRAFT', 'SPAM', 'TRASH'}:
        return None
    payload = mensaje.get('payload')
    if not isinstance(payload, Mapping):
        raise CorreoDHLInvalido('Falta el payload completo del mensaje.')
    headers = payload.get('headers', [])
    if not isinstance(headers, list):
        raise CorreoDHLInvalido('Cabeceras inválidas.')
    cabeceras = {}
    for header in headers:
        if not isinstance(header, Mapping) or not isinstance(header.get('name'), str):
            raise CorreoDHLInvalido('Cabecera inválida.')
        nombre = header['name'].lower()
        if nombre not in {'from', 'subject'}:
            continue
        valor = header.get('value')
        if nombre in cabeceras or not isinstance(valor, str) or '\r' in valor or '\n' in valor:
            raise CorreoDHLInvalido('Cabecera de origen/asunto ambigua.')
        cabeceras[nombre] = valor.strip()
    remitente = cabeceras.get('from', '')
    # Filtro, no validación criptográfica: From puede falsificarse.
    if not re.fullmatch(r'(?:[^<>\r\n]*<AR\.E-Billing@dhl\.com>|AR\.E-Billing@dhl\.com)',
                        remitente, flags=re.IGNORECASE):
        return None
    asunto = re.fullmatch(r'DHL Invoice services\s+([0-9]{11})\s*[–-]\s*([0-9]{4}A[0-9]{8})',
                          cabeceras.get('subject', ''))
    if not asunto or asunto[1] != cuit_esperado:
        return None
    numero = asunto[2]
    pendientes = [(payload, 0)]
    vistos = 0
    candidatos = []
    while pendientes:
        parte, profundidad = pendientes.pop()
        vistos += 1
        if profundidad > 8 or vistos > 200 or not isinstance(parte, Mapping):
            raise CorreoDHLInvalido('Estructura MIME inválida o demasiado compleja.')
        mime = parte.get('mimeType', '')
        if not isinstance(mime, str):
            raise CorreoDHLInvalido('Tipo MIME inválido.')
        if mime.lower().startswith('multipart/'):
            hijos = parte.get('parts', [])
            if not isinstance(hijos, list) or len(hijos) > 200:
                raise CorreoDHLInvalido('Partes MIME inválidas.')
            pendientes.extend((hijo, profundidad + 1) for hijo in hijos)
            continue
        if mime.lower() != 'application/pdf':
            continue
        archivo = parte.get('filename', '')
        patron = re.fullmatch(r'DHL-([0-9]{4}A[0-9]{8})_([0-9]{8})\.pdf', archivo) if isinstance(archivo, str) else None
        if not patron or patron[1] != numero:
            continue
        try:
            datetime.strptime(patron[2], '%d%m%Y')
        except ValueError as exc:
            raise CorreoDHLInvalido('Fecha del nombre de archivo inválida.') from exc
        cuerpo = parte.get('body')
        if not isinstance(cuerpo, Mapping):
            raise CorreoDHLInvalido('Falta el cuerpo del adjunto candidato.')
        tamano = cuerpo.get('size')
        if type(tamano) is not int or not 0 < tamano <= 8 * 1024 * 1024:
            raise CorreoDHLInvalido('Tamaño del adjunto inválido o superior a 8 MB.')
        parte_id = parte.get('partId')
        adjunto_id = cuerpo.get('attachmentId')
        datos_inline = cuerpo.get('data')
        if not isinstance(parte_id, str):
            raise CorreoDHLInvalido('Falta el identificador de parte MIME.')
        if adjunto_id is not None and (not isinstance(adjunto_id, str) or not adjunto_id.strip()):
            raise CorreoDHLInvalido('Identificador de adjunto inválido.')
        if adjunto_id is None and (not isinstance(datos_inline, str) or not datos_inline):
            raise CorreoDHLInvalido('El adjunto no tiene contenido ni referencia.')
        candidatos.append(AdjuntoCandidatoDHL(mensaje_id, parte_id, adjunto_id, archivo, numero))
    if len(candidatos) > 1:
        raise CorreoDHLInvalido('Varios adjuntos coinciden; se requiere revisión manual.')
    return candidatos[0] if candidatos else None
