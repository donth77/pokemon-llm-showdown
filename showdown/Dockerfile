FROM node:20-slim

WORKDIR /showdown

RUN apt-get update && apt-get install -y git curl && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/smogon/pokemon-showdown.git . && \
    npm install --production && \
    npm run build

COPY config/config.js config/config.js
COPY static/index.html server/static/index.html
COPY static/trainers/ server/static/trainers/

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["node", "pokemon-showdown", "start", "--no-security"]
