---
name: practice-agentic
description: How to build agents and tools in ABI well - narrow scope, tested tools, evaluation sets, grounding, least privilege, publish after the evals pass. Use with custom-agent, for tools, and when the person asks for an assistant, a bot or a self-service helper.
---
# Agentic engineering practice
- **Narrow job.** One agent per audience and task ("answers revenue questions for one channel"); its purpose says what
  it must refuse.
- **Least privilege.** Scope to the tables and measures it needs; tools only those it calls; `tools_only` and
  `grounding: true` so every number comes from a tool result.
- **Tools before agents.** A repeated calculation becomes a tool (`save_tool`) with typed inputs, a semantic query and
  at least two tests whose expected rows you checked with semantic_query; `test_tool`, then `publish_tool`.
- **Evaluation set first.** Five or more cases (question, must contain, must call which tool), including one it must
  refuse and one edge case; `test_agent` until all pass; only then `publish_agent` (the person confirms).
- **Instructions are short and concrete**: quote numbers exactly, ask for the missing parameter, never invent.
- **Versioning.** A change goes back to draft; rerun the evaluation set before publishing again.
- **Explain the boundary** to the person: what it can answer, what it refuses, where its numbers come from.
