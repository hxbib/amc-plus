FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AMC_STATE_DIR=/app/state

WORKDIR /app

RUN mkdir -p /app/state

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

CMD ["sh", "-c", "mkdir -p /app/state && exec python -u amc_monitor.py --config ./config.jsonc"]