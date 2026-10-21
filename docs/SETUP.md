# SETUP: from an empty machine to a running pipeline

Follow this file top to bottom and you can run the whole project: install it, connect Jira, GitLab,
Google Docs and the AI agents, point it at your repositories, and take a first ticket from Jira to
merge requests. Day-to-day use is in [GUIDE.md](GUIDE.md); what the system is and how it is built is
in [PROJECT.md](PROJECT.md).

Every step has a checkbox. Section 13 is the final test: when it passes, you are done.

**What you will have at the end**

```
 you (terminal)  ──►  pipeline cli  ──►  Jira (tickets, status, audit comments)
                          │          ──►  GitLab (branches, merge requests, reviews)
                          │          ──►  Google Docs (the tech spec)
                          ▼
              AI agents (Claude Code / Codex) working in git worktrees
              under ~/.ai-pipeline, one folder per ticket
```

**Time needed:** about half a day the first time (most of it is accounts and tokens), 30 minutes
for a second machine.

---

## 0. What you need before you start

| You need | Why |
|---|---|
| A Mac or Linux machine you can leave running (a laptop is fine for one person) | The pipeline, the agents and the live tests run here |
| Access to the product repositories in GitLab (and the VPN, if GitLab is internal) | The agents work on clones of them |
| A Jira project, with the right to add a bot account or API token | Tickets and status |
| A Claude subscription and/or a ChatGPT (Codex) subscription, or API keys | The agents |
| A Google account that can read the tech-spec documents | Specs are Google Docs |
| Optional, for live acceptance tests: read/write access to a **development** database, Redis installed locally | See §10 |

---

## 1. Tools on the machine

Commands are for macOS with Homebrew; use your package manager on Linux.

- [ ] **git 2.40 or newer**: `git --version` (worktrees are used heavily)
- [ ] **Python 3.11+ and uv**: `brew install uv` (or `curl -LsSf https://astral.sh/uv/install.sh | sh`), then `uv --version`. uv installs the right Python by itself.
- [ ] **Node.js and npm**, the **same major version the product repos use** (check each repo's `.nvmrc` / `engines`): `node --version`. The agents run each repo's `npm ci`, lint and tests.
- [ ] **Claude Code** (the Claude provider's engine), logged in: `npm install -g @anthropic-ai/claude-code`, then `claude` once and sign in. `claude --version` must work.
- [ ] **Codex CLI** (only if you use the Codex provider), logged in: `npm install -g @openai/codex`, then `codex login`
- [ ] **Redis**: `brew install redis` and `openssl`. Live acceptance tests (§10) start their own private Redis (they only need the `redis-server` binary). Separately, some repos' own unit tests expect a Redis on `localhost:6379`: run `brew services start redis` (without it the ticket stops with "a problem on the machine: Redis isn't running" before any coder attempt is spent)
- [ ] Optional: `gitleaks` (secret scan; a built-in regex scan is used otherwise), Docker (only if a repo's own tests need it)

Check current install instructions in the Claude Code and Codex documentation if a command above
has changed.

## 2. Get the code and install it

```bash
git clone <this repository's URL> ai-sdlc-pipeline
cd ai-sdlc-pipeline
uv sync                                   # creates .venv and installs everything
uv run pipeline --help                    # lists all commands
```

The two files you will create (both are git-ignored, never commit them):

| File | In | Holds |
|---|---|---|
| `.env` | the project folder | Secrets and every value that differs per environment (§8) |
| `pipeline.config.yaml` | the project folder | Which repos, agents, gates, limits, live-test setup (§9) |

```bash
cp .env.example .env
cp pipeline.config.example.yaml pipeline.config.yaml
```

Run every `uv run pipeline …` command **from the project folder** (the config and `.env` are read
from the current folder). To keep the config elsewhere: `export PIPELINE_CONFIG=/path/to/pipeline.config.yaml`
(a shell variable, not a `.env` line).

**The data folder** (`PIPELINE_HOME`, default `~/.ai-pipeline`) is created on first use and holds
everything the pipeline generates:

```
~/.ai-pipeline/
├── repos/<repo>/               base clones of your product repos
├── workspaces/<KEY>/           one folder per ticket: .agent/ (ticket, spec, plan) + one git worktree per repo
├── specs/                      cached spec documents
├── runs/<KEY>/                 transcripts of every agent run (secrets masked)
├── secrets/                    Google OAuth client + login token (never inside a repo)
├── pipeline.db                 SQLite: LangGraph checkpoints + tracking tables
└── logs/pipeline.log           the pipeline's log (secrets masked)
```

- [ ] Choose `PIPELINE_HOME` (needs a few GB: each ticket holds worktrees and `node_modules`)
- [ ] `mkdir -p ~/.ai-pipeline/secrets`

## 3. The AI agents

The pipeline runs agents on **Claude** or **Codex**, chosen per role in `pipeline.config.yaml`
(§9). By default both use your **subscription** login, so no API key is needed.

**Claude (default provider)**
- [ ] Claim the monthly **Agent SDK credit** for your plan (Claude Help Center: "Use the Claude Agent SDK with your Claude plan"). Pipeline runs draw on it, not on your interactive Claude Code limits.
- [ ] `claude setup-token`, then put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN` (lets headless runs authenticate reliably).
- [ ] Do **not** set `ANTHROPIC_API_KEY` while using subscription auth (a key overrides the login; the runner also removes it from the agents' environment).

**Codex (second provider, optional)**
- [ ] `codex login` with your ChatGPT account (no token to copy: the login is stored by the CLI and reused when the pipeline runs as the same OS user).
- [ ] Test: `codex exec --skip-git-repo-check "say hello"`.
- [ ] Do **not** set `OPENAI_API_KEY` while using subscription auth.

**Team rollout, API keys instead of subscriptions**
- [ ] Set `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` in `.env` and `auth: api_key` per provider in the config:

```yaml
agents:
  default_provider: claude
  fallback_provider: codex          # retried once when a provider's usage limit is hit
  providers:
    claude: { auth: api_key }
    codex:  { auth: api_key }
```

Rough cost at API prices, one ticket through a service and a gateway: planning about $0.9, coding
about $3, review about $1.5, deployment note under $0.5: about $6 for one review round.
`pipeline report` shows what each ticket used.

## 4. Jira

Cloud, any plan. A project (key used below: `DEMO`).

- [ ] **Statuses** in the project's workflow: `To Do`, `In Dev`, `Blocked`, `RFT`, `Testing`, `QA Failed`, `QA Passed`. The pipeline moves tickets between them, so allow those transitions (for team-managed projects: allow any status to move to any other). Different names? Set them in `jira.statuses` in the config.
- [ ] **A user for the pipeline** (a bot account is best: the audit comments appear under its name) and its **API token**: https://id.atlassian.com/manage-profile/security/api-tokens
  - `JIRA_BASE_URL=https://<your-site>.atlassian.net`, `JIRA_EMAIL=<the account's email>`, `JIRA_API_TOKEN=<token>` in `.env`
  - The account must be able to browse the project, comment, edit issues, transition and attach files.
- [ ] `JIRA_PROJECT=DEMO` in `.env`. Tickets the terminal accepts must start with this key (`DEMO-12`).
- [ ] The label `ai-ready` is only used if you set `gates.start: auto` (Jira pickup); by default tickets are started from the terminal. Then also set `JIRA_PICKUP_JQL` in `.env`.
- [ ] **QA bugs are separate Jira tickets** (the bug in the description). Nothing to configure: QA creates them, and the developer passes their keys to `fix` (GUIDE §8).
- [ ] Jira comment commands (`/stop`, `/retry`, `/guide …`) work for anyone who can comment on the ticket. Keep comment permission to the team.

**Custom fields are optional.** `implement KEY` asks for the tech-spec link and the repositories
in the terminal. Only if tickets still carry them as Jira fields (or for Jira pickup), add a
**Repos** checkbox field (one option per repo name in the config) and a **Tech Spec URL** field,
find their ids (open `https://<site>.atlassian.net/rest/api/3/issue/DEMO-1?expand=names` and look for
`customfield_10XXX` next to the field names) and set `JIRA_REPOS_FIELD` / `JIRA_SPEC_FIELD` in `.env`.
Fallbacks without fields: `Repos: a, b` and `Spec: <url>` lines in the description.

## 5. GitLab

The pipeline acts as two GitLab **users** so the merge request's author and its approver differ,
as in your real process:

| User | Role in each repo | Token scopes | Does |
|---|---|---|---|
| **Coder** (`ai-coder`) | Developer | `api`, `write_repository` → `GITLAB_CODER_TOKEN` | Pushes `ai/<KEY>-…` branches, opens merge requests, replies to threads |
| **Reviewer** (`ai-reviewer`) | Developer, with approval rights | `api` → `GITLAB_REVIEWER_TOKEN` | Posts the review summary, comments, resolves threads, approves |

Use bot users, never a person's account. Each needs its own email (a `+reviewer` alias often works).

- [ ] Create the two users and verify their emails
- [ ] Add both to **every** product repository (Manage → Members) as **Developer**
- [ ] Each user: avatar → Edit profile → Access tokens → create a personal access token with the scopes above and an expiry → `.env`
- [ ] `GITLAB_BASE_URL=https://<your GitLab host>` in `.env` (no trailing path). If it is internal, the machine needs the **VPN**.
- [ ] **The branch merge requests target.** Every ticket branch starts from, and every merge request goes to, the repo's `default_branch` from the config. Protect it (Settings → Repository → Protected branches: nobody pushes directly; people merge). The pipeline never pushes to it.
  - Our company repos use a branch called `pipeline_develop` (a copy of `develop` the pipeline may target). Create it once and keep it current: `git checkout develop && git pull && git push origin develop:pipeline_develop`.
- [ ] **One account for both roles** (small teams): set `git.allow_same_gitlab_identity: true` in the config and put the same token in both variables. GitLab must allow approving your own merge request.
- [ ] Per repository, write down its **project path** (`group/subgroup/project`, from the URL after the host). It goes in `.env` as `REPO_<NAME>_GITLAB_PROJECT` (§8).

## 6. Google Docs (the tech spec)

The pipeline reads the spec documents through Google's API as **you**, read-only. You create a small
private "app" once; a browser login then grants it access.

- [ ] https://console.cloud.google.com → create a project (e.g. `ai-sdlc-pipeline`)
- [ ] APIs & Services → Library → enable **Google Drive API** and **Google Docs API**
- [ ] OAuth consent screen: user type **External** (or **Internal** if the project belongs to your company's Google Workspace), app name, your email as contact; add your Google account under Test users
- [ ] Credentials → Create credentials → **OAuth client ID** → **Desktop app** → download the JSON
- [ ] Save it as `~/.ai-pipeline/secrets/google_oauth_client.json` and set in `.env`:
  `GOOGLE_OAUTH_CLIENT_PATH=/Users/<you>/.ai-pipeline/secrets/google_oauth_client.json`
  `GOOGLE_TOKEN_PATH=/Users/<you>/.ai-pipeline/secrets/google_token.json`
  (use absolute paths)
- [ ] The first `pipeline check --spec-url <a spec doc>` (§13) opens a browser: sign in with the account that can see the specs. Google warns the app is unverified: it is your own app, choose Continue. The token is stored at `GOOGLE_TOKEN_PATH`.

Good to know:
- An External app in *Testing* mode logs you out after about 7 days: run `pipeline check --spec-url …` again.
- On a company Workspace the admin may block unverified apps: create the Cloud project inside the company organisation and choose **Internal**, or ask the admin to allow the app.
- The spec link given to `implement` must be a **Google Doc** (`docs.google.com/document/d/<id>`) the signed-in account can read. Use real headings (Heading 1/2/3) and the same names for routes, fields and error codes as the tickets, so the planner can find a ticket's section.
- For a server without a browser: do the first login on a laptop and copy `google_token.json`, or use a service account with the spec folder shared to it.

## 7. Prepare the product repositories

For **each** repo the pipeline will work on (gateway, services; Vue is not supported yet):

**On GitLab (merged to the default branch)**
- [ ] `CLAUDE.md` and an identical `AGENTS.md`: folder layout, naming, error format and codes, validation, internal auth, logging, helpers to reuse, how to test. Claude reads `CLAUDE.md`, Codex reads `AGENTS.md`.
- [ ] `REVIEW.md`: the checklist the Reviewer applies to this repo
- [ ] `DEPLOYMENT_TEMPLATE.md`: the deployment-note shape for this repo (env vars, migrations, infrastructure)
- [ ] A complete `.env.example` (the verify step and the coder use it; real values never)
- [ ] `.agent/` in `.gitignore`
- [ ] A working lint on changed files and a working test command
- [ ] Rules the agents must never break go **in the repo's own `CLAUDE.md`** (for example "never edit the authentication middleware or `validateSchema`", "validation lives in the controller, the service only does CRUD"). Files that must never change can also be listed in `repos[].shared_paths` in the config (enforced).

**On this machine** (only for live acceptance tests, §10)
- [ ] A normal clone of the repo with a working `.env` for development (test values, never production). Its path goes in `.env` as `REPO_<NAME>_ENV_FROM`.

## 8. `.env`: every variable

Edit `.env` (copied from `.env.example`). Nothing here is ever committed. `*` = required.

| Variable | Example | Meaning |
|---|---|---|
| `JIRA_BASE_URL`* | `https://site.atlassian.net` | Jira Cloud site |
| `JIRA_EMAIL`*, `JIRA_API_TOKEN`* | | The pipeline's Jira user (§4) |
| `JIRA_PROJECT`* | `DEMO` | Project key; tickets must start with it |
| `JIRA_PICKUP_JQL` | `project = DEMO AND status = "To Do" AND labels = ai-ready` | Only for `gates.start: auto` |
| `JIRA_SPEC_FIELD`, `JIRA_REPOS_FIELD` | `customfield_10043` | Optional Jira fields (§4) |
| `GITLAB_BASE_URL`* | `https://gitlab.example.com` | GitLab host |
| `GITLAB_CODER_TOKEN`*, `GITLAB_REVIEWER_TOKEN`* | | The two users' tokens (§5) |
| `REPO_<NAME>_GITLAB_PROJECT`* | `closio/backend/nodejs-ws1closio` | One per repo in the config |
| `REPO_<NAME>_ENV_FROM` | `~/Documents/Japfa/nodejs-ws1closio/.env` | One per repo: the developer clone's `.env` for live tests |
| `GOOGLE_OAUTH_CLIENT_PATH`*, `GOOGLE_TOKEN_PATH`* | absolute paths | Google login files (§6) |
| `PIPELINE_HOME`* | `/Users/you/.ai-pipeline` | The data folder (§2) |
| `CLAUDE_CODE_OAUTH_TOKEN` | | From `claude setup-token` (§3) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | | Only for `auth: api_key` |
| `REVIEW_MODE` | `local` (default) or `gitlab` | Where the code review happens (PROJECT §7) |
| `ACCEPTANCE_ALLOWED_DB_HOSTS` | `dev-db.example.rds.amazonaws.com` | Live tests: the only DB hosts they may touch (comma-separated) |
| `ACCEPTANCE_PARTNER_ID`, `ACCEPTANCE_USER_UUID`, `ACCEPTANCE_USER_PK`, `ACCEPTANCE_USER_ID` | | Live tests: run as this existing partner/user instead of new random ones |

**`<NAME>`** is the repo's `name` in the config **in capitals, with every `-` or other symbol as
`_`**: the repo `nodejs-ws1closio` gives `REPO_NODEJS_WS1CLOSIO_GITLAB_PROJECT`.

## 9. `pipeline.config.yaml`: what to edit

The example file explains every field in comments. These are the parts you must look at.

### 9.1 Your repositories (`repos`)

One entry per repository. Replace the example entries with yours.

```yaml
repos:
  - name: nodejs-ws2targetsalesincentive   # the name people see and the folder name; also the key of the REPO_… variables
    role: service                          # service | gateway | frontend (coding order: service → gateway → frontend)
    default_branch: develop                # the branch tickets start from and merge requests target (ours: pipeline_develop)
    commands:
      install: "npm ci"
      lint: "npx eslint --config eslint.config.mjs {files}"   # {files} = only the changed files
      test: "npm test"
      extra: ["npm run coverage:check", "npm run check:circular"]
    # shared_paths: ["src/middlewares/auth/"]   # optional: existing files here are off-limits to the coder
    # env_file: "$PIPELINE_HOME/env/<repo>.env" # optional test .env for coder/verify runs (never production)
    stack: { … }                           # only for live tests (§10)
```

- The GitLab project path and the `.env` path are **not** here: they are `REPO_<NAME>_GITLAB_PROJECT` / `_ENV_FROM` in `.env`.
- The commands are run in the repo's worktree with a restricted environment. If a repo's scripts need an environment variable, add its name to `verify.env_passthrough`.
- The base clone is made from `GITLAB_BASE_URL` + the project path, under `$PIPELINE_HOME/repos/<name>`.

### 9.2 Agents, pool, gates, limits

```yaml
agents:
  single_session: true      # one coder session and one reviewer session per ticket, across all repos
  default_provider: claude  # claude | codex
  fallback_provider: codex  # used once if a provider's usage limit is hit; null = stop and ask a person
  providers: { claude: { auth: subscription }, codex: { auth: subscription } }
  roles:                    # model, effort, max_turns, timeout_min per role
    planner:  { provider: claude, model: claude-sonnet-5, effort: low, max_turns: 100, timeout_min: 40 }
    coder:    { provider: claude, model: claude-sonnet-5, effort: medium, max_turns: 300, timeout_min: 60 }
    reviewer: { provider: claude, model: claude-sonnet-5, effort: medium, max_turns: 100, timeout_min: 30 }
    deployment: { provider: claude, model: claude-haiku-4-5, max_turns: 80, timeout_min: 20 }
agent_pool:                 # named slots; the number of slots = how many agents run at once
  coder:    [{name: Coder-A, provider: claude}, {name: Coder-B, provider: codex}]
  reviewer: [{name: Reviewer-A, provider: claude}, {name: Reviewer-B, provider: codex}]
  deploy:   [{name: Deploy-A, provider: claude}]
gates:                      # manual = wait for a person's command; auto = continue
  start: manual
  plan_approval: manual
  review: auto
  fix: auto
  re_review: auto
  deploy_doc: auto
  qa_fix: manual
```

A slot is only occupied while an agent runs; each pool needs at least one slot, and slot names
must be unique. Limits (`limits:`) are safety nets set far above a normal ticket; leave them.

### 9.3 The rest

| Section | Edit when |
|---|---|
| `jira.statuses` | Your workflow uses other status names |
| `chat` | `agents:` to rename Coder/Reviewer/DevOps; `default_fix_scope`; `verbosity`; `local_port` (the local server port, default 8765) |
| `chat_llm` | §11 (the summary agent) |
| `git` | `review_mode` (or `REVIEW_MODE`), `coder_author`/`deploy_author` (the commit authors), `allow_same_gitlab_identity` |
| `polling` | How often Jira/GitLab are polled (seconds) |
| `qa` | The placeholders used in the QA cURLs; leave |
| `acceptance` | §10 |
| `demo.tickets` | Tickets `pipeline demo-reset` resets |

## 10. Live acceptance tests (optional but recommended)

Before any merge request, the pipeline can run the changed apps **for real** on this machine, against
the team's **shared development database**, and call their endpoints. The Tester agent writes the
cases from the acceptance criteria; the pipeline runs them and deletes exactly the rows each case
created. They are **off by default** per ticket: start a ticket with `implement KEY testing=true`
to include them.

What it needs:
- [ ] The machine can reach the dev database (VPN if it is private) and the apps' dependencies
- [ ] `ACCEPTANCE_ALLOWED_DB_HOSTS` in `.env`: the dev DB host(s). **Anything else is refused**; never a production host.
- [ ] `redis-server` and `openssl` installed (§1). The pipeline starts a private Redis per run; it never touches yours.
- [ ] Per repo, `REPO_<NAME>_ENV_FROM` in `.env`: the developer clone's `.env` (the DB credentials go only into the test apps' environment, never into a worktree, a prompt or a message)
- [ ] `acceptance.mode` in the config: `off` (skip), `advisory` (run and report, never block) or `required` (a failing case goes back to the coder, then to a person)
- [ ] `repos[].stack` for each repo: how to start it (`start`), its `health_path`, the names of its DB variables (`db:`), and `env` overrides (ports, local Redis, mocked external services, a local log folder). Copy the example's entries and change the names.
- [ ] `acceptance.identities` / `default_identity` / `repo_headers`: how the tests authenticate (client headers for a service, a seeded session for a gateway)
- [ ] `acceptance.externals`: services your apps call outside the ticket's repos: `fixtures` (answers recorded from dev) or `blocked`. Record a real answer with `pipeline record <external> <path>` (read-only GET).
- [ ] Optional `ACCEPTANCE_PARTNER_ID` etc.: run as an existing partner/user

Check it: `uv run pipeline env <KEY>` (preflight: tools, DB reachable and allowlisted, env, apps boot).
A ticket whose migration is not yet on the dev DB waits; a **person** applies it with
`uv run pipeline migrate <KEY>` (the pipeline never runs a migration by itself).

## 11. The summary agent (optional)

The one-paragraph description of each merge request is written by a tiny **Claude or Codex run**
(the `summary` role; Claude Haiku by default): it uses the same login as the other agents, so there
is no extra key or account. It never decides anything, never runs anything, and sees no code unless
`chat_llm.send_code: true`. With `provider: none` (or `summaries: false`) a template is used.

```yaml
chat_llm:
  provider: agent        # agent | none
agents:
  roles:
    summary: { provider: claude, model: claude-haiku-4-5, max_turns: 8, timeout_min: 3 }   # or provider: codex
```

- [ ] Run `uv run pipeline check --chat-llm`: one tiny run, shows the provider, model and time (about 6 seconds).
- Every prompt is masked for secrets first.

## 12. Secrets and safety checklist

- [ ] `.env` and `pipeline.config.yaml` are git-ignored (they are); the Google files live under `$PIPELINE_HOME/secrets`
- [ ] No production credentials anywhere in the pipeline's config or `.env`
- [ ] Tokens have an expiry and are rotated; personal demo tokens are replaced by bot tokens for the team
- [ ] Protected default branches on every repo
- [ ] The Jira project's comment permission is limited to the team (comment commands are not allowlisted)

## 13. First run: check everything, then one ticket

**13.1 The checks** (from the project folder)

```bash
uv run pipeline db init                                        # creates pipeline.db
uv run pipeline check --agents --chat-llm --spec-url "https://docs.google.com/document/d/<id>/edit"
```

`check` verifies the config and `.env`, that the database is writable, Jira (login, project, the
optional fields), GitLab (both users log in; each has at least Developer access to every repo; each
repo's default branch exists), Google (the OAuth file; with `--spec-url` it fetches the document and
opens the browser on the first run), the Claude/Codex CLIs and their login, and with `--agents` a
tiny real run per provider (`--chat-llm`: one tiny run of the summary agent). Every line should be ok (a
warn is explained in its message). Fix what is marked fail, using the message.

```bash
uv run pipeline warm                    # base clones + node_modules cache (the first run takes a few minutes)
uv run pipeline cli --dry-run           # everything fake: type `implement DEMO-1`, `help`, `exit`
```

The dry run needs no accounts. To run the whole flow on fakes non-interactively, set
`acceptance.mode: "off"` in a copy of the config and run
`PIPELINE_CONFIG=copy.yaml uv run pipeline run DEMO-7 --dry-run` (about 3 seconds, no cost).

**13.2 A real ticket**

1. In Jira, create a small ticket with numbered acceptance criteria (see [GUIDE.md](GUIDE.md) §2).
2. Start the terminal and the ticket:

   ```bash
   uv run pipeline cli
   sdlc> implement DEMO-12
   ```

   It asks for the spec link, checks it, and lists the repositories to tick.
3. After 2–5 minutes the **plan** appears (also on Jira). `approve DEMO-12`.
4. The agents code every repo, run the checks, review, fix, and open the merge requests. You are told
   when it needs you. Details of each step: [GUIDE.md](GUIDE.md).

If it works up to the merge requests appearing in GitLab, the setup is complete.

## 14. Running it for a team

**One server.** A Linux or macOS machine with VPN access to GitLab and Jira, Python/uv, Node, git,
Redis (for live tests), the Claude Code CLI and optionally Codex. Repeat §1–§13 on it, then run
the pipeline as a service so tickets keep working when nobody is logged in:

```ini
[Unit]
Description=AI SDLC pipeline
After=network-online.target

[Service]
User=pipeline
WorkingDirectory=/opt/ai-sdlc-pipeline
EnvironmentFile=/etc/ai-pipeline/env
ExecStart=/usr/local/bin/uv run pipeline serve
Restart=on-failure
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

People then connect with `uv run pipeline cli` on that machine (over SSH): it attaches to the
running server.

- **Secrets** can come from a secret manager that writes the `EnvironmentFile` (the pipeline reads the process environment first; the `.env` file is not required).
- **A restart** drops steps that had not started, and resumes each cut-off step once with a message; a step cut off twice goes to "needs a person". Deploy while no agent runs (`pipeline status`), or `pipeline pause` first.
- **Backups:** `sqlite3 $PIPELINE_HOME/pipeline.db ".backup '/backups/pipeline-$(date +%F).db'"` daily, plus `$PIPELINE_HOME/runs/`. SQLite is enough for one server.
- **Disk:** `pipeline gc` removes finished tickets' worktrees; `pipeline gc --cache` also clears the `node_modules` cache.
- **Adding a repository:** prepare it (§7), give both bot users access, add its `repos[]` entry (§9.1) and its two `REPO_<NAME>_…` variables (§8), then `uv run pipeline warm <name>` and `uv run pipeline check`. Run the first ticket with the gates manual.
- **More parallel tickets:** add slots to `agent_pool` (about three coder slots and four reviewer slots handle three tickets at once).

## 15. When setup goes wrong

| Symptom | Fix |
|---|---|
| `Config … needs these set in .env` | The message names the variable (`JIRA_PROJECT`, `REPO_<NAME>_GITLAB_PROJECT`, …) |
| `check`: GitLab 401/403 | Token scope or expiry; the user is not a member of that repo |
| `check`: cannot clone / timeout | VPN is off; wrong `GITLAB_BASE_URL` or project path |
| `check`: Google asks to log in again | The 7-day Testing-mode expiry: re-run `check --spec-url …` |
| `Cannot implement KEY: …` | The spec link is not a readable Google Doc, or the account cannot open it |
| An agent says it is not logged in | Run `claude` / `codex login` as the **same OS user** that runs the pipeline; for headless Claude set `CLAUDE_CODE_OAUTH_TOKEN` |
| `provider_limit` | Your plan's usage limit; the fallback provider takes over, or wait |
| Live tests: "not an allowed database host" | Put the dev DB host in `ACCEPTANCE_ALLOWED_DB_HOSTS` (and never a production host) |
| Live tests: "Redis is not installed" | `brew install redis` |

More: [GUIDE.md](GUIDE.md) §11 (running and recovering tickets).
