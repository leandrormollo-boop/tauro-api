FROM python:3.11-slim

# Variables del entorno Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /app

# Dependencias del sistema (gcc para reportlab/pillow si hace falta, tzdata para zona)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python (capa cacheable)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Usuario sin privilegios: si algún día un bug permite ejecutar código en la
# app, que corra como `tauro` y no como root del contenedor. El código queda
# de root (sólo lectura para tauro); la ÚNICA carpeta que la app escribe es
# /app/var (checkpoint del tracking de FedEx), así que se crea y se le da
# dueño a tauro. Sin esto, el mkdir de servicios/tracking_fedex_tauro.py
# fallaría al importar y la app no arrancaría (regresión encontrada en la
# auditoría del 03/08). El módulo además tolera que falle, como red.
RUN useradd --create-home --shell /usr/sbin/nologin tauro \
    && mkdir -p /app/var \
    && chown -R tauro:tauro /app/var
USER tauro

# Render setea $PORT dinámicamente
EXPOSE 8000

# El bind a 0.0.0.0 es necesario para que Render lo expose afuera del container
# --no-server-header: no regalar la versión de uvicorn en cada respuesta.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header
