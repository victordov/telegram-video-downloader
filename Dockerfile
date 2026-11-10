FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads \
    DATABASE_PATH=/app/data/bot.sqlite3

WORKDIR /app

# ffmpeg unlocks merged video+audio downloads, thumbnails and shrinking.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py video_downloader.py telegram_bot.py run_bot.py app.py \
     storage.py access.py monitoring.py updater.py ./

# site-packages stays root-owned, so the bot cannot update yt-dlp in place
# here; rebuild the image to pick up a new release.
RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/downloads /app/data \
    && chown -R botuser:botuser /app/downloads /app/data
USER botuser

# Only used in webhook mode (WEBHOOK_URL set).
EXPOSE 8443

CMD ["python", "run_bot.py"]
