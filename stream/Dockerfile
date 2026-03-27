FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    ffmpeg \
    pulseaudio \
    curl \
    xdotool \
    unclutter \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY . .

RUN chmod +x entrypoint.sh supervisor.sh

CMD ["./entrypoint.sh"]
