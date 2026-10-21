"""Review (Phase 5): the review gate, the `review` node (per-repo review + contract check,
settle the round, post as the **reviewer**) and the temporary `approve` node.

Settling a round (`settle_round`), with `fix_scope` from the last fix gate:

- findings the people chose last round (`fix_scope.ids`) go back to the reviewer; listed again
  with the same id = still open (reply "Still open"), left out = resolved (thread resolved);
- `deferred` findings (open, not chosen) are carried over by the orchestrator, untouched, and
  don't block approval; `disputed_by_human` ones are closed with the person's reason;
- anything else the reviewer lists is new and gets a fresh id `R<round>-<repo|X>-F<k>`;
- the verdict is recomputed: changes requested only while a blocker / major / human finding
  that isn't deferred is open.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pipeline.acceptance import (
    checks_finding,
    criterion_findings,
    hear_the_coder,
    live_evidence,
    required_criteria,
    settle_criteria,
    unmet,
    veto_finding,
    with_live,
)
from pipeline.agents import reviewer
from pipeline.chat.cards import Action, Card
from pipeline.chat.summaries import error_words, plural
from pipeline.findings import (
    BLOCKING,
    CONTRACT_TAG,
    counts_words,
    diff_new_lines,
    finding_id,
    finding_md,
    ids_from_markers,
    is_contract_id,
    is_derived_id,
    location,
    numbered,
    numbers_by_id,
    one_line,
    open_findings,
    plain_comment,
    position_ok,
    short,
    sort_findings,
)
from pipeline.graph.gates import gate_mode, gate_prepare, gate_wait
from pipeline.graph.nodes.common import (
    after_run,
    agent_slot,
    has_mr,
    is_local,
    ordered,
    review_diff,
)
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import halt
from pipeline.markers import gitlab_marker, has_marker, make_marker
from pipeline.schemas import Finding, ReviewResult
from pipeline.verify import WARNING


def mr_of(deps: Any, state: Any, repo: str) -> tuple[str, int]:
    return deps.config.repo(repo).gitlab_project, state["repos"][repo]["mr_iid"]


def threads_by_finding(deps: Any, key: str, project: str, iid: int) -> dict[str, dict]:
    """Unresolved reviewer threads of an MR, by the finding ids in their markers."""
    out: dict[str, dict] = {}
    for t in deps.gitlab.unresolved_threads(project, iid):
        for fid in ids_from_markers([n.get("body") or "" for n in t.get("notes", [])], key):
            out.setdefault(fid, t)
    return out


def _thread_has(thread: dict, marker: str) -> bool:
    return any(has_marker(n.get("body") or "", marker) for n in thread.get("notes", []))


# ---------- review gate ----------


def review_card(state: TicketState, deps: Any) -> Card:
    key = state["key"]
    return Card(
        title=f"{key}: ready for review" if is_local(state)
        else f"{key}: merge requests ready for review",
        sections=[
            "The changes are ready. When you're ready, ask me to review them." if is_local(state)
            else "The merge requests are open (links above). When you're ready, ask me to review "
            "them."
        ],  # fmt: skip
        actions=[
            Action("Start review", f"review {key}", style="positive"),
            Action("Stop", f"stop {key} --confirm", style="destructive"),
        ],
    )


def review_gate(state: TicketState, deps: Any) -> dict:
    update = gate_prepare("review", state, deps, review_card, round=state.get("review_round", 0))
    if (update.get("last_event") or {}).get("type") == "gate":  # auto: the Coder hands over
        many = len(state.get("repos") or {}) > 1
        what = ("changes" if many else "change") if is_local(state) else (
            "merge requests" if many else "merge request")  # fmt: skip
        deps.notifier.handoff(state["key"], "coder", "reviewer",
                              f"the {what} for {state['key']} {'are' if many else 'is'} "
                              "ready. Please review "
                              f"{'them' if many else 'it'}.")  # fmt: skip
    return update


def review_gate_wait(state: TicketState, deps: Any) -> dict:
    return gate_wait("review", state, deps, ("review", "change"),
                     round=state.get("review_round", 0), node="review_gate_wait")  # fmt: skip


# ---------- review ----------


def _dump(f: Finding, numbers: dict[str, int]) -> dict:
    return {**f.model_dump(mode="json"), "number": numbers.get(f.id)}


def _resolutions(state: TicketState) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for work in (state.get("repos") or {}).values():
        res = work.get("coder_result")
        for r in res.resolutions if res is not None else []:
            out[r.finding_id] = r.model_dump(mode="json")
    return out


def human_decision(state: TicketState) -> dict:
    """`.agent/human_decision.json` for the coder and the next reviewer."""
    scope = state.get("fix_scope") or {}
    prev = {f.id: f for f in open_findings(state)}
    numbers = numbers_by_id(state)
    rejected = scope.get("disputed_by_human") or {}
    return {
        "scope": scope.get("mode"),
        "chosen": [_dump(prev[i], numbers) for i in scope.get("ids") or [] if i in prev],
        "notes": scope.get("notes") or "",
        "decided_by": scope.get("by"),
        "deferred": [_dump(prev[i], numbers) for i in scope.get("deferred") or [] if i in prev],
        "disputed_by_human": [
            {"finding_id": i, "reason": (v or {}).get("reason", ""), "by": (v or {}).get("by")}
            for i, v in rejected.items()
        ],
    }


def _verdict(reviewer_verdict: str, findings: list[Finding]) -> str:
    """P9: a blocker / major / human finding blocks whether or not people deferred it (only
    `accept` gets past it), and the reviewer's own "changes requested" always stands."""
    blocking = any(f.severity in BLOCKING for f in findings)
    return "changes_requested" if blocking or reviewer_verdict != "approve" else "approve"


def _answered_open(f: Finding, answer: Any) -> Finding:
    """A chosen finding the reviewer didn't confirm fixed: still open, with why."""
    why = (f"Still open: {answer.note.strip()}" if answer is not None and answer.note.strip()
           else "Still open: the review didn't confirm it was fixed.")  # fmt: skip
    first = f.comment.split("\n\nStill open:")[0].rstrip()
    return f.model_copy(update={"comment": f"{first}\n\n{why}"})


def settle_round(state: TicketState, round_: int, results: dict[str, ReviewResult],
                 contract: ReviewResult | None,
                 failing: dict[str, list[str]] | None = None) -> dict:  # fmt: skip
    """New review results for the round + what to do in GitLab (see module docstring).

    P9 (audit F1/F2): a chosen finding closes only when the reviewer answers `fixed` /
    `withdrawn` in `previous` (or a person resolved its thread in GitLab); deferred findings
    are carried over and still count for the verdict; every required acceptance criterion that
    isn't met becomes (or keeps) an `A-` finding, and so do checks that fail on the reviewed
    commit (`failing`: repo → problems, P9 audit F3)."""
    repos = ordered(state)
    scope = state.get("fix_scope") or {}
    prev = {f.id: f for f in open_findings(state)}  # empty before any review or change
    chosen = set(scope.get("ids") or []) & set(prev)
    rejected = set(scope.get("disputed_by_human") or {})
    by_person = set(scope.get("skipped") or []) & chosen  # threads people resolved in GitLab
    deferred = (set(scope.get("deferred") or []) & set(prev)) - rejected
    deferred = {i for i in deferred if not is_derived_id(i)}  # those are settled afresh below
    answers = {p.finding_id: p for r in [*results.values(), contract] if r is not None
               for p in r.previous}  # fmt: skip
    resolutions = _resolutions(state)  # the coder's answers to the last fix run's findings
    heard: dict[str, list[Any]] = {}  # criteria accepted on the coder's word, per repo
    new: list[Finding] = []
    still_open: list[Finding] = []
    listed: set[str] = set()

    def take(res: ReviewResult, tag: str, fix_repo: Any) -> list[Finding]:
        out: list[Finding] = []
        k = 0
        for f in res.findings:
            if is_derived_id(f.id):  # the orchestrator's own: never taken from a reviewer
                continue
            target = fix_repo(f)
            if target != f.repo and state["repos"].get(f.repo, {}).get("status") == "merged":
                f = f.model_copy(
                    update={
                        "comment": f.comment.rstrip() + f"\n\n{f.repo} is "
                        f"already merged, so the fix goes into {target}."
                    }
                )
            f = f.model_copy(update={"repo": target})
            if f.id in chosen and f.id not in listed and f.id not in by_person:
                listed.add(f.id)
                still_open.append(f)
                out.append(f)
            elif f.id in prev:  # deferred / rejected: people decided, the orchestrator owns it
                continue
            else:
                k += 1
                f = f.model_copy(update={"id": finding_id(round_, tag, k)})
                new.append(f)
                out.append(f)
        return out

    def unanswered(pred: Any) -> list[Finding]:
        """Chosen findings the reviewer left out: open unless it said fixed / withdrawn."""
        out = []
        for i in sorted(chosen - listed - by_person):
            f = prev[i]
            if is_derived_id(i) or not pred(f):
                continue
            answer = answers.get(i)
            if answer is not None and answer.outcome in ("fixed", "withdrawn"):
                continue
            f = _answered_open(f, answer)
            still_open.append(f)
            out.append(f)
        return out

    def derived(repo: str, tag: str, res: ReviewResult, found: list[Finding]) -> list[Finding]:
        """Unmet criteria (per repo) and a bare veto → the orchestrator's findings."""
        out: list[Finding] = []
        if tag != CONTRACT_TAG:
            required = required_criteria(state, repo)
            answers = settle_criteria(required, res.acceptance_criteria_check)
            answers, accepted = hear_the_coder(answers, tag, resolutions)
            heard[repo] = accepted
            out += criterion_findings(repo, tag, required, answers)
            if repo in (failing or {}):
                out.append(checks_finding(repo, tag, (failing or {})[repo]))
        if res.verdict != "approve" and not found and not out:
            out.append(veto_finding(repo, tag, res))
        out = [f for f in out if f.id not in rejected]  # a person dismissed it, with a reason
        for f in out:
            if f.id not in prev:
                new.append(f)
            elif f.id in chosen:
                still_open.append(f)
        return out

    # a contract finding must land on an MR that can still change (K40): a merged repo's side
    # is fixed on the open side, preferably the gateway
    open_repos = [r for r in repos if state["repos"][r].get("status") != "merged"] or repos
    gateway = next((r for r in open_repos if state["repos"][r].get("role") == "gateway"),
                   open_repos[0])  # fmt: skip

    def contract_repo(f: Finding) -> str:
        return f.repo if f.repo in open_repos else gateway

    new_results: dict[str, ReviewResult] = {}
    derived_now: set[str] = set()
    settled_tags: set[str] = set()
    for repo in repos:
        res = results.get(repo)
        if res is None:
            continue
        settled_tags.add(repo)
        found = take(res, repo, lambda f, repo=repo: repo)
        found += unanswered(lambda f, repo=repo: not is_contract_id(f.id) and f.repo == repo)
        found += [prev[i] for i in sorted(deferred)
                  if not is_contract_id(i) and prev[i].repo == repo]  # fmt: skip
        mine = derived(repo, repo, res, found)
        derived_now |= {f.id for f in mine}
        found += mine
        new_results[repo] = res.model_copy(
            update={"scope": repo, "findings": found, "verdict": _verdict(res.verdict, found)}
        )
    new_contract = None
    if contract is not None:
        settled_tags.add(CONTRACT_TAG)
        found = take(contract, CONTRACT_TAG, contract_repo)
        found += unanswered(lambda f: is_contract_id(f.id))
        found += [prev[i] for i in sorted(deferred) if is_contract_id(i)]
        mine = derived(gateway, CONTRACT_TAG, contract, found)
        derived_now |= {f.id for f in mine}
        found += mine
        new_contract = contract.model_copy(
            update={
                "scope": "contract",
                "findings": found,
                "verdict": _verdict(contract.verdict, found),
            }  # fmt: skip
        )
    open_ids = {f.id for f in still_open}
    # an orchestrator finding of a scope reviewed this round that no longer applies is resolved
    gone = {i for i in prev if is_derived_id(i) and i not in derived_now
            and _derived_tag(i, prev[i]) in settled_tags}  # fmt: skip
    return {
        "results": new_results,
        "contract": new_contract,
        "new": new,
        "still_open": still_open,
        "resolved": [prev[i] for i in sorted((chosen - open_ids - derived_now) | gone)],
        "rejected": [prev[i] for i in sorted(rejected & set(prev))],
        "deferred": deferred,
        "heard": heard,
    }


def _derived_tag(fid: str, f: Finding) -> str:
    return CONTRACT_TAG if is_contract_id(fid) else f.repo


def _post_round(state: TicketState, deps: Any, round_: int, settled: dict, numbers: dict,
                diffs: dict[str, str]) -> None:  # fmt: skip
    """GitLab, reviewer identity only (§9.2). Idempotent through markers."""
    key = state["key"]
    gl = deps.gitlab
    repos = [r for r in ordered(state) if has_mr(state, r)]  # D207: none before the approval
    if not repos:
        return
    urls = {r: state["repos"][r].get("mr_url") for r in repos}
    lines = {r: diff_new_lines(d) for r, d in diffs.items()}
    nits: dict[str, list[Finding]] = {}
    for f in sort_findings([f for f in settled["new"] if f.repo in repos], state):
        if f.severity == "nit":
            nits.setdefault(f.repo, []).append(f)
            continue
        project, iid = mr_of(deps, state, f.repo)
        inline = position_ok(f, lines.get(f.repo, {}))
        links = ""
        if is_contract_id(f.id):
            others = [f"[{r}]({urls[r]})" for r in repos if r != f.repo and urls.get(r)]
            links = "Other side: " + ", ".join(others) if others else ""
        body = finding_md(numbers.get(f.id), f, inline, links)
        gl.add_inline(project, iid, f.file if inline else "", f.line if inline else 0, body,
                      make_marker(key, "review", round_, f.id))  # fmt: skip
    for repo, items in nits.items():
        project, iid = mr_of(deps, state, repo)
        body = "**Nits** (small, optional)\n\n" + "\n".join(
            f"- #{numbers.get(f.id, 0)}" + (f" `{location(f)}`" if f.file else "")
            + f": {short(plain_comment(f))}"
            for f in items)  # fmt: skip
        gl.add_note(project, iid, body, make_marker(key, "review", round_, f"nits-{repo}"))

    # earlier findings: still open → reply; resolved → resolve; rejected by people → close
    earlier = [(f, "open") for f in settled["still_open"]] + \
              [(f, "resolved") for f in settled["resolved"]] + \
              [(f, "rejected") for f in settled["rejected"]]  # fmt: skip
    rejected_by = (state.get("fix_scope") or {}).get("disputed_by_human") or {}
    by_repo: dict[str, dict[str, dict]] = {}
    for f, what in earlier:
        if f.severity == "nit" or f.repo not in repos:
            continue
        if f.repo not in by_repo:
            by_repo[f.repo] = threads_by_finding(deps, key, *mr_of(deps, state, f.repo))
        thread = by_repo[f.repo].get(f.id)
        if thread is None:
            continue  # already resolved (by a person in GitLab, or an earlier run)
        project, iid = mr_of(deps, state, f.repo)
        marker = make_marker(key, "review", round_, f"{what}-{f.id}")
        if what == "open":
            n = numbers.get(f.id)
            first = short(plain_comment(f))
            text = f"Still open{f' (now #{n})' if n else ''}: {first}"
        elif what == "resolved":
            text = "Resolved."
        else:
            who = rejected_by.get(f.id) or {}
            reason = who.get("reason") or "no reason given"
            text = f"Closed: {who.get('by') or 'a person'} said “{reason}”"
        if not _thread_has(thread, marker):
            gl.reply(project, iid, thread["id"], text + gitlab_marker(marker), as_role="reviewer")
        if what != "open":
            gl.resolve(project, iid, thread["id"])

    for repo in repos:
        res = settled["results"].get(repo)
        if res is None:
            continue
        project, iid = mr_of(deps, state, repo)
        mine = [f for f in open_findings({"repos": {repo: {"review_result": res}},
                                          "contract_review": settled["contract"]})
                if f.repo == repo]  # fmt: skip
        verdict = "approved" if res.verdict == "approve" else "changes requested"
        # short (D109): verdict + counts, the reviewer's summary, unmet acceptance criteria only;
        # the findings themselves are the threads
        head = f"**Review round {round_}: {verdict}**"
        if mine:
            head += f" · {counts_words(mine)}"
        note = [head, short(res.summary)]
        settled_now, _ = hear_the_coder(
            settle_criteria(required_criteria(state, repo), res.acceptance_criteria_check),
            repo, _resolutions(state))  # fmt: skip
        missing = unmet(settled_now)
        if missing:
            note.append("Not met yet:\n" + "\n".join(
                f"- {c.criterion}" + (f": {short(c.note)}" if c.note else "")
                for c in missing))  # fmt: skip
        heard = (settled.get("heard") or {}).get(repo) or []
        if heard:
            note.append("Accepted on the coder's word (not verified by the review):\n" + "\n".join(
                f"- {c.criterion}: {short(c.note.split(': ', 1)[-1])}" for c in heard))
        gl.add_note(project, iid, "\n\n".join(p for p in note if p),
                    make_marker(key, "review", round_, "summary"))  # fmt: skip


def findings_details_md(state: Any, findings: list[Finding], numbers: dict[str, int]) -> str:
    """The numbered list (fixed-format headings) with MR links, for Jira and the chat."""
    repos = state.get("repos") or {}
    out = []
    for f in sort_findings(findings, state):
        url = (repos.get(f.repo) or {}).get("mr_url")
        link = f" ([merge request]({url}))" if url else ""
        out.append(f"- {one_line(numbers.get(f.id, 0), f)}{link}")
    return "\n".join(out)


CONTRACT_JOB = "(contract)"


def review(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    round_ = state.get("review_round", 0) + 1
    repos = ordered(state)
    scope = state.get("fix_scope") or {}
    prev = {f.id: f for f in open_findings(state)}
    chosen = set(scope.get("ids") or [])
    numbers_before = numbers_by_id(state)
    resolutions = _resolutions(state)
    decision = human_decision(state) if scope.get("ids") is not None else None

    def previous_for(pred: Any) -> tuple[list[dict], list[dict]]:
        # criteria findings (A-…) are answered through acceptance_criteria_check, not here
        items = [prev[i] for i in sorted(chosen)
                 if i in prev and not is_derived_id(i) and pred(prev[i])]  # fmt: skip
        return ([_dump(f, numbers_before) for f in items],
                [resolutions[f.id] for f in items if f.id in resolutions])  # fmt: skip

    # K40: a merged MR can't change any more, so a re-review (people pushed to another MR after
    # the approval, D116) reviews only the open ones; the contract check still sees every diff
    reviewing = [r for r in repos if state["repos"][r].get("status") != "merged"]
    heads: dict[str, str] = {}
    checks: dict[str, Any] = {r: state["repos"][r].get("verify_result") for r in reviewing}
    for repo in reviewing:  # K35: someone may have pushed to the MR branch in GitLab
        work = state["repos"][repo]
        path = deps.workspaces.worktree_path(key, repo)
        if work.get("branch") and work.get("mr_iid"):
            deps.workspaces.refresh(path, work["branch"])
        heads[repo] = deps.workspaces.head(path)
        # P9 (audit F3): checks that ran on another commit are run again on this one first
        last = checks[repo]
        if last is not None and last.head_sha and last.head_sha != heads[repo]:
            checks[repo] = deps.verifier.verify(key, repo, state.get("plan"))
            deps.store.record_event(key, "node_end", node="review", repo=repo,
                                    data={"reverified": heads[repo][:12],
                                          "ok": checks[repo].ok})  # fmt: skip
    # P10: criteria a live test proved on this code. The reviewer is told not to verify them again
    # and the same set is credited as met below (`with_live`), so what it was told is what counts
    proven: dict[str, dict[str, str]] = {
        r: live_evidence(deps, key, r, state["repos"].get(r), heads.get(r)) for r in reviewing}
    diffs: dict[str, str] = {r: review_diff(deps, state, r) for r in repos}
    # P9 (time): from round 2 the reviewer gets what the fixes changed, and may raise new
    # findings only there (a blocker anywhere): findings noticed late in untouched code kept
    # DEMO-11's gateway in review for four rounds
    since: dict[str, str] = {}
    for repo in reviewing:
        old = state["repos"][repo].get("reviewed_sha")
        if old and old != heads[repo]:
            since[repo] = deps.workspaces.diff_between(
                deps.workspaces.worktree_path(key, repo), old, heads[repo])
    # P9 (cost): a later round reviews only what changed; an untouched repo keeps its result
    unchanged = unchanged_repos(state, reviewing, heads, chosen)
    reviewing = [r for r in reviewing if r not in unchanged]
    run_contract = len(repos) > 1 and contract_needed(state, deps, reviewing, heads, chosen)
    contract_skipped = len(repos) > 1 and not run_contract
    if unchanged or contract_skipped:
        deps.store.record_event(key, "node_end", node="review",
                                data={"skipped": sorted(unchanged),
                                      "contract_skipped": contract_skipped})  # fmt: skip
    what = "merge requests" if len(reviewing) > 1 else "merge request"
    if is_local(state) and not any(has_mr(state, r) for r in repos):
        what = "changes" if len(reviewing) > 1 else "change"
    deps.notifier.progress(key, f"Reviewing the {what} for {key}.", "reviewer")

    def review_one(repo: str) -> tuple[Any, Any, Any]:
        previous, res = previous_for(lambda f: f.repo == repo and not is_contract_id(f.id))
        with agent_slot(state, deps, "reviewer") as slot:
            result, run = reviewer.review_repo(
                deps, key, repo, round_, diffs[repo],
                verify=checks[repo], previous=previous,
                resolutions=res, decision=decision, slot=slot.name,
                provider_override=state.get("provider_override"),
                acceptance=state["repos"][repo].get("acceptance_result"),
                proven=proven.get(repo), criteria_texts=dict(required_criteria(state, repo)),
                since=since.get(repo, ""),
            )  # fmt: skip
        return result, run, slot

    def check_contract() -> tuple[Any, Any, Any]:
        previous, res = previous_for(lambda f: is_contract_id(f.id))
        with agent_slot(state, deps, "contract_check") as slot:
            result, run = reviewer.contract_check(
                deps, key, round_, diffs, previous=previous, resolutions=res,
                decision=decision, slot=slot.name,
                provider_override=state.get("provider_override"), since=since,
            )  # fmt: skip
        return result, run, slot

    outcomes: dict[str, tuple[Any, Any, Any]] = {}
    if deps.config.agents.single_session and (reviewing or run_contract):
        # one reviewer session for every repo and the contract: each prompt's fixed part is paid
        # once, and the contract check reads the repos it is reviewing anyway
        earlier, answers = previous_for(lambda f: True)
        with agent_slot(state, deps, "reviewer_ticket") as slot:
            by_scope, run = reviewer.review_ticket(
                deps, key, round_, diffs, reviewing, contract=run_contract, verify=checks,
                previous=earlier, resolutions=answers, decision=decision, since=since,
                acceptance={r: state["repos"][r].get("acceptance_result") for r in reviewing},
                proven=proven,
                criteria_texts={r: dict(required_criteria(state, r)) for r in reviewing},
                slot=slot.name, provider_override=state.get("provider_override"),
            )  # fmt: skip
        for name in [*reviewing, *([CONTRACT_JOB] if run_contract else [])]:
            wanted = "contract" if name == CONTRACT_JOB else name
            outcomes[name] = ((by_scope or {}).get(wanted), run, slot)
    else:
        # Every review and the contract check at once: read-only and independent (D129).
        jobs: dict[str, Any] = {r: (review_one, r) for r in reviewing}
        if run_contract:
            jobs[CONTRACT_JOB] = (check_contract,)
        with ThreadPoolExecutor(max_workers=max(len(jobs), 1),
                                thread_name_prefix=f"{key}-review") as ex:  # fmt: skip
            futures = {name: ex.submit(*job) for name, job in jobs.items()}
            outcomes = {name: f.result() for name, f in futures.items()}

    results: dict[str, ReviewResult] = {}
    contract: ReviewResult | None = None
    failure: dict | None = None
    for name, (result, run, _slot) in outcomes.items():  # repos in order, then the contract
        if result is None and failure is None:
            what = "The contract check" if name == CONTRACT_JOB else f"The review of {name}"
            failure = halt("review", f"{what} failed twice: {error_words(run.error)}")
        elif name == CONTRACT_JOB:
            contract = result
        elif result is not None:
            results[name] = result
    update = after_run(state, deps, next(iter(outcomes.values()))[2]) if outcomes else {}
    if failure is not None:
        return {**update, **failure}

    failing = {r: [p for p in c.problems if not p.startswith(WARNING)] or ["checks failed"]
               for r, c in checks.items() if c is not None and not c.ok}  # fmt: skip
    # P10: criteria a live acceptance test proved on this code count as met (real evidence)
    results = {r: with_live(res, required_criteria(state, r), proven.get(r) or {})
               for r, res in results.items()}  # fmt: skip
    settled = settle_round(state, round_, results, contract, failing)
    new_repos: dict[str, RepoWork] = {}
    for repo in repos:
        res = settled["results"].get(repo)
        if res is None:  # merged: not reviewed again (K40)
            new_repos[repo] = state["repos"][repo]
            continue
        new_repos[repo] = {**state["repos"][repo], "review_result": res,  # type: ignore[typeddict-item]
                           "verify_result": checks.get(repo), "reviewed_sha": heads.get(repo),
                           "status": "merged" if state["repos"][repo].get("status") == "merged"
                           else "approved" if res.verdict == "approve"
                           else "changes_requested"}  # fmt: skip
    contract_now = settled["contract"] if run_contract else state.get("contract_review")
    after = {**state, "repos": new_repos, "contract_review": contract_now}
    numbers = numbers_by_id(after)
    _post_round(state, deps, round_, settled, numbers, diffs)

    verdicts = [r.verdict for r in settled["results"].values()]
    # a kept result blocks only with a blocking finding still open (a bare veto is already an
    # A-…-V1 major; minors left open on purpose don't block)
    for r in unchanged:
        kept = state["repos"][r].get("review_result")
        blocking = kept is not None and any(f.severity in BLOCKING for f in kept.findings)
        verdicts.append("changes_requested" if blocking else "approve")
    if contract_now is not None:
        verdicts.append(contract_now.verdict)
    approved = all(v == "approve" for v in verdicts)
    open_now = open_findings(after)
    deps.notifier.review_done(key, round_, approved, _summary_facts(after, open_now),
                              findings_details_md(after, open_now, numbers),
                              next_auto=gate_mode("fix", deps, state) == "auto")  # fmt: skip
    deps.store.upsert_ticket(key, review_rounds=round_)  # Jira stays In Dev (D127)

    # the coder disputed a finding and the reviewer kept it open, twice → people decide (4.5)
    disputes = dict(scope.get("disputes") or {})
    for f in settled["still_open"]:
        if (resolutions.get(f.id) or {}).get("action") == "disputed":
            disputes[f.id] = disputes.get(f.id, 0) + 1
    new_scope = {"disputed_by_human": dict(scope.get("disputed_by_human") or {}),
                 "disputes": disputes, "deferred": sorted(settled["deferred"])}  # fmt: skip
    log = list(state.get("review_log") or [])
    for kind, items in (("new", settled["new"]), ("resolved", settled["resolved"]),
                        ("rejected", settled["rejected"])):  # fmt: skip
        for f in items:
            n = numbers.get(f.id) or numbers_before.get(f.id) or 0
            log.append({"round": round_, "repo": f.repo, "kind": kind, "line": one_line(n, f)})
    out: dict = {**update, "repos": new_repos, "contract_review": contract_now,
                    "review_round": round_, "fix_scope": new_scope, "review_log": log,
                    "last_event": {"type": "reviewed", "round": round_,
                                   "approved": approved}}  # fmt: skip
    # P9 (DEMO-10): a finding that needs a change to shared code goes to a person at once
    stuck = [f for f in settled["still_open"]
             if disputes.get(f.id, 0) >= 2 or (disputes.get(f.id) and shared_conflict(
                 f.comment, (resolutions.get(f.id) or {}).get("note")))]  # fmt: skip
    stuck += [f for f in open_now if f not in stuck and f.severity in BLOCKING
              and shared_conflict(f.comment)]  # fmt: skip
    if stuck and not approved:
        items = "\n".join(f"- {one_line(numbers.get(f.id, 0), f)}" for f in stuck)
        why = ("these findings need a change to shared code (or the coder and the reviewer "
               "disagree on them twice now)")
        return {**out, **halt("review", f"{why[0].upper()}{why[1:]}, so I need a person to decide "
                                 "(`accept` to merge without them, or `guide` with what to do):"
                                 f"\n\n{items}")}  # fmt: skip
    # P9 (W15 lite): two fix rounds in a row that closed none of the blocking findings they
    # worked on → a person decides, instead of spending every remaining round on the same thing
    # (findings the coder disputes have their own rule above)
    tried = {i for i in chosen if i in prev and prev[i].severity in BLOCKING
             and (resolutions.get(i) or {}).get("action") != "disputed"}  # fmt: skip
    left = {f.id for f in open_now if f.severity in BLOCKING}
    stalled = scope.get("stalled", 0) + 1 if tried and tried <= left else 0
    out["fix_scope"] = {**new_scope, "stalled": stalled}
    if not approved and stalled >= 2:
        items = findings_details_md(after, [f for f in open_now if f.id in tried], numbers)
        return {**out, **halt("review", "Two fix rounds in a row didn't close these findings, "
                                 f"so I need a person to decide:\n\n{items}")}  # fmt: skip
    limit = deps.config.limits.max_review_rounds
    if not approved and round_ >= limit:
        items = findings_details_md(after, open_now, numbers)
        return {**out, **halt("review", f"The review still asks for changes after "
                                 f"{plural(round_, 'round')} (the limit), so I need a person to "
                                 f"decide. Open findings:\n\n{items}")}  # fmt: skip
    return out


def shared_conflict(*texts: str | None) -> bool:
    """The reviewer or the coder says only a change to shared code would settle it."""
    return any("shared code:" in (t or "").lower() for t in texts)


def unchanged_repos(state: Any, reviewing: list[str], heads: dict[str, str],
                    chosen: set[str]) -> set[str]:  # fmt: skip
    """Repos a later round needn't review again (P9, cost; DEMO-9 re-reviewed an untouched
    repo twice): same commit as at their last review, and none of their findings were sent
    for fixing (a disputed finding leaves the code unchanged but needs the reviewer's answer)."""
    out: set[str] = set()
    sent = {f.repo for f in open_findings(state) if f.id in chosen}
    for repo in reviewing:
        work = state["repos"][repo]
        if (work.get("review_result") is not None and work.get("reviewed_sha")
                and work.get("reviewed_sha") == heads.get(repo) and repo not in sent):  # fmt: skip
            out.add(repo)
    return out


# changes that can't alter how the repos fit together (P9): tests, docs, translations, DB scripts
_NOT_CONTRACT = re.compile(
    r"(^|/)(test|tests|__tests__|spec|docs?|locales|migrations|seeders)/|\.(md|txt)$", re.I
)


def contract_needed(state: Any, deps: Any, reviewing: list[str], heads: dict[str, str],
                    chosen: set[str]) -> bool:  # fmt: skip
    """The cross-repo contract check runs again only when it can find something new (P9, cost):
    no result yet, it has open findings or one is being fixed, or a repo reviewed again changed
    any source file since its last review. Conservative on purpose (accuracy first): a shared
    helper can change error mapping, so only tests, docs, translations and DB scripts are
    left out."""
    last = state.get("contract_review")
    if last is None or last.findings or any(is_contract_id(i) for i in chosen):
        return True
    ws = deps.workspaces
    for repo in reviewing:
        old = state["repos"][repo].get("reviewed_sha")
        files = (ws.changed_between(ws.worktree_path(state["key"], repo), old, heads[repo])
                 if old else None)  # fmt: skip
        if files is None or any(not _NOT_CONTRACT.search(f) for f in files):
            return True
    return False


def _summary_facts(state: Any, findings: list[Finding]) -> dict:
    by_repo: dict[str, list[Finding]] = {}
    for f in findings:
        where = "contract" if is_contract_id(f.id) else f.repo
        by_repo.setdefault(where, []).append(f)
    return {"per repository": [f"{r}: {counts_words(fs)}" for r, fs in by_repo.items()],
            "total": counts_words(findings)}  # fmt: skip


# ---------- approve (then the deployment note, D121) ----------


def moved_since_review(state: TicketState, deps: Any) -> dict[str, str]:
    """Open MRs whose branch head is no longer the commit the last review saw (P9, audit F3):
    repo → the new head. Any new commit counts, whoever wrote it."""
    moved: dict[str, str] = {}
    ws = deps.workspaces
    for repo in ordered(state):
        work = state["repos"][repo]
        reviewed = work.get("reviewed_sha")
        if work.get("status") == "merged" or not reviewed or not work.get("branch"):
            continue
        path = ws.worktree_path(state["key"], repo)
        # before the merge request exists the branch is only here (D207)
        head = ws.remote_head(path, work["branch"]) if work.get("mr_iid") else ws.head(path)
        if head and head != reviewed:
            moved[repo] = head
    return moved


def approve(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    repos = ordered(state)
    override = state.get("override")
    moved = moved_since_review(state, deps)
    if moved:  # never approve a commit nobody reviewed: back to the review first
        heads = {r: h[:12] for r, h in moved.items()}
        deps.store.record_event(key, "node_end", node="approve", data={"stale_review": heads})
        return {"override": None, "last_event": {"type": "stale_review", "repos": sorted(moved)}}
    if is_local(state) and not any(has_mr(state, r) for r in repos):
        # D207: approved on this machine; publish_mrs pushes, opens the merge requests, posts the
        # review summary and the approval, and tells the chat
        new_repos = {r: {**w, "status": "merged" if w.get("status") == "merged" else "approved"}
                     for r, w in state["repos"].items()}  # fmt: skip
        deps.store.record_event(key, "node_end", node="approve", data={"local": True})
        return {"repos": new_repos, "last_event": {"type": "approved_local"}}
    for repo in repos:
        if state["repos"][repo].get("status") == "merged":
            continue
        project, iid = mr_of(deps, state, repo)
        if override:
            items = "\n".join(f"- {line}" for line in override.get("open_findings") or []) or \
                "- (none)"  # fmt: skip
            deps.gitlab.add_note(
                project, iid,
                f"**Accepted as it is** by {override.get('by')} ({override.get('at')}), "
                f"without fixing these open findings:\n\n{items}",
                make_marker(key, "approve", state.get("review_round", 0), "override"),
            )  # fmt: skip
        deps.gitlab.approve(project, iid, sha=state["repos"][repo].get("reviewed_sha"))
    new_repos = {r: {**w, "status": "merged" if w.get("status") == "merged" else "approved"}
                 for r, w in state["repos"].items()}  # fmt: skip
    mrs = [{"repo": r, "role": state["repos"][r].get("role", ""),
            "url": state["repos"][r].get("mr_url")} for r in repos]  # fmt: skip
    # P9: minor findings nobody fixed are shown on the MR card, so people can still ask for them
    left = [] if override else [one_line(n, f) for n, f in numbered(state)]
    deps.notifier.approved(key, mrs, override, round=state.get("review_round", 0), left=left)
    deps.store.upsert_ticket(key, waiting_for=None)  # In Dev until merged (D127)
    return {"repos": new_repos, "last_event": {"type": "approved"}}
