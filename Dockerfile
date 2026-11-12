FROM python:3.12-slim

# ffmpeg joins Shorts' video and sound; deno lets yt-dlp read YouTube's player.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin-2.1.4 /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1
CMD ["python", "-m", "bot.server"]
