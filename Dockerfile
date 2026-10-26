FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=10000
# Deno solves YouTube's JavaScript challenges. ffmpeg merges DASH audio/video.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip ffmpeg && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/opt/deno sh && \
    ln -s /opt/deno/bin/deno /usr/local/bin/deno && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
