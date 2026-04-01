FROM node:20-slim AS showdown-build

WORKDIR /opt/pokemon-showdown

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 https://github.com/smogon/pokemon-showdown.git . \
    && npm install --production \
    && npm run build

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=showdown-build /opt/pokemon-showdown /opt/pokemon-showdown

ENV SHOWDOWN_HOME=/opt/pokemon-showdown

COPY . .

RUN mkdir -p /data /manager-data /personas /app/static/trainers /app/static/portraits

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
