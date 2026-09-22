"""Worker efímero sin DB/red: bytes por stdin, miniatura JPEG por stdout."""
import io
import math
import sys
import warnings


def render(contenido):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 24_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    if contenido.startswith(b"%PDF-"):
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(contenido) as pdf:
            pagina = pdf[0]
            try:
                width, height = pagina.get_size()
                if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                    raise ValueError("Página inválida")
                bitmap = pagina.render(scale=min(160 / width, 208 / height))
                try:
                    img = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                pagina.close()
    else:
        with Image.open(io.BytesIO(contenido)) as original:
            original.thumbnail((208, 208))
            img = ImageOps.exif_transpose(original).convert("RGBA")
            fondo = Image.new("RGBA", img.size, "white")
            fondo.alpha_composite(img)
            img = fondo.convert("RGB")
    img.thumbnail((160, 208))
    salida = io.BytesIO()
    img.save(salida, format="JPEG", quality=75, optimize=True)
    return salida.getvalue()


if __name__ == "__main__":
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    data = sys.stdin.buffer.read(32 * 1024**2 + 1)
    if len(data) > 32 * 1024**2:
        raise ValueError("Documento demasiado grande")
    sys.stdout.buffer.write(render(data))
