# Build context: repository root. Base version intentionally matches production.
FROM ghcr.io/ibm/mcp-context-forge:v1.0.10
USER 0
COPY deploy/contextforge-timeout/patch_read_timeout.py /tmp/patch_read_timeout.py
RUN python /tmp/patch_read_timeout.py /app/mcpgateway/config.py \
    && rm /tmp/patch_read_timeout.py
# Restore the upstream v1.0.10 non-root user. Preserve its entrypoint and command.
USER 10001:10001
# Validate through the production wrapper using ephemeral test secrets in RAM.
# No production secrets are read, and no generated credentials are persisted.
RUN HTTPX_READ_TIMEOUT=1500 python -c \
    "import os,secrets; os.environ.update({k:secrets.token_urlsafe(64) for k in ('JWT_SECRET_KEY','AUTH_ENCRYPTION_SECRET','BASIC_AUTH_PASSWORD','PLATFORM_ADMIN_PASSWORD','DEFAULT_USER_PASSWORD')}); from mcpgateway.config import settings; assert settings.httpx_read_timeout == 1500"
