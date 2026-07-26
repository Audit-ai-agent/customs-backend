# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS uv
FROM python:3.11.15-alpine3.24@sha256:25976e9d34a0fab1f278cae931f34c8303d97bf0c0d7f85b6b4dcf641d7702a4 AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY packages ./packages
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --extra production --no-editable \
    && test ! -e /app/.venv/bin/pip \
    && test ! -e /app/.venv/bin/uv \
    && /app/.venv/bin/python -c "import customs_backend, operational_db"

FROM python:3.11.15-alpine3.24@sha256:25976e9d34a0fab1f278cae931f34c8303d97bf0c0d7f85b6b4dcf641d7702a4 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

# The official Python base includes packaging tools for convenience. The
# application runs only from the locked venv copied from the build stage, so
# remove the system installer metadata and ensurepip payload from the final
# image instead of carrying an unused second Python package environment.
RUN rm -rf /usr/local/lib/python3.11/site-packages/* \
        /usr/local/lib/python3.11/ensurepip \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.11 \
        /usr/local/bin/wheel \
    && addgroup -S -g 10001 customs \
    && adduser -S -D -H -h /app -u 10001 -G customs customs

WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
COPY --from=build --chown=10001:10001 /app/migrations /app/migrations
COPY --from=build --chown=10001:10001 /app/packages/operational_db/migrations /app/packages/operational_db/migrations
COPY --from=build --chown=10001:10001 /app/alembic.ini /app/alembic.ini

USER 10001:10001
EXPOSE 8080
CMD ["customs-backend-api"]
