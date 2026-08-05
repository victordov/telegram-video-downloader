FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads

WORKDIR /app

# ffmpeg unlocks merged video+audio downloads and thumbnail generation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py video_downloader.py telegram_bot.py run_bot.py ./

RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/downloads \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "run_bot.py"]
