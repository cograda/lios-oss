FROM python:3.12-slim

WORKDIR /app

# Install system deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy coglib and install it first (changes rarely)
COPY coglib/ /tmp/coglib/
RUN pip install --no-cache-dir /tmp/coglib/ && rm -rf /tmp/coglib/

# Install Python deps (coglib already installed above)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fastembed model cache directory
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed

# Copy backend
COPY backend/ backend/

# Copy frontend build (built on host)
COPY frontend/dist/ frontend/dist/

# Copy built client wheels so /api/client/download/latest can serve them to daemons.
# Without this the auto-updater finds no wheel and silently never advances. See ISS-015.
COPY client-dist/ client-dist/

EXPOSE 8000

WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
