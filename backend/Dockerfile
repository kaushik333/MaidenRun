FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY infra ./infra

# Narrow, explicit copies (not the whole feedback-stack/ dir) so local dev
# artifacts (.venv, node_modules, tests/) never end up baked into the image
# or bundled into the S3 tarball this control plane ships to a feedback-stack
# instance — see app/aws/s3_bundle.py, which tars exactly this subtree.
COPY feedback-stack/docker-compose.yml ./feedback-stack/docker-compose.yml
COPY feedback-stack/dashboard-placeholder ./feedback-stack/dashboard-placeholder
COPY feedback-stack/backend/app ./feedback-stack/backend/app
COPY feedback-stack/backend/pyproject.toml ./feedback-stack/backend/pyproject.toml
COPY feedback-stack/backend/Dockerfile ./feedback-stack/backend/Dockerfile
COPY feedback-stack/frontend/src ./feedback-stack/frontend/src
COPY feedback-stack/frontend/index.html ./feedback-stack/frontend/index.html
COPY feedback-stack/frontend/vite.config.js ./feedback-stack/frontend/vite.config.js
COPY feedback-stack/frontend/package.json ./feedback-stack/frontend/package.json
COPY feedback-stack/frontend/package-lock.json ./feedback-stack/frontend/package-lock.json

RUN uv pip install --system --no-cache .

ENV INFRA_DIR=/app/infra
ENV FEEDBACK_STACK_DIR=/app/feedback-stack

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
