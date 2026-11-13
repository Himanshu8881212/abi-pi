# reports: reports, visuals, exports, sharing
- `save_report {project, report}`: `{id, name, pages: [{id, name, filters?, visuals: [{id, type, title, query:
  {measures: [ids], dimensions: [table.column], filters, order_by: [{field, desc}], limit}, layout: {x, y, w, h}}]}]}`.
  Grid is 12 columns wide. Types: kpi, bar, line, pie, table, matrix, scatter, slicer, map, combo, waterfall, funnel,
  heatmap, treemap, sankey. Use measure IDs, never display names.
- A form card runs an action type on one object: `{id, type: "form", title, form: {action, key?}, layout}`, no `query`;
  without `key` the person picks the object in the card.
- After saving: `abi_sync` renders `workspaces/<ws>/_views/<project>/reports/<id>.html` (interactive, offline).
- `visual_data` returns one visual's rows; `report_snapshot`/`export_visual` make PNG/PDF/CSV files.
- Sharing widens who sees data: it needs the person's confirmation. A public link to a report and embedding a report
  in another site are not available in this install (ABI refuses `create_share_link` and `create_embed_key`): say so
  and that a workspace owner can use Share on the report instead (the people they pick open it after signing in).
- `delete_report` is destructive: the person confirms.
