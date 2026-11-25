# syntax=docker/dockerfile:1
# A workspace served over HTTP. See docs/deploy.md. Build from the base folder:
#   docker build -t cerebro .
# Every package comes from PyPI as uv.lock pins it, Factoid too, with no token and no login.

# The build runs on the full image and the image that runs is the slim one.
ARG BUILD_IMAGE=python:3.13
ARG RUNTIME_IMAGE=python:3.13-slim
ARG UV_VERSION=0.11.32

FROM ${BUILD_IMAGE} AS build
ARG UV_VERSION
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra postgres

FROM ${RUNTIME_IMAGE}
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/cerebro cerebro \
    && mkdir /workspace /config \
    && chown cerebro:cerebro /workspace /config
COPY --from=build /app/.venv /app/.venv
# The config folder holds the secrets file and the list of trusted folders, and must stay outside /workspace.
# PYTHONSAFEPATH keeps the working folder, /workspace, off the import path of any python run here, so a file in the
# workspace named like a standard module never runs in place of it.
ENV PATH=/app/.venv/bin:$PATH \
    XDG_CONFIG_HOME=/config \
    CEREBRO_SECRETS_STORE=file \
    PYTHONSAFEPATH=1 \
    PYTHONUNBUFFERED=1
USER cerebro
WORKDIR /workspace
EXPOSE 4747
# Isolated, and from a folder outside the workspace: nothing of /workspace is on its import path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["sh", "-c", "cd / && exec python -I -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:4747/healthz\", timeout=4)'"]
ENTRYPOINT ["cerebro"]
CMD ["serve", "--host", "0.0.0.0", "--port", "4747"]
