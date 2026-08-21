# Technopoliss Vernika AI Voice Agent — production image
# FastAPI + uvicorn. Runs the /vobiz/answer + /ws/vobiz + dashboard (port 9090).
# TLS/WSS is terminated by Caddy in docker-compose; this container stays plain HTTP
# on the internal network.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Build deps needed for miniaudio / numpy wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    g++ \
    libasound2-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps first so rebuilds after code changes are fast
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r ./backend/requirements.txt

# Trim build deps (keeps curl for healthchecks)
RUN apt-get remove -y gcc g++ libasound2-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# App code (exclude .venv, logs, data are mounted)
COPY backend ./backend
COPY frontend ./frontend

# Technopolis dashboard bundle (served from project root by api/routes/ui.py)
COPY index.html app.js index.css kpi_modal.js /app/

# Data + writable dirs (bind-mounted from the host / named volume at runtime)
RUN mkdir -p /app/backend/data

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV HOST=0.0.0.0 \
    PORT=9090 \
    VERN_DATA_DIR=/app/backend/data

WORKDIR /app/backend

EXPOSE 9090

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9090", "--log-level", "info"]
