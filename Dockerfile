# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.11

# ============================================================
# Builder stage
# ============================================================

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

# Packages required to compile Python dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        pkg-config \
        libpq-dev \
        unixodbc-dev \
        libjpeg62-turbo-dev \
        zlib1g-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.production.lock.txt /build/requirements.txt

# Create one isolated Python environment and install/check all
# packages inside that same environment.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi \
    && /usr/local/bin/python -m venv "${VIRTUAL_ENV}" \
    && "${VIRTUAL_ENV}/bin/python" -m pip install --upgrade \
        pip \
        setuptools \
        wheel \
        packaging \
    && "${VIRTUAL_ENV}/bin/python" -m pip install \
        -r /build/requirements.txt \
    && "${VIRTUAL_ENV}/bin/python" -m pip check \
    && "${VIRTUAL_ENV}/bin/python" -m pip list

# ============================================================
# Runtime stage
# ============================================================

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PORT=8000 \
    GUNICORN_WORKERS=1 \
    GUNICORN_TIMEOUT=1860 \
    DJANGO_SETTINGS_MODULE=buro_first.settings_production

ARG WSGI_MODULE=buro_first.wsgi
ENV WSGI_MODULE=${WSGI_MODULE}

WORKDIR /app

# Runtime libraries only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libpq5 \
        unixodbc \
        libjpeg62-turbo \
        zlib1g \
        libffi8 \
        libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 appgroup \
    && useradd \
        --system \
        --uid 10001 \
        --gid appgroup \
        --home-dir /app \
        --shell /usr/sbin/nologin \
        appuser

# Copy the complete virtual environment from the builder.
COPY --from=builder /opt/venv /opt/venv

# Copy application source.
COPY --chown=appuser:appgroup . /app/

RUN mkdir -p \
        /app/staticfiles \
        /app/media \
        /app/logs \
    && chown -R appuser:appgroup \
        /app/staticfiles \
        /app/media \
        /app/logs

# Fail the image build early if core imports are unavailable.
RUN python -c "\
import django; \
import gunicorn; \
from zoneinfo import ZoneInfo; \
print('Django:', django.get_version()); \
print('Gunicorn import: OK'); \
print('ZoneInfo import: OK')"

USER 10001:10001

EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn ${WSGI_MODULE}:application --bind 0.0.0.0:${PORT:-8000} --workers 1 --worker-class gthread --threads 4 --timeout 1860 --access-logfile - --error-logfile -"]
