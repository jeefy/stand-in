FROM docker.io/library/python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /bin/uv

# Bytecode is compiled at build time; nothing is written at runtime, so the
# image runs with a read-only root filesystem.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY patterns.yaml ./
COPY src ./src
# Non-editable: standin and its force-included patterns.yaml land in
# site-packages, so the venv does not depend on /app/src.
RUN uv sync --locked --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}"
USER 1000:1000
EXPOSE 9101 9102
CMD ["python", "-m", "standin"]
