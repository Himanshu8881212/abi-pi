# ABI-PI in one container: its own engine (engine/, a copy of ABI's backend) with Postgres, the Pi agent harness
# and the app. No other ABI needed. All data in the /data volume; nothing secret is in the image.
FROM node:22.12.0-bookworm-slim AS node

FROM node AS ui
WORKDIR /ui
COPY engine/frontend/package.json engine/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY engine/frontend/ ./
COPY engine/schemas /schemas
RUN npx vite build --outDir /static --emptyOutDir --logLevel warn

FROM python:3.13-slim
# Pi needs Node 22 or newer (Debian's packaged Node is 20)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# postgresql: the engine's database; libgomp1: LightGBM; git: project copies; the rest: the headless Chrome Remotion uses
RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql libgomp1 git ca-certificates curl procps tini \
      libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*' \
    && git config --system user.name ABI-PI && git config --system user.email abi-pi@localhost  # the copies' local harness commits
ENV PGBIN=/usr/lib/postgresql/17/bin

RUN npm install -g --no-audit --no-fund @earendil-works/pi-coding-agent@1.3.0

# the engine (as ABI's own Dockerfile builds it): its venv, DuckDB extensions, the calculation-tool sandbox and the
# Chromium shell its report snapshots use; one root-owned HOME for them, read-only to the runtime user
ENV ENGINE_HOME=/opt/abi/home
WORKDIR /app/engine/backend
COPY engine/backend/pyproject.toml engine/backend/uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.11,source=/uv,target=/bin/uv uv sync --frozen --no-dev --no-cache  # uv: build time only
RUN mkdir -p $ENGINE_HOME && HOME=$ENGINE_HOME .venv/bin/python -c "import duckdb; [duckdb.install_extension(e) for e in ('ducklake', 'postgres', 'mysql', 'httpfs', 'excel', 'delta', 'iceberg', 'avro', 'sqlite')]; duckdb.install_extension('nanoarrow', repository='community')"
ADD --checksum=sha256:67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.13.15/python-3.13.15-wasi_sdk-24.zip /opt/abi/sandbox/
COPY engine/backend/abi/tools/sandbox.py /tmp/
RUN .venv/bin/python -I /tmp/sandbox.py --install /opt/abi/sandbox && rm /tmp/sandbox.py
RUN HOME=$ENGINE_HOME .venv/bin/playwright install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(ls -d $ENGINE_HOME/.cache/ms-playwright/chromium_headless_shell-*/chrome-*/chrome-headless-shell $ENGINE_HOME/.cache/ms-playwright/chromium_headless_shell-*/chrome-*/headless_shell 2>/dev/null | head -1)" /opt/abi/headless_shell \
    && test -x /opt/abi/headless_shell
# one Chrome for everything: the video renderer (Remotion) uses the engine's; one Python: ABI-PI's scripts run on the engine's
ENV REMOTION_BROWSER=/opt/abi/headless_shell PATH=/app/engine/backend/.venv/bin:$PATH

# an unprivileged user runs everything; it owns only the app's folder and the data volume
RUN useradd --create-home --uid 10001 abi && mkdir -p /app /data && chown abi:abi /app /data
WORKDIR /app
USER abi
COPY --chown=abi:abi scripts/video/package.json scripts/video/package-lock.json* scripts/video/
RUN cd scripts/video && npm ci --no-audit --no-fund
COPY --chown=abi:abi . .
COPY --from=ui --chown=abi:abi /static engine/backend/static
# Pi loads a project's own extensions only from a trusted folder: the app's folder is this image's own
RUN mkdir -p /home/abi/.pi/agent && echo '{"/app": true}' > /home/abi/.pi/agent/trust.json

ENV ABI_PI_DATA=/data ABI_PI_WORKSPACES=/data/workspaces ABI_PROJECTS_PATH=/data/projects PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8790
# healthy: the app answers, the engine is ready and its workers beat (scripts/engine.sh restarts what dies)
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 CMD ["/app/scripts/engine.sh", "health", "http://127.0.0.1:8790/"]
# tini as PID 1 reaps the engine's exited children (else a stopped worker stays a zombie that looks alive)
ENTRYPOINT ["tini", "--", "docker/entrypoint.sh"]
