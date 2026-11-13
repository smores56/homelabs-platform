# The in-cluster agent image: nanobot's virtualenv plus the shell its skills
# need. It carries no config — the pod mounts `nanobot-policy` (prompts, models)
# and the tenant ConfigMap (skills, cron), and the entrypoint renders
# $HOME/.nanobot from them on every start.
#
# The base is digest-pinned and the two packages are exact pins; bump them
# deliberately (docs/manual-steps.md, nanobot image step).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    UV_NO_CACHE=1 \
    PATH=/opt/nanobot/bin:$PATH

# Barebones on purpose: bash, coreutils, grep, sed, curl, git, tar, gzip and the
# CA bundle. No jq/yq/rg/node/gh/sdlc — a skill that needs one of those fails at
# start (metadata.nanobot.requires.bins) instead of drifting with the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      coreutils \
      curl \
      git \
      grep \
      gzip \
      sed \
      tar \
    && rm -rf /var/lib/apt/lists/*

# uv resolves the runtime into a virtualenv, like the nix image did; nothing is
# installed into the system interpreter, so PEP 668 and distro packages cannot
# interfere. nanobot-ai's wheel already ships the built WebUI, so the frontend is
# never built here.
RUN pip install --no-cache-dir 'uv==0.13.8' \
    && uv venv --python 3.12 /opt/nanobot \
    && uv pip install --python /opt/nanobot/bin/python --no-cache 'nanobot-ai==0.4.1'

COPY render.py /usr/local/lib/nanobot/render.py
COPY entrypoint.sh /usr/local/bin/nanobot-entrypoint
RUN chmod 0755 /usr/local/bin/nanobot-entrypoint

# The pod already runs as 65532 and the rootfs is read-only; nothing here needs
# a writable layer.
USER 65532:65532

WORKDIR /

ENTRYPOINT ["/usr/local/bin/nanobot-entrypoint"]
