"""Fix loop (Phase 5): the fix gate (findings card), `fix` (choose the findings from the
unresolved reviewer threads, send the repos with chosen findings back to the coder), then the
Phase 3 `code_next_repo` / `verify` loop in fix mode, `push_fixes` (coder identity), and the
re-review gate.

`fix_scope` carries the decision into the fix and the next review:
`{mode, ids, numbers, notes, by, round, deferred, disputed_by_human, disputes}`; `fix` adds
`active`, `findings` (per repo, what the coder gets) and `threads` (finding id → discussion).
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.chat.cards import Action, Card
from pipeline.chat.summaries import join_words
from pipeline.findings import (
    SCOPE_WORDS,
    counts_words,
    in_scope,
    note_for,
    numbered,
    numbers_by_id,
    one_line,
    open_findings,
    resolve_choice,
    short,
)
from pipeline.graph.gates import gate_mode, gate_prepare, gate_wait
from pipeline.graph.nodes.common import has_mr, ordered, reset_repo
from pipeline.graph.nodes.review import human_decision, mr_of, threads_by_finding
from pipeline.graph.nodes.sync import sync_before_push
from pipeline.graph.state import RepoWork, TicketState
from pipeline.markers import gitlab_marker, has_marker, make_marker

# "dismiss #2: " in front of the reason a person gave (the grammar word, not the reason)
_DISMISS_LEAD = re.compile(r"^\s*dismiss\s+[#\d\s,&]*(?:and[#\d\s,&]*)*[:—–-]?\s*", re.I)

RESOLUTION_WORDS = {"fixed": "Fixed", "disputed": "Not changed",
                    "not_applicable": "Not applicable"}  # fmt: skip


def choosable(state: Any) -> list[Any]:
    """Open findings people can still choose (not rejected by a person earlier)."""
    rejected = set((state.get("fix_scope") or {}).get("disputed_by_human") or {})
    return [f for f in open_findings(state) if f.id not in rejected]


def refs(ids: list[str], numbers: dict[str, int]) -> str:
    """ "#1 and #3", in number order."""
    ids = sorted(ids, key=lambda i: (numbers.get(i) is None, numbers.get(i) or 0, i))
    return join_words([f"#{numbers[i]}" if i in numbers else i for i in ids]) if ids else "none"


# ---------- fix gate ----------


def fix_card(state: TicketState, deps: Any) -> Card:
    key = state["key"]
    round_ = state.get("review_round", 0)
    deferred = set((state.get("fix_scope") or {}).get("deferred") or [])
    items = numbered(state)
    urls = {r: w.get("mr_url") for r, w in state["repos"].items()}
    lines = []
    for n, f in items:  # short (D109): number, severity, where, one sentence, a link
        url = urls.get(f.repo)
        tail = f" [view]({url})" if url else ""
        tail += " (left open earlier)" if f.id in deferred else ""
        lines.append(f"- {one_line(n, f, with_location=False)}{tail}")
    limit = deps.config.limits.max_review_rounds
    total = counts_words([f for _, f in items])
    return Card(
        # before any review, the open findings are a person's change requests (§4b)
        title=(
            f"{key}: review round {round_} of {limit}: {total}"
            if round_
            else f"{key}: changes to make"
        ),  # fmt: skip
        # posted by the Coder (D111): the person tells it which findings to fix
        sections=["Which of these should I fix?", "\n".join(lines)],
        actions=[
            Action("Fix blockers", f"fix {key} blockers"),
            Action("Fix major+", f"fix {key} major", style="positive"),
            Action("Fix all", f"fix {key} all"),
            Action(
                "Fix selected…",
                f"fix {key}:",
                input_hint="<numbers and notes, e.g. 1, 3: dismiss #2, the helper checks it>",
            ),
            Action("Accept as is", f"accept {key} --confirm", style="destructive"),
            Action("Stop", f"stop {key} --confirm", style="destructive"),
        ],
    )


def fix_gate(state: TicketState, deps: Any) -> dict:
    update = gate_prepare("fix", state, deps, fix_card, round=state.get("review_round", 0))
    if (update.get("last_event") or {}).get("type") == "gate":  # auto: the default scope
        scope = choose(state, deps, {}, "auto")
        update["fix_scope"] = scope
        numbers = numbers_by_id(state)  # the Reviewer hands the findings to the Coder (D112)
        left = scope.get("deferred") or []
        where = ("the merge requests" if any(has_mr(state, r) for r in state["repos"])
                 else "the review")  # fmt: skip
        text = (f"please fix {refs(scope['ids'], numbers)}. The details are in the comments on "
                f"{where} and in Jira." if where != "the review" else
                f"please fix {refs(scope['ids'], numbers)}. The details are in the review "
                "summary and in Jira.")  # fmt: skip
        if left:
            text += (
                f" {refs(left, numbers)} can stay as {'it is' if len(left) == 1 else 'they are'}."
            )
        deps.notifier.handoff(state["key"], "reviewer", "coder", text)
    return update


def choose(state: TicketState, deps: Any, args: dict, by: str) -> dict:
    """The fix gate's decision → `fix_scope` (3.2). Findings the person explicitly dismissed
    (`dismissed_numbers`: `dismiss #2`, or the Chat LLM for "#2 is fine because …") are
    recorded as `disputed_by_human` with the person's words and never re-raised. A number that
    is only mentioned in the notes ("#2 is still broken") decides nothing (P9, audit F2)."""
    scope = dict(state.get("fix_scope") or {})
    numbers = numbers_by_id(state)
    notes = str(args.get("notes") or "").strip()
    rejected = dict(scope.get("disputed_by_human") or {})
    by_n = {n: f for n, f in numbered(state)}
    for n in args.get("dismissed_numbers") or []:
        f = by_n.get(int(n))
        if f is not None:
            reason = _DISMISS_LEAD.sub("", note_for(int(n), notes) or notes).strip()
            rejected[f.id] = {"by": by, "reason": reason or "dismissed without a reason"}
    findings = [f for f in choosable(state) if f.id not in rejected]
    mode = args.get("scope") or deps.config.chat.default_fix_scope
    if mode == "ids":
        ids, _unknown = resolve_choice(state, args.get("finding_numbers") or [],
                                       args.get("finding_ids") or [])  # fmt: skip
        chosen = [f.id for f in findings if f.id in ids]
    else:
        chosen = [f.id for f in in_scope(mode, findings)]
    if not chosen:  # e.g. "blockers" with only majors open: fall back to the default rule
        mode = "major+"
        chosen = [f.id for f in in_scope(mode, findings)] or [f.id for f in findings]
    deferred = [f.id for f in findings if f.id not in chosen and f.id not in rejected]
    return {**scope, "mode": mode, "ids": chosen, "numbers": {i: numbers.get(i) for i in chosen},
            "notes": notes, "by": by, "round": state.get("review_round", 0),
            "deferred": deferred, "disputed_by_human": rejected}  # fmt: skip


def fix_gate_wait(state: TicketState, deps: Any) -> dict:
    round_ = state.get("review_round", 0)
    update = gate_wait("fix", state, deps, ("fix", "accept", "change"), round=round_,
                       node="fix_gate_wait")  # fmt: skip
    ev = update.get("last_event") or {}
    if ev.get("type") != "gate" or ev.get("action") == "change":
        return update
    by = ev.get("by") or "someone"
    if ev.get("action") == "accept":  # 3.3: human override, recorded everywhere
        items = numbered(state)
        update["override"] = {
            "by": by, "at": deps.clock().isoformat(timespec="seconds"),
            "open_findings": [one_line(n, f) for n, f in items],
            "finding_ids": [f.id for _, f in items],
        }  # fmt: skip
        deps.store.record_event(state["key"], "gate_pass", node="fix",
                                data={"override": update["override"]})  # fmt: skip
        return update
    update["fix_scope"] = choose(state, deps, ev.get("args") or {}, by)
    return update


# ---------- fix ----------


def fix(state: TicketState, deps: Any) -> dict:
    """Choose the findings per repo from GitLab (unresolved reviewer threads, by marker;
    nits come from the review result: they share one note, not threads) and send those repos
    back to the coder. Unchosen findings get nothing."""
    key = state["key"]
    scope = dict(state.get("fix_scope") or {})
    chosen = set(scope.get("ids") or [])
    numbers = numbers_by_id(state)
    notes = scope.get("notes") or ""
    decision = human_decision(state)
    threads: dict[str, str] = {}
    findings: dict[str, list[dict]] = {}
    skipped: list[str] = []
    by_repo: dict[str, dict[str, dict]] = {}
    for f in [f for f in open_findings(state) if f.id in chosen]:
        if f.severity != "nit" and has_mr(state, f.repo):  # D207: no threads before the MR
            if f.repo not in by_repo:
                by_repo[f.repo] = threads_by_finding(deps, key, *mr_of(deps, state, f.repo))
            thread = by_repo[f.repo].get(f.id)
            if thread is None:  # a person resolved the thread in GitLab: not ours to fix now
                skipped.append(f.id)
                continue
            threads[f.id] = thread["id"]
        n = numbers.get(f.id)
        findings.setdefault(f.repo, []).append(
            {
                **f.model_dump(mode="json"),
                "number": n,
                "human_note": note_for(n, notes) if n else "",
            }  # fmt: skip
        )
    repos: dict[str, RepoWork] = dict(state["repos"])
    for repo in findings:
        repos[repo] = reset_repo(repos[repo])
    scope.update(active=bool(findings), findings=findings, threads=threads, skipped=skipped,
                 decision=decision)  # fmt: skip
    if skipped:
        one = len(skipped) == 1
        deps.notifier.progress(key, f"{refs(skipped, numbers)} {'was' if one else 'were'} "
                               f"already resolved in GitLab, so I'm leaving "
                               f"{'it' if one else 'them'} out.", "coder")  # fmt: skip
    if not findings:  # everything chosen was resolved in GitLab: straight to the re-review
        deps.notifier.progress(key, "There is nothing left for me to fix, so the merge "
                               "requests are ready for another review.", "coder")  # fmt: skip
        return {"fix_scope": scope, "last_event": {"type": "nothing_to_fix"}}
    todo = [f"{refs([x['id'] for x in items], numbers)} in {repo}"
            for repo, items in findings.items()]  # fmt: skip
    deps.notifier.progress(key, f"Fixing {join_words(todo)}.", "coder")
    return {"repos": repos, "fix_scope": scope, "last_event": {"type": "fixing"}}


def fix_inputs(state: Any, repo: str) -> tuple[list[dict] | None, dict | None]:
    """What `code_next_repo` passes the coder in fix mode (None, None outside it)."""
    scope = state.get("fix_scope") or {}
    if not scope.get("active") or repo not in (scope.get("findings") or {}):
        return None, None
    return scope["findings"][repo], scope.get("decision")


def fix_active(state: Any) -> bool:
    return bool((state.get("fix_scope") or {}).get("active"))


# ---------- push fixes ----------


def push_fixes(state: TicketState, deps: Any) -> dict:
    """Push each re-coded repo; reply on every chosen thread as the **coder** with the
    resolution (never resolve: that's the reviewer's job, 4.3). Jira stays In Dev (D127)."""
    key = state["key"]
    scope = dict(state.get("fix_scope") or {})
    round_ = scope.get("round") or state.get("review_round", 0)
    numbers = scope.get("numbers") or {}
    chosen = set(scope.get("ids") or [])
    ws = deps.workspaces
    coded = [r for r in ordered(state) if state["repos"][r].get("status") == "coded"]
    # D207: a repo without a merge request yet is fixed in place; nothing leaves this machine
    published = [r for r in coded if has_mr(state, r)]
    # D125: rebase onto the latest default branch (and re-check) before anything is pushed
    back, leases, synced = sync_before_push(state, deps, "push_fixes", published)
    if back is not None:
        return back
    repos: dict[str, RepoWork] = dict(synced)
    pushed: list[str] = []
    outcome: dict[str, list[str]] = {"fixed": [], "disputed": [], "not_applicable": []}
    notes_out: list[str] = []
    for repo in ordered(state):
        work = dict(repos[repo])
        if work.get("status") != "coded":
            continue
        cfg = deps.config.repo(repo)
        online = has_mr(state, repo)
        if online:
            ws.push(ws.worktree_path(key, repo), work["branch"], cfg.default_branch,
                    leases.get(repo))  # fmt: skip
        pushed.append(repo)
        work["status"] = "mr_open"
        repos[repo] = work  # type: ignore[assignment]
        coder: Any = work.get("coder_result")
        resolutions = [r for r in (coder.resolutions if coder else []) if r.finding_id in chosen]
        threads = (threads_by_finding(deps, key, *mr_of(deps, state, repo))
                   if online and resolutions else {})  # fmt: skip
        nit_lines = []
        for r in resolutions:
            outcome.setdefault(r.action, []).append(r.finding_id)
            n = numbers.get(r.finding_id)
            # one line in the thread (D109): "Fixed: <note>"
            text = f"{RESOLUTION_WORDS.get(r.action, r.action)}: {short(r.note)}"
            if r.action == "disputed":
                notes_out.append(f"#{n or r.finding_id}: {short(r.note)}")
            thread = threads.get(r.finding_id)
            if thread is None:
                nit_lines.append(f"- #{n or r.finding_id} {text}")
                continue
            project, iid = mr_of(deps, state, repo)
            marker = make_marker(key, "fix", round_, r.finding_id)
            if not any(has_marker(x.get("body") or "", marker) for x in thread.get("notes", [])):
                deps.gitlab.reply(project, iid, thread["id"], text + gitlab_marker(marker))
        if nit_lines and online:
            project, iid = mr_of(deps, state, repo)
            deps.gitlab.add_note(project, iid, "**Nits**\n\n" + "\n".join(nit_lines),
                                 make_marker(key, "fix", round_, f"notes-{repo}"),
                                 as_role="coder")  # fmt: skip
    deps.notifier.fixes_pushed(key, round_, SCOPE_WORDS.get(scope.get("mode") or "", "chosen"),
                               refs(outcome["fixed"], numbers), pushed, notes_out,
                               refs(outcome["not_applicable"], numbers)
                               if outcome["not_applicable"] else "",
                               next_auto=gate_mode("re_review", deps, state) == "auto",
                               pushed_word="pushed" if published else "saved")  # fmt: skip
    scope.update(active=False, findings={}, threads={})
    return {"repos": repos, "fix_scope": scope,
            "last_event": {"type": "fixes_pushed", "repos": pushed}}  # fmt: skip


# ---------- re-review gate ----------


def re_review_card(state: TicketState, deps: Any) -> Card:
    key = state["key"]
    round_ = state.get("review_round", 0)
    limit = deps.config.limits.max_review_rounds
    return Card(
        title=f"{key}: ready for review round {round_ + 1} of {limit}",
        actions=[
            Action("Review again", f"review {key}", style="positive"),
            Action("Stop", f"stop {key} --confirm", style="destructive"),
        ],
    )


def re_review_gate(state: TicketState, deps: Any) -> dict:
    round_ = state.get("review_round", 0)
    update = gate_prepare("re_review", state, deps, re_review_card, round=round_)
    if (update.get("last_event") or {}).get("type") == "gate":  # auto: the Coder hands back
        text = (f"I pushed the fixes for review round {round_}. Please check again." if round_
                else "I pushed the changes. Please review them.")  # fmt: skip
        deps.notifier.handoff(state["key"], "coder", "reviewer", text)
    return update


def re_review_gate_wait(state: TicketState, deps: Any) -> dict:
    return gate_wait("re_review", state, deps, ("review", "change"),
                     round=state.get("review_round", 0), node="re_review_gate_wait")  # fmt: skip
