# syntax=docker/dockerfile:1
FROM python:3.13.1-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

RUN useradd --system --user-group --no-create-home app

# Manifest first; source last so that source edits do not invalidate earlier layers.
COPY pyproject.toml ./
COPY . .
RUN pip install .

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

ENTRYPOINT ["pyref-service"]
CMD ["serve", "--host", "0.0.0.0"]
