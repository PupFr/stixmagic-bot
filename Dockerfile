# ── Build stage ──────────────────────────────────────────────
FROM python:3.12-slim AS base

# Install ffmpeg (required for animated sticker conversion)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Runtime ───────────────────────────────────────────────────
COPY . .

# Expose the Flask API port
EXPOSE 5000

# Environment variables expected at runtime:
#   TELEGRAM_BOT_TOKEN   – bot token from BotFather
#   STIXMAGIC_API_KEY    – secret key for the REST API
#   PORT                 – (optional) override API port, default 5000
#   CORS_ALLOW_ORIGIN    – (optional) restrict CORS, default *
#   MINIAPP_URL          – (optional) Telegram Mini App URL
CMD ["python", "main.py"]
