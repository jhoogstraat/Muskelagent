# Kitchen Agent server image (linux/arm64 for the Raspberry Pi, linux/amd64 elsewhere). Runs as uid 1000, state in /data.
FROM docker.io/library/python:3.13-slim-trixie

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project
COPY kitchen ./kitchen
COPY muskelmarkt_extractor.py offer_mcp_server.py ./
RUN python -m compileall -q kitchen muskelmarkt_extractor.py

RUN useradd --uid 1000 --create-home kitchen && mkdir /data && chown kitchen /data
USER kitchen
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    KITCHEN_DB=/data/kitchen.sqlite3 KITCHEN_TRANSPORT=http KITCHEN_HOST=0.0.0.0 KITCHEN_PORT=8000
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "kitchen.server"]
