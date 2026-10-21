"""Live acceptance testing before the merge requests (Phase 10).

- `preflight`: before coding, can this ticket be tested at all (tools, env, the dev DB, the
  apps boot)? → `EnvReport` (the `env_check` node).
- `stack`: the ticket's apps as local processes, with a private local Redis and a fixture
  server for external services; the database is the shared dev DB from the repo's own `.env`.
- `cases`: test cases from the interface contract (auth, validation) + the tester agent's cases
  per acceptance criterion; `executor` runs them (HTTP + read-only DB checks), records evidence
  and deletes exactly the rows the run created (`ledger`).
- `migrations`: a ticket whose migration isn't on the dev DB yet waits for it (read-only check of
  `SequelizeMeta`); the pipeline never runs migrations itself except `pipeline migrate`, which a
  person runs in a terminal.
- `service.AcceptanceService` ties it together for the graph nodes and the CLI.
"""
