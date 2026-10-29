# ABI-PI: the Pi agent harness and its app for ABI, in one container. ABI itself runs elsewhere (its URL and its
# projects folder are given at run time); so do the model proxy's address and key. Nothing secret is in the image.
FROM node:22.12.0-bookworm-slim AS node

FROM python:3.13-slim
# Pi needs Node 22 or newer (Debian's packaged Node is 20)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# git: read-only clones of ABI's projects; node: Pi and the video renderer; the rest: the headless Chrome Remotion uses
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl procps \
      libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

RUN npm install -g --no-audit --no-fund @earendil-works/pi-coding-agent@1.3.0
RUN pip install --no-cache-dir duckdb jsonschema "cryptography>=44,<47"

# an unprivileged user runs everything; it owns only the app's folder and the data volume
RUN useradd --create-home --uid 10001 abi && mkdir -p /app /data && chown abi:abi /app /data
WORKDIR /app
USER abi
COPY --chown=abi:abi scripts/video/package.json scripts/video/package-lock.json* scripts/video/
RUN cd scripts/video && npm ci --no-audit --no-fund
COPY --chown=abi:abi . .
# Pi loads a project's own extensions only from a trusted folder: the app's folder is this image's own
RUN mkdir -p /home/abi/.pi/agent && echo '{"/app": true}' > /home/abi/.pi/agent/trust.json

# the app's data (project copies, vault, chats, packs) lives in a volume, never in the image
ENV ABI_PI_WORKSPACES=/data/workspaces ABI_PROJECTS_PATH=/abi-projects PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 CMD curl -fsS -o /dev/null http://127.0.0.1:8790/ || exit 1
ENTRYPOINT ["docker/entrypoint.sh"]
