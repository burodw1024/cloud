# Preserve the observed existing metadata/application version.
ARG SUPERSET_BASE_IMAGE=apache/superset:4.1.2
FROM ${SUPERSET_BASE_IMAGE}
USER root
RUN python -m pip install --no-cache-dir psycopg2-binary==2.9.10 \
    && python -c "import psycopg2; from importlib.metadata import version; assert version('apache-superset') == '4.1.2'; print('Superset 4.1.2 PostgreSQL driver verified')"
USER superset
