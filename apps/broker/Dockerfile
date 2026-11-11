# The broker has no third-party dependency: the image is the stdlib and the
# package under ./broker. Replace the base tag with `python:3.13-slim@sha256:...`
# when the broker image is published (docs/manual-steps.md, broker image step).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY broker /app/broker

# The pod already runs as 65532; the image does not depend on it.
USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["python", "-m", "broker"]
