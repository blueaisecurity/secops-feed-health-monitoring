# syntax=docker/dockerfile:1.7
#
# Base image. The floating `python:3.12-slim` tag is convenient for
# local builds but a moving target — the next `docker pull` may resolve
# to a different digest with different CVEs. For production builds,
# pin to an explicit digest captured from `docker pull python:3.12-slim`:
#
#   FROM python:3.12-slim@sha256:<digest-from-your-pull>
#
# See REFERENCE.md → Hardening → Supply chain for the full rationale
# and the equivalent application-image digest pinning at deploy time.
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
