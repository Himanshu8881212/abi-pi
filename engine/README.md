# engine

ABI-PI's own copy of ABI's engine (the backend, its JSON schemas and the web UI the report snapshots render), taken
from ABI commit fcc3aa35 with `git archive`: committed files only, without ABI's tests, design notes and work logs.
ABI-PI runs on this copy alone; no other ABI install is needed or touched. Licence: AGPL-3.0-or-later (`LICENSE`,
`THIRD_PARTY_NOTICES`), which ABI-PI as a whole inherits.

    (cd backend && uv sync --frozen --no-dev)                                         once
    (cd frontend && npm ci && npx vite build --outDir ../backend/static --emptyOutDir)   once, and after UI changes
    ../scripts/engine.sh start | stop | status                                        Postgres + engine + worker
    ../scripts/accounts.py                                                            first run: the accounts

To take a newer ABI: `git -C <ABI> archive HEAD backend/abi backend/pyproject.toml backend/uv.lock schemas frontend
LICENSE THIRD_PARTY_NOTICES | tar -x -C engine`, delete `frontend/src/*.test.*`, rerun the tests.
