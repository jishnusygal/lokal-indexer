FROM python:3.11-slim-bookworm AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.11-slim-bookworm
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels \
    && python -m playwright install --with-deps chromium \
    && mv /root/.cache/ms-playwright /opt/ms-playwright \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /home/app/.cache /app/data /app/torrents \
    && ln -s /opt/ms-playwright /home/app/.cache/ms-playwright \
    && chmod -R a+rX /opt/ms-playwright \
    && chown -R app:app /app /home/app \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=app:app main.py config.py database.py models.py scraper.py notifier.py torznab.py ./
COPY --chown=app:app templates ./templates
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "930"]
