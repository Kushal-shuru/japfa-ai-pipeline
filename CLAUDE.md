# AI SDLC Pipeline — Master Context

> Single source of truth for **what we are building and how**.
> Lives at the pipeline repo root; Claude Code loads it automatically every session.
> Then read `docs/PROGRESS.md` (current state, decisions, known issues, backlog).

---

## 1. What we are building

An AI pipeline that takes a Jira ticket to a set of **approved, deploy-documented, QA-ready GitLab MRs** — one MR per repo the ticket touches. The team drives it from the **terminal** (`pipeline cli`, exact commands such as `implement DEMO-12`, `approve`, `fix #1 #3`). Humans keep control of which tickets the AI takes, the plan, which review findings to fix, merges, infrastructure changes and QA sign-off.

**Product architecture the agents work on:** microservices over REST.

- **API gateway repo** (Node + Express): public routes, auth, request validation schemas, calls internal services over REST, maps their errors to public responses.
- **Service repos** (Node + Express): business logic and DB operations; internal REST endpoints the gateway calls.
- **Frontend repo** (Vue.js): consumes gateway endpoints.

A typical backend ticket touches **the gateway + one or more services**. Every repo has its **own** standards (`CLAUDE.md`, identical `AGENTS.md`) and review checklist (`REVIEW.md`).

**Components:**

| Component | Does | Never does |
|---|---|---|
| **Orchestrator** (this repo: Python + LangGraph) | Runs the workflow graph, handles commands, makes every Jira/GitLab write, tracks state in SQLite, enforces limits | Write product code |
| **Terminal front-end** (`pipeline cli`: exact commands, template wording, D167) | Receives commands, posts progress and cards into one section per ticket. One bot with a **team inside** (D111): every ticket message and card is spoken by a named agent — **Coder** (plans, codes, fixes, change requests) and **Reviewer** (reviews, accept); **DevOps** (deployment note, DevOps steps); QA notes are the Coder's. Names in `chat.agents`; people address them by name (`reviewer review DEMO-3`, `coder fix #1`) | Decide anything itself |
| **Summary agent** [D209] (a tiny Claude / Codex run, role `summary`, outside the pool) | Writes short plain-language texts (the merge request description) | Run commands, call Jira/GitLab, read repos, decide |
| **Planner Agent** (read-only, all ticket repos) | Per-repo tasks, **interface contract** between gateway and services, coding order | Edit code |
| **Coder Agent** (one run per repo, inside that repo) | Implements the repo's task per its standards and the contract; tests; fixes chosen findings | Push to default branch, merge, change CI/secrets, deviate from the contract silently |
| **Reviewer Agent** (read-only) | Reviews each MR with that repo's `REVIEW.md`, plus one cross-repo **contract check** | Edit code |
| **Tester Agent** [P10] (read-only, reviewer pool) | Writes the **live acceptance cases** per criterion as data (seed rows, HTTP calls, read-only DB checks); the orchestrator runs them against the ticket's apps on this machine and the shared dev DB | Run calls itself, touch rows its cases didn't create |
| **Deployment Agent** (read-only) | One deployment note per ticket (all repos: deploy order + env vars, migrations, infrastructure; rollback only for migrations); QA handoff | Change infra or env |

Two layers: the **orchestrator** talks to people in templates; **Claude/Codex agents** are the engine (plan, code, review, docs, and a tiny `summary` run for short plain texts, D209). Agents run as named **slots** in an agent pool (Coder-A, Coder-B, Reviewer-A, …), each on a **switchable provider**: **Claude** (Claude Agent SDK) or **Codex** (`codex exec`), with subscription auth by default (§4.1).

Full rationale: `docs/DESIGN.md` (written early; where it disagrees with this file or with a decision in `docs/PROGRESS.md`, **this file and the decisions win**). Setup: `docs/SETUP.md`; using it: `docs/GUIDE.md`; what it is and how it is built: `docs/PROJECT.md`.

---

## 2. Session protocol (every session)

1. Read `CLAUDE.md` (this file) → `docs/PROGRESS.md` (Current state, Decisions, Known issues, Backlog).
2. Check what's already ticked; run `ruff` and `pipeline run <KEY> --dry-run` to confirm. Don't redo finished work unless PROGRESS.md says it's broken.
3. Before coding, give a short plan for this session and wait for the user's go-ahead.
4. Work task by task. After each task: run ruff and the dry run.
5. Any deviation from this file or from an earlier decision → record it under **Decisions** in PROGRESS.md immediately.
6. Contracts in §7 and tables in §8 are fixed. To change one: stop, ask, then update this file and log it.
7. Only work on what was asked. Ideas for later → **Backlog** in PROGRESS.md.
8. Verify library/SDK/CLI APIs against current docs, not memory.
9. End of session (or when context gets long): wrap-up — PROGRESS.md session log, status, known issues, **exact next step**.

---

## 3. Architecture

```
 Terminal (`pipeline cli`, `pipeline say`) ─┐
 CLI (`pipeline cli`, `pipeline say`) ────┼─► Command bus ─► start / resume ticket thread
 Jira comments (/stop, /retry, /guide) ───┤        │
 Pollers: GitLab merges, Jira QA status ──┘        ▼   (bugs = one Jira ticket each, given to `fix`)
                                          LangGraph workflow (one thread per Jira key)
                                           │   ├─ gates: wait for a human, or continue (config)
                                           │   └─ nodes call agents through the agent pool
                                           ▼
     Agent pool: Coder-A/B/C · Reviewer-A/B · Deploy-A   (provider: claude | codex)
                                           │
     Outputs: ChatPort (console) · Jira (status + audit comments) · GitLab (MRs, reviews)
              (short plain texts by the `summary` agent, D209; structured details from the pipeline)
     State:   SQLite pipeline.db = LangGraph checkpoints + tracking tables
     Code:    $PIPELINE_HOME/workspaces/<KEY>/<repo>/  (git worktree per repo)
```

### 3.1 Workflow graph (one thread per Jira key)

```
[start]  manual: "implement DEMO-12" in the terminal
   ▼
load_context ─(missing spec / repos / AC)→ tell user (chat + Jira) → END
   ▼
plan ─(needs clarification)→ post questions → needs_human
   ▼
[gate plan_approval] ── revise(feedback) → plan (revise) → gate again
   │ approve                               stop → needs_human
   ▼
env_check [P10] (tools, dev DB reachable + allowlisted, env, apps boot) ─(problem)→ needs_human, before any coding
   ▼
┌─► code_next_repo  (every pending repo at once, D129; each coder sees only its repo + the contract)
│      verify (one repo per pass, services → gateway → frontend; retry once) ─(fail)→ needs_human
│             (a machine problem — Redis down, disk full — → needs_human at once, no retry, D134)
│      contract deviation → replan (max 1)
└──────┘ more repos pending?
   ▼
acceptance [P10] (live: contract cases + the Tester's per criterion; cleanup of exactly the run's rows)
   │ failed → code_next_repo, for the case's repo and the services it calls (each fixes only its own
   │          fault; all say "not mine" → needs_human) (max limits.max_acceptance_attempts, then needs_human; `accept` allowed)
   │ a migration not on the dev DB → migration_wait ⏸ (Jira Blocked; poller / `test`) → acceptance
   ▼                                (fix passes: acceptance → push_fixes)
open_mrs  (rebase onto the latest default branch first, D125; one MR per repo, cross-linked)
          — review_mode gitlab (the old flow). With review_mode local (default, D207) there is no
          open_mrs here: start_review only hands the local branches to the review, and the MRs
          open in publish_mrs after the Reviewer approves (see below)
   ▼
[gate review] ──► review  (per-repo + contract check)
                    │ all approve                       │ changes requested (round < max)
                    ▼                                   ▼
              approve (all MRs)                  [gate fix] ── fix all | blockers | major+ | selected ids
                    ▼                                   │     accept (human override) → approve
              [gate deploy_doc] → deploy_doc            ▼     stop → needs_human
                (one note per ticket → Jira + the terminal)
                    ▼                           fix + verify + rebase + push
              wait_merge ⏸ (all MRs merged; humans merge in GitLab;   ▼
                    ▼         DevOps works before the merges, D124)  [gate re_review] → review
              post_qa_notes (RFT: QA notes; after a QA round "Resolved: 1, 2" on Jira)
                    ▼
              wait_qa ⏸ ──(Jira QA - Passed)→ qa_done → END
                    │ QA files each bug as its own Jira ticket + moves it to Blocked
                    ▼
              [gate qa_fix] `fix KEY DEMO-13 DEMO-14` (the bug tickets) → qa_fix (new branches …-qa<n>)
                    → plan (qa_fix) → [gate plan_approval] → code loop → open_mrs
                    → review (auto in QA rounds) … → merge → post_qa_notes
```

**Local review (D207, default `git.review_mode: local`)**: `acceptance → start_review → [gate review] → review ⟲ fix → approve → publish_mrs → [gate deploy_doc] …`. The Reviewer reads each branch against its default branch from git; findings live in the ticket state, Jira and the terminal, not in GitLab threads; the coder's fixes are committed locally (nothing is pushed). `publish_mrs` rebases onto the latest default branch, pushes, opens the cross-linked MRs, posts one summary note per MR (rounds, what was raised and fixed, what stayed open, acceptance criteria) and applies the Reviewer's approval. Changes requested later (`change`, people's commits) use GitLab threads as before, because the MRs exist by then. `REVIEW_MODE=gitlab` keeps the old flow (open_mrs first, findings as threads).

`[gate x]` = configurable (§3.5). ⏸ = always waits for an external event. Rounds exhausted anywhere → needs_human.

### 3.2 Jira statuses

| Status | Set by | Next |
|---|---|---|
| To Do | Human | In Development |
| In Development | Orchestrator (load_context; a QA round's `fix KEY DEMO-13 …`) — the whole way from the plan to the last merge: plan approval, coding, MRs, review, fixes, deployment note, merge wait (D127) | RFT, Blocked |
| Blocked | Orchestrator — only when it needs a person to unblock it (a failed step, questions, a limit reached, a stop; P10: the ticket's migration isn't on the dev DB yet); `retry` / `guide` put the previous status back | the previous status |
| Ready for Test | Orchestrator (all MRs merged; DevOps did its part before the merges, D124) | Testing, Blocked, QA - Passed |
| Testing | **Human (QA)** when they start testing | Blocked, QA - Passed |
| Blocked (QA failed) | **Human (QA)** after filing each bug as its own Jira ticket (orchestrator too, for bugs typed in the terminal): the board has no QA Failed status | In Development (`fix KEY DEMO-13 …`) |
| QA - Passed | **Human (QA)** | — |

Status names come from config. Jira keeps the audit trail even though the conversation happens in the terminal: with `chat.verbosity: minimal` (the default, P9) one comment per **plan version** (never edited, so earlier plans stay readable), one **delivery summary** comment per ticket edited in place (status, current plan, merge requests, deployment note, every decision people made), plus the QA comments; with `normal`, a comment for every decision and step.

### 3.3 Commands

All inputs go through one **command bus** (`chat/commands.py`). The terminal and Jira comments are just different front-ends.

**One front-end** (D167; Teams was removed in D204): `pipeline cli`, the interactive terminal: exact commands only, a leading `/` allowed, `[y/N]` confirmations, template wording (only the merge request description is written by the `summary` agent, D209). The list was shortened in D168: older names (`approve plan`, `revise plan`, `change`, `fix bugs`) still parse but aren't shown.

| Command (`pipeline cli`, or `pipeline say "…"`) | Button | Effect |
|---|---|---|
| `implement DEMO-12 [--spec LINK --repos a,b] [--provider codex] [--slot Coder-B] [testing=true]` | — | Start a ticket (start gate). In `pipeline cli` it asks for the tech spec link (checked: a readable Google Doc) and the repositories (tick-box list from the config); elsewhere give `--spec` and `--repos` (D202, §3.4). `testing=true` [P10]: include the preflight and the live acceptance tests for this ticket (default: off since 2026-10-21, D205; `acceptance.mode` still decides how they count) |
| `approve [DEMO-12]` (also `approve plan`) | Approve plan | Pass plan_approval gate |
| `review [DEMO-12]` | Start review | Pass review / re_review gate |
| `fix [DEMO-12] [blockers \| major \| minor \| all \| #1 #3 \| <finding ids>] [dismiss #2] [: notes]` | Fix blockers · Fix major+ · Fix all · Fix selected… | Pass fix gate with a scope (default from config; `major` = `major+`, `minor` = all but nits, D168) or the findings card's `#n` numbers; notes go to the coder; only `dismiss #n` (or a sentence the Chat LLM reads as "#n is fine / not needed") records a finding as rejected by a person, with their words (P9: a number merely mentioned decides nothing). **After approval, before the first merge** (D168): a scope or numbers send the findings the review left open back into the fix loop (re-review, approve again); a bare `fix` = all of them; words only = `change`. **In testing** (D126): `fix 1 2` / `fix all` picks the QA bugs by their Jira numbers (qa_fix gate) |
| `accept [DEMO-12]` | Accept as is | Human override: proceed without fixing remaining findings (confirmation required, logged) |
| `deploy-doc [DEMO-12]` | Write deployment note | Pass deploy_doc gate |
| `bug DEMO-12: <description>` | — | Record a QA bug by hand (QA normally files each bug as its own Jira ticket and gives them to `fix`; D126, D206) |
| `status [DEMO-12]` | — | One ticket, or all in-flight tickets + slot usage |
| `stop [DEMO-12]` (Jira: `/stop`) | Stop | Halt → Needs Human (confirmation required) |
| `retry [DEMO-12]` (Jira: `/retry`) | Retry | Re-run the last failed step |
| `guide [DEMO-12]: <text>` (Jira: `/guide …`) [P4b] | Request changes | A person's input, by state (D168): at plan approval = revise the plan; at Needs Human = guidance, then the step reruns; while MRs are open = `change` |
| `change [DEMO-12]: <request>` [P5] (shown as `guide`) | — | Human change request while MRs are open: small → human findings on the same MRs (fix gate); contract-affecting → replan (plan approval); then review again. Up to `max_plan_revisions` change requests per ticket, refused at once after that; they don't count against `max_replans` (D131) |
| `test [DEMO-12]` [P10] | Test again | Run the live acceptance tests again: at the migration wait (the migration is on dev now), or after the tests stopped (= `retry`) |
| `help` | — | Command list (CLI mode: every exact command, grouped) |
| `reset DEMO-12 [--full]` (terminal only, D167) | — | Forget the ticket locally (= `pipeline reset`); `--full` = `pipeline demo-reset` for it. `[y/N]`, refused while a step runs |
| `report` [P8] | — | Pipeline report card: tickets, time waiting on people, rounds, runs, cost; path to `$PIPELINE_HOME/report.html` |
| `pause[: reason]` / `unpause` [P8] | Confirm | Whole pipeline (never from Jira): nothing new starts; tickets wait and carry on after `unpause` (D138) |

Rules:
- Inside a ticket's thread the key can be omitted (CLI: always name it). A leading `/` is fine everywhere.
- People may address a team member by name first (`@Reviewer review DEMO-3`, `coder, fix #1`; names from `chat.agents`, D111). Every command has an owner (Coder: implement, plans, guide, retry, change, fix; Reviewer: review, accept) who answers it; asking the wrong member still runs it, with a short hand-off. `status`, `help`, `stop`: anyone.
- **Exact commands only.** There is no free-text interpretation: a mistyped command gets the exact form (`commands.cli_hint`). `accept`, `stop` and `pause` always confirm.
- A command is accepted only if it matches what the ticket is waiting for; otherwise the bot replies with the current state and what it is waiting for.
- Jira `/stop`, `/retry`, `/guide` stay supported for people outside the terminal. QA files each bug as its own Jira ticket, with the bug in its description (D206). `devops-done` is retired (D124: DevOps works before the merges; the command only explains that).

### 3.4 The tech spec and which repos a ticket touches (D202)

1. **The person who starts the ticket gives them.** In `pipeline cli`, `implement KEY` asks "Please paste the tech spec link": the link must be a Google **Doc** the pipeline can read (`docs.google.com/document/d/<id>`), otherwise it prints "Cannot implement KEY: <reason>" and does not start; then it shows the config's `repos` as a tick-box list (the pre-defined options come from `pipeline.config.yaml` / `.env`). Anywhere else: `implement KEY --spec <link> --repos a,b` (same checks). The answer is kept as the ticket note `intake`, so `retry` and restarts reuse it, and `reset` deletes it.
2. Fallback for tickets that still carry them: the Jira custom fields (`jira.repos_field` / `jira.spec_field`, both **optional**), `Repos:` / `Spec:` lines in the description, the epic's spec. Jira pickup (`gates.start: auto`) can only use these.
3. Nothing given and nothing in Jira, or unknown names → the pipeline says what to give and doesn't start. It never guesses.

### 3.5 Gates (manual vs automatic steps)

Each gate is `manual` (post a card with buttons in the ticket thread, mirror "waiting for …" on Jira, then pause) or `auto` (continue immediately). Defaults:

| Gate | Default | Why |
|---|---|---|
| `plan_approval` | manual | Cheapest point to catch a wrong approach |
| `review` | auto (P9; D87 made it manual) | The review starts when the MRs open; approval needs every acceptance criterion met and every chosen finding confirmed fixed (P9) |
| `fix` | auto (P9) | Blockers and majors (`chat.default_fix_scope`) are fixed without asking; `manual` = people choose |
| `re_review` | auto (P9) | The fixes are reviewed again until the review passes, two fix rounds in a row make no progress, or `max_review_rounds` → Needs Human |
| `deploy_doc` | auto | Always needed after approval |
| `qa_fix` | manual | Humans give the bug tickets (`fix KEY DEMO-13 DEMO-14`; `fix KEY` asks for them); `auto` = all open typed bugs after 5 quiet minutes |

Live acceptance tests [P10] are not a gate (and a ticket opts in: `implement KEY testing=true`; off by default, D205): `acceptance.mode` is `off` (skipped), `advisory` (run and report, never block) or `required` (a failing case goes back to the coder, then to a person). Always human, not configurable: merging (in GitLab), QA Failed / QA - Passed (in Jira). In a QA round the `review` and `re_review` gates run automatically (D126).

**Automatic review loop** (D112): with `review`, `fix` and `re_review` all `auto`, the team talks to itself in the ticket thread — Coder: "@Reviewer, the merge requests … are ready", Reviewer: "@Coder, please fix #1 …" (scope `chat.default_fix_scope`), Coder: "@Reviewer, I pushed the fixes …" — until the review passes, `max_review_rounds` is reached or a finding is disputed twice (→ Needs Human). Each gate can also be set on its own.

### 3.6 Coding order and interface contract

- Default order from roles: `service` → `gateway` → `frontend`; the planner may reorder services that depend on each other.
- The planner writes an **InterfaceContract** (§7). Each coder run receives its repo's task + its part of the contract (the endpoints it provides or calls; D171) + the spec sections its task relies on. Services implement it exactly; the gateway calls it exactly and mocks it in tests from the contract's schemas.
- A coder that can't follow the contract stops with `contract_deviations` → one `replan` → re-code affected repos. A second deviation → needs_human.
- **One session per ticket** (`agents.single_session`, D200): the coder is one run in the ticket's folder that codes every repo in plan order and fixes review findings and failed checks in the same conversation (`coder_ticket` / `coder_ticket_fix`, result = `CoderResults`, one commit plan naming each repo); the review is one run for every repo plus the contract check (`reviewer_ticket`, result = `ReviewResults`). Inputs are in the prompt once (plan, compact interface, ticket, spec sections, each repo's standards, files, tests); no per-repo copies of the ticket, spec, task or contract are written. Branches, commits, verify, live tests, merge requests and findings stay per repo. Off = the per-repo runs above.
- **Small commits** (D169): coders don't run `git commit`; they write a commit plan `.agent/commits.json` (`[{message, files}]`, one entry per module or mini feature with its tests; one per finding in a fix run). After a successful run the orchestrator makes one commit per entry (`--no-verify`, `type(KEY): what`, ≤72 chars), then one commit for anything left. Verify still runs every check before the push.

### 3.7 Agent pool

Named slots per pool, each with a provider; slots are the concurrency limit.

```yaml
agent_pool:
  coder:    [{name: Coder-A, provider: claude}, {name: Coder-B, provider: codex}, {name: Coder-C, provider: claude}]
  reviewer: [{name: Reviewer-A, provider: claude}, {name: Reviewer-B, provider: codex}]
  deploy:   [{name: Deploy-A, provider: claude}]
```

- Roles map to pools: `planner`, `coder`, `coder_fix`, `coder_ticket`, `coder_ticket_fix` (D200), `coder_rebase` (rebase conflicts, D125) → **coder**; `reviewer`, `reviewer_ticket` (D200), `contract_check`, `tester` [P10] → **reviewer**; `deployment`, `qa_notes` → **deploy**; `intent` runs outside the pool.
- A slot is **held only while an agent is running**; it is released while the ticket waits at a gate or ⏸. On resume the ticket prefers its previous slot of that pool (sticky), else any free one.
- All slots of a pool busy → the ticket is queued and the bot posts its queue position.
- Provider for a run: command `--provider` → the chosen slot's provider → role default (§4.1).

---

## 4. Tech stack

| Concern | Choice |
|---|---|
| Python | 3.11+, managed with **uv** |
| Workflow | `langgraph` + `langgraph-checkpoint-sqlite` (no LangChain) |
| Agents | Provider per slot/role: `claude-agent-sdk` (Python) or Codex CLI (`codex exec`); subscription auth by default (§4.1) |
| Chat | The terminal (`pipeline cli`, prompt_toolkit + rich); cards render as text with the commands to type |
| Tracking DB | SQLite via **SQLAlchemy Core** (same `pipeline.db`; Postgres later) |
| Models / validation | `pydantic` v2, `pydantic-settings` |
| Jira / GitLab / Docs | `atlassian-python-api`, `python-gitlab`, `google-api-python-client` + `google-auth-oauthlib` |
| HTTP server | `aiohttp` (the localhost endpoint of `pipeline serve`) |
| Summary writer [D209] | The `summary` role through the same agent runner as every agent (Claude Haiku by default, or Codex); result `{text}` |
| Scheduling | `apscheduler` (GitLab/Jira pollers) |
| CLI | `typer` + `rich` |
| Logging | `structlog` |
| Lint / format | `ruff`; `mypy` (non-blocking). No unit or graph tests in this repo (D201): the checks are `ruff`, `pipeline run <KEY> --dry-run` and the live acceptance tests on real tickets |

### 4.1 Agent providers and authentication

The orchestrator never calls a model API directly. Every agent run goes through `run_agent()`, which dispatches to a **provider**:

| Provider | Runtime | Default auth | Optional auth | Reads repo standards from |
|---|---|---|---|---|
| `claude` | Claude Agent SDK (Python) → Claude Code | Claude subscription (usage draws on the plan's monthly **Agent SDK credit**; `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` for headless runs) | `ANTHROPIC_API_KEY` | `CLAUDE.md` |
| `codex` | `codex exec` subprocess | ChatGPT/Codex subscription (`codex login`, stored by the CLI; no token to pass) | `OPENAI_API_KEY` | `AGENTS.md` |

Rules:
- **Auth mode per provider:** `subscription` (default) or `api_key`. In `subscription` mode the runner **removes** `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the agent's environment (a key would silently override the login).
- **Provider-neutral contract:** same prompts for both; agents write results to `.agent/*.json`, validated by pydantic.
- **Codex specifics:** pass `--skip-git-repo-check` when `cwd` isn't a git repo (ticket-level runs, temp dirs); read-only sandbox for read-only roles, workspace-write for coder roles.
- **Permissions:** Claude tool allowlists / Codex sandbox modes; the orchestrator's git-status check enforces "no changes outside `.agent/`" for read-only roles regardless of provider.
- **Cost:** subscription runs may not report cost. Record `cost_usd` when available; always tokens (if reported), turns, duration. No per-ticket cost or run cap (D192).
- **Plan limits:** a provider limit fails the run with `provider_limit` → retry once on `agents.fallback_provider` if set, else Needs Human.
- **Team rollout:** `auth: api_key` for shared automation.
- Your own interactive build sessions are separate and use your normal Claude Code / Codex login.

---

## 5. Project structure

Phase that creates each part in brackets.

```
ai-sdlc-pipeline/
├── CLAUDE.md                        # master context (this file)
├── README.md                        # [P8]
├── pyproject.toml                   # [P1]
├── .env.example                     # [P1]
├── pipeline.config.example.yaml     # [P1]
├── docs/                            # SETUP.md (install + configure), GUIDE.md (use), PROJECT.md (features + architecture), PROGRESS.md, DESIGN.md
├── pipeline/
│   ├── cli.py                       # [P1] grows each phase
│   ├── config.py                    # [P1]
│   ├── logging.py                   # [P1]
│   ├── schemas.py                   # [P1] all contracts (§7)
│   ├── markers.py                   # [P1]
│   ├── formatting.py                # [P1] markdown → Jira wiki markup
│   ├── store.py                     # [P1] tracking tables (§8)
│   ├── deps.py                      # [P1] Deps container, make_deps(dry_run)
│   ├── integrations/
│   │   ├── base.py                  # [P1] Protocols + IntegrationError
│   │   ├── jira_client.py           # [P1]
│   │   ├── gitlab_client.py         # [P1]
│   │   ├── gdocs.py                 # [P1]
│   │   └── fakes.py                 # [P1] (+ FakeChat in P3)
│   ├── context.py                   # [P1]
│   ├── workspace.py                 # [P1]
│   ├── agents/
│   │   ├── runner.py                # [P2] run_agent(): provider, limits, validation, tracking
│   │   ├── roles.py                 # [P2]
│   │   ├── providers/               # [P2] base, claude_provider, codex_provider, fake_provider; [P8] children (pidfiles)
│   │   ├── planner.py               # [P2]
│   │   ├── pool.py                  # [P3] agent slots: acquire / release / queue
│   │   ├── coder.py                 # [P3]
│   │   ├── reviewer.py              # [P5]
│   │   ├── deployment.py            # [P6]
│   │   └── qa_notes.py              # [P7]
│   ├── chat/
│   │   ├── commands.py              # [P3] exact-grammar parser + command bus
│   │   ├── notifier.py              # [P3] ticket messages → ChatPort + Jira mirror
│   │   ├── console_chat.py          # [P3] ChatPort for CLI / dev
│   │   ├── cards.py                 # [P4] cards, rendered as text
│   │   ├── llm.py                   # [D209] the `summary` agent as a text writer (ChatLLMChain, AgentLLM, FakeLLM)
│   │   └── delivery.py              # [P9] minimal notifications: Jira delivery summary + merge-request card
│   ├── terminal.py                  # [D167] `pipeline cli`: interactive terminal, exact commands
│   ├── verify.py                    # [P3]
│   ├── acceptance.py                # [P9] criteria + finding outcomes in the review; [P10] live evidence
│   ├── testing/                     # [P10] live acceptance tests: preflight, stack (apps + local Redis),
│   │                                #   mock_server (externals), db (guards), cases, executor (+ cleanup),
│   │                                #   identities, migrations, envfile, service
│   ├── safe_env.py                  # [P9] environments for repo commands and git
│   ├── findings.py                  # [P5] finding ids ↔ threads, #n numbering, fix scopes, fixed format
│   ├── bugs.py                      # [P7] QA bugs from bug tickets (D206), numbering, choice
│   ├── analysis/
│   │   ├── devops_facts.py          # [P6]
│   │   ├── spec_excerpt.py          # [P7] the spec's relevant sections for the planner (D128)
│   │   ├── repo_map.py              # [P7] prepared repo inputs (D128/D129)
│   │   └── express_routes.py        # [P7]
│   ├── graph/
│   │   ├── state.py                 # [P3]
│   │   ├── wrapper.py               # [P3] guards, events, errors
│   │   ├── gates.py                 # [P3] gate(): manual → card + interrupt; auto → pass
│   │   ├── nodes/                   # [P3, P5–P7] (P5: review.py, fixing.py, change.py; P6: deploy.py, waits.py, sync.py; P7: qa.py)
│   │   └── build.py                 # [P3]
│   ├── events.py                    # [P3] pollers (GitLab merges, Jira comments); [P10] migrations
│   ├── server.py                    # [P3] workers + pollers; [P4] + bot endpoint
│   ├── environment.py               # [P8] machine problems vs code problems (D134)
│   ├── pause.py                     # [P8] PAUSE file: wait while paused (D138)
│   ├── reminders.py                 # [P8] gate reminders (D141)
│   ├── maintenance.py               # [P8] gc, demo-reset (D143)
│   ├── logs.py                      # [P8] `pipeline logs` timeline
│   └── report.py                    # [P8] report: text / chat card / HTML (D139)
└── prompts/
    ├── planner.md                   # [P2]
    ├── coder_backend.md             # [P3]
    ├── coder_fix.md                 # [P3]
    ├── chat_summary.md              # [P4b] plain-language chat / Jira Summary wording
    ├── reviewer.md                  # [P5]
    ├── contract_check.md            # [P5]
    ├── deployment.md                # [P6]
    ├── coder_rebase.md              # [P7] rebase conflicts before a push (D125)
    ├── coder_frontend.md            # [P7, deferred: Vue]
    ├── qa_notes_backend.md          # [P7]
    ├── coder_ticket.md              # [D200] one coder session for every repo of a ticket
    ├── coder_ticket_fix.md          # [D200] …and its fixes
    ├── reviewer_ticket.md           # [D200] preface for one review session (+ reviewer.md + contract_check.md)
    ├── tester.md                    # [P10] live acceptance cases
    └── qa_notes_frontend.md         # [P7, deferred: Vue]
```

Runtime data in `$PIPELINE_HOME` (default `~/.ai-pipeline/`):

```
~/.ai-pipeline/
├── repos/<repo>/                       # base clones
├── workspaces/<KEY>/                   # one folder per ticket
│   ├── .agent/                         # ticket-level: ticket.md, spec.md, plan.json, contract.json
│   ├── api-gateway/                    # git worktree (branch ai/<KEY>-<slug>), own .agent/
│   └── orders-service/
├── specs/<docId>.md                    # spec cache (+ .hash)
├── runs/<KEY>/<role>-<repo|ticket>-<n>-<provider>.jsonl   # transcripts
├── secrets/                            # Google OAuth client + token (never in a repo)
├── pipeline.db                         # checkpoints + tracking tables
└── logs/pipeline.log
```

---

## 6. Python conventions

- Type hints everywhere; `from __future__ import annotations`.
- Nodes get dependencies through `Deps` (jira, gitlab, spec, chat, runner, pool, store, config, clock); never import concrete clients. This enables `--dry-run`.
- Node signature: `def node(state: TicketState, deps: Deps) -> dict` returning only changed keys.
- Chat handlers **acknowledge within a few seconds** and hand work to a background worker; never run an agent inside a request handler.
- No network at import time. No global mutable state except cached config.
- Every external write is **idempotent** (comment markers, status check before transition, existing-MR check, card updates by message id).
- **Nothing people read is cut** (D203): no `…`, no "first sentence only", no "and N more" in plans, findings, GitLab comments, merge request descriptions, Jira comments or chat. A Jira text over its comment limit is split into parts (`split_md`), never shortened. (Tails of failed check output and the live-test evidence kept for the coder are logs, not content.)
- Integration errors raise `IntegrationError`; agent failures return `ok=False`, never raise into the graph.
- Log with structlog, binding `ticket`, `node`, `repo`, `slot` where relevant.
- No unit or graph tests in this repo (D201). Checks: `ruff`, `pipeline run <KEY> --dry-run` (the whole flow on fakes) and, on real tickets, the live acceptance tests (P10).

---

## 7. Contracts (`pipeline/schemas.py`)

Changing them = update this section + log in PROGRESS.md.

```python
RepoRole = Literal["gateway", "service", "frontend"]
RepoKind = Literal["backend", "frontend"]
Provider = Literal["claude", "codex", "fake"]

class TicketContext(BaseModel):
    key: str
    summary: str
    description_md: str
    acceptance_criteria: list[str]
    repos: list[str]
    spec_urls: list[str]
    spec_md: str
    spec_hash: str
    epic_key: str | None = None

# ---------- planning ----------
class ErrorCase(BaseModel):
    status: int
    code: str
    when: str

class InternalEndpoint(BaseModel):       # exposed by a service, called by the gateway
    id: str                              # e.g. "orders.create"
    provider_repo: str
    consumer_repos: list[str]
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str                            # e.g. /internal/v1/orders
    description: str
    request_schema: dict                 # JSON Schema: {params, query, body}
    response_schema: dict
    errors: list[ErrorCase] = []
    auth: str                            # e.g. "X-Internal-Token header"
    is_new: bool

class PublicEndpoint(BaseModel):         # exposed by the gateway
    id: str
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str
    auth: str
    validation: dict                     # JSON Schema enforced by the gateway validator
    calls: list[str]                     # InternalEndpoint ids, in call order
    error_mapping: list[dict]            # {internal status/code → public status/code}
    response_schema: dict

class InterfaceContract(BaseModel):
    internal_endpoints: list[InternalEndpoint] = []
    public_endpoints: list[PublicEndpoint] = []
    notes: list[str] = []

class FileChange(BaseModel):            # one file a task adds or edits (the short plan comment, D128)
    file: str
    change: Literal["new", "edit"]
    functions: list[str] = []
    what: str                            # one line: what it does for the ticket

class RepoTask(BaseModel):
    repo: str
    role: RepoRole
    order: int
    summary: str
    steps: list[str]
    changes: list[FileChange] = []
    files_hint: list[str] = []
    acceptance_criteria: list[str]       # ids from the brief ("AC4"); own criteria as text
    implements: list[str] = []           # endpoint ids provided
    consumes: list[str] = []             # endpoint ids called

class AcceptanceCriterion(BaseModel):
    id: str                              # "AC1": the ticket's own numbering when it has one
    text: str                            # Given / When / Then in one or two lines

class TicketBrief(BaseModel):            # the planner's reading of the whole ticket (D128)
    preconditions: list[str] = []        # e.g. a "Shared Given" for all ACs
    criteria: list[AcceptanceCriterion] = []

class ExternalCall(BaseModel):          # [P10] a call the ticket's code makes to a service outside the ticket's repos
    service: str                         # `externals` name in config, e.g. "partner_public"
    repo: str                            # the repo that makes the call
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str                            # e.g. /users/{id}
    response_from: Literal["recorded", "existing_code", "spec", "unknown"] = "unknown"   # where the expected response shape comes from
    note: str = ""

class ImplementationPlan(BaseModel):
    status: Literal["ready", "needs_clarification"]
    mode: Literal["initial", "revise", "replan", "qa_fix", "change"]   # revise = human feedback at plan approval; change [P5]
    brief: TicketBrief = TicketBrief()
    flow: str = ""                       # one line: how the request travels through the repos
    tasks: list[RepoTask]
    contract: InterfaceContract
    open_questions: list[str] = []
    risks: list[str] = []
    changes_from_previous: list[str] = []                   # revise / replan / qa_fix
    external_calls: list[ExternalCall] = []                 # [P10] so missing fixtures show before coding

# ---------- coding ----------
class ApiChange(BaseModel):
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str
    note: str = ""

class UiChange(BaseModel):
    route: str
    component: str
    note: str = ""

class FindingResolution(BaseModel):
    finding_id: str
    action: Literal["fixed", "disputed", "not_applicable"]
    note: str

class CoderResult(BaseModel):
    repo: str
    status: Literal["success", "failed", "needs_clarification"]
    summary: str
    files_changed: list[str] = []
    tests: dict[str, Literal["passed","failed","skipped"]] = {}
    new_env_vars: list[str] = []
    migrations: list[str] = []
    new_dependencies: list[str] = []
    api_changes: list[ApiChange] = []
    ui_changes: list[UiChange] = []
    contract_deviations: list[str] = []
    open_questions: list[str] = []
    resolutions: list[FindingResolution] = []

class CoderResults(BaseModel):           # [single_session] one session coded several repos
    results: list[CoderResult]           # exactly one per repo it was asked to code

class VerifyResult(BaseModel):
    repo: str
    ok: bool
    checks: dict[str, Literal["passed","failed","skipped"]]
    output_tail: dict[str, str] = {}
    problems: list[str] = []
    head_sha: str | None = None          # [P9] the commit the checks ran on (set by the orchestrator)

# ---------- review ----------
class Finding(BaseModel):
    id: str                              # "R<round>-<repo>-F<n>" or "R<round>-X-F<n>" (contract)
    repo: str                            # repo that must change
    severity: Literal["blocker","major","minor","nit","human"]   # human = a person's change request [P5]
    category: str
    file: str | None = None
    line: int | None = None              # new-file line present in the diff
    comment: str
    suggestion: str | None = None

class CriterionCheck(BaseModel):         # [P9] one acceptance criterion of the repo's task
    criterion: str                       # the id from task.md: "AC4", or "T1" for the task's own criteria
    status: Literal["met", "not_met", "unknown", "not_applicable"]
    evidence: str = ""                   # file:line or test name; required for met
    evidence_kind: Literal["real", "simulated", "traced", ""] = ""   # [P10] real = a live acceptance test proved it
    note: str = ""                       # why not met / unknown / not applicable

    @model_validator(mode="before")
    @classmethod
    def _old_shape(cls, data: Any) -> Any:   # {"criterion", "met": bool} before P9
        if isinstance(data, dict) and "status" not in data and "met" in data:
            met = data.get("met")
            data = {**data, "status": "met" if met is True else "not_met" if met is False else "unknown"}
        return data

class PreviousCheck(BaseModel):          # [P9] the reviewer's answer on a finding people chose to fix
    finding_id: str
    outcome: Literal["fixed", "still_open", "withdrawn"]   # withdrawn = the coder's dispute convinced the reviewer
    note: str = ""

class ReviewResult(BaseModel):
    scope: str                           # repo name, or "contract"
    verdict: Literal["approve", "changes_requested"]
    summary: str
    findings: list[Finding] = []
    acceptance_criteria_check: list[CriterionCheck] = []
    previous: list[PreviousCheck] = []   # [P9] one per finding in previous_review.json; unanswered = still open

class ReviewResults(BaseModel):          # [single_session] one session reviewed several repos
    results: list[ReviewResult]          # one per repo (scope = the repo name) + one scope "contract"

# ---------- deployment / QA ----------
class DevopsFacts(BaseModel):
    repo: str
    new_env_vars: list[str]
    removed_env_vars: list[str]
    build_time_env_vars: list[str]
    undeclared_env_vars: list[str]
    migration_files: list[str]
    infra_files_changed: list[str]
    new_dependencies: list[str]
    notes: list[str]

class DeployResult(BaseModel):
    doc_markdown: str
    deploy_order: list[str]
    risk_level: Literal["low","medium","high"]
    needs_devops: bool                   # set by orchestrator from facts

class QaNotesResult(BaseModel):
    comment_markdown: str

# ---------- environment & live acceptance [P10] ----------
class ServiceNeed(BaseModel):            # one thing a ticket's repos need before they can be tested live
    repo: str | None = None              # None = the machine (a tool), not one repo
    kind: Literal["tool", "env", "database", "redis", "app", "external"]
    name: str                            # e.g. "redis-server", "PG_HOST_WS2TARGETSALES", "partner_public"
    status: Literal["ok", "missing", "failed", "warning"]
    detail: str = ""                     # plain words: what was found / what to do

class EnvReport(BaseModel):              # the preflight (`env_check`) before coding
    ok: bool
    needs: list[ServiceNeed] = []
    problems: list[str] = []             # what stops the ticket, in plain words
    checked_at: str = ""

class SeedRow(BaseModel):                # a row the test inserts before its calls (deleted afterwards)
    table: str
    alias: str = ""                      # refer to its columns later as {{seed.<alias>.<column>}}
    values: dict

class StepExpect(BaseModel):
    status: int | list[int] | None = None
    json_checks: list[dict] = Field(default=[], alias="json")   # {"path": "data.id", "equals"|"exists"|"contains"|"type": …}
    model_config = {"populate_by_name": True}

class DbCheck(BaseModel):                # a read-only query after the calls (runs in a READ ONLY transaction)
    sql: str                             # SELECT …; {{…}} values become parameters, never text
    equals: Any = None                   # the first column of the first row
    at_least: int | None = None
    wait_s: float = 0                    # async work (queues): retry until true or this many seconds pass

class Lookup(BaseModel):                 # a read-only query run before the case: its value is data the dev DB must have
    name: str                            # used later as {{lookup.<name>}}
    sql: str                             # SELECT …; first column of the first row; no row = the case is blocked, not failed
    why: str = ""                        # what the dev DB lacks when it finds nothing (shown to people)

class AcceptanceStep(BaseModel):         # one HTTP call
    repo: str                            # the repo whose local instance is called
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str                            # the public path, without the repo's APP_PATH_PREFIX
    identity: str = Field(default="default", alias="as")   # an `acceptance.identities` name; "none" = no auth
    query: dict = {}
    body: Any = None
    headers: dict[str, str] = {}
    save: dict[str, str] = {}            # name → response path ("data.id"), used later as {{saved.<name>}}
    track: list[dict] = []               # rows the call creates: {"table": …, "where": {column: value}} (for cleanup)
    setup: bool = False                  # prepares data, proves nothing: if it fails the case is invalid, never the coder's
    expect: StepExpect = StepExpect()
    model_config = {"populate_by_name": True}

class AcceptanceCase(BaseModel):
    id: str                              # "C1" (contract) / "A1" (tester; T1… are task criteria)
    criterion: str = ""                  # "AC4"; "" = a contract case
    repo: str                            # the repo the case proves
    source: Literal["contract", "tester"]
    title: str
    use: list[str] = []                  # names of `AcceptancePlan.fixtures` whose seed rows are added before `seed`
    lookups: list[Lookup] = []
    seed: list[SeedRow] = []
    steps: list[AcceptanceStep]
    db: list[DbCheck] = []
    tables: list[str] = []               # every table it seeds, writes or reads (migration wait, cleanup)
    externals: list[str] = []            # `externals` names it needs fixtures for

class AcceptancePlan(BaseModel):         # the tester agent's result
    fixtures: dict[str, list[SeedRow]] = {}   # named seed sets many cases share (written once)
    cases: list[AcceptanceCase] = []
    blocked: list[dict] = []             # {"criterion": "AC9", "reason": "…"}: can't be tested locally, and why
    notes: list[str] = []

class CaseResult(BaseModel):
    case_id: str
    criterion: str = ""
    repo: str
    title: str = ""
    status: Literal["passed", "failed", "blocked", "invalid"]   # invalid = the case itself is wrong (back to the tester)
    reason: str = ""
    evidence: list[dict] = []            # per step: request, response (masked), checks
    evidence_kind: Literal["real", "simulated"] = "real"

class AcceptanceResult(BaseModel):
    repo: str | None = None              # None = the whole ticket
    ok: bool                             # nothing failed or invalid (blocked cases don't count)
    mode: Literal["required", "advisory"] = "required"
    head_shas: dict[str, str] = {}       # repo → the commit tested
    results: list[CaseResult] = []
    pending_migrations: list[str] = []   # the ticket's migrations not applied on the dev DB yet
    cleanup: dict = {}                   # {"inserted": n, "deleted": n, "left": n, "tables": {...}, "error": …}
    duration_s: float = 0

# ---------- commands & agents ----------
Verb = Literal["implement","approve_plan","revise_plan","review","fix","accept","deploy_doc",
               "devops_done","bug","fix_bugs","status","stop","retry","help",
               "guide","change","pause","unpause","report","test"]  # guide [P4b], change [P5], pause/unpause/report [P8]

class Command(BaseModel):
    verb: Verb
    key: str | None = None
    args: dict = {}                      # provider, slot, scope, finding_ids, feedback, text
    actor_id: str
    actor_name: str
    source: Literal["cli", "jira"]
    raw: str
    interpreted: bool = False            # kept for old rows (always False)
    confirmed: bool = False

class CommandResult(BaseModel):
    accepted: bool
    message_md: str                      # what the bot replies
    needs_confirmation: bool = False

class AgentRun(BaseModel):
    role: str
    repo: str | None
    slot: str | None
    run_no: int
    provider: Provider
    auth_mode: Literal["subscription", "api_key", "none"]
    model: str | None
    ok: bool
    duration_s: float
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    transcript_path: str
    error: str | None = None             # timeout, invalid_result, read_only_violation, provider_limit, cost_cap, run_cap
```

`TicketState` (`graph/state.py`):

```python
GateName = Literal["plan_approval","review","fix","re_review","deploy_doc","qa_fix"]

class RepoWork(TypedDict, total=False):
    repo: str
    role: RepoRole
    order: int
    status: Literal["pending","coded","mr_open","changes_requested","approved","merged"]
    branch: str
    worktree: str
    mr_iid: int | None
    mr_url: str | None
    coder_result: CoderResult | None
    verify_result: VerifyResult | None
    review_result: ReviewResult | None
    devops_facts: DevopsFacts | None
    verify_attempts: int
    awaiting_verify: bool                # coded in a parallel batch, not verified yet (D129)
    reviewed_sha: str | None             # [P9] the commit the last review saw; approval needs it unchanged
    acceptance_result: AcceptanceResult | None   # [P10] the last live acceptance run for this repo

class TicketState(TypedDict, total=False):
    key: str
    started_by: str
    context: TicketContext | None
    plan: ImplementationPlan | None
    plan_feedback: list[str]
    plan_revisions: int
    repos: dict[str, RepoWork]
    replans: int
    review_round: int
    qa_round: int
    contract_review: ReviewResult | None
    fix_scope: dict | None               # {"mode": "major+"|"minor"|"all"|"blockers"|"ids", "ids": [...]}
    override: dict | None                # {"by": name, "at": ts, "open_findings": [...]}
    deploy_result: DeployResult | None
    waiting_for: GateName | Literal["merge_all","devops_done","qa","migration"] | None   # migration [P10]
    gate_message_id: str | None          # card to update when the gate is passed
    provider_override: Provider | None
    preferred_slots: dict[str, str]      # pool → slot name (sticky)
    bugs: list[dict]
    guidance: list[dict]                 # [P4b] {by, at, text, step}: human guidance (`guide`)
    last_event: dict | None
    halted_reason: str | None
    total_cost_usd: float
    testing: bool                        # [P10] False (the default for new tickets) = no live tests; True = `implement KEY testing=true`; missing (old tickets) = on
    env_report: EnvReport | None         # [P10] the preflight before coding
    acceptance_attempts: int             # [P10] live acceptance runs sent back to the coder
    review_mode: Literal["local", "gitlab"]   # [D207] set at the start from git.review_mode; missing (old tickets) = gitlab
    review_log: list[dict]               # [D207] per review round: {round, repo, kind: new|resolved|rejected, line}; goes into the MR summary note
    acceptance_override: dict | None     # [P10] {"by", "at", "failing", "heads"}: `accept` at an acceptance stop
```

Integration Protocols (`integrations/base.py`):

```python
class JiraPort(Protocol):
    def search(self, jql: str) -> list[str]: ...
    def get_issue(self, key: str) -> dict: ...
    def current_status(self, key: str) -> str: ...
    def transition(self, key: str, status_name: str) -> None: ...
    def comment(self, key: str, body_md: str, marker: str) -> None: ...
    def upsert_comment(self, key: str, body_md: str, marker: str) -> None: ...                # [P9] edit the comment with this marker in place, else post it
    def comments_since(self, key: str, since: datetime) -> list[dict]: ...
    def labels(self, key: str) -> list[str]: ...
    def attach(self, key: str, filename: str, content: bytes) -> None: ...   # [P6] same name + size → skip; same name → replaced
    def delete_bot_comments(self, key: str) -> int: ...                       # [P8] demo-reset: comments with an aipl marker
    def delete_attachments(self, key: str, prefix: str) -> int: ...           # [P8] demo-reset: e.g. the deployment notes

class GitLabPort(Protocol):
    def find_mr(self, project: str, source_branch: str) -> dict | None: ...
    def create_mr(self, project: str, source: str, target: str, title: str, body_md: str) -> dict: ...
    def update_mr_description(self, project: str, iid: int, body_md: str) -> None: ...
    def mr_diff(self, project: str, iid: int) -> str: ...
    def mr_state(self, project: str, iid: int) -> Literal["opened","merged","closed"]: ...
    def mr_commits(self, project: str, iid: int) -> list[dict]: ...
    def add_inline(self, project: str, iid: int, path: str, line: int, body_md: str, marker: str) -> None: ...
    def add_note(self, project: str, iid: int, body_md: str, marker: str, as_role: str = "reviewer") -> None: ...
    def unresolved_threads(self, project: str, iid: int) -> list[dict]: ...
    def reply(self, project: str, iid: int, discussion_id: str, body_md: str, as_role: str = "coder") -> None: ...  # coder; reviewer: "Still open" [P5]
    def resolve(self, project: str, iid: int, discussion_id: str) -> None: ...                # reviewer
    def approve(self, project: str, iid: int, sha: str | None = None) -> None: ...            # reviewer; [P9] sha = the reviewed head
    def list_mrs(self, project: str, source_prefix: str) -> list[dict]: ...                  # [P8] demo-reset, any state
    def close_mr(self, project: str, iid: int) -> None: ...                                   # [P8] coder
    def delete_branch(self, project: str, branch: str) -> None: ...                           # [P8] coder; never a default branch

class SpecPort(Protocol):
    def fetch(self, url: str) -> tuple[str, str]: ...

class ChatPort(Protocol):
    def ensure_thread(self, key: str, origin: dict | None = None) -> None: ...   # create/find the ticket thread
    def post(self, key: str | None, text_md: str, card: dict | None = None) -> str: ...  # None = main channel
    def update(self, message_id: str, text_md: str | None = None, card: dict | None = None) -> None: ...
```

---

## 8. State & tracking database

One SQLite file, `$PIPELINE_HOME/pipeline.db`:

1. **LangGraph checkpoints** — full `TicketState` per thread; used to pause/resume.
2. **Tracking tables** (`pipeline/store.py`, SQLAlchemy Core) — queryable history.

```
tickets       key PK, repos JSON, stage, waiting_for NULL, jira_status, review_rounds, qa_rounds,
              replans, total_cost_usd, started_by, started_at, updated_at, finished_at, halted_reason
ticket_mrs    id PK, key, repo, round_kind ('initial'|'qa<n>'), branch, mr_iid, mr_url,
              state ('opened'|'merged'|'closed'), opened_at, merged_at
events        id PK, key, ts, type, node, repo NULL, data JSON
              type ∈ node_start, node_end, gate_wait, gate_pass, transition, comment, chat_post,
                     human_command, resume, error, halt, llm_call,
                     env_check, acceptance, migrate [P10]
agent_runs    id PK, key, repo NULL, role, slot NULL, run_no, provider, auth_mode, model NULL,
              started_at, duration_s, ok, turns NULL, input_tokens NULL, output_tokens NULL,
              cost_usd NULL, transcript_path, error NULL
commands      id PK, ts, key NULL, verb, args JSON, actor_id, actor_name, source, raw,
              interpreted, accepted, reason NULL
agent_slots   name PK, pool, provider, busy_key NULL, busy_role NULL, busy_since NULL
chat_threads  key PK, channel_id, conversation_ref JSON, root_message_id, created_at
ticket_notes  key + name PK, value JSON, updated_at         [P9] Jira comment cursor (`jira_comments`), delivery record (`delivery`); [P10] `migrations` (file → hash when first seen applied on dev)
```

Rules:
- The node wrapper writes `node_start/node_end/error`; gates write `gate_wait/gate_pass`; the command bus writes `commands` + `human_command`; integrations write `transition/comment/chat_post`.
- The text writer (`chat/llm.py`, D209) writes one `llm_call` per attempt (key = ticket or `chat`; node = purpose; data: provider, model, latency_s, tokens, ok, error; never message text).
- The runner writes `agent_runs`; the pool maintains `agent_slots` (all reset to free on startup).
- [P8] A restart's recovery writes `resume` with `data.recovered` (D135); `needs_human`'s `halt` has `data.kind` (run_cap, cost_cap, provider_limit, machine, rounds, stopped, other; D140); a card's `chat_post` keeps its title/author (`data.card`), a reminder its `data.reminder` (D137, D141).
- Checkpoints = how to resume; tracking tables = what happened. If they disagree, checkpoints win.
- WAL mode on. Team rollout: `langgraph-checkpoint-postgres` + same tables on Postgres.

---

## 9. Invariants and guardrails

1. The pipeline **never merges** and **never pushes to a default branch**. Before every push it rebases its own MR branch onto the latest default branch and pushes with `--force-with-lease` pinned to the remote state it saw, so commits people pushed in between are kept, never overwritten (D125). **Local review (D207, `git.review_mode: local`, the default): nothing is pushed and no MR exists until the Reviewer approves; `publish_mrs` then rebases, pushes, opens the MRs, posts one review-summary note per MR and the Reviewer's approval.** `gitlab` opens the MRs first.
2. GitLab identities: **coder** pushes, opens MRs, replies to threads; **reviewer** comments, resolves, approves. Never mix tokens up between roles (for now one personal token is used for both, with `git.allow_same_gitlab_identity: true`, D194; two separate accounts also work with it `false`).
3. Every Jira/GitLab comment carries a hidden marker (`aipl:{KEY}:{node}:{round}:{extra}`); writers skip if present. GitLab: an HTML comment. Jira: a hidden comment property `aipl` (`{"marker": …}`, read back with `expand=properties`), so nothing shows in the comment (D128); older comments with the grey short form (`aipl:{node}:{round}[:{extra}]`) are still recognised.
4. Limits (safety nets, set far above what a real ticket needs since P9, D163): `max_review_rounds` 6, `max_qa_rounds` 5, `max_verify_attempts` 4, `max_replans` 3, `max_plan_revisions` 10, per-role `timeout_min`/`max_turns` (planner 40 min / 60, coder 60 / 300, reviewer 30 / 100, doc roles 20 / 60–80). There is no per-ticket cost, agent-run or diff-size cap (D192). Pointless loops end earlier through the stall and dispute rules.
5. The orchestrator **re-verifies** every agent claim before pushing.
6. Placeholders only (`{{QA_BASE_URL}}`, `{{QA_TOKEN}}`) in posted cURLs.
7. Read-only roles can only write `.agent/`; enforced by git-status check whatever the provider.
8. A coder run only edits **its own repo's worktree**. (`single_session`: the ticket's one coder session edits the worktrees of the ticket's repos and nothing else; each repo's `shared_paths` stay off-limits, enforced by deny rules and an automatic undo.)
9. Commands run as the local user (the terminal); Jira commands come from people with access to the ticket. `accept` and `stop` always require confirmation.
10. Commands must match the ticket's current `waiting_for`; anything else is rejected with an explanation.
11. `stop` halts a ticket; a `$PIPELINE_HOME/PAUSE` file (`pause`) holds every ticket before its next step and every agent start until `unpause` (D138).
12. Agents never see production credentials. Everything posted to chat, Jira and GitLab (attachments too, P9), and every agent transcript, is masked for secrets (D142). Repo commands (install, lint, tests, build) run with an environment allowlist (`safe_env`, + `verify.env_passthrough`) and the orchestrator's git runs without repo hooks or pipeline credentials (P9).
13. Jira status is the source of truth: if a human moves the ticket out of the expected status (In Dev for every step from the plan to the last merge, D127), the thread halts with `moved_by_human`.
14. A ticket is approved/merged/deployed as a **set** of MRs.
15. Human overrides (`accept`) are recorded in state, the tracking DB, an MR note and Jira.
16. The `summary` agent only writes wording: it never calls integrations, never sees secrets or `.env` values, and gets code only when `chat_llm.send_code` is on.
17. **Approval is evidence-bound (P9):** a repo's review approves only when every acceptance criterion of its task is `met` with evidence (or `not_applicable` with a reason) and every finding people chose is answered `fixed`/`withdrawn`; an unanswered finding stays open; a deferred blocker/major still blocks (only `accept` gets past it); the reviewer's own `changes_requested` is never turned into an approval. The approval names the reviewed head sha; any new commit on an MR (whoever wrote it) sends the ticket back to review, and checks that ran on an older commit run again first. In local mode the sha is the local head; `publish_mrs`'s rebase onto the newer default branch carries the approval to the new commit only when the branch's own patch is unchanged (compared with blob ids and hunk numbers left out), else the repo goes back to the review; failing checks go back to the coder.
18. **Live tests touch only their own data (P10).** They connect only to `acceptance.allowed_db_hosts` (never a name that says prod); every delete is by primary key from the run's ledger (seeded rows, tracked rows, rows under the run's own **random** test partner — never a real partner set in `acceptance.run_values` — and their foreign-key children), children first, in one transaction, then checked; no TRUNCATE, no DDL; agent-written DB checks run READ ONLY; a PUT/PATCH/DELETE may only target a row the case created; writes to real external services are refused. Redis is a private local one per run. The dev DB credentials go only into the test apps' environment, never into a worktree, prompt or message. A case may set up rows in any table it needs (parents, reference data), all deleted by primary key right after the case; calls made `as <identity>!own` act as the run's **own** partner (always new, swept), so service-level cases need not touch a real partner's data; a `lookup` that finds nothing blocks the case (never fails the code); a failing `setup` step makes the case invalid, never the coder's.
19. **The pipeline never applies a migration on its own (P10, narrows D123).** A ticket whose migration isn't on the dev DB waits for it; only a person's `pipeline migrate <KEY>` in a terminal (never chat or Jira, always `[y/N]`, listing every migration it would run) applies one.
20. **Live evidence is pinned to the tested code (P10).** It counts for a criterion only while the ticket's own files are unchanged since the run (and the reviewer is told not to verify those criteria again; a `not_met` from it still wins); an `accept` of failing live tests holds only for the tested commits.

---

## 10. Configuration

Secrets in `.env`, and so are the values that differ per environment (D193): `JIRA_PROJECT`, `JIRA_PICKUP_JQL`, per repo `REPO_<NAME>_GITLAB_PROJECT` / `REPO_<NAME>_ENV_FROM` (name in capitals, `-` as `_`), `ACCEPTANCE_ALLOWED_DB_HOSTS` (comma-separated) and `ACCEPTANCE_PARTNER_ID` / `_USER_UUID` / `_USER_PK` / `_USER_ID`; everything else in `pipeline.config.yaml` (every field explained in `pipeline.config.example.yaml`). `REVIEW_MODE` (`local` | `gitlab`, D207) sets `git.review_mode`.

`.env`: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `GITLAB_BASE_URL`, `GITLAB_CODER_TOKEN`, `GITLAB_REVIEWER_TOKEN`, `GOOGLE_OAUTH_CLIENT_PATH`, `GOOGLE_TOKEN_PATH`, `PIPELINE_HOME`; optional: `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (only for `api_key` auth).

`pipeline.config.yaml` sections: `jira`, `repos`, `agents` (default/fallback provider, per-provider auth, per-role model/limits, `single_session` D200), `agent_pool`, `gates`, `chat` (the team's names `agents`, default fix scope), `chat_llm` (D209: `provider` agent | none, `timeout_s`, `summaries`, `send_code`; the model comes from `agents.roles.summary`), `limits`, `polling`, `qa`, `git`, `demo` [P8] (`tickets` for `demo-reset`), `verify` [P9] (`env_passthrough`), `acceptance` [P10] (mode, allowed_db_hosts, run_values (a chosen partner / user), identities, default_identity, repo_headers, externals, notes; per repo `stack`: start, env_from, env templates, health_path, db variable names). `chat` also has `reminder_hours` (0 = off, the default since P9), `verbosity` [P9] (`minimal` default | `normal`).

---

## 11. CLI (grows by phase)

```
pipeline check [--agents] [--chat-llm]  # [P1] integrations; [P2] --agents: tiny run per provider; [P4b] --chat-llm
pipeline context <KEY>              # [P1]
pipeline workspace <KEY>            # [P1]
pipeline db init|stats              # [P1]
pipeline plan <KEY> [--provider P]  # [P2] planner only
pipeline cli [--dry-run]            # [D167] interactive terminal: exact commands, all tickets' messages, reset/exit
pipeline say "<command>"            # [P3] any chat command through the command bus, e.g. "implement DEMO-1"
pipeline serve [--dry-run]          # [P3] headless: workers + pollers + the localhost endpoint
pipeline status [<KEY>]             # [P3] stage, waiting_for, MRs, slots, rounds, cost
pipeline slots                      # [P3] agent pool usage + queue
pipeline reset <KEY>                # [P3]
pipeline report [--html] [--days N] # [P8]
pipeline logs <KEY> [--transcripts] # [P8]
pipeline pause [reason] | unpause   # [P8]
pipeline warm [repo]                # [P8] fetch + node_modules cache (D136)
pipeline gc [--cache] [--dry-run]   # [P8]
pipeline demo-reset [KEYS…] [--yes] # [P8] GitLab MRs/branches, Jira comments/status, local state
pipeline env <KEY> [--no-boot]      # [P10] live test preflight (tools, dev DB, env, apps boot)
pipeline acceptance <KEY> [--cleanup] # [P10] run the live tests now / delete a crashed run's rows
pipeline migrate <KEY> [--yes]      # [P10] a person applies the ticket's migrations on the dev DB
pipeline record <external> <path>   # [P10] save a real dev GET answer as a fixture
```

`pipeline say` sends one command to a running server (or runs it here): same commands, same replies, cards rendered as text.

---

## 12. Checks (there are no unit or graph tests in this repo, D201)

- **Lint:** `ruff check .` (and `ruff format`); `mypy` is non-blocking.
- **Dry run:** `pipeline run <KEY> --dry-run` runs the whole workflow on fakes (fake Jira, GitLab, spec, chat and scripted agents, local git remotes): every step connects, in about 3 s, at no cost. Set `acceptance.mode: off` for it (the dry run does not exercise the live tests). `pipeline serve --dry-run` does the same in the Agents Playground.
- **Live acceptance tests (P10):** on real tickets, the ticket's apps run against the dev database (`acceptance.mode`).
- **Manual:** `pipeline cli`.

---

## 13. Glossary

- **Thread:** one LangGraph run, keyed by Jira key. A ticket's messages in the terminal are its chat thread.
- **Gate:** a step that either waits for a human (`manual`) or continues (`auto`), per config.
- **Command bus:** single entry point for terminal and Jira commands.
- **Slot:** a named agent in the pool (Coder-A …); the unit of concurrency.
- **Provider:** agent runtime for a run: `claude` or `codex`.
- **Change set:** all MRs of one ticket, advanced together.
- **Contract:** the planner's InterfaceContract between gateway and services.
- **Round:** one review pass or one QA return.
- **Verify:** orchestrator-side lint/tests/sanity/secret scan.
- **Marker:** hidden idempotency tag on posted comments.
- **Target repo:** a product repo; this repo is the **pipeline repo**.
