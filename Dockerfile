FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG APP_VERSION=dev

ENV APP_VERSION=$APP_VERSION

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# v4 schema is Alembic-owned. The runtime image now carries alembic + the migration
# env so the PreSync migrate Job (vanfreckle-platform) runs `alembic upgrade head`
# from this same image — no separate -migrate overlay, so the Job's tag tracks the
# app tag via the kustomize image rewrite. Folded in from the retired Dockerfile.migrate
# after the v4.0.30 ledger-drift incident (create_all was masking unrun migrations).
RUN pip install --no-cache-dir "alembic>=1.14,<2.0"
COPY alembic.ini ./
COPY alembic ./alembic

RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=30s \
	--timeout=5s \
	--start-period=10s \
	--retries=3 \
	CMD curl --fail http://localhost:8000/ \
	|| exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
