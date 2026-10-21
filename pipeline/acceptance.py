"""Acceptance criteria in the review (Phase 9, audit F1): what must be true before a repo's
review can approve.

- The required criteria of a repo are its task's (`planner.task_criteria`): brief ids ("AC4")
  and the task's own ("T1").
- The reviewer answers each one in `acceptance_criteria_check`. `met` needs evidence;
  `not_applicable` needs a note. Anything else, or no answer, is **not** met.
- Every criterion that isn't met becomes a finding the orchestrator owns (`A-<repo>-<id>`, the
  same id every round), so it goes through the normal fix loop and closes only when a later
  review reports the criterion met.
- A reviewer that asks for changes without naming a single finding gets one too (`A-<repo>-V1`):
  its "changes requested" is never turned into an approval.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.findings import derived_id
from pipeline.schemas import CriterionCheck, Finding, ReviewResult

_REF = re.compile(r"^\s*(AC|T)\s*-?\s*(\d+)\b", re.I)
VETO_REF = "V1"
CHECKS_REF = "C1"
CATEGORY = "acceptance"


def criterion_ref(text: str) -> str | None:
    """ "AC 4: …" / "ac-4" / "T1" → "AC4" / "T1"; None when it doesn't start with an id."""
    m = _REF.match(text or "")
    return f"{m.group(1).upper()}{int(m.group(2))}" if m else None


def required_criteria(state: Any, repo: str) -> list[tuple[str, str]]:
    """(id, text) for `repo`'s task in the current plan; [] without a plan or task."""
    from pipeline.agents.planner import task_criteria  # planner imports the runner stack

    plan = state.get("plan")
    task = next((t for t in plan.tasks if t.repo == repo), None) if plan is not None else None
    return task_criteria(task, plan) if task is not None else []


def settle_criteria(required: list[tuple[str, str]],
                    checks: list[CriterionCheck]) -> dict[str, CriterionCheck]:  # fmt: skip
    """The effective answer per required id: unanswered, unproven (met without evidence) or
    unexplained (not applicable without a note) answers count as `unknown`."""
    answers: dict[str, CriterionCheck] = {}
    for check in checks:
        ref = criterion_ref(check.criterion)
        if ref and ref not in answers:
            answers[ref] = check
    out: dict[str, CriterionCheck] = {}
    for cid, _text in required:
        found = answers.get(cid)
        c = found or CriterionCheck(criterion=cid, status="unknown",
                                    note="the review didn't answer it")  # fmt: skip
        if found is None:
            pass
        elif c.status == "met" and not c.evidence.strip():
            c = c.model_copy(update={"status": "unknown", "note": "met, but no evidence given"})
        elif c.status == "not_applicable" and not c.note.strip():
            c = c.model_copy(update={"status": "unknown",
                                     "note": "not applicable, but no reason given"})  # fmt: skip
        out[cid] = c.model_copy(update={"criterion": cid})
    return out


_WORD_ON_UNKNOWN = {"fixed", "not_applicable", "disputed"}  # the coder said it's done / not here
_WORD_ON_NOT_MET = {"not_applicable", "disputed"}  # the coder says the cause isn't in its repo


def hear_the_coder(settled: dict[str, CriterionCheck], tag: str,
                   resolutions: dict[str, dict]) -> tuple[dict[str, CriterionCheck],
                                                          list[CriterionCheck]]:  # fmt: skip
    """The coder's word on a criterion counts (DEMO-11: the gateway coder answered "not
    applicable" to AC18 four rounds in a row, the reviewer kept it `unknown` and the ticket
    went round again). With a reason, the coder's answer closes a criterion the review could
    not settle:
    - `unknown` (the reviewer couldn't tell) + the coder says fixed / not applicable / not mine;
    - `not_met` only because a live test failed (the failing case's blame already went to every
      repo it passed through, D185) + the coder says it isn't this repo's.
    The criterion becomes `not_applicable` with "not verified, accepted on the coder's word", so
    people see it in the review note; a `not_met` that points at code stays open.
    Returns (the settled answers, the ones accepted on the coder's word)."""
    out: dict[str, CriterionCheck] = {}
    heard: list[CriterionCheck] = []
    for cid, c in settled.items():
        r = resolutions.get(derived_id(tag, cid)) or {}
        action, note = r.get("action"), str(r.get("note") or "").strip()
        word = note and (
            (c.status == "unknown" and action in _WORD_ON_UNKNOWN)
            or (c.status == "not_met" and action in _WORD_ON_NOT_MET
                and "live test" in c.note.lower()))  # fmt: skip
        if word:
            c = c.model_copy(update={"status": "not_applicable",
                                     "note": f"Not verified, accepted on the coder's word: {note}"})
            heard.append(c)
        out[cid] = c
    return out, heard


def unmet(settled: dict[str, CriterionCheck]) -> list[CriterionCheck]:
    return [c for c in settled.values() if c.status in ("not_met", "unknown")]


def criterion_findings(repo: str, tag: str, required: list[tuple[str, str]],
                       settled: dict[str, CriterionCheck]) -> list[Finding]:  # fmt: skip
    """One finding per criterion that isn't met (`tag`: the repo, or X for the contract)."""
    texts = dict(required)
    out: list[Finding] = []
    for c in unmet(settled):
        text = texts.get(c.criterion) or ""
        why = f" ({c.note.strip()})" if c.note.strip() else ""
        if c.status == "not_met":
            comment = f"{c.criterion} is not met{why}: {text}".rstrip(": ")
            suggestion = f"Make {c.criterion} true: {text}".rstrip(": ")
        else:
            comment = f"{c.criterion} isn't shown to be met yet{why}: {text}".rstrip(": ")
            suggestion = (f"Show that {c.criterion} holds: the code that meets it, or a test "
                          "that proves it.")  # fmt: skip
        out.append(Finding(id=derived_id(tag, c.criterion), repo=repo, severity="major",
                           category=CATEGORY, comment=comment + ".",
                           suggestion=suggestion))  # fmt: skip
    return out


def checks_finding(repo: str, tag: str, problems: list[str]) -> Finding:
    """The orchestrator's checks fail on the reviewed commit (people pushed after the last
    checks, P9 audit F3): nothing is approved on failing checks."""
    listed = "; ".join(p.strip() for p in problems[:3])
    return Finding(id=derived_id(tag, CHECKS_REF), repo=repo, severity="blocker",
                   category="tests", comment=f"The automated checks fail on this commit: {listed}.",
                   suggestion="Make the checks pass again.")  # fmt: skip


def veto_finding(repo: str, tag: str, res: ReviewResult) -> Finding:
    """The reviewer asked for changes but named none: its summary becomes the finding."""
    return Finding(
        id=derived_id(tag, VETO_REF), repo=repo, severity="major", category="review",
        comment=f"The review asks for changes without naming one: {res.summary.strip()}",
    )  # fmt: skip


# ---------- P10: live acceptance evidence ----------


def live_evidence(deps: Any, key: str, repo: str, work: Any, head: str | None) -> dict[str, str]:
    """Criterion id → evidence, for criteria whose live cases all passed **on the ticket's code
    as it is now**: the tested commit, or a later one that only brought in other people's work
    (a rebase onto the default branch) without touching the ticket's own files."""
    res = (work or {}).get("acceptance_result")
    tested = res.head_shas.get(repo) if res is not None else None
    if res is None or not tested:
        return {}
    if head and tested != head:
        ws = deps.workspaces
        path = ws.worktree_path(key, repo)
        changed = ws.changed_between(path, tested, head)
        if changed is None:
            return {}
        own = set(ws.changed_paths(path, deps.config.repo(repo).default_branch)["vs_default"])
        if set(changed) & own:
            return {}
    by: dict[str, list[Any]] = {}
    for r in res.results:
        if r.criterion and r.repo == repo:
            by.setdefault(criterion_ref(r.criterion) or r.criterion, []).append(r)
    return {cid: "live test " + ", ".join(r.case_id for r in rs) + " passed (real calls "
            "against the dev database)" for cid, rs in by.items()
            if rs and all(r.status == "passed" for r in rs)}  # fmt: skip


def with_live(res: ReviewResult, required: list[tuple[str, str]],
              live: dict[str, str]) -> ReviewResult:  # fmt: skip
    """The review's criterion answers with the live evidence added: a criterion a live test
    proved is `met` (evidence_kind real) unless the reviewer said `not_met` or `not_applicable`
    (it may see something the test didn't cover)."""
    wanted = {cid for cid, _ in required}
    live = {cid: ev for cid, ev in live.items() if cid in wanted}
    if not live:
        return res
    checks: list[CriterionCheck] = []
    done: set[str] = set()
    for c in res.acceptance_criteria_check:
        ref = criterion_ref(c.criterion)
        if ref in live and ref not in done and c.status not in ("not_met", "not_applicable"):
            evidence = "; ".join(e for e in (c.evidence.strip(), live[ref]) if e)
            c = c.model_copy(update={"criterion": ref, "status": "met", "evidence": evidence,
                                     "evidence_kind": "real"})  # fmt: skip
            done.add(ref)
        elif ref in live and c.status in ("not_met", "not_applicable"):
            done.add(ref)
        checks.append(c)
    checks += [CriterionCheck(criterion=cid, status="met", evidence=ev, evidence_kind="real")
               for cid, ev in live.items() if cid not in done]  # fmt: skip
    return res.model_copy(update={"acceptance_criteria_check": checks})

