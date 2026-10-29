---
name: measures
description: Write MEL measures and glossary terms (sums, counts, ratios, latest values, time intelligence). Use for KPIs, metrics, "define X".
---
# Measures (MEL)
- First: the vault (`abi_vault search`) for the business definition; follow it and cite it. Then `semantic_model`
  for existing IDs (reuse them) and the gold columns.
- `set_measure {id, name, expression, description}`; IDs lowercase with `_`. Write base measures before the ones that
  use them; refer to another measure as `[id]`.
- Aggregates on `table.column`: `SUM`, `AVG`, `MIN`, `MAX`, `COUNT`, `DISTINCT_COUNT`, `MEDIAN`.
- Scalars: `DIVIDE(a, b)` (safe division; never AVG of a ratio), `IF(cond, then, else)`, `ROUND(v, n)`, `ABS(v)`;
  arithmetic `+ - * /` between measures and numbers: `SUM(t.minutes) / 60`, `DIVIDE([revenue] * 1000, [views])`.
- Filters: `WHERE([claims], gold_claims.claim_status = 'active')`; ignore a filter: `IGNORE([m], t.col)`.
- Time (needs the calendar): `PREVIOUS_YEAR([m])`, `PREVIOUS_PERIOD([m])`, `YTD([m])`, `QTD`, `MTD`,
  `ROLLING([m], 7, day)`. Change: `DIVIDE([m] - [m_prev], [m_prev])`.
- A measure may use two facts that share dimensions (RPM = partner revenue from the revenue fact / views from the
  audience fact); query it only by shared dimensions (calendar, channel, video, country).
- Snapshot data (running totals) is never summed over days: latest row or gains.
- A rejected measure comes back with the column list and a fix: use it, don't guess again.
- Tie words to measures: `set_glossary_term {term, definition, measures, columns}`, definition in the vault's words.
- Verify a few with `semantic_query` and state values from the rows only.
