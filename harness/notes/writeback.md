# writeback: planning, input tables, proposals
- Input tables (`create_input_table {table, columns: [{name, type}], key}`) hold hand-entered rows (targets, budgets).
  Relate them like any table (`add_relationship targets.month -> calendar.day`).
- From an agent every edit becomes a proposal (`propose_edit`, or `edit_cells`/`edit_total` which ABI turns into
  proposals): say "proposed, waiting for approval". `edit_proposals` lists them; `approve_edit` is a person's step and
  shows old -> new for their confirmation.
- Read current values with semantic_query first; compute new values only from tool rows (a 10% raise: ask ABI for the
  current value, state it, and propose the new one; the preview shows the exact figures).
- `undo_edits` undoes a batch; `sync_edits` writes edits back to the source (outward-facing: confirmation).
