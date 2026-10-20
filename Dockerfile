FROM python:3.11-slim

# Instala ffmpeg e dependencias de midia
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copia os arquivos da aplicacao
COPY app.py .
COPY index.html .

EXPOSE 5000

ENV PORT=5000
ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 300 app:app"]
