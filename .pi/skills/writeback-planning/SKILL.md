---
name: writeback-planning
description: Planning and write-back - targets, budgets, input tables, proposals and approvals. Use for "raise/lower/set the target", "budget", "plan".
---
# Write-back and planning
1. `abi_tools ["writeback"]`. Find the input table (targets, budget) and its key columns in semantic_model.
2. Read the current value with semantic_query (filtered to the asked members). If the key doesn't have the asked
   grain (targets by channel but asked by category), say so and offer the nearest valid change.
3. Propose with `propose_edit` (or `edit_cells`; from you it becomes a proposal). New values only from tool rows.
4. Tell the person: proposed, waiting for approval; ABI's preview shows old -> new. Approval (`approve_edit`) is the
   person's step: the harness asks them and shows ABI's preview.
