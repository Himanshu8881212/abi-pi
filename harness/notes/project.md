# project and Git
- `create_project` (use abi_project instead: it also makes the folder). `set_auto_commit on` makes each change a commit.
- `changes` (uncommitted), `commit`, `history`, `diff`, `undo`, `revert`; `import_project` takes a whole bundle
  (the harness's `abi_cli validate|diff` dry-runs the folder).
- Promote to production: dev pushes (`push`), the production project pulls (`pull`: Owner, confirmation with the diff).
  Production projects are view-only; changes are made in dev and promoted. `set_environment`, `set_remote` (person only).
- `set_project_locale` sets time zone and currency.
