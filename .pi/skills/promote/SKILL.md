---
name: promote
description: Promote a dev project to production (push, pull with the diff, production view-only). Use for "promote", "go live", "production".
---
# Promote to production
1. `abi_tools ["project"]`. `changes` must be empty (auto-commit on); `remote_settings` shows the Git remote (set by
   the person with a secret; set_remote is a person's click in ABI).
2. `push` from dev. In the production project `pull` shows the diff and asks the Owner's confirmation.
3. Production is view-only: later changes go to dev, then promote again. `abi_sync` both folders.
