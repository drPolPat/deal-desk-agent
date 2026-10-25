FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --uid 10001 --no-create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH"
# Cloud Run sets PORT.
CMD ["sh", "-c", "exec uvicorn deal_desk.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
