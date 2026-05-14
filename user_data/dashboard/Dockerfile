# Lightweight container for the trading-bot dashboard.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so they cache between code edits.
COPY requirements.txt /app/requirements.txt
# Resolver is uv with a 48h release-age cooldown — refuses versions published in the last 2 days.
RUN pip install --no-cache-dir "uv==0.11.11" \
    && UV_EXCLUDE_NEWER="$(date -u -d '2 days ago' +%Y-%m-%d)" \
       uv pip install --system --no-cache -r /app/requirements.txt

# Copy the app *into the container* so it can be run standalone, but the
# real production deployment mounts user_data/ as a volume (see compose).
COPY . /app/dashboard

# At runtime we expect /app/user_data to be a bind mount of the
# trading-bot's user_data directory (the same one the bot uses), so the
# dashboard can read trade_journal + evolution.json directly.
ENV USER_DATA_ROOT=/app/user_data
ENV DASHBOARD_PAIRS="BTC/USD,ETH/USD,SOL/USD"
ENV DASHBOARD_TIMEFRAME="5m"

EXPOSE 8081

# Run the FastAPI app from /app, pointing at the in-image copy. uvicorn
# auto-handles WebSocket upgrades.
CMD ["uvicorn", "dashboard.app:app", \
     "--host", "0.0.0.0", "--port", "8081", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
