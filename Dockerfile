# pfa container image: runs as a non-root user and works with a read-only root filesystem.
#   docker build -t pfa:dev .
#   docker run --rm -v ~/.kube:/tmp/.kube:ro pfa:dev diagnose checkout-api -n pfa-lab --no-llm

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin pfa
COPY --from=build /app/.venv /app/.venv
# HOME=/tmp: the only writable place when the root filesystem is read-only (Helm mounts an
# emptyDir there). Bytecode is compiled at build time, so Python never writes next to the code.
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001
ENTRYPOINT ["pfa"]
CMD ["--help"]
