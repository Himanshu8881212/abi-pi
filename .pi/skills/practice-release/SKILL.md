---
name: practice-release
description: How release and operations engineering works in ABI - commit hygiene, release notes from the journal, documentation, a rollback plan before publishing, environments in step, operations health. Use with promote and for any commit, publish, release notes, docs or "is it healthy" question.
---
# Release and operations practice
- **Commits tell a story.** `changes` first; one commit per logical change with a message in the imperative
  ("Add gold revenue facts and RPM measures"); never commit someone's half-finished work without saying so.
- **Release notes from the journal**, not from memory: what changed for viewers (new reports, changed definitions,
  new alerts), what was checked, what is known to be open.
- **Documentation.** `docs` regenerates ABI's project documentation; table and measure descriptions must read for a
  business person; flag missing ones.
- **Before publish or promote: a rollback plan.** Name the commit Viewer shows now and the one it will show; the
  person publishes (the Lead asks them). Production changes only by promote (push, then pull with the diff).
- **Operations health.** data_quality (freshness, gaps, row drops), the stream arriving (latest day), flow_runs
  failures, ML models not retrained for 30 days: report each with the rows that show it.
- You change definitions never; you only commit, document and report.
