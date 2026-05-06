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

# Render setea $PORT dinámicamente
EXPOSE 8000

# El bind a 0.0.0.0 es necesario para que Render lo expose afuera del container
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
