FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl git gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/* /root/.gnupg

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

RUN useradd --create-home --uid 10001 app
COPY --chown=app:app . .
RUN uv sync --frozen --no-dev \
    && mkdir -p /app/backups /app/data/photos \
    && chown -R app:app /app

USER app

CMD ["uvicorn", "health_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
