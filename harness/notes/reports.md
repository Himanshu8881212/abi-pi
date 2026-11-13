# reports: reports, visuals, exports, sharing
- `save_report {project, report}`: `{id, name, pages: [{id, name, filters?, visuals: [{id, type, title, query:
  {measures: [ids], dimensions: [table.column], filters, order_by: [{field, desc}], limit}, layout: {x, y, w, h}}]}]}`.
  Grid is 12 columns wide. Types: kpi, bar, line, pie, table, matrix, scatter, slicer, map, combo, waterfall, funnel,
  heatmap, treemap, sankey. Use measure IDs, never display names.
- A form card runs an action type on one object: `{id, type: "form", title, form: {action, key?}, layout}`, no `query`;
  without `key` the person picks the object in the card.
- After saving: `abi_sync` renders `workspaces/<ws>/_views/<project>/reports/<id>.html` (interactive, offline).
- `visual_data` returns one visual's rows; `report_snapshot`/`export_visual` make PNG/PDF/CSV files.
- Sharing, share links and embed keys widen who sees data: they need the person's confirmation. A new share link or
  embed key (and a flow's new webhook token) is shown to the person once, in a dialog only they read: you never see
  it. Say it was made and that they should keep it safe; if they didn't copy it, they revoke it and make a new one.
  Never ask for it or write one yourself.
- `delete_report` is destructive: the person confirms.
