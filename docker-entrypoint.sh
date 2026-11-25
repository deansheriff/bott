#!/bin/sh
set -eu

# Seed the knowledge volume on first start only (never overwrite edited files).
if [ -z "$(ls -A "${KNOWLEDGE_DIR}" 2>/dev/null || true)" ]; then
  cp /app/knowledge-seed/*.md "${KNOWLEDGE_DIR}/" 2>/dev/null || true
fi

# One worker: the webhook rate limiter and background tasks are in-process.
# --proxy-headers + FORWARDED_ALLOW_IPS: trust X-Forwarded-For from the reverse proxy
# (Coolify's Traefik). Only safe because the container port is not published publicly.
exec uvicorn app.main:create_app --factory \
  --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
  --no-access-log --no-server-header \
  --workers 1
