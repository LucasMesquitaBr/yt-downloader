FROM python:3.11-slim

# Instala ffmpeg e dependencias de midia (incluindo nodejs para desafio JS do yt-dlp)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia os arquivos da aplicacao
COPY app.py index.html manifest.json sw.js ./
COPY static/ ./static/

EXPOSE 5000

ENV PORT=5000
ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 300 app:app"]
