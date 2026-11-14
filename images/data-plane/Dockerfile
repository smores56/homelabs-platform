# Data-plane service image: stdlib only, no package manager, no secret.
# The base tag is pinned; bump it deliberately, never to `latest`.
FROM python:3.13.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/libs:/app

WORKDIR /app
COPY libs/ /app/libs/
COPY apps/data_plane/ /app/apps/data_plane/

# Numeric uid keeps the image usable on a read-only root filesystem with no
# passwd entry; the pod may still impose its own runAsUser.
USER 1000:1000
EXPOSE 8080

ENTRYPOINT ["python", "-m", "apps.data_plane.run"]
