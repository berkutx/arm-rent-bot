ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 HOST=0.0.0.0
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    && useradd --no-create-home --uid 10001 rent \
    && mkdir -p /app/data && chown rent:rent /app/data
COPY server.py source_sync.py ./
COPY web/index.html web/districts.json ./web/
COPY web/vendor/*LICENSE ./licenses/
COPY web/vendor/leaflet-1.9.4.* ./web/vendor/
COPY scripts/backup.py scripts/telegram_login.py scripts/fill_locations.py ./scripts/
COPY deploy/docker-entrypoint.sh /entrypoint.sh
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
