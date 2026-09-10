# --- stage 1: build the React questionnaire ---
FROM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
RUN npm run build

# --- stage 2: FastAPI runtime, serving the API and the built React app ---
FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN uv pip install --system --no-cache .

COPY --from=frontend-build /build/dist ./static

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
