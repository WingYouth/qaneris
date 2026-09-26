FROM node:22-slim AS frontend
WORKDIR /web/frontend
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci
COPY web/frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl libgssapi-krb5-2 \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -f /tmp/packages-microsoft-prod.deb \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md README.zh-CN.md ./
COPY qaneris ./qaneris
COPY web ./web
COPY --from=frontend /web/frontend/dist ./web/frontend/dist
RUN pip install --no-cache-dir ".[sql,redis,mongodb,cassandra,hbase,neo4j,influxdb,search,milvus,qdrant]"
EXPOSE 8000
CMD ["uvicorn", "qaneris.interfaces.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
