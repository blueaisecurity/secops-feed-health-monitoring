# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so the layer can be cached when only
# application code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code + non-sensitive settings.
# feeds.yaml and variables.yaml are intentionally NOT copied — they are
# provided at runtime via env vars / Secret Manager / GCS volume mount.
COPY app/ ./app/
COPY config.yaml ./config.yaml

# Run as non-root.
RUN useradd --create-home --shell /usr/sbin/nologin runner \
    && chown -R runner /app
USER runner

CMD ["python", "-m", "app.main"]
