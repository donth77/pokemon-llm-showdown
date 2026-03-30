FROM node:20-slim

WORKDIR /showdown

RUN apt-get update && apt-get install -y git curl && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/smogon/pokemon-showdown.git . && \
    npm install --production && \
    npm run build

COPY showdown/config/config.js config/config.js
COPY showdown/static/index.html server/static/index.html
COPY assets/static/trainers/ server/static/trainers/

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["node", "pokemon-showdown", "start", "--no-security"]
