# Kitchen Agent server for a Raspberry Pi (arm64) or any Docker host.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY kitchen ./kitchen
COPY muskelmarkt_extractor.py offer_mcp_server.py ./
ENV KITCHEN_DB=/data/kitchen.sqlite3 KITCHEN_TRANSPORT=http KITCHEN_HOST=0.0.0.0 KITCHEN_PORT=8000 PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "kitchen.server"]
