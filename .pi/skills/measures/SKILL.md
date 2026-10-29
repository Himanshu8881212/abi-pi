---
name: measures
description: Write MEL measures and glossary terms (sums, counts, ratios, latest values, time intelligence). Use for KPIs, metrics, "define X".
---
# Measures (MEL)
- `set_measure {id, name, expression, description}`. Check existing IDs in semantic_model first; reuse them.
- Counts: `COUNT(t.key)`, `DISTINCT_COUNT(t.col)`. Sums: `SUM(t.col)`. Ratios: `DIVIDE([a], [b])` (never AVG of a
  ratio). Conditions: `WHERE(SUM(t.views), t.latest = TRUE)`.
- Time: `PREVIOUS_PERIOD([m])`, `YTD([m])`, `ROLLING([m], 7, day)`; MoM change: `DIVIDE([m] - [m_prev], [m_prev])`.
- Snapshot data (running totals) is never summed over days: latest row or gains.
- Tie words to measures with `set_glossary_term {term, definition, measures, columns}`.
- Verify a new measure once with semantic_query and state its value from the rows.
