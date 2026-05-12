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

# Application code only. config.yaml, feeds.yaml and variables.yaml are
# intentionally NOT copied — they are provided at runtime:
#   - config.yaml + feeds.yaml: mounted from a GCS bucket at /etc/feed-health/
#     (point CONFIG_PATH / FEEDS_PATH env vars at the mount path)
#   - variables.yaml values: --set-env-vars + --set-secrets (Secret Manager)
# See REFERENCE.md → Setup — Cloud Run (production).
COPY app/ ./app/

# Run as non-root.
RUN useradd --create-home --shell /usr/sbin/nologin runner \
    && chown -R runner /app
USER runner

CMD ["python", "-m", "app.main"]
