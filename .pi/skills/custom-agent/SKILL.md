---
name: custom-agent
description: Create, test and publish an ABI custom agent with a knowledge scope and an evaluation set; ask an existing one with run_agent.
---
# Custom agent
1. `abi_tools ["agents"]`. `save_agent` a draft: id, name, purpose, instructions (quote numbers exactly), scope (tables,
   measures), tools (semantic_query at least), tools_only true, grounding true, and five evaluation cases
   (question, must contain, must call tools) whose answers you checked with semantic_query.
2. `test_agent`; fix failing cases; `publish_agent` (the person confirms).
3. Ask it with `run_agent {project, agent, question}`.
