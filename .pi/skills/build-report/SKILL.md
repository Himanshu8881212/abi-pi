---
name: build-report
description: Build or change an ABI report (pages, KPIs, charts, filters) and show it in the app. Use for "report", "dashboard", "chart of X by Y".
---
# Build a report
1. `semantic_model` for measure IDs and `table.column` dimensions. A missing measure: write it first (measures skill).
2. `abi_tools ["reports"]`; `save_report {report}` with one page per theme. The layout is a 12-column grid:
   ```json
   {"id": "executive", "name": "Executive overview", "pages": [{"id": "main", "name": "Overview", "visuals": [
     {"id": "kpi-revenue", "type": "kpi", "title": "Partner revenue", "query": {"measures": ["partner_revenue"], "dimensions": []},
      "layout": {"x": 0, "y": 0, "w": 3, "h": 3}},
     {"id": "rev-month", "type": "line", "title": "Partner revenue by month", "query": {"measures": ["partner_revenue"],
      "dimensions": ["calendar.year", "calendar.month"], "order_by": [{"field": "calendar.year"}, {"field": "calendar.month"}]},
      "layout": {"x": 0, "y": 3, "w": 12, "h": 5}},
     {"id": "top-channels", "type": "table", "title": "Top 10 channels", "query": {"measures": ["partner_revenue", "views", "rpm"],
      "dimensions": ["dim_channel.channel_display_name"], "order_by": [{"field": "partner_revenue", "desc": true}], "limit": 10},
      "layout": {"x": 0, "y": 8, "w": 12, "h": 6}}]}]}
   ```
   KPIs on top (w 3, h 3); `bar` for categories (order by the measure desc, limit 20); `line` for calendar.day, or
   calendar.year with calendar.month (one time axis; the month alone adds the years together); `table`/`matrix` for
   detail; `map` for countries; `slicer` for a filter. Five KPIs: w 2 or wrap.
3. Only measure IDs and dimensions from semantic_model; ABI rejects others with a fix: apply it.
4. In one step, `abi_show {page: "report", report: <id>}` so the person sees it and `visual_data {report, page, visual}`
   for each visual: its rows are the figures you may quote (save_report's result names the pages and visuals).
5. Describe what each visual shows by its title and the rows you read for it; name the visuals (a count only with
   their names right after it: "3 visuals: A, B and C"). No visual_data rows read: describe it without figures.
