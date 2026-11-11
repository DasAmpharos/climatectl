# syntax=docker/dockerfile:1
# The climatectl command. `docker run ... climatectl-image logger` is the logger; any other
# command works the same way. Build: docker build --build-arg VERSION=0.2.0 -t climatectl .

# Build stage: install the locked, production-only dependencies and climatectl into a venv.
FROM python:3.14-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# The version comes from the git tag, which the build context doesn't have.
ARG VERSION=0.0.0+docker
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
WORKDIR /app
# Dependencies first, so rebuilding after a code change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project --no-editable
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-editable

# Runtime stage: same base image (so the venv's Python path matches), nothing else copied over.
FROM python:3.14-slim-trixie
COPY --from=build /app/.venv /app/.venv
# /data owned by the app user, so a new named volume mounted there starts out writable.
RUN mkdir /data && chown 10001:10001 /data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 CLIMATECTL_DB=/data/climatectl.db
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["climatectl"]
CMD ["logger"]
