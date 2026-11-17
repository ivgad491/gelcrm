FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system gelcrm \
    && useradd --system --gid gelcrm --home-dir /app gelcrm

WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY --chown=gelcrm:gelcrm . .
RUN chmod 755 /app/docker-entrypoint.sh \
    && mkdir -p /app/private_backups /app/staticfiles \
    && chown -R gelcrm:gelcrm /app/private_backups /app/staticfiles

USER gelcrm
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "1300", "--access-logfile", "-", "--error-logfile", "-"]
