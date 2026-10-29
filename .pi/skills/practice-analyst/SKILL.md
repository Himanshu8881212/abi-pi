---
name: practice-analyst
description: How a senior analyst works in ABI - frame the question, use the business definitions, compare properly, segment before concluding, show the rows. Use for any question about the data, "why", "how much", "which", and when building reports or reading ML results.
---
# Analyst practice
- **Frame it.** Restate the question as measure x dimension x period x comparison ("partner revenue by channel,
  September 2026 vs August 2026"). Ask only when two readings give different answers.
- **Definitions from the vault.** `abi_vault search` for the business term; use the measure that implements it
  (glossary); cite the document when a definition decides the answer (net vs gross revenue, RPM vs CPM).
- **The right comparison.** Month over month for trend, year over year for seasonality (Q4 lifts ad rates), a share of
  total for mix. State both values the rows give; let ABI compute changes with a change measure, not you.
- **Segment before you conclude.** A total moved: break it down by the dimensions that can explain it (format,
  country, channel, traffic source, device) and find where the change sits. Check mix effects (more Shorts can lower
  RPM with no channel getting worse).
- **Grain discipline.** Ratios (RPM, CPM, engagement) are measures, never averages of daily ratios; don't add numbers
  across facts at different grains.
- **Show it.** Answer first in one sentence, then the two or three rows that prove it, then `abi_show` the report or a
  filtered view so the person sees the evidence.
- **Say what you can't know.** Generated or partial data, a stream still arriving, a definition the data can't meet:
  say so plainly.
- In Viewer you are read-only: suggest the change, never make it.
