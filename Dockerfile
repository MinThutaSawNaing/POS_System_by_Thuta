FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

# Run as an unprivileged user and keep mutable data in mountable directories.
RUN addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app --home /app app \
    && mkdir -p /app/instance /app/uploads/products /app/uploads/receipts \
    && chown -R app:app /app

USER app

EXPOSE 8888

HEALTHCHECK --interval=60s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/healthz', timeout=2).read()"]

# Resource quotas are enforced by docker run/Compose, not by the image. These
# settings additionally bound per-process concurrency, queued sockets, idle
# connections, and request memory. The Flask application already rejects bodies
# over 5 MiB, so Waitress uses the same limit.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8888", "--threads=2", "--connection-limit=32", "--backlog=64", "--channel-timeout=30", "--cleanup-interval=15", "--max-request-header-size=32768", "--max-request-body-size=5242880", "--ident=Parrot-POS", "app:app"]