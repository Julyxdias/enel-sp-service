FROM node:20-slim

# Dependências do sistema para o Playwright/Chromium
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia manifesto e instala dependências
COPY package*.json ./
RUN npm ci --omit=dev

# Diz ao Playwright para usar o Chromium do sistema
# em vez de baixar um próprio (evita os ~300 MB extras)
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Copia o restante do código
COPY src/ ./src/

# Garante que o diretório de secrets existe com permissão restrita
RUN mkdir -p /app/secrets && chmod 700 /app/secrets

EXPOSE 3000

CMD ["node", "src/index.js"]
