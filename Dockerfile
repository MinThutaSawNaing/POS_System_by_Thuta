FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

# Run as an unprivileged user and keep mutable data in mountable directories.
RUN addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app --home /app app \
    && mkdir -p /app/instance /app/uploads/products \
    && chown -R app:app /app

USER app

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/healthz', timeout=3).read()"]

# One process avoids duplicate import-time SQLite migrations. Waitress provides a
# production WSGI server and bounded concurrency without Flask's debug reloader.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8888", "--threads=4", "app:app"]