# The broker's runtime is the stdlib plus `cryptography` (RS256 signing for
# GitHub App JWTs, imported lazily so the unit suite still runs on the stdlib).
# Replace the base tag with `python:3.13-slim@sha256:...` when the broker image
# is published (docs/manual-steps.md, broker image step).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY broker /app/broker

# Installed before dropping privileges: pip writes to /usr/local, which the
# unprivileged runtime uid cannot.
RUN pip install --no-cache-dir 'cryptography==44.0.0'

# The pod already runs as 65532; the image does not depend on it.
USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["python", "-m", "broker"]
