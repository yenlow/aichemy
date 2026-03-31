# Stage 1: Build React frontend
FROM node:20-slim AS frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src/ src/
COPY public/ public/
RUN npm run build

# Stage 2: Python runtime
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yml app.yaml start.py tools.txt ./
COPY agent/ agent/
COPY server/ server/
COPY skills/ skills/

COPY --from=frontend /app/dist dist/

ENV AGENT_PORT=8080
ENV DATABRICKS_APP_PORT=10000

EXPOSE 10000

CMD ["python", "start.py"]
