# Application image for the Clinical & Claims Analyst Agent (FastAPI + LiteLLM).
# Python pinned to 3.12 per the committed stack. Synthea generation (which needs
# Java) is a host/dev step via `python -m src.data.load`, not part of this image.
# The eval suite (including gold_sql) is deliberately NOT shipped — the runtime
# agent must never have it on disk.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install project + pinned deps. Copy the build inputs first so the layer caches
# until pyproject or source actually changes.
COPY pyproject.toml README.md ./
COPY src ./src
COPY ui ./ui

RUN pip install --upgrade pip && pip install .

# Run as a non-root user; the app needs no write access to the image.
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

# Reachable liveness probe (matches GET /health; honors $PORT like CMD does).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://localhost:'+port+'/health').status==200 else 1)"

# Honor $PORT (Render/Fly inject it); default 8000 for local compose.
CMD ["sh", "-c", "uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
