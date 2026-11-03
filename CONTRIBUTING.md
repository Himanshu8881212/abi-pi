# Contributing to ABI

Thanks for helping. ABI is AGPL-3.0: by sending a change you agree it is published under that license.

## Run it from source

The steps are in the [README](README.md#contributing). The container is the release: `docker compose up -d --build`.

## Tests

```sh
.venv/bin/python -m pytest -q tests                  # offline: needs the engine running, no model
ABI_PI_LIVE=1 .venv/bin/python -m pytest -q -m live tests   # the agent end to end: needs a model, takes minutes
ABI_PI_LIVE_MODEL=provider/model ABI_PI_LIVE_FALLBACK=provider/other ...   # another model, a fallback on 429/5xx
```

A fix comes with the test that failed before it. Keep changes small and in the style of the file around them.

## The rules the agent works under

[AGENTS.md](AGENTS.md): data only through ABI's semantic engine, changes only through ABI's actions, every number
from a tool result, a person's yes before anything destructive or outward-facing, no secrets in prompts or logs.
A change that loosens one of them needs a very good reason and a test.

## Never commit

`.env`, `data/`, `workspaces/`, `logs/`, keys, passwords, tokens or real customer data. `git diff --cached` before
you push.
