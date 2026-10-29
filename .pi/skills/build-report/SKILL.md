---
name: build-report
description: Build or change an ABI report (pages, KPIs, charts, filters) and its interactive HTML view. Use for "report", "dashboard", "chart of X by Y".
---
# Build a report
1. `semantic_model` for measure IDs and table.column dimensions. Missing measure: write it first (measures skill).
2. `abi_tools ["reports"]`; `save_report` with one page per theme; a row of KPIs on top (`kpi`, w 3, h 3), charts below
   (w 6 or 12, h 5): `bar` for categories (order_by the measure desc, limit 20), `line` for calendar.day/month,
   `table`/`matrix` for detail, `map` for countries, `slicer` for a filter.
3. Use only measure IDs and dimensions from semantic_model; ABI rejects others with a fix.
4. `list_reports` to confirm it exists, `abi_sync`, then give the path `workspaces/<ws>/_views/<project>/reports/<id>.html`.
5. Describe what each visual shows without numbers, or quote numbers from visual_data rows only.
