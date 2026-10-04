FROM node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203
WORKDIR /workspace/browser
COPY browser/package*.json ./
ENV PLAYWRIGHT_BROWSERS_PATH=/workspace/browser/.browsers
RUN npm ci && npx --no-install playwright-core install --with-deps chromium
WORKDIR /workspace/frontend
COPY frontend/package*.json ./
RUN npm ci
WORKDIR /workspace
COPY frontend/src frontend/src
COPY browser browser
COPY scripts scripts
USER node
CMD ["node", "browser/run.cjs"]
