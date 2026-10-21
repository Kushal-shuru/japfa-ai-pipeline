# AI SDLC Pipeline

Takes a Jira ticket to a set of **reviewed, deployment-documented, QA-ready GitLab merge requests**, one per repository the ticket touches (gateway + services).

You drive it from the terminal (`pipeline cli`) with exact commands:

```
implement SCRUM-12     # pick the tech spec and the repositories
approve SCRUM-12       # the plan looks right
fix SCRUM-12 #1 #3     # which review findings to fix
```

People keep control of which tickets the AI takes, the plan, which review findings to fix, merges, infrastructure changes and QA sign-off. The AI never merges, never pushes to a default branch and never runs a migration.

## How it works

```
implement → read the ticket (Jira + tech spec) → plan → [you approve] → code + automated checks
          → live acceptance tests (optional) → local review ⟲ fix → merge requests opened
          → deployment note → [you merge] → Ready for Test + QA notes → QA → QA - Passed
```

- **Orchestrator:** Python + LangGraph, one checkpointed thread per Jira key, gates that wait for people, state in one SQLite file. It makes every Jira and GitLab write.
- **Agents** (Claude Code or Codex, per slot, on your subscription by default): Planner, Coder, Reviewer, Tester (live acceptance cases), Deployment writer, QA notes writer. Each writes a JSON result the pipeline validates and re-checks.
- **Small helper agents:** the **summary** agent writes the merge request description; the **criteria** agent copies the acceptance criteria out of the ticket description word for word (the pipeline rejects anything that is not a quote from it).
- **Tech spec:** a file you put in the `tech_spec/` folder (`.docx`, `.md`, `.txt`; the default), or a Google Doc link. Set with `TECH_SPEC_SOURCE`.
- **Review:** by default entirely local. The Reviewer reviews the branches on this machine and the merge requests are pushed and opened only after it approves, with one review-summary comment each. `REVIEW_MODE=gitlab` opens the merge requests first.
- **Jira statuses:** To Do → In Development → Ready for Test → Testing → QA - Passed. Blocked whenever the pipeline needs a person, and when QA files bugs (there is no QA Failed status). Names are configurable (`jira.statuses`).
- **GitLab:** one personal access token is used for both the coder and the reviewer role for now (SETUP §5).

## Docs

| Doc | For |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | **Start here.** Install and configure end to end: tools, agents, Jira, GitLab, the tech spec, `.env`, config, live tests, first run |
| [docs/GUIDE.md](docs/GUIDE.md) | Working tickets: every command and every step of a ticket from Jira to merged code; how to reset a ticket completely |
| [docs/PROJECT.md](docs/PROJECT.md) | What the project is: all features, the architecture, how it is built |
| [Interactive walkthrough](https://claude.ai/artifact/KJg6vD2LFhFmHKvj1shLTi) | The walkthrough with diagrams (also linked at the top of [docs/PROJECT.md](docs/PROJECT.md)) |
| [CLAUDE.md](CLAUDE.md) | Master context: contracts, graph, commands, guardrails (also read by Claude Code) |

`docs/PROGRESS.md` (decisions, known issues, session log) is a local working file: it is git-ignored and not in the repository.

## Setup

Follow [docs/SETUP.md](docs/SETUP.md). In short:

```bash
uv sync
cp .env.example .env                                  # secrets and per-environment values (SETUP §8)
cp pipeline.config.example.yaml pipeline.config.yaml  # repos, agents, gates, live tests (SETUP §9)
uv run pipeline db init
uv run pipeline check --agents --chat-llm             # with google-doc specs also: --spec-url <a spec Google Doc>
uv run pipeline warm                                  # base clones + node_modules cache
```

Things you set in `.env`: your Jira site, login and API token, `JIRA_PROJECT` (the key prefix your tickets use, for example `SCRUM`), one GitLab token in both `GITLAB_CODER_TOKEN` and `GITLAB_REVIEWER_TOKEN`, each repo's GitLab project path, and optionally `TECH_SPEC_SOURCE`, `REVIEW_MODE` and the live-test database allowlist.

Then put the tech spec documents in `tech_spec/` (the folder's contents are git-ignored).

The product repos need the agent files `CLAUDE.md`/`AGENTS.md` (identical), `REVIEW.md` and a deployment template (SETUP §7). Nothing needs to be created in Jira: no custom fields, no label.

Config sections (`pipeline.config.yaml`): `jira`, `repos`, `agents` (providers, auth, per-role model and limits), `agent_pool`, `gates`, `chat` (team names, reminders), `chat_llm`, `limits`, `polling`, `qa`, `git`, `spec`, `demo`, `verify`, `acceptance`. The example file explains each field.

## Run

```bash
uv run pipeline cli                 # the interactive terminal (runs the pipeline in this process)
uv run pipeline cli --dry-run       # the same with everything fake (no Jira/GitLab/agent calls)
uv run pipeline serve               # optional: headless server; `pipeline cli` / `pipeline say` attach to it
```

In the terminal type `implement SCRUM-12`. It lists the documents in `tech_spec/` (type the number or the file name), then the repositories (numbers, names or `all`). `help` lists every command. One-off: `uv run pipeline say "implement SCRUM-12 --spec 1 --repos all"`.

Tickets must start with the `JIRA_PROJECT` key. The examples here and in the docs use `DEMO` or `SCRUM`; use yours.

## Commands

Exact commands (the full list and every step: [docs/GUIDE.md](docs/GUIDE.md)):

| Command | Does |
|---|---|
| `implement KEY [--spec N-or-FILE --repos a,b] [--provider codex] [--slot Coder-B] [testing=true]` | Start a ticket; the terminal asks for the tech spec and the repositories. `testing=true` adds the live acceptance tests |
| `approve KEY` · `guide KEY: <text>` | Approve the plan · your input (revise the plan, help a stopped step, or a change while merge requests are open) |
| `review KEY` | Start the review (only when the review gate is manual) |
| `fix KEY blockers\|major\|minor\|all\|#1 #3 [dismiss #2] [: notes]` · `accept KEY` | Choose review fixes, or accept as is (confirmed) |
| `deploy-doc KEY` | Write the deployment note again |
| `fix KEY DEMO-13 DEMO-14` (in testing) · `bug KEY: <text>` | QA bugs: one Jira ticket each |
| `test KEY` | Run the live acceptance tests again |
| `status [KEY]` · `stop KEY` (confirmed) · `retry KEY` · `help` | Control |
| `reset KEY [--full]` | Forget the ticket here; `--full` also closes its merge requests, deletes its branches and the bot's Jira comments, and moves it back to To Do |
| `report` · `pause[: reason]` (confirmed) · `unpause` | The whole pipeline |

CLI (developers, on the pipeline machine):

| Command | Does |
|---|---|
| `pipeline check [--agents] [--chat-llm]` | Config, Jira, GitLab, the tech spec, agent providers, the summary agent |
| `pipeline context KEY` · `pipeline workspace KEY` · `pipeline plan KEY` | Ticket context, worktrees, planner only |
| `pipeline cli [--dry-run]` | The interactive terminal |
| `pipeline serve [--dry-run]` | The headless server |
| `pipeline say "<command>"` | Send one command |
| `pipeline status [KEY]` · `pipeline slots` | Tickets, waits, agent slots and queues |
| `pipeline report [--html] [--days N]` | Per ticket: stages, time waiting on people, rounds, runs, tokens, cost |
| `pipeline logs KEY [--transcripts]` | One ticket's timeline: events, commands, agent runs |
| `pipeline pause [reason]` · `pipeline unpause` | Pause / resume everything |
| `pipeline warm [repo]` | Fetch and fill the node_modules cache |
| `pipeline gc [--cache] [--dry-run]` | Remove finished tickets' worktrees |
| `pipeline reset KEY` | Forget one ticket locally (GitLab and Jira untouched) |
| `pipeline demo-reset [KEYS…] [--yes]` | The full reset: merge requests, branches, Jira comments and status, local state |
| `pipeline env KEY` · `pipeline acceptance KEY` | Live-test preflight · run the live tests now |
| `pipeline migrate KEY` | A person applies the ticket's migrations on the dev database (never automatic) |
| `pipeline record EXTERNAL PATH` | Save a real dev answer as a fixture for the live tests |
| `pipeline run KEY --dry-run` | The whole flow with fakes, in about 3 seconds |
| `pipeline db init\|stats` | Tracking database |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "I can't find SCRUM-12 in Jira" for a ticket that exists | Jira is not accepting the login: check `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` (a classic API token, from the account that can see the ticket). `uv run pipeline check` shows it |
| "SCRUM-12 is not a … ticket" | The key does not start with `JIRA_PROJECT` |
| `Cannot implement KEY: …` about the tech spec | `local`: `tech_spec/` is missing, empty, or the number / name is not in the list. `google-doc`: the link is not a readable Google Doc |
| "I found no acceptance criteria" | Add them to the ticket description in any format, then `retry` |
| "A problem on the machine … Redis isn't running" | Start the service it names (`brew services start redis`), then `retry KEY`. No coder attempt was spent |
| "Someone moved the Jira ticket to …" | A person changed the status mid-run: move it back and `retry KEY`, or stop |
| A ticket waits and nothing happens | `pipeline status KEY`. Is the pipeline paused (`pipeline unpause`)? Waiting for a person, a live test, a merge? |
| After a restart, a ticket says it picked up again | Expected: a cut-off step resumes once; a second cut-off goes to Needs Human |
| Start over | `reset KEY --full` in the terminal |
| Anything else | `pipeline logs KEY --transcripts`, `~/.ai-pipeline/logs/pipeline.log` (secrets masked), the transcript files under `~/.ai-pipeline/runs/KEY/` |

## Development

```bash
uv run ruff check .                      # there are no unit or graph tests in this repo
uv run pipeline run SCRUM-7 --dry-run    # the whole flow on fakes (use a key with your JIRA_PROJECT prefix)
```

Session protocol, contracts and guardrails: [CLAUDE.md](CLAUDE.md).
