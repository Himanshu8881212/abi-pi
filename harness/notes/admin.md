# admin and security
- Users, roles and tenants: listing is fine; changing access (`set_role`, `invite_user`, tenants) is the person's click
  in ABI's app, never yours. Secrets: `list_secrets` shows names only; the person creates them in ABI.
- Row-level security: `set_rls_rule`, `set_column_rule`, `rls_rules`; changes ask confirmation.
- `usage` shows LLM usage.
