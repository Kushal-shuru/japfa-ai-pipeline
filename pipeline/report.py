"""`pipeline report` / "show me the report" (Phase 8 §3): what happened, from the tracking tables.

Per ticket: repos, who started it, stage, MRs per repo, plan revisions, review / QA rounds,
overrides, machine time per stage (`node_end` durations), **time waiting on people** per wait
(a manual gate's `gate_wait` → its `gate_pass`; Needs Human → the person's retry / guide / stop;
approval → the last merge; ready for testing → QA's verdict) and agent runs by role / slot /
provider with tokens and cost where known. Then totals, averages, a provider comparison and the
Chat LLM's calls and estimated cost per day (1.10). Rendered as text (CLI), Markdown (chat) or
one self-contained HTML file (`$PIPELINE_HOME/report.html`).
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# machine time: graph node → stage people understand
STAGE_OF_NODE: dict[str, str] = {
    "load_context": "starting", "plan": "planning", "replan": "planning", "change": "planning",
    "code_next_repo": "coding", "verify": "checks", "fix": "coding", "push_fixes": "merge requests",
    "sync": "merge requests", "open_mrs": "merge requests", "review": "review",
    "approve": "review", "deploy_doc": "deployment note", "post_qa_notes": "test notes",
    "qa_fix": "QA round", "qa_done": "finished", "needs_human": "other",
}  # fmt: skip
STAGES = ("starting", "planning", "coding", "checks", "merge requests", "review",
          "deployment note", "test notes", "QA round")  # fmt: skip
WAIT_LABELS: dict[str, str] = {
    "plan_approval": "plan approval", "review": "starting the review", "fix": "choosing fixes",
    "re_review": "starting the next review", "deploy_doc": "deployment note",
    "qa_fix": "choosing QA bugs", "needs_human": "help after a stop",
    "merge": "merging (people + DevOps)", "testing": "testing (QA)",
}  # fmt: skip
# Chat LLM prices, USD per million tokens (in, out); PROGRESS "Chat LLM providers"
LLM_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-20b": (0.075, 0.30), "openai/gpt-oss-120b": (0.15, 0.60),
    "gpt-6-luna": (0.10, 0.50), "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}  # fmt: skip


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _secs(a: Any, b: Any) -> float:
    a, b = _ts(a), _ts(b)
    return max((b - a).total_seconds(), 0.0) if a and b else 0.0


def duration_words(seconds: float | None) -> str:
    """90 → "1m 30s"; 5400 → "1h 30m"; None / 0 → "—"."""
    if not seconds:
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _usd(x: float | None) -> str:
    return "—" if not x else f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def _ticket_cost(t: dict) -> str:
    """Reported cost when the runs were billed (API key), else the list-price estimate with ~."""
    if t.get("cost_usd"):
        return _usd(t["cost_usd"])
    return "~" + _usd(t["est_cost_usd"]) if t.get("est_cost_usd") else "—"


def _tokens(n: int | None) -> str:
    if not n:
        return "—"
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


_COST = re.compile(r'"total_cost_usd":\s*([0-9][0-9.eE+-]*)')


def transcript_cost(path: Any) -> float:
    """The list-price cost a Claude run's transcript ends with (`total_cost_usd`), else 0.
    Subscription runs are not billed per run and store no cost, so this is an estimate: what the
    same tokens would cost on API prices."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            hits = _COST.findall(f.read().decode("utf-8", "replace"))
        return float(hits[-1]) if hits else 0.0
    except (OSError, ValueError, TypeError):
        return 0.0


# ---------- collect ----------


def human_waits(events: list[dict], mrs: list[dict], finished_at: Any,
                now: datetime) -> dict[str, float]:  # fmt: skip
    """Seconds waiting on people, by wait (WAIT_LABELS keys). An open wait counts until `now`."""
    waits: dict[str, float] = defaultdict(float)
    open_gate: dict[str, Any] = {}
    halted_at: Any = None
    rft_at: Any = None
    merge_start: dict[str, Any] = {}  # round kind → first wait_merge start
    for ev in events:
        t, node, data = ev["type"], ev.get("node") or "", ev.get("data") or {}
        if t == "gate_wait":
            open_gate.setdefault(node, ev["ts"])
        elif t == "gate_pass" and node in open_gate:
            if data.get("by") != "auto":
                waits[node] += _secs(open_gate[node], ev["ts"])
            open_gate.pop(node)
        elif t == "halt" and node == "needs_human" and halted_at is None:
            halted_at = ev["ts"]
        elif t == "resume" and node == "needs_human" and halted_at is not None:
            waits["needs_human"] += _secs(halted_at, ev["ts"])
            halted_at = None
        elif t == "node_start" and node == "wait_merge":
            kind = _round_at(mrs, ev["ts"])
            merge_start.setdefault(kind, ev["ts"])
        elif t == "node_end" and node == "post_qa_notes" and rft_at is None:
            rft_at = ev["ts"]
        elif t == "gate_pass" and node == "qa_fix" and rft_at is not None:
            waits["testing"] += _secs(rft_at, ev["ts"])
            rft_at = None
    for node, since in open_gate.items():  # still waiting
        waits[node] += _secs(since, finished_at or now)
    if halted_at is not None:
        waits["needs_human"] += _secs(halted_at, finished_at or now)
    if rft_at is not None:
        waits["testing"] += _secs(rft_at, finished_at or now)
    for kind, start in merge_start.items():
        merged = [m["merged_at"] for m in mrs if m["round_kind"] == kind and m.get("merged_at")]
        still_open = any(m["round_kind"] == kind and m["state"] == "opened" for m in mrs)
        end = (finished_at or now) if still_open or not merged else max(merged, key=_ts)
        waits["merge"] += _secs(start, end)
    return dict(waits)


def _round_at(mrs: list[dict], ts: Any) -> str:
    """The MR round (initial, qa1 …) whose MRs were opened last before `ts`."""
    opened = sorted(((m["opened_at"], m["round_kind"]) for m in mrs if m.get("opened_at")),
                    key=lambda x: _ts(x[0]))  # fmt: skip
    kind = "initial"
    for at, k in opened:
        if _ts(at) <= _ts(ts):
            kind = k
    return kind


def machine_time(events: list[dict]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for ev in events:
        if ev["type"] == "node_end":
            stage = STAGE_OF_NODE.get(ev.get("node") or "", "other")
            out[stage] += float((ev.get("data") or {}).get("duration_s") or 0)
    return dict(out)


def ticket_report(store: Any, row: dict, now: datetime) -> dict:
    key = row["key"]
    events = store.events_for(key)
    mrs = store.mrs_for(key)
    runs = store.runs_for(key)
    passes = [e for e in events if e["type"] == "gate_pass"]
    revisions = sum(1 for e in passes if (e.get("data") or {}).get("action") == "revise_plan")
    overrides = [e for e in passes if (e.get("data") or {}).get("action") == "accept"]
    changes = sum(1 for e in passes if (e.get("data") or {}).get("action") == "change")
    waits = human_waits(events, mrs, row.get("finished_at"), now)
    machine = machine_time(events)
    first = _ts(events[0]["ts"]) if events else _ts(row.get("started_at"))
    end = _ts(row.get("finished_at")) or (_ts(events[-1]["ts"]) if events else None)
    by_role: dict[str, dict] = {}
    for r in runs:
        agg = by_role.setdefault(r["role"], {"runs": 0, "ok": 0, "seconds": 0.0, "in": 0,
                                             "out": 0, "cost": 0.0, "slots": set(),
                                             "providers": set()})  # fmt: skip
        agg["runs"] += 1
        agg["ok"] += bool(r["ok"])
        agg["seconds"] += r.get("duration_s") or 0
        agg["in"] += r.get("input_tokens") or 0
        agg["out"] += r.get("output_tokens") or 0
        agg["cost"] += r.get("cost_usd") or 0
        if r.get("slot"):
            agg["slots"].add(r["slot"])
        agg["providers"].add(r["provider"])
    for agg in by_role.values():
        agg["slots"], agg["providers"] = sorted(agg["slots"]), sorted(agg["providers"])
    return {
        "key": key,
        "repos": row.get("repos") or [],
        "started_by": row.get("started_by") or "",
        "stage": row.get("stage") or "",
        "finished": bool(row.get("finished_at")),
        "halted_reason": row.get("halted_reason"),
        "started_at": first,
        "ended_at": end,
        "elapsed_s": _secs(first, end or now),
        "mrs": [
            {
                "repo": m["repo"],
                "round": m["round_kind"],
                "url": m.get("mr_url"),
                "state": m["state"],
            }
            for m in mrs
        ],  # fmt: skip
        "plan_revisions": revisions,
        "review_rounds": row.get("review_rounds") or 0,
        "qa_rounds": row.get("qa_rounds") or 0,
        "replans": row.get("replans") or 0,
        "change_requests": changes,
        "overrides": [{"by": (e.get("data") or {}).get("by"), "at": e["ts"]} for e in overrides],
        "machine_s": machine,
        "machine_total_s": sum(machine.values()),
        "waits_s": waits,
        "human_total_s": sum(waits.values()),
        "runs": len(runs),
        "runs_by_role": by_role,
        "input_tokens": sum(r.get("input_tokens") or 0 for r in runs),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in runs),
        "cost_usd": sum(r.get("cost_usd") or 0 for r in runs) or None,
        "est_cost_usd": sum(transcript_cost(r.get("transcript_path")) for r in runs
                            if r.get("cost_usd") is None) or None,
        "_runs": runs,
    }


def llm_usage(store: Any, keys: list[str]) -> list[dict]:
    """Chat LLM calls per day and provider/model: calls, failures, tokens, estimated cost."""
    days: dict[tuple[str, str, str], dict] = {}
    for key in ["chat", *keys]:
        for ev in store.events_for(key, "llm_call"):
            d = ev.get("data") or {}
            day = _ts(ev["ts"]).date().isoformat()  # type: ignore[union-attr]
            k = (day, str(d.get("provider") or "?"), str(d.get("model") or "?"))
            agg = days.setdefault(k, {"day": k[0], "provider": k[1], "model": k[2], "calls": 0,
                                      "failed": 0, "in": 0, "out": 0})  # fmt: skip
            agg["calls"] += 1
            agg["failed"] += 0 if d.get("ok", True) else 1
            agg["in"] += int(d.get("input_tokens") or 0)
            agg["out"] += int(d.get("output_tokens") or 0)
    for agg in days.values():
        price = LLM_PRICES.get(agg["model"])
        agg["cost_usd"] = ((agg["in"] * price[0] + agg["out"] * price[1]) / 1e6
                           if price else None)  # fmt: skip
    return sorted(days.values(), key=lambda a: (a["day"], a["provider"]), reverse=True)


def collect(store: Any, now: datetime | None = None, days: int | None = None) -> dict:
    """The whole report. `days`: only tickets started in the last `days` days."""
    now = now or datetime.now(UTC)
    rows = store.list_tickets()
    if days is not None:
        since = now - timedelta(days=days)
        rows = [r for r in rows if (_ts(r.get("started_at")) or now) >= since]
    tickets = [ticket_report(store, r, now) for r in rows]
    tickets.sort(key=lambda t: t["started_at"] or now, reverse=True)
    providers: dict[str, dict] = {}
    for t in tickets:
        for r in t["_runs"]:
            p = providers.setdefault(r["provider"], {"runs": 0, "ok": 0, "seconds": 0.0,
                                                     "in": 0, "out": 0, "cost": 0.0})  # fmt: skip
            p["runs"] += 1
            p["ok"] += bool(r["ok"])
            p["seconds"] += r.get("duration_s") or 0
            p["in"] += r.get("input_tokens") or 0
            p["out"] += r.get("output_tokens") or 0
            p["cost"] += r.get("cost_usd") or 0
    waits: dict[str, float] = defaultdict(float)
    for t in tickets:
        for w, s in t["waits_s"].items():
            waits[w] += s
    done = [t for t in tickets if t["finished"]]
    n = len(tickets) or 1
    return {
        "generated_at": now,
        "days": days,
        "tickets": tickets,
        "totals": {
            "tickets": len(tickets),
            "finished": len(done),
            "needs_human": sum(1 for t in tickets if t["stage"] == "needs_human"),
            "mrs": sum(len(t["mrs"]) for t in tickets),
            "runs": sum(t["runs"] for t in tickets),
            "input_tokens": sum(t["input_tokens"] for t in tickets),
            "output_tokens": sum(t["output_tokens"] for t in tickets),
            "cost_usd": sum(t["cost_usd"] or 0 for t in tickets) or None,
            "est_cost_usd": sum(t["est_cost_usd"] or 0 for t in tickets) or None,
            "machine_s": sum(t["machine_total_s"] for t in tickets),
            "human_s": sum(t["human_total_s"] for t in tickets),
            "avg_machine_s": sum(t["machine_total_s"] for t in tickets) / n,
            "avg_human_s": sum(t["human_total_s"] for t in tickets) / n,
            "avg_review_rounds": sum(t["review_rounds"] for t in tickets) / n,
            "overrides": sum(len(t["overrides"]) for t in tickets),
        },
        "waits_s": dict(waits),
        "providers": providers,
        "llm": llm_usage(store, [t["key"] for t in tickets]),
    }


# ---------- render ----------


def _waits_line(waits: dict[str, float]) -> str:
    parts = [f"{WAIT_LABELS.get(w, w)} {duration_words(s)}" for w, s in
             sorted(waits.items(), key=lambda kv: -kv[1]) if s >= 1]  # fmt: skip
    return ", ".join(parts) or "—"


def render_markdown(rep: dict, limit: int | None = None) -> str:
    """Markdown for the chat and the terminal: totals, one row per ticket, waits, providers."""
    tot = rep["totals"]
    scope = f"the last {rep['days']} days" if rep.get("days") else "all tickets so far"
    lines = [
        f"**Pipeline report** ({scope})",
        "",
        f"{tot['tickets']} tickets ({tot['finished']} finished, {tot['needs_human']} waiting "
        f"for help), {tot['mrs']} merge requests, {tot['runs']} agent runs, "
        f"{_tokens(tot['input_tokens'])} tokens in / {_tokens(tot['output_tokens'])} out"
        + (f", ${tot['cost_usd']:.2f} reported cost" if tot["cost_usd"] else "")
        + (f", about {_usd(tot['est_cost_usd'])} at list prices (subscription runs are not billed)"
           if tot["est_cost_usd"] else "")
        + ".",
        f"Per ticket on average: machine {duration_words(tot['avg_machine_s'])}, waiting on "
        f"people {duration_words(tot['avg_human_s'])}, "
        f"{tot['avg_review_rounds']:.1f} review rounds."
        + (
            f" People accepted {tot['overrides']} review(s) as they were."
            if tot["overrides"]
            else ""
        ),  # fmt: skip
    ]
    tickets = rep["tickets"][:limit] if limit else rep["tickets"]
    if tickets:
        lines += ["", "| Ticket | Stage | Machine | Waiting on people | Runs | Rounds "
                  "| Tokens in / out | Cost |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for t in tickets:
            rounds = f"review {t['review_rounds']}" + (f", QA {t['qa_rounds']}"
                                                       if t["qa_rounds"] else "")  # fmt: skip
            machine = duration_words(t["machine_total_s"])
            human = duration_words(t["human_total_s"])
            tokens = f"{_tokens(t['input_tokens'])} / {_tokens(t['output_tokens'])}"
            lines.append(f"| {t['key']} | {_stage_words(t)} | {machine} | {human} | {t['runs']} "
                         f"| {rounds} | {tokens} | {_ticket_cost(t)} |")  # fmt: skip
        if limit and len(rep["tickets"]) > limit:
            lines.append(f"\n…and {len(rep['tickets']) - limit} more in the full report.")
    if rep["waits_s"]:
        lines += ["", f"Time waiting on people, all tickets: {_waits_line(rep['waits_s'])}."]
    if len(rep["providers"]) > 1:
        lines += ["", "Providers: " + "; ".join(
            f"{p} {a['runs']} runs ({a['ok']} ok), avg {duration_words(a['seconds'] / a['runs'])}"
            for p, a in sorted(rep["providers"].items()))]  # fmt: skip
    return "\n".join(lines)


def _stage_words(t: dict) -> str:
    from pipeline.chat.commands import STAGE_LABELS

    return STAGE_LABELS.get(t["stage"], t["stage"].replace("_", " ") or "—")


_CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--card:#fff;--accent:#2b59c3;
--warn:#b25b00}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecebe6;
--muted:#9b9a93;--line:#2e2d2a;--card:#1e1e1c;--accent:#8aa9ff;--warn:#f0a35e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:32px 0 8px}.muted{color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;
margin-top:16px}.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 12px}.tile b{display:block;font-size:20px}.tile span{color:var(--muted);
font-size:13px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;
background:var(--card);font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;
text-align:left;vertical-align:top;white-space:nowrap}th{color:var(--muted);font-weight:600}
td.wrap{white-space:normal;min-width:220px}a{color:var(--accent)}.warn{color:var(--warn)}
"""


def render_html(rep: dict) -> str:
    e = html.escape
    tot = rep["totals"]
    gen = rep["generated_at"].strftime("%Y-%m-%d %H:%M UTC")
    tiles = [
        (tot["tickets"], "tickets"), (tot["finished"], "finished"), (tot["mrs"], "merge requests"),
        (tot["runs"], "agent runs"), (duration_words(tot["avg_machine_s"]), "machine / ticket"),
        (duration_words(tot["avg_human_s"]), "waiting on people / ticket"),
        (f"{tot['avg_review_rounds']:.1f}", "review rounds / ticket"),
        (f"${tot['cost_usd']:.2f}" if tot["cost_usd"] else "—", "reported cost"),
    ]  # fmt: skip
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
           f"content='width=device-width,initial-scale=1'><title>Pipeline Report</title>"
           f"<style>{_CSS}</style></head><body><main><h1>Pipeline report</h1>"
           f"<div class=muted>Generated {e(gen)}"
           + (f" · last {rep['days']} days" if rep.get("days") else " · all tickets")
           + "</div><div class=tiles>"]  # fmt: skip
    out += [f"<div class=tile><b>{e(str(v))}</b><span>{e(k)}</span></div>" for v, k in tiles]
    out.append(
        "</div><h2>Tickets</h2><div class=scroll><table><tr><th>Ticket</th><th>Stage</th>"
        "<th>Started by</th><th>Elapsed</th><th>Machine</th><th>Waiting on people</th>"
        "<th>Rounds</th><th>Runs</th><th>Tokens in/out</th><th>Merge requests</th></tr>"
    )
    for t in rep["tickets"]:
        mrs = "<br>".join(
            (f"<a href='{e(m['url'])}'>{e(m['repo'])}</a>" if m["url"] else e(m["repo"]))
            + f" <span class=muted>{e(m['round'])}, {e(m['state'])}</span>" for m in t["mrs"]
        ) or "—"  # fmt: skip
        stage = e(_stage_words(t))
        if t["stage"] == "needs_human":
            stage = f"<span class=warn>{stage}</span>"
        rounds = f"plan rev {t['plan_revisions']}, review {t['review_rounds']}, QA {t['qa_rounds']}"
        if t["overrides"]:
            rounds += f", <span class=warn>{len(t['overrides'])} accepted as is</span>"
        out.append(
            f"<tr><td><b>{e(t['key'])}</b><br><span class=muted>{e(', '.join(t['repos']))}</span>"
            f"</td><td>{stage}</td><td>{e(t['started_by'])}</td>"
            f"<td>{duration_words(t['elapsed_s'])}</td>"
            f"<td>{duration_words(t['machine_total_s'])}</td>"
            f"<td class=wrap>{e(_waits_line(t['waits_s']))}</td><td class=wrap>{rounds}</td>"
            f"<td>{t['runs']}</td><td>{_tokens(t['input_tokens'])} / "
            f"{_tokens(t['output_tokens'])}<br><span class=muted>{_ticket_cost(t)}</span></td>"
            f"<td>{mrs}</td></tr>"
        )  # fmt: skip
    out.append(
        "</table></div><h2>Machine time per stage</h2><div class=scroll><table><tr>"
        "<th>Ticket</th>" + "".join(f"<th>{s}</th>" for s in STAGES) + "</tr>"
    )
    for t in rep["tickets"]:
        out.append(
            f"<tr><td>{e(t['key'])}</td>"
            + "".join(f"<td>{duration_words(t['machine_s'].get(s))}</td>" for s in STAGES)
            + "</tr>"
        )
    out.append(
        "</table></div><h2>Time waiting on people</h2><div class=scroll><table><tr>"
        "<th>Wait</th><th>Total</th></tr>"
    )
    for w, s in sorted(rep["waits_s"].items(), key=lambda kv: -kv[1]):
        out.append(f"<tr><td>{e(WAIT_LABELS.get(w, w))}</td><td>{duration_words(s)}</td></tr>")
    out.append(
        "</table></div><h2>Agent runs by role</h2><div class=scroll><table><tr><th>Ticket"
        "</th><th>Role</th><th>Runs (ok)</th><th>Time</th><th>Slots</th><th>Providers</th>"
        "<th>Tokens in/out</th><th>Cost</th></tr>"
    )
    for t in rep["tickets"]:
        for role, a in sorted(t["runs_by_role"].items()):
            cost = f"${a['cost']:.2f}" if a["cost"] else "—"
            out.append(
                f"<tr><td>{e(t['key'])}</td><td>{e(role)}</td><td>{a['runs']} ({a['ok']})</td>"
                f"<td>{duration_words(a['seconds'])}</td><td>{e(', '.join(a['slots']))}</td>"
                f"<td>{e(', '.join(a['providers']))}</td><td>{_tokens(a['in'])} / "
                f"{_tokens(a['out'])}</td><td>{cost}</td></tr>"
            )  # fmt: skip
    out.append(
        "</table></div><h2>Providers</h2><div class=scroll><table><tr><th>Provider</th>"
        "<th>Runs</th><th>OK</th><th>Avg time</th><th>Tokens in/out</th><th>Cost</th></tr>"
    )
    for p, a in sorted(rep["providers"].items()):
        avg = duration_words(a["seconds"] / a["runs"]) if a["runs"] else "—"
        cost = f"${a['cost']:.2f}" if a["cost"] else "— (subscription)"
        out.append(
            f"<tr><td>{e(p)}</td><td>{a['runs']}</td><td>{a['ok']}</td><td>{avg}</td>"
            f"<td>{_tokens(a['in'])} / {_tokens(a['out'])}</td><td>{cost}</td></tr>"
        )
    out.append(
        "</table></div><h2>Chat LLM</h2><div class=scroll><table><tr><th>Day</th>"
        "<th>Provider</th><th>Model</th><th>Calls</th><th>Failed</th><th>Tokens in/out"
        "</th><th>Est. cost</th></tr>"
    )
    for a in rep["llm"]:
        cost = f"${a['cost_usd']:.4f}" if a["cost_usd"] is not None else "—"
        out.append(
            f"<tr><td>{a['day']}</td><td>{e(a['provider'])}</td><td>{e(a['model'])}</td>"
            f"<td>{a['calls']}</td><td>{a['failed']}</td><td>{_tokens(a['in'])} / "
            f"{_tokens(a['out'])}</td><td>{cost}</td></tr>"
        )
    out.append(
        "</table></div><p class=muted>Machine time = graph steps (agents, checks, git). "
        "Waiting on people = a manual gate until someone decided, Needs Human until "
        "someone answered, approval until the last merge, ready for testing until QA's "
        "verdict. Subscription runs report no cost.</p></main></body></html>"
    )
    return "\n".join(out)


def write_html(rep: dict, home: Path) -> Path:
    path = home / "report.html"
    path.write_text(render_html(rep))
    return path


def chat_summary(deps: Any, limit: int = 8) -> str:
    """ "Show me the report" in chat: the Markdown summary (latest tickets) + where the full
    HTML report is (written now)."""
    rep = collect(deps.store, now=deps.clock())
    text = render_markdown(rep, limit=limit)
    try:
        path = write_html(rep, deps.home)
        text += f"\n\nThe full report is on the pipeline machine: `{path}`."
    except OSError:
        pass
    return text
