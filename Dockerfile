# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.163.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm AS gateway
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git fonts-noto-cjk libgomp1 libstdc++6 gosu socat \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 pocket && useradd -u 10001 -g pocket -d /state -M pocket
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /app
COPY bridge/requirements.txt /app/bridge/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_CONFIG_FILE=/dev/null python -m pip install --index-url https://pypi.org/simple \
    --prefer-binary --timeout 60 --retries 5 -r /app/bridge/requirements.txt
COPY bridge/bridge.py bridge/console.html bridge/icons.json bridge/test_*.py /app/bridge/
COPY deploy/export_connection.py /app/deploy/export_connection.py
COPY assistant-workspace/AGENTS.md /app/default-AGENTS.md
COPY setup-speech.py /app/setup-speech.py
COPY docker/runtime.py docker/gateway-entrypoint.sh /app/docker/
RUN chmod 755 /app/docker/gateway-entrypoint.sh
ENV HOME=/state CODEX_HOME=/state/.codex CODEX_BINARY=/usr/local/bin/codex \
    POCKET_STATE_DIR=/state/runtime POCKET_WORKSPACE=/workspace \
    POCKET_MODEL_DIR=/models/whisper-small POCKET_CA_FILE=/trust/root.crt \
    POCKET_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
    POCKET_EXTERNAL_SANDBOX=docker PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models/huggingface TOKENIZERS_PARALLELISM=false
ENTRYPOINT ["/app/docker/gateway-entrypoint.sh"]
CMD ["serve"]

FROM caddy:2.11.7-alpine AS ingress
COPY docker/Caddyfile /etc/caddy/Caddyfile
COPY docker/caddy-entrypoint.sh /etc/caddy/pocket-entrypoint.sh
ENTRYPOINT ["/bin/sh", "/etc/caddy/pocket-entrypoint.sh"]
