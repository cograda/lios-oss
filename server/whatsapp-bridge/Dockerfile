FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Baileys depends on libsignal-node via SSH git URL — rewrite to HTTPS
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" \
    && git config --global --get-regexp url

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY src/ src/

# Auth state persisted via Docker volume at /app/auth_state
RUN mkdir -p /app/auth_state

EXPOSE 3100

CMD ["node", "src/index.js"]
