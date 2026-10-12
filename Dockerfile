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

COPY requirements.docker.txt /build/requirements.txt

# Create one isolated Python environment and install/check all
# packages inside that same environment.
RUN /usr/local/bin/python -m venv "${VIRTUAL_ENV}" \
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
    GUNICORN_WORKERS=3 \
    GUNICORN_TIMEOUT=120

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
    && groupadd --system appgroup \
    && useradd \
        --system \
        --gid appgroup \
        --home-dir /app \
        --shell /usr/sbin/nologin \
        appuser

# Copy the complete virtual environment from the builder.
COPY --from=builder /opt/venv /opt/venv

# Copy application source.
COPY --chown=appuser:appgroup . /app/

RUN mkdir -p \
        /app/logs \
        /app/staticfiles \
        /app/media \
    && chown -R appuser:appgroup \
        /app/logs \
        /app/staticfiles \
        /app/media

# Fail the image build early if core imports are unavailable.
RUN python -c "\
import django; \
import gunicorn; \
from zoneinfo import ZoneInfo; \
print('Django:', django.get_version()); \
print('Gunicorn import: OK'); \
print('ZoneInfo import: OK')"

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn ${WSGI_MODULE}:application --bind 0.0.0.0:${PORT:-8000} --workers ${GUNICORN_WORKERS:-3} --timeout ${GUNICORN_TIMEOUT:-120} --access-logfile - --error-logfile -"]
