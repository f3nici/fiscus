# Fiscus: one container, one process.
#
# Two stages so the image carries no compiler. Dependencies install from the
# hashed lockfile, so a build can only ever get the exact packages that were
# tested. Runs as an unprivileged user; /data is the only writable place.

FROM python:3.12-slim-bookworm AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /venv \
    && /venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.lock

FROM python:3.12-slim-bookworm
ARG BUILD_HASH=local
ENV PATH=/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    FISCUS_DATA_DIR=/data \
    FISCUS_TAX_DIR=/app/config/tax \
    FISCUS_BUILD_HASH=$BUILD_HASH
RUN groupadd --system --gid 10001 fiscus \
    && useradd --system --uid 10001 --gid fiscus --home-dir /app --shell /usr/sbin/nologin fiscus \
    && mkdir -p /data \
    && chown fiscus:fiscus /data
COPY --from=build /venv /venv
WORKDIR /app
COPY fiscus ./fiscus
# Tax tables sit outside the package so a newer year can be mounted over
# /app/config/tax without rebuilding.
COPY config ./config
COPY VERSION ./VERSION
USER fiscus
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; u='http://127.0.0.1:%s/healthz' % os.environ.get('FISCUS_PORT', '8000'); sys.exit(0 if urllib.request.urlopen(u, timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "fiscus"]
