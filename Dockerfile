# One process: webhook receiver + scheduler + operator view (docs/ops.md).
#   docker build -t pra . && docker run --env-file .env -p 8080:8080 -p 8000:8000 pra
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 1000 pra
WORKDIR /srv/pra
COPY --chown=pra:pra . .
RUN pip install --quiet . && mkdir -p /srv/pra/data && chown pra:pra /srv/pra/data
USER pra
# SQLite lives on the volume; override with DATABASE_URL for Postgres.
ENV DATABASE_URL=sqlite:////srv/pra/data/recovery.db MERCHANTS_DIR=/srv/pra/merchants
VOLUME ["/srv/pra/data"]
EXPOSE 8080 8000
CMD ["pra", "serve", "--host", "0.0.0.0"]
