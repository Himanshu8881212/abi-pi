# ABI-PI: the Pi agent harness and its app for ABI, in one container. ABI itself runs elsewhere (its URL and its
# projects folder are given at run time); so do the model proxy's address and key. Nothing secret is in the image.
FROM python:3.13-slim

# git: read-only clones of ABI's projects; node: Pi and the video renderer; the rest: the headless Chrome Remotion uses
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl nodejs npm \
      libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

RUN npm install -g --no-audit --no-fund @earendil-works/pi-coding-agent@1.3.0
RUN pip install --no-cache-dir duckdb jsonschema "cryptography>=44,<47"

WORKDIR /app
COPY scripts/video/package.json scripts/video/package-lock.json* scripts/video/
RUN cd scripts/video && npm ci --no-audit --no-fund
COPY . .

# the app's data (project copies, vault, chats, packs) lives in a volume, never in the image
ENV ABI_PI_WORKSPACES=/data/workspaces ABI_PROJECTS_PATH=/abi-projects PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8790
ENTRYPOINT ["docker/entrypoint.sh"]
