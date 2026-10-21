# AI SDLC Pipeline

Takes a Jira ticket to a set of **reviewed, deployment-documented, QA-ready GitLab merge requests**, one per repository the ticket touches (gateway + services).

The team drives it from the terminal (`pipeline cli`) with exact commands:
- `implement DEMO-12`
- `approve`
- `fix #1 #3`

People keep control of which tickets the AI takes, the plan, which review findings to fix, merges, infrastructure and QA sign-off.

- **Orchestrator:** Python + LangGraph, one checkpointed thread per ticket, gates that wait for people.
- **Agents:** Claude Code (Agent SDK) or Codex, per slot. They plan, code, review and write the deployment note and the QA test notes.
- **Summary agent:** a tiny Claude / Codex run that writes the merge request description; it never decides anything.
- **Integrations:** Jira (statuses, audit trail, QA bugs), GitLab (coder and reviewer identities), tech specs (the `tech_spec/` folder, or Google Docs).

| Doc | For |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | **Start here.** Everything to install and configure, end to end: tools, agents, Jira, GitLab, Google, `.env`, config, live tests, first run, running it for a team |
| [docs/GUIDE.md](docs/GUIDE.md) | Working tickets: every command and every step of a ticket from Jira to merged code, for developers, QA and DevOps |
| [docs/PROJECT.md](docs/PROJECT.md) | What the project is: all features, the architecture, how it is built |
| [Interactive walkthrough](https://claude.ai/artifact/KJg6vD2LFhFmHKvj1shLTi) | The walkthrough with diagrams (also linked at the top of [docs/PROJECT.md](docs/PROJECT.md)) |
| [CLAUDE.md](CLAUDE.md) | Master context: contracts, graph, commands, guardrails (also read by Claude Code) |
| [docs/PROGRESS.md](docs/PROGRESS.md) | Decisions, known issues, measurements, session log |
| [docs/DESIGN.md](docs/DESIGN.md) | The early design (history) |

## Setup

Follow [docs/SETUP.md](docs/SETUP.md). In short:

```bash
uv sync
cp .env.example .env                                  # secrets and per-environment values (SETUP §8)
cp pipeline.config.example.yaml pipeline.config.yaml  # repos, agents, gates, live tests (SETUP §9)
uv run pipeline db init
uv run pipeline check --agents --chat-llm   # google-doc specs: add --spec-url <a spec Google Doc>
uv run pipeline warm                                  # base clones + node_modules cache
```

The product repos need the agent files `CLAUDE.md`/`AGENTS.md` (identical), `REVIEW.md` and a deployment template (SETUP §7).

Config sections (`pipeline.config.yaml`): `jira`, `repos`, `agents` (providers, auth, per-role model and limits), `agent_pool`, `gates`, `chat` (team names, reminders), `chat_llm`, `limits`, `polling`, `qa`, `git`, `demo`. The example file explains each field.

**Where the review happens:** `REVIEW_MODE` in `.env` (`local` is the default, `gitlab` the earlier flow). `local`: the Reviewer reviews the branches on this machine and the merge requests are pushed and opened only after it approves, with one review-summary comment each. `gitlab`: the merge requests open first and the Reviewer posts its findings on them as threads.

## Run

```bash
uv run pipeline cli                 # the interactive terminal (runs the pipeline in this process)
uv run pipeline cli --dry-run       # the same with everything fake (no Jira/GitLab/agent calls)
uv run pipeline serve               # optional: headless server; `pipeline cli` / `pipeline say` attach to it
```

In the terminal type `implement DEMO-12`; `help` lists every command. One-off: `uv run pipeline say "implement DEMO-12"`.

## Commands

Exact commands (the full list and every step: [docs/GUIDE.md](docs/GUIDE.md)):

| Command | Does |
|---|---|
| `implement KEY [--spec LINK --repos a,b] [--provider codex] [--slot Coder-B]` | Start a ticket; the terminal asks for the tech spec link and the repositories |
| `approve plan` · `revise plan: <feedback>` | Plan approval |
| `review` | Start the review / the next review |
| `fix all\|blockers\|major+\|#1 #3 [: notes]` · `accept` | Choose review fixes, or accept as is (confirmed) |
| `change: <request>` | A person's change while the MRs are open |
| `deploy-doc` | Write the deployment note again |
| `fix KEY DEMO-13 DEMO-14` (in testing; `fix KEY` asks for the bug tickets) · `bug KEY: <text>` | QA bugs: one Jira ticket each |
| `status [KEY]` · `stop` (confirmed) · `retry` · `guide: <text>` · `help` | Control |
| `report` · `pause[: reason]` (confirmed) · `unpause` | The whole pipeline |

CLI (developers, on the pipeline machine):

| Command | Does |
|---|---|
| `pipeline check [--agents] [--chat-llm]` | Integrations, agent providers, the summary agent |
| `pipeline context KEY` · `pipeline workspace KEY` · `pipeline plan KEY` | Ticket context, worktrees, planner only |
| `pipeline cli [--dry-run]` | The interactive terminal |
| `pipeline serve [--dry-run]` | The headless server |
| `pipeline say "<text>"` | Send one command |
| `pipeline status [KEY]` · `pipeline slots` | Tickets, waits, agent slots and queues |
| `pipeline report [--html] [--days N]` | Per ticket: stages, time waiting on people, rounds, runs, tokens, cost |
| `pipeline logs KEY [--transcripts]` | One ticket's timeline: events, commands, agent runs |
| `pipeline pause [reason]` · `pipeline unpause` | Pause / resume everything |
| `pipeline warm [repo]` | Fetch and fill the node_modules cache |
| `pipeline gc [--cache] [--dry-run]` | Remove finished tickets' worktrees |
| `pipeline reset KEY` | Forget one ticket locally (GitLab and Jira untouched) |
| `pipeline demo-reset [KEYS…] [--yes]` | Demo tickets back to the start on GitLab, Jira and locally |
| `pipeline run KEY --dry-run` | The whole flow with fakes, in about 3 seconds |
| `pipeline db init\|stats` | Tracking database |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "This is a problem on the machine … Redis isn't running" | Start the service it names (`brew services start redis`), then say "try again". No coder attempt was spent |
| "Someone moved the Jira ticket to …" | A person changed the status mid-run: move it back and say "try again", or stop |
| A ticket waits and nothing happens | `pipeline status KEY`. Is the pipeline paused (`pipeline unpause`)? Waiting for a person or a merge? |
| After a restart, a ticket says it picked up again | Expected: a cut-off step resumes once; a second cut-off goes to Needs Human |
| Anything else | `pipeline logs KEY --transcripts`, `~/.ai-pipeline/logs/pipeline.log` (secrets masked), the transcript files under `~/.ai-pipeline/runs/KEY/` |

## Development

```bash
uv run ruff check .       # there are no unit or graph tests in this repo (D201)
uv run pipeline run DEMO-7 --dry-run   # the whole flow on fakes (set acceptance.mode: off first)
```

Session protocol, contracts and guardrails: [CLAUDE.md](CLAUDE.md).
