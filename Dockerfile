FROM node:22-slim AS frontend
WORKDIR /web/frontend
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci
COPY web/frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY pyproject.toml README.md README.zh-CN.md ./
COPY smartdata ./smartdata
COPY web ./web
COPY --from=frontend /web/frontend/dist ./web/frontend/dist
RUN pip install --no-cache-dir ".[neo4j]"
EXPOSE 8000
CMD ["uvicorn", "smartdata.interfaces.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
