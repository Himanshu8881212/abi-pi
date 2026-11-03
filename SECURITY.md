# Security

Please report a vulnerability privately: open a [security advisory](../../security/advisories/new) on this
repository (Security › Report a vulnerability), not a public issue. Include what you did, what happened and the ABI
version (`docker exec abi cat /data/VERSION`). You get an answer within a week.

## What ABI protects

- **Secrets:** API keys, SMTP and SSO client secrets, S3 keys are encrypted at rest (the engine's key and the app's
  key in the volume, or `ABI_SECRET_KEY`), bound to the host they are for, and never returned to a page, a log or
  the agent.
- **Sign-in:** ABI's accounts (password, optional authenticator code, OpenID Connect); sessions are HttpOnly
  same-site cookies; the app refuses other Host headers (DNS rebinding) and framing.
- **The agent:** reads data only through the semantic engine as the signed-in person (their row-level security),
  changes the project only through ABI's actions, and never confirms a destructive or outward-facing step itself.

Run ABI behind HTTPS (a reverse proxy) when people reach it over a network, and set `ABI_PUBLIC_URL` to that address.
