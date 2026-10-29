---
name: build-model
description: Build or fix the semantic model until readiness says Ready (facts, dimensions, keys, relationships, calendar, descriptions).
---
# Build the model
1. `abi_tools ["model"]`, `build_model` (it guesses kinds, keys, joins and makes `calendar` from DATE columns), then
   `semantic_model` and `readiness`.
2. Staging layers out of the model: `set_table {table, hidden: true}` for bronze, silver and reference tables.
3. Gold tables: `set_table {table, kind: "fact"|"dimension", key: [...], description: "one line"}`.
   **A dimension's key must be set before a join to it**: `add_relationship` refuses "must be the key of another model
   table" otherwise.
4. Joins: `add_relationship {from: "fact.col", to: "dimension.key"}` (many-to-one only). Check `semantic_model`'s
   `relationships` first and skip the ones build_model already made. A fact's date column joins `calendar.day`.
5. Every model table needs a description, `calendar` too (`set_table {table: "calendar", description}`): readiness
   lists each one that is missing.
6. `set_project_locale` time zone if the data's days are not UTC.
7. Repeat readiness until Ready; `abi_sync`; show the person the model (abi_show page model).
