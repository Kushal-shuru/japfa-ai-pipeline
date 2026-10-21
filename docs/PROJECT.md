# PROJECT: what the AI SDLC pipeline is and how it is built

Read this to know every feature of the pipeline and how it works inside. To install it:
[SETUP.md](SETUP.md). To use it day to day: [GUIDE.md](GUIDE.md). The exact data contracts, tables and
guardrails are in [CLAUDE.md](../CLAUDE.md); every design decision, with its reason, is a row in
[PROGRESS.md](PROGRESS.md) (D-numbers below); [DESIGN.md](DESIGN.md) is the early design (history).

---

## 1. What it is

An AI pipeline that takes a **Jira ticket** to a set of **reviewed, deployment-documented, QA-ready
GitLab merge requests** (one per repository the ticket touches). A developer drives it from a
terminal. AI agents (Claude Code or Codex) plan, code, review and write the documents; a Python
orchestrator runs the workflow, talks to Jira/GitLab/Google, enforces the rules and keeps the state.

**Humans keep control of:** which tickets the AI takes, the plan, which review findings to fix,
merging, infrastructure changes (migrations, env vars) and QA sign-off. The AI never merges, never
pushes to a default branch, never runs a migration, never changes CI or secrets.

**The product it works on:** microservices over REST.

| Repo kind | Stack | Does |
|---|---|---|
| Gateway | Node + Express | Public routes, auth, request validation, calls internal services over REST, maps their errors |
| Service | Node + Express | Business logic and DB operations; internal REST endpoints |
| Frontend | Vue | Consumes the gateway (not supported by the pipeline yet) |

A typical backend ticket touches the gateway plus one or more services. Every repo has its **own**
standards (`CLAUDE.md`, an identical `AGENTS.md`) and review checklist (`REVIEW.md`); the agents obey
the repo they work in.

## 2. Feature catalogue

**Intake**
- `implement KEY` asks for the tech-spec link (validated: must be a readable Google Doc) and the repositories (a numbered list from the config); Jira custom fields are optional. Never guesses repos.
- Optional automatic pickup of Jira tickets (`gates.start: auto`, label `ai-ready`).

**Planning**
- A read-only Planner reads the ticket, the relevant spec sections and the repos; writes per-repo tasks, the coding order, an **interface contract** between gateway and services (endpoints, schemas, errors, error mapping), and maps **every acceptance criterion** to a task (nothing may be left uncovered).
- It asks questions instead of guessing when the ticket is unclear. A person approves or revises the plan (`approve` / `guide`).

**Coding**
- One **coder session per ticket** across all repos (or one run per repo in parallel); follows each repo's standards; writes tests; makes small, readable commits from its own commit plan.
- The pipeline **re-verifies** everything itself (lint, tests, extra checks, secret scan) and sends failures back; it never trusts "I ran the tests".
- Protected files per repo (`shared_paths`, or rules in the repo's `CLAUDE.md`) cannot be changed: enforced by deny rules and an automatic undo.
- A contract the coder cannot follow triggers one re-plan; machine problems (Redis down, disk full) stop immediately without wasting an attempt.

**Live acceptance tests** (opt-in per ticket: `testing=true`)
- A read-only Tester agent writes test cases per acceptance criterion as data; the pipeline starts the changed apps locally, runs the cases against the **shared dev database** and deletes exactly the rows each case created. Failures go back to the coder; passing cases become **real evidence** for the review.

**Review** (default: local, before any merge request exists)
- A Reviewer reads each repo's changes with that repo's `REVIEW.md`, checks **every acceptance criterion with evidence**, and runs one cross-repo **contract check**.
- Findings are numbered; blockers/majors are fixed automatically in a loop with stall, dispute and round limits; people can choose, dismiss (with a reason) or accept.
- **Approval is evidence-bound**: every criterion met with proof, every chosen finding confirmed fixed, and tied to a commit.
- Only after approval does the pipeline rebase, push and open the merge requests, with one review summary comment and the Reviewer's GitLab approval each. (`REVIEW_MODE=gitlab` keeps the earlier flow: MRs first, findings as threads.)

**Delivery**
- One **deployment note per ticket**: deploy order, new env vars, migrations, infrastructure, rollback (only for migrations), risk; attached to Jira and summarised in the terminal. DevOps acts on it before the merge.
- The pipeline watches GitLab for the merges (people merge), detects people's own commits on its branches, and supports `change` requests while merge requests are open.

**QA loop**
- At RFT the Coder writes copy-paste **test notes** (cURLs with placeholders) on Jira.
- QA files **each bug as its own Jira ticket**; `fix KEY DEMO-13 DEMO-14` turns them into numbered bugs and starts a QA round (new branch, plan, code, review, merge request, deployment note) until **QA Passed**.

**Control and operations**
- Gates (manual or automatic) at plan approval, review, fix, re-review, deployment note, QA fix.
- `stop`, `retry`, `guide`, `accept`, `pause`/`unpause`, `reset`, `status`, `report`, `test`; Jira comment commands (`/stop`, `/retry`, `/guide`).
- Full audit trail on Jira (statuses, plan versions, a delivery summary, QA comments), all secrets masked everywhere, restart recovery, cost/time/token reports, per-ticket logs and transcripts.
- Dry-run mode runs the whole flow on fakes in seconds.

## 3. Architecture

```
 Terminal (`pipeline cli`, `pipeline say`) ─┐
 Jira comments (/stop /retry /guide) ───────┼─► Command bus ─► start / resume a ticket's graph run
 Pollers: GitLab merges, Jira status ───────┘        │
                                                      ▼
                          LangGraph workflow (one checkpointed thread per Jira key)
                           ├─ gates: wait for a person, or continue (config)
                           └─ nodes call agents through the agent pool
                                                      ▼
        Agent pool: Coder-A/B/… · Reviewer-A/B/… · Deploy-A/B   (provider: claude | codex)
                                                      │
 Outputs: terminal (ChatPort) · Jira (status, comments, attachments) · GitLab (branches, MRs, approval)
 State:   SQLite pipeline.db = LangGraph checkpoints + tracking tables
 Code:    $PIPELINE_HOME/workspaces/<KEY>/<repo>/   (a git worktree per repo)
```

Two layers (D87): the **orchestrator** (Python, deterministic) owns the workflow, every external
write, limits and state; the **agents** (LLM runs) only do the thinking and the code, and write their
results as JSON files the orchestrator validates. A tiny `summary` agent run only writes wording.

| Component | Does | Never does |
|---|---|---|
| **Orchestrator** (Python + LangGraph) | Runs the graph, handles commands, makes every Jira/GitLab write, tracks state, enforces limits | Write product code |
| **Terminal front-end** (`pipeline cli`) | Reads exact commands, shows each ticket's messages and cards, asks `[y/N]` | Interpret free text |
| **Planner** (read-only) | Tasks, interface contract, order, criteria mapping | Edit code |
| **Coder** | Implements per repo standards and the contract; tests; fixes chosen findings; resolves rebase conflicts | Push, merge, change CI/secrets, deviate from the contract silently |
| **Reviewer** (read-only) | Per-repo review + contract check; confirms fixes | Edit code |
| **Tester** (read-only) | Writes live acceptance cases as data | Run calls itself or touch rows it did not create |
| **Deployment writer** (read-only) | Deployment note; QA test notes | Change infra or env |
| **Summary agent** (a tiny Claude/Codex run) | Writes merge-request description sentences | Decide, run commands, call Jira/GitLab, read repos |

**Tech stack:** Python 3.11+ with uv; LangGraph + SQLite checkpointer (no LangChain); Claude Agent SDK
and Codex CLI as agent providers; SQLAlchemy Core + SQLite (Postgres later); pydantic v2;
`atlassian-python-api`, `python-gitlab`, Google API client; aiohttp (local endpoint); APScheduler
(pollers); typer + rich + prompt_toolkit (CLI); structlog; ruff.

## 4. The workflow (one LangGraph thread per ticket)

```
implement → load_context ─(missing spec/repos/criteria)→ tell the user, stop
          → plan ─(questions)→ needs_human
          → [gate plan_approval] ── revise → plan again
          → env_check [testing=true]  (tools, dev DB reachable + allowlisted, env, apps boot)
          → code ⟲ verify              (every repo; retry on failures; replan once on contract deviation)
          → acceptance [testing=true]  (live tests; failures → code again; migration missing → wait)
          → start_review  (local mode)   or   open_mrs  (gitlab mode)
          → [gate review] → review ──approve──────────────────────────┐
                              │ changes requested                      │
                              ▼                                        │
                       [gate fix] → fix → code/verify → push_fixes     │
                              │ (local: commit only)                   │
                       [gate re_review] ──► review ◄───────────────────┘
          → approve → publish_mrs (local mode: rebase, push, open MRs, summary, approval)
          → [gate deploy_doc] → deploy_doc (note → Jira + terminal)
          → wait_merge ⏸  (people merge; polls GitLab)
          → post_qa_notes (Jira RFT + test notes)
          → wait_qa ⏸ ──QA Passed──► qa_done
                 │ QA Failed + `fix KEY DEMO-13 …`
                 ▼
          qa_fix (new branch …-qa<n>) → plan (qa_fix) → … the same loop → post_qa_notes
          (any step can end in needs_human: a message says why; retry / guide / stop / accept)
```

**Gates** are `manual` (post a card, wait for a command) or `auto` (continue). Defaults:
`start` manual · `plan_approval` manual · `review` auto · `fix` auto · `re_review` auto ·
`deploy_doc` auto · `qa_fix` manual. Merging and QA Failed / QA Passed are always human.

**Jira statuses** (D127): To Do (person) → **In Dev** (pipeline, from the plan to the last merge) →
**RFT** (all merged); **Blocked** when it needs a person; **Testing**, **QA Failed**, **QA Passed**
are set by QA. If a person moves the ticket out of the expected status, the thread halts
(`moved_by_human`): Jira is the source of truth.

**Wrapper and routing.** Every node runs inside a wrapper (`graph/wrapper.py`) that records events,
honours stop/pause, converts exceptions into a halted state, and routes every halted state to
`needs_human`, which posts what is wrong and waits for `retry`, `guide`, `accept` or `stop`.

## 5. Agents

**Providers.** Every agent run goes through one runner (`agents/runner.py`) that dispatches to the
`claude` provider (Claude Agent SDK driving Claude Code) or the `codex` provider (`codex exec`). The
orchestrator never calls a model API directly. Auth is the user's **subscription** by default
(Claude's monthly Agent SDK credit; the Codex login) or an **API key** per provider. In subscription
mode the runner strips API keys from the agent's environment. A provider usage limit retries once on
`fallback_provider`, else stops for a person.

**Roles and pools.** `planner`, `coder`, `coder_fix`, `coder_rebase`, `coder_ticket`,
`coder_ticket_fix` → coder pool; `reviewer`, `reviewer_ticket`, `contract_check`, `tester` → reviewer
pool; `deployment`, `qa_notes` → deploy pool. A **slot** (Coder-A, Reviewer-B, …) is held only while an
agent runs, preferred again on resume (sticky); the number of slots is the concurrency limit; if all
are busy the ticket queues and says its position. Model, effort, max turns and timeout are per role.

**Contract with every agent.** The same prompts for both providers (`prompts/*.md`); the agent writes
its result to `.agent/*.json`, validated by pydantic (`schemas.py`); invalid results are repaired or
retried. Read-only roles may write only `.agent/`: a git-status check enforces it whatever the
provider. Claude runs use tool allow/deny lists; Codex runs use sandbox modes.

**Single session (D200).** By default one coder run covers all repos of the ticket (it runs each
repo's checks from that repo's folder) and fixes findings in the same conversation; one reviewer run
reviews all repos plus the contract. The ticket's inputs (plan, interface, ticket, spec sections,
each repo's standards, files and tests) are in the prompt once; nothing is copied per repo. This cut
duplicate files and tokens. `single_session: false` returns to one run per repo, in parallel.

## 6. Planning and the interface contract

The Planner receives the ticket, the relevant sections of the spec (found by names in the ticket),
prepared maps of each repo (routes, existing helpers) and writes an `ImplementationPlan`:

- a **brief** (preconditions and every acceptance criterion with its id, in the ticket's words; criteria the planner missed are completed or assigned),
- **tasks** per repo (steps, files to add or edit, which criteria each satisfies, which endpoints it implements or consumes), in coding order (service → gateway → frontend),
- an **InterfaceContract**: internal endpoints (service side) and public endpoints (gateway side) with request/response JSON Schemas, error cases, auth, and the gateway's error mapping,
- risks, open questions, external calls (for live-test fixtures).

Each coder gets its repo's task plus **its part of the contract only**. Services implement it
exactly; the gateway calls it exactly and mocks it in tests from the schemas. Re-planning modes:
`revise` (a person's feedback), `replan` (contract deviation), `qa_fix` (bugs), `change` (a person's
change request). The plan is shown on Jira as one comment per version.

## 7. Coding, checks, and the review

### 7.1 Coding and verify

- Worktrees: one git worktree per repo on branch `ai/<KEY>-<slug>` from the latest default branch (QA rounds: `…-qa<n>`), under `$PIPELINE_HOME/workspaces/<KEY>/`. The base clone is shared and cached with `node_modules`.
- The coder does not run `git commit`: it writes a **commit plan** (`[{message, files}]`, one entry per module with its tests); the pipeline makes one commit per entry and one for the rest (`type(KEY): what`).
- **Verify** (`verify.py`) per repo: install if needed, lint on changed files, tests, extra commands, secret scan, protected-file check, all with a restricted environment (`safe_env`). Failure → back to the coder (limited attempts).
- **Rebase before any push (D125).** The branch is rebased onto the latest default branch (a Coder run resolves conflicts); checks re-run if it moved; the push uses `--force-with-lease` pinned to what was seen, so people's commits on the branch are kept.

### 7.2 The review in detail

Review runs on the branch's diff against the default branch (local mode) or the merge request's diff
(gitlab mode). The Reviewer returns, per repo and for the contract, a `ReviewResult`: verdict,
findings (`blocker | major | minor | nit | human`), and a check of **each acceptance criterion**
(`met` with evidence, `not_met`, `unknown`, `not_applicable` with reason).

The orchestrator (`graph/nodes/review.py`, `settle_round`) then settles the round:
- findings people chose to fix go back to the reviewer, who must answer `fixed`, `still_open` or `withdrawn`; an unanswered one stays open;
- findings deferred or dismissed by a person are carried or closed with the person's words;
- unmet criteria and failing checks become orchestrator findings (`A-…`), a bare veto becomes a major;
- later rounds review only what changed and may raise new findings only there (a blocker anywhere);
- the contract check re-runs only when something that can change how repos fit together changed.

**Approval is evidence-bound (P9):** a repo approves only when every criterion is met with evidence
and every chosen finding is confirmed fixed; the approval names the reviewed commit; any new commit
sends it back to review. A criterion proven by a live test counts as met with real evidence while
the tested files are unchanged.

**Loop control:** blockers/majors fixed automatically (`chat.default_fix_scope`); stop and ask a
person after `max_review_rounds`, two fix rounds that close nothing, a finding disputed twice, or a
finding that needs a change to shared code. `accept` is the human override (recorded).

### 7.3 Local review vs GitLab review (D207)

| | `REVIEW_MODE=local` (default) | `REVIEW_MODE=gitlab` |
|---|---|---|
| Review happens | On this machine, before anything is pushed | On open merge requests |
| Findings live in | Ticket state, Jira, the terminal | GitLab threads (inline comments) |
| Fixes | Committed locally | Pushed; coder replies to threads, reviewer resolves them |
| Merge requests open | After approval: `publish_mrs` rebases, pushes, opens them cross-linked, posts one review summary note each and the Reviewer's approval | Before the review (`open_mrs`) |
| Rebase changes the code after approval | Compared patch by patch (blob ids and line numbers ignored): same patch → approval carries to the new commit; else back to review | n/a (rebase happens before each push) |
| Later changes (people's commits, `change`) | Use GitLab threads, since the MRs exist then | Same |

The mode is recorded per ticket when it starts (old tickets without it keep the GitLab flow).

## 8. Live acceptance tests (P10)

`acceptance.mode` = `off` | `advisory` | `required`; a ticket opts in with `testing=true`.

1. **Preflight (`env_check`)**: tools (`node`, `redis-server`, `openssl`), env, the dev DB reachable and allowlisted, each app boots once.
2. **Cases**: contract cases (the pipeline's) plus the Tester's cases per criterion: seed rows, HTTP calls, expected status/JSON, read-only DB checks. Fixtures, lookups (reference data the dev DB must have: missing → case blocked, not failed), date templates, "own partner" identities.
3. **Run**: the repos' apps start as local processes with a private TLS Redis and mocked external services; calls are made as seeded identities.
4. **Cleanup**: every delete is by primary key from the run's ledger (children first, one transaction, then checked). No TRUNCATE, no DDL; PUT/PATCH/DELETE may only target a row the case created; agent-written SQL runs in READ ONLY transactions; writes to real external services are refused.
5. **Outcomes**: failed cases go to the coder (a case that is itself wrong, `invalid`, goes back to the Tester); repeated failure goes to a person (`accept` holds only for the tested commits). A ticket whose migration is not on the dev DB waits; only a person's `pipeline migrate KEY` applies it.
6. Live evidence is pinned to the tested code; the Reviewer is told not to re-verify those criteria.

## 9. Delivery, merging and QA

- **Deployment note** (`agents/deployment.py`, `analysis/devops_facts.py`): facts are computed from the diffs (new/removed env vars, undeclared ones, migrations, infra files, new dependencies); the agent writes one note for the whole ticket with the deploy order and risk. DevOps' steps happen before the merges.
- **Merge wait** (`waits.py`): polls GitLab; the card is edited per merge; closed-without-merge and people's commits are noticed; `change` and `fix` are accepted meanwhile.
- **QA notes**: test cases with cURLs from the changed routes, placeholders `{{QA_BASE_URL}}` / `{{QA_TOKEN}}`.
- **QA rounds** (`graph/nodes/qa.py`, `bugs.py`): bug tickets → numbered bugs (kept for the life of the ticket; statuses open → fixing → fixed) → new worktrees on `…-qa<n>` → `plan` in `qa_fix` mode (affected repos only) → the same loop → "Resolved: #1, #2" on Jira.

## 10. Chat front-end, commands and the summary agent

All inputs go through one **command bus** (`chat/commands.py`): the terminal, Jira comments and the
pollers. A command is accepted only if it matches what the ticket is waiting for; otherwise the reply
says what it waits for (`status`). `accept`, `stop` and `pause` always need confirmation. Every
command (who, when, accepted or why not) is stored in the `commands` table.

- **Terminal** (`terminal.py`): prompt_toolkit + rich. Runs the pipeline in-process, or attaches to a `pipeline serve` server through a localhost-only HTTP endpoint (`server.py`). Messages carry the speaker (Coder, Reviewer, DevOps; names in `chat.agents`).
- **Notifier** (`chat/notifier.py`, `delivery.py`): one place that turns events into terminal messages and Jira comments; idempotent; `verbosity: minimal` posts only what people need.
- **Summary agent** (`chat/llm.py`, D209): the merge request description is written by a tiny agent run (role `summary`, Claude Haiku by default or Codex, outside the pool, about 6 seconds). It never runs commands, decides, reads repos or sees secrets; prompts are masked; code is only included if `chat_llm.send_code: true`. Each run is logged (provider, model, latency, tokens, never text). If it fails a template is used.

## 11. State, persistence and recovery

- **One SQLite file** `$PIPELINE_HOME/pipeline.db` (WAL): LangGraph **checkpoints** (the full `TicketState` per thread: how to resume) plus **tracking tables** (what happened): `tickets`, `ticket_mrs`, `events`, `agent_runs`, `commands`, `agent_slots`, `chat_threads`, `ticket_notes`. If they disagree, checkpoints win. Postgres is a planned swap.
- **Idempotency**: every Jira/GitLab write carries a hidden marker (`aipl:KEY:node:round:extra`) and is skipped if it exists; status changes check first; MRs are looked up before creating.
- **Restart recovery**: steps not started are dropped, agent child processes killed; each cut-off step resumes once, with a message; cut off twice → needs a person.
- **Intake** (spec link, repos) is kept as a ticket note so `retry` and restarts reuse it.

## 12. Guardrails (summary; the full numbered list is CLAUDE.md §9)

Never merge; never push to a default branch (own branches only, lease-pinned); coder and reviewer are
separate GitLab identities; coder edits only its worktrees and not protected files; read-only roles
write only `.agent/`; the pipeline re-verifies every agent claim; agents never see production
credentials or the pipeline's tokens; everything posted and every transcript is masked; repo commands
run with an environment allowlist and git without hooks; limits and stall rules bound every loop;
live tests touch only their own rows on allowlisted hosts and never apply migrations; approval is
bound to a reviewed commit; human overrides are recorded in state, the database, the MR and Jira.

## 13. Configuration model

| Where | What | Why |
|---|---|---|
| `.env` | Secrets and every value that differs per environment: Jira site/project, GitLab host and per-repo project paths, per-repo developer `.env` path, DB host allowlist, run values, `REVIEW_MODE` | Never committed; the same config file works on every machine |
| `pipeline.config.yaml` | Repos and their commands, agents/roles/models, pool, gates, limits, chat, summary agent, git, polling, QA placeholders, live-test stack | Reviewed, explained field by field in `pipeline.config.example.yaml` |
| The product repos | `CLAUDE.md`/`AGENTS.md`, `REVIEW.md`, `DEPLOYMENT_TEMPLATE.md` | Rules the agents and reviewer follow live with the code |

`apply_env` fills the config from the environment (D193) and names any missing variable. The config is
strict: unknown keys are an error.

## 14. Code layout

```
pipeline/
  cli.py terminal.py server.py     entry points: shell commands, the interactive terminal, the headless server
  config.py schemas.py             configuration model; every data contract (pydantic)
  context.py intake.py             ticket context (Jira + spec + intake); the spec-link / repo questions
  graph/                           build.py (the graph), nodes/*.py (one file per phase), gates.py, wrapper.py, state.py, runtime.py, checkpoint.py
  agents/                          runner.py (one entry for all agents), providers/ (claude, codex, fake), pool.py (slots),
                                   planner.py coder.py reviewer.py tester.py deployment.py qa_notes.py, roles.py
  chat/                            commands.py (grammar + bus), notifier.py, delivery.py, cards.py, console_chat.py, summaries.py, llm.py
  integrations/                    jira_client.py gitlab_client.py gdocs.py, base.py (protocols), fakes.py (dry run)
  testing/                         live acceptance tests: preflight, stack, mock_server, db guards, cases, executor, identities, migrations, service
  analysis/                        spec_excerpt, repo_map, express_routes, devops_facts (inputs prepared for the agents)
  store.py workspace.py verify.py  tracking tables; git worktrees; the pipeline's own checks
  findings.py acceptance.py bugs.py  finding ids/numbers/scopes; criteria evidence; QA bugs
  events.py pause.py reminders.py maintenance.py report.py logs.py environment.py safe_env.py  pollers, pause, gc/demo-reset, reports, machine-problem detection
prompts/                           one markdown prompt per role
docs/                              SETUP, GUIDE, PROJECT, PROGRESS (decisions, known issues), DESIGN (history)
```

Runtime data: `$PIPELINE_HOME` (see SETUP §2). Conventions (CLAUDE.md §6): type hints; nodes take
`(state, deps)` and return only changed keys; dependencies come through a `Deps` container so
`--dry-run` swaps in fakes; no network at import; handlers acknowledge fast and never run an agent;
every external write is idempotent; agent failures return `ok=False`, never raise into the graph;
**nothing people read is ever truncated** (long Jira texts are split into parts, D203).

## 15. How the project itself is checked

There are no unit or graph tests (D201). The checks are `uv run ruff check .`, the **dry run**
(`uv run pipeline run <KEY> --dry-run`: the whole workflow on fake Jira/GitLab/Google and scripted
agents with real local git remotes, in seconds, both review modes), and on real tickets the live
acceptance tests and the Reviewer.

## 16. Limits and not-yet

- Frontend (Vue) repositories are not supported yet (the prompts and checks for them are deferred).
- Jira comment commands (`/stop` …) are not allowlisted: anyone who can comment can use them.
- SQLite only (one server); Postgres needs a small code change.
- Bug splitting from typed text is a simple rule; real bugs come from tickets.
- Live-test coverage depends on dev-DB reference data and on external fixtures; criteria that cannot be tested locally are reported as blocked, with the reason.
Known issues, backlog and measurements: [PROGRESS.md](PROGRESS.md).

## 17. Glossary

- **Thread**: one LangGraph run, keyed by the Jira key. **Gate**: a step that waits for a person (`manual`) or continues (`auto`). **Command bus**: the single entry point for commands.
- **Slot**: a named agent in the pool (the unit of concurrency). **Provider**: `claude` or `codex`.
- **Change set**: all merge requests of one ticket, advanced together. **Contract**: the planner's interface between gateway and services. **Round**: one review pass or one QA return.
- **Verify**: the pipeline's own lint/tests/secret scan. **Marker**: the hidden tag that makes a posted comment idempotent.
- **Local review**: the Reviewer works on branches before any merge request exists. **Evidence-bound approval**: approval only with proof for every criterion.
