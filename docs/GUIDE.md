# GUIDE: working tickets with the pipeline

For developers, QA and DevOps. After reading this you can take a Jira ticket from "To Do" to
merged, QA-tested code with the pipeline: every command, and every step in order.

- Not installed yet? [SETUP.md](SETUP.md).
- What the system is and how it is built: [PROJECT.md](PROJECT.md).

**The short version**

1. You write a normal Jira ticket (numbered acceptance criteria).
2. In the terminal: `implement DEMO-12`. Give the spec link, tick the repositories.
3. You read the plan and `approve DEMO-12`.
4. The AI codes every repository, checks it, reviews it itself, fixes what the review finds, and only
   then opens **one merge request per repository**, with a deployment note.
5. **People** merge. **DevOps** does the infrastructure steps before the merge. **QA** tests and
   files each bug as a Jira ticket; you give those to `fix`.

The AI never merges, never pushes to a default branch, never runs migrations and never changes CI or secrets.

---

## 1. Who does what

| Step | Who | Where |
|---|---|---|
| Write the ticket | Developer / lead | Jira |
| Start it, approve or change the plan | Developer | Terminal |
| Plan, code, tests, fixes | Coder (AI) | Terminal, Jira |
| Review each repo + the gateway ↔ service contract | Reviewer (AI) | Terminal; later GitLab |
| Choose which findings to fix, or accept as is | Developer | Terminal |
| Deployment note (order, env vars, migrations, infrastructure) | DevOps (AI) | Jira attachment, terminal |
| Env vars, migrations, infrastructure **before** the merge | DevOps (person) | from the note |
| Merge, in the order on the card | Developer | GitLab |
| Test notes with cURLs | Coder (AI) | Jira |
| Testing; QA Failed / QA Passed; bug tickets | QA | Jira |
| Choose which bugs to fix | Developer | Terminal |

## 2. Writing a ticket the AI can take

| Put in the ticket | Why |
|---|---|
| **Description**: what and why, a few sentences | The planner reads it |
| **Acceptance criteria**, numbered, one behaviour each: `AC1 Given … When … Then …`. A shared precondition once, as "Given (all)" | Every criterion must appear in the plan, and the review checks each one |
| **Out of scope** | Stops the plan from growing |
| **Permission ID** only if one applies (e.g. `securityID 123`) | "As an admin" alone means any logged-in user; the AI never invents permission ids |
| Use the same names for routes, fields and error codes as the tech spec | The planner finds the right section of the spec by those names |

**Not on the ticket:** the tech-spec link and the repositories. You give them when you start the
ticket (§5 step 1). Good size: one endpoint (or one change to one) through the service and the
gateway. Split bigger work into several tickets.

## 3. Start the terminal

```bash
cd ai-sdlc-pipeline
uv run pipeline cli            # interactive; runs the pipeline in this process
```

Or keep a headless server running (a service, or `uv run pipeline serve`) and attach with the same
`uv run pipeline cli` from any shell on that machine. Every ticket's messages print above the
prompt (`[DEMO-12] 10:42 ───`), with the commands to type under each card.

Rules of the terminal:
- Commands are exact. A leading `/` is fine (`/retry DEMO-12`). **Always name the ticket.**
- A mistyped command shows the exact form to type. `help` lists everything.
- `stop`, `accept` and `pause` ask `[y/N]` (add `--confirm` to skip the question).
- You may address a team member first: `reviewer review DEMO-12`, `coder fix DEMO-12 #1`. A wrong one hands over to the right one.
- Leave with `exit`. Steps that are running carry on when the server (or the next start) picks them up.

## 4. All the commands

**In the terminal (`pipeline cli`, or one at a time with `uv run pipeline say "…"`)**

| Group | Command | What it does |
|---|---|---|
| Start | `implement DEMO-12 [--spec LINK --repos a,b] [--provider claude\|codex] [--slot Coder-B] [testing=true]` | Start a ticket. The terminal asks for the tech-spec link and the repositories; with `--spec` and `--repos` it does not ask. `testing=true` adds the live acceptance tests (off by default) |
| Steer | `approve DEMO-12` | Approve the plan: coding starts |
| | `guide DEMO-12: <text>` | Your input. At plan approval: revise the plan. At "needs help": guidance, then the step reruns. While merge requests are open: a change request |
| Review | `review DEMO-12` | Start the review (only when `gates.review` is manual) |
| | `fix DEMO-12 [blockers\|major\|minor\|all] [: notes]` | Fix findings by severity (default `major`: blockers + majors) |
| | `fix DEMO-12 #1 #3 [dismiss #2] [: notes]` | Fix those findings; `dismiss #2` records that you reject #2, with your reason |
| | `accept DEMO-12` | Approve without fixing the rest (asks to confirm; recorded everywhere) |
| | `change DEMO-12: <request>` | Your own change while the merge requests are open (a small one becomes findings; one that touches the interface re-plans) |
| Delivery | `deploy-doc DEMO-12` | Write the deployment note again (it is written automatically after the approval) |
| QA | `fix DEMO-12 DEMO-13 DEMO-14` | In testing: fix the bugs filed as those Jira tickets. `fix DEMO-12` alone asks for the ticket numbers |
| | `fix DEMO-12 1 2` · `fix DEMO-12 all` | In testing: fix bugs already recorded (by their numbers) |
| | `bug DEMO-12: <text>` | Record a bug by hand (normally QA files a ticket) |
| Live tests | `test DEMO-12` | Run the live acceptance tests again (after the migration is on the dev DB, or after they stopped) |
| Recovery | `retry DEMO-12` | Run the step that stopped again |
| | `stop DEMO-12` | Halt the ticket (Jira → Blocked) |
| | `reset DEMO-12 [--full]` | Terminal only. Forget the ticket here; `--full` also closes its merge requests, deletes its branches and the bot's Jira comments, and moves Jira back to To Do. Refused while a step runs |
| Information | `status [DEMO-12]` · `report` · `help` | Where things are · the report card · the command list |
| Whole pipeline | `pause[: reason]` · `unpause` | Hold / release every ticket |

**In a shell (developer tools; from the project folder)**

| Command | What it does |
|---|---|
| `uv run pipeline cli [--dry-run]` | The interactive terminal (`--dry-run`: everything fake) |
| `uv run pipeline serve [--dry-run]` | Headless server: workers, pollers (Jira, GitLab merges), the local endpoint |
| `uv run pipeline say "implement DEMO-12"` | Send one command to a running server (or run it here) |
| `uv run pipeline run DEMO-7 --dry-run` | The whole flow on fakes in about 3 seconds |
| `uv run pipeline check [--agents] [--chat-llm] [--spec-url URL]` | Check config, DB, Jira, GitLab, Google, agents |
| `uv run pipeline status [KEY]` · `slots` | Tickets and what they wait for · agent slots and queues |
| `uv run pipeline logs KEY [--transcripts]` | One ticket's timeline: events, commands, agent runs |
| `uv run pipeline report [--html] [--days N]` | Time (machine vs waiting on people), rounds, runs, tokens, cost; `report.html` in the data folder |
| `uv run pipeline pause [reason]` · `unpause` | Same as in the terminal |
| `uv run pipeline context KEY` · `workspace KEY` · `plan KEY` | Show what the planner would get · create the worktrees · run only the planner |
| `uv run pipeline warm [repo]` | Fetch the base clones and fill the `node_modules` cache (run before a busy day) |
| `uv run pipeline gc [--cache] [--dry-run]` | Remove finished tickets' worktrees |
| `uv run pipeline reset KEY` | Forget a ticket locally (GitLab and Jira untouched) |
| `uv run pipeline demo-reset [KEYS…] [--yes]` | Put demo tickets back to the start on GitLab, Jira and locally (`demo.tickets` in the config) |
| `uv run pipeline env KEY [--no-boot]` | Live-test preflight: tools, dev DB, env, apps boot |
| `uv run pipeline acceptance KEY [--cleanup]` | Run the live tests now / delete the rows of a crashed run |
| `uv run pipeline migrate KEY [--yes]` | **A person** applies the ticket's migrations on the dev DB (lists them first) |
| `uv run pipeline record EXTERNAL PATH` | Save a real dev GET answer as a fixture for the live tests |
| `uv run pipeline db init\|stats` | Create / inspect the tracking database |

**In Jira (comments), for people outside the terminal:** `/stop`, `/retry`, `/guide <text>`.

## 5. A ticket, step by step

Typical times are from real runs and vary by about ±50 %.

### Step 1. Start it

```
sdlc> implement DEMO-12
Please paste the tech spec link (Google Doc): https://docs.google.com/document/d/…/edit
Link OK. Which repositories are in scope for this ticket?
  1. nodejs-ws2targetsalesincentive
  2. nodejs-ws1closio
Repositories in scope (numbers or names, comma-separated, or all): 1,2
```

- The link must be a Google Doc the pipeline's Google login can read. Otherwise: `Cannot implement DEMO-12: <reason>` and nothing starts.
- Jira goes to **In Dev**. Add `testing=true` to include the live acceptance tests (§5 step 4).
- Rehearsing? `uv run pipeline cli --dry-run` does the same on fakes.

### Step 2. Read and approve the plan (2–5 minutes)

A **plan card** appears (and one Jira comment, kept per plan version): tasks per repository, the
coding order, the interface between gateway and service, every acceptance criterion mapped to a task,
and open questions.

- Fine → `approve DEMO-12`.
- Something is off → `guide DEMO-12: return 404 when the config does not exist`. The plan is redone with your words (about 2 minutes) and a new card appears. Nothing is coded yet: this is the cheapest place to steer.
- The planner asks questions → the ticket says it needs you; answer with `guide DEMO-12: <answers>`.

### Step 3. It codes (about 10–25 minutes; quiet)

One coder session works through every repository in order (services first, then the gateway). It
follows each repo's own `CLAUDE.md`, writes tests and commits in small steps. The pipeline then runs
that repo's lint and tests itself (it does not trust the agent's word) and sends failures back
(up to a few attempts). You hear nothing until something needs you. Ask any time: `status DEMO-12`.

If a repo's contract cannot be followed, it stops once to re-plan; a second time it asks you.

### Step 4. Live acceptance tests (only with `testing=true`)

Before review the changed apps start locally and are called for real against the dev database. Test
data created is deleted again. A failure goes back to the coder; the same failure again goes to you.
If the ticket's migration is not on the dev DB, the ticket waits (Jira → Blocked): apply it with
`uv run pipeline migrate DEMO-12`, then `test DEMO-12` (or wait for the poller).

### Step 5. The review (by default entirely local)

With the default `REVIEW_MODE=local` nothing is on GitLab yet. The Reviewer reads each repo's
changes against its default branch, applying that repo's `REVIEW.md`, **every acceptance
criterion**, and one cross-repo contract check. Findings are numbered (`#1 major · gateway: …`).

- By default (`gates.fix: auto`) blockers and majors go straight back to the Coder; the Reviewer
  checks again. This repeats until the review passes, two fix rounds in a row make no progress, a
  finding is disputed twice, or the round limit is hit. Then it asks you.
- Minor/nit findings are not blocking; they are listed on the merge request at the end.
- With `gates.fix: manual`, or when it stops, a **findings card** appears. Answer with one of:
  - `fix DEMO-12 major` · `fix DEMO-12 all` · `fix DEMO-12 blockers`
  - `fix DEMO-12 #1 #3 dismiss #2: the helper already validates it` (#2 is closed as your decision)
  - `accept DEMO-12`: merge as it is (recorded as your override)
- Wrong severity or a mistake in a finding? Dismiss it with a reason; it is never raised again.

### Step 6. The merge requests open (after approval)

Once the Reviewer approves, the pipeline rebases each branch onto the latest default branch, re-runs
the checks, pushes, and opens **one merge request per repo**, cross-linked, with:

- a short description (the Jira link, what and why),
- **one review summary comment**: rounds, what was raised, fixed or dismissed, what stayed open, the
  acceptance criteria and their evidence,
- the Reviewer's GitLab approval at the reviewed commit.

If the rebase changed the code (the base moved under your change), the Reviewer looks again before
anything is published. A **merge-request card** shows them in merge order and is edited as things
move on. (`REVIEW_MODE=gitlab` instead opens the merge requests first and posts findings as threads.)

### Step 7. The deployment note

Written automatically after approval from the real diffs: deploy order, new environment variables,
migrations, infrastructure changes, rollback (only when there are migrations), risk. It is attached
to Jira (`DEMO-12-deployment.md`) and summarised on the card.

**DevOps (a person)** does their part *before* merging: set the variables, run the migrations, apply
the infrastructure changes listed under "Before merging, DevOps". The AI never does these.

### Step 8. Merge (people)

In GitLab, merge the merge requests **in the order on the card** (services first, gateway last).
The card marks each "(merged)", then "All merged". Anything you push to the AI's branch is fine: it
is kept, and the next review looks at it. Want a change before merging? `change DEMO-12: <what>`.

### Step 9. Ready for testing

All merged → Jira **RFT** and a comment with **test notes** for QA: test cases with cURL commands
using `{{QA_BASE_URL}}` and `{{QA_TOKEN}}` placeholders.

### Step 10. QA

1. Replace the placeholders with your environment's values and test. Moving the ticket to **Testing** is optional.
2. **Each bug is its own Jira ticket**, with the bug in its description (what you did, what you expected, what happened). Then move the *main* ticket to **QA Failed**.
3. Tell the developer the bug keys (e.g. `DEMO-13 DEMO-14`).
4. All good → move the main ticket to **QA Passed**; the pipeline says thanks and cleans up.

### Step 11. Fix the bugs

```
sdlc> fix DEMO-12
Bug ticket number(s) for DEMO-12, for example DEMO-13 DEMO-14 (empty = the bugs already recorded): DEMO-13 DEMO-14
```

or in one line `fix DEMO-12 DEMO-13 DEMO-14`. The pipeline reads each bug ticket's summary and
description, numbers them (#1, #2 … on the main ticket's Jira), moves Jira to **In Dev**, and runs
the same loop on a new branch (`…-qa1`): a plan for the fix (`approve DEMO-12`), code, local
review, merge requests, deployment note, merge, RFT with "Resolved: #1, #2" on Jira. Then QA
tests again: back to Step 10, until **QA Passed**.

## 6. What the cards mean

| Card | Means | What you do |
|---|---|---|
| **Plan for DEMO-12** | Tasks, order and interface per repo | `approve` / `guide` / `stop` |
| **Ready for review** (manual review gate) | Changes ready | `review DEMO-12` |
| **Review round n of m** | Numbered findings (only when you decide) | `fix …` / `accept` / `stop` |
| **Merge requests** | One card per round, in merge order, edited in place: Reviewing → Approved: ready to merge (or Accepted as it is) → each "(merged)" → All merged; the deployment note is added | Merge in that order |
| **Ready for testing** | All merged; test notes are on Jira | QA tests |
| **Bugs found** | The bugs read from the bug tickets | `fix DEMO-12 …` |
| **… needs your help** | Something stopped; the message says why in plain words (a question, a failed check, a limit, a machine problem) | `retry` / `guide` / `stop` |
| **Please confirm** | A destructive command waits for your answer | `y` or `n` |

**How much you hear** (`chat.verbosity: minimal`, the default): the plan, the merge-request card,
cards that need a decision, one message when something needs a person, and the testing messages.
Coding progress, review rounds and fixes are not posted: ask `status DEMO-12`. `verbosity: normal`
shows every step (for debugging).

**What Jira gets:** status changes, one comment per plan version (never edited), one *delivery
summary* comment kept up to date (status, current plan, merge requests, deployment note, every
decision people made), the deployment note as an attachment, and the QA comments.

## 7. Jira statuses

| Status | Set by | Means |
|---|---|---|
| To Do | People | Not started |
| In Dev | Pipeline | From the plan to the last merge (plan, coding, review, fixes, deployment note, merge wait) |
| Blocked | Pipeline | It needs a person (question, failed step, limit, stop, a migration to apply). `retry` / `guide` put the previous status back |
| RFT | Pipeline | All merge requests merged; ready for testing |
| Testing | QA | QA is testing |
| QA Failed | QA | QA filed bug tickets |
| QA Passed | QA | Done |

If you move a ticket to another status while the pipeline works on it, it stops at its next step
and says so. Move it back and `retry DEMO-12`, or `stop` it.

## 8. Accept, Stop, Pause

- **Accept** (`accept DEMO-12`): the remaining findings are fine or not worth it. Recorded as your override in the terminal, on Jira and on the merge requests.
- **Stop** (`stop DEMO-12`): halt one ticket (Jira → Blocked). `retry` continues from the same point.
- **Pause** (`pause: release freeze`): halt everything. Nothing new starts; waiting tickets carry on after `unpause`. Use before a deployment freeze or maintenance.

## 9. Limits and what they mean

| Limit | Default | When reached |
|---|---|---|
| Review rounds | 6 | The ticket asks you, with the open findings |
| Fix rounds that make no progress | 2 in a row | The ticket asks you |
| A finding disputed by the coder and kept by the reviewer | twice | The ticket asks you |
| QA rounds | 5 | Needs a person |
| Verify attempts per repo | 4 | Needs a person |
| Plan revisions + change requests | 10 | Refused, with the reason |
| An agent's usage limit | | Retried once on the other provider; if that fails too, a person |

## 10. How the AI is kept inside its limits

- It works only in its own git worktrees under the data folder, never in your clone.
- It cannot `git push`, `git stash`, `git reset --hard`, run migrations or seeds, or edit a repo's protected files (`shared_paths`); the pipeline undoes any attempt.
- Everything posted anywhere and every transcript is masked for secrets.
- Every agent claim is re-checked by the pipeline before anything is pushed.
- The review approves only when **every acceptance criterion is met with evidence** and every finding you asked to fix is confirmed fixed.

## 11. Running it, and what to do when it stops

| Task | How |
|---|---|
| Where is a ticket? | `status DEMO-12` (or `status` for all), `uv run pipeline slots` for agent slots |
| Why did it stop? | The "needs your help" message. Then `uv run pipeline logs DEMO-12` (add `--transcripts` for the agent transcripts under `~/.ai-pipeline/runs/DEMO-12/`) and `~/.ai-pipeline/logs/pipeline.log` |
| Try the step again | `retry DEMO-12` (add input with `guide DEMO-12: <text>`) |
| Pause everything | `pause: reason` then `unpause` |
| Numbers | `report` or `uv run pipeline report --html --days 7` |
| Start over | `reset DEMO-12` (local only) or `reset DEMO-12 --full` (also GitLab and Jira). Code a previous run already merged into the default branch stays there |
| Disk | `uv run pipeline gc` (`--cache` also clears the `node_modules` cache) |
| Restart the server | Steps that had not started are dropped; each cut-off step resumes once, and says so; a second cut-off asks a person. Prefer to restart while `uv run pipeline status` shows no running agent |

| Symptom | Cause / fix |
|---|---|
| "A problem on the machine … Redis isn't running" | Start the service it names (`brew services start redis`), then `retry DEMO-12`. No coder attempt was spent |
| "Someone moved the Jira ticket to …" | A person changed the status mid-run: move it back, `retry DEMO-12` |
| A ticket waits and nothing happens | `status DEMO-12`: is the pipeline paused (`unpause`)? waiting for you, a merge, or QA? |
| "Cannot implement DEMO-12: …" | The spec link is not a readable Google Doc, or the key does not start with the project key |
| The message mentions a provider limit | Your plan's usage limit; the other provider is tried once, otherwise wait or switch `--provider` |
| The review keeps asking for the same thing | Dismiss it with a reason (`fix DEMO-12 dismiss #2: …`) or `accept DEMO-12`, or put the rule in the repo's `REVIEW.md` / `CLAUDE.md` |
| Live tests blocked: "dev database has no data for this case" | The case needs reference data the dev DB lacks; it is blocked, not failed. Provide the data or accept |
| Anything else | `uv run pipeline logs DEMO-12 --transcripts` |

## 12. Rehearsing, and a demo run

```bash
uv run pipeline cli --dry-run          # all fake: Jira, GitLab, agents (~3 s per step), same cards
uv run pipeline run DEMO-7 --dry-run   # the whole flow without typing
```

For a real demo: a ticket whose change is **not** already in the default branch; `uv run pipeline check --agents`;
`uv run pipeline warm` (so there is no `npm ci` on stage); `uv run pipeline demo-reset --yes` (puts the
demo tickets named in `demo.tickets` back to the start); then `uv run pipeline cli`. After: `uv run pipeline report --html`.

## 13. Questions

- **A command was refused.** It must match what the ticket waits for; the reply says what it waits for (`status DEMO-12`).
- **I pushed a commit to the AI's branch.** Fine. The pipeline rebases onto it, and the next review looks at it.
- **Which agent did what, and how long did we wait?** `report` / `uv run pipeline report --html`.
- **Can two people work the same ticket?** Anyone at the terminal can give the commands; each command is recorded with who gave it.
