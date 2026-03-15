# EMHASS HVAC Companion App — Dockerfile
# Python 3.12 slim · ~380 MB imagen final
FROM python:3.12-slim

# Metadatos
LABEL org.opencontainers.image.title="EMHASS HVAC Companion App"
LABEL org.opencontainers.image.version="0.4.0"
LABEL org.opencontainers.image.description="RC gray-box + ML thermal model server"

# Variables de entorno
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    LOG_LEVEL=INFO \
    PORT=8765

WORKDIR /app

# Instalar dependencias del sistema (scipy las necesita)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libatlas-base-dev \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python primero (capa cacheada)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY . .

# Crear directorio de datos persistentes
RUN mkdir -p /app/data

# Usuario no privilegiado
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

CMD ["python", "-m", "uvicorn", "api_server:app", \
     "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
