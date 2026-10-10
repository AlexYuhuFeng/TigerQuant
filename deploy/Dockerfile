# TigerQuant application image.
#
# Built from the same pyproject.toml and uv.lock used everywhere else, so the container
# runtime and the developer runtime resolve to identical versions (ADR-0001, ADR-0013 §2).
#
#   docker build -f deploy/Dockerfile -t tigerquant:local .
#   docker run --rm --network host tigerquant:local doctor
#
# The image runs as a non-root user, contains no credentials, and exposes no live trading
# capability: the CLI has no command that can enable live mode (see src/tigerquant/cli.py).

# ---- build stage -----------------------------------------------------------
FROM python:3.14-slim-bookworm AS build

# uv is copied from its official image rather than pip-installed, so the tool version is
# pinned by the image tag.
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Dependency layers first: they change far less often than the source, so a code edit does
# not re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --extra db

COPY src ./src
COPY schemas ./schemas
COPY config ./config
RUN uv sync --frozen --extra db

# ---- runtime stage ---------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

# tini reaps zombies and forwards signals, so `docker stop` reaches the application instead
# of the shell that started it. A trading process that ignores SIGTERM would leave orders
# unreconciled, which is precisely what ADR-0008 §4 exists to prevent.
RUN apt-get update \
    && apt-get install --no-install-recommends -y tini \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user. The process never needs to write outside its own working directory.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin tigerquant

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Deterministic replay artefacts, regardless of the host platform.
    PYTHONHASHSEED=0 \
    TIGERQUANT_HOME=/var/lib/tigerquant

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/src /app/src
COPY --from=build /app/schemas /app/schemas
COPY --from=build /app/config /app/config

RUN mkdir -p /var/lib/tigerquant /var/lib/tigerquant/journals \
    && chown -R tigerquant:tigerquant /var/lib/tigerquant /app

USER tigerquant
VOLUME ["/var/lib/tigerquant"]

# Diagnostics only. This is not a health endpoint that implies trading readiness, and it
# deliberately does not touch a broker.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-m", "tigerquant", "doctor"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "tigerquant", "doctor"]
