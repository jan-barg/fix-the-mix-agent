FROM python:3.12-slim

# FFmpeg decodes uploads and runs the ebur128 loudness meter. Pango and HarfBuzz let WeasyPrint render the
# PDF report; Inter is the typeface of the charts and the PDF (the web pages load it from Google Fonts).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-inter \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY static ./static
COPY demo/corpus.json ./demo/corpus.json

# Cloud Run sets PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
