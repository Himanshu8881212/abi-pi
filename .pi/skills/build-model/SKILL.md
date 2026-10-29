---
name: build-model
description: Build or fix the semantic model until readiness says Ready (facts, dimensions, keys, relationships, calendar, descriptions).
---
# Build the model
1. `abi_tools ["model"]`, `build_model`, then `readiness`.
2. Fix each missing item: a fact needs a many-to-one to a dimension (a DATE column gives the generated calendar; a
   lookup table needs `set_table kind: dimension, key: [id]` and `add_relationship fact.col -> dim.key`); every table
   needs a one-line description (`set_table description`); staging tables `hidden: true`.
3. `set_project_locale` time zone if the data's days are not UTC.
4. Repeat readiness until Ready, then `abi_sync` and show the model view (index.html, Model section).
