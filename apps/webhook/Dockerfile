# The receiver is the stdlib only: no client dependency, no crypto package
# (HMAC-SHA256 is `hmac`/`hashlib`). The base image is digest-pinned; bump it
# deliberately in the same change as the published digest (docs/manual-steps.md,
# webhook image step).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY webhook /app/webhook

# The pod already runs as 65532; the image does not depend on it.
USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["python", "-m", "webhook"]
