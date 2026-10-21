"""`pipeline logs <KEY>` (Phase 8 3.4): one ticket's story in order — graph events, the
commands people gave (with who and whether they were accepted), then its agent runs with the
transcript files."""

from __future__ import annotations

from typing import Any

from pipeline.report import _ts, duration_words

# events worth a line (node_start is implied by node_end's duration; chat posts are noise)
SHOWN = frozenset({"node_end", "gate_wait", "gate_pass", "transition", "halt", "error",
                   "resume", "human_command", "comment", "llm_call"})  # fmt: skip


def _event_line(ev: dict) -> str:
    t, node, data = ev["type"], ev.get("node") or "", ev.get("data") or {}
    repo = f" [{ev['repo']}]" if ev.get("repo") else ""
    if t == "node_end":
        return f"step    {node}{repo} done in {duration_words(data.get('duration_s'))}"
    if t == "gate_wait":
        return f"wait    {node}: waiting for a person"
    if t == "gate_pass":
        return f"pass    {node}: {data.get('action') or 'passed'} by {data.get('by', '?')}"
    if t == "halt":
        reason = str(data.get("reason") or "").splitlines()[0][:160] if data.get("reason") else ""
        return f"halt    {node}: {reason}"
    if t == "error":
        return f"ERROR   {node}: {str(data.get('error', ''))[:160]}"
    if t == "resume":
        if data.get("recovered"):
            return f"restart {node}: resumed after a restart" + (" (again)" if data.get("again")
                                                                    else "")  # fmt: skip
        return f"resume  {node}: {data.get('action', '')} by {data.get('by', '?')}"
    if t == "transition":
        return f"jira    → {data.get('to') or data.get('status') or data}"
    if t == "comment":
        marker = str(data.get("marker") or "")
        what = marker.split(":")[2] if marker.count(":") >= 2 else data.get("attachment", "")
        return f"jira    comment ({what})" if what else "jira    comment"
    if t == "llm_call":
        ok = "ok" if data.get("ok", True) else f"failed ({data.get('error')})"
        return f"llm     {node} {data.get('provider')} {data.get('latency_s', '?')}s {ok}"
    return f"{t:<7} {node}{repo} {data}"[:200]


def timeline(store: Any, key: str, transcripts: bool = False) -> str:
    events = [e for e in store.events_for(key) if e["type"] in SHOWN
              and e["type"] != "human_command"]  # fmt: skip
    rows: list[tuple[Any, str]] = [(e["ts"], _event_line(e)) for e in events]
    for c in store.commands_for(key):
        how = "said" if c.get("interpreted") else "sent"  # sent: exact text or a button
        ok = "accepted" if c["accepted"] else f"refused: {c.get('reason') or ''}"
        rows.append((c["ts"], f"person  {c.get('actor_name')} {how} “{c.get('raw')}” → "
                              f"{c['verb']} ({ok})"))  # fmt: skip
    rows.sort(key=lambda r: _ts(r[0]))  # type: ignore[arg-type,return-value]
    row = store.get_ticket(key)
    if row is None and not rows:
        return f"No records for {key}."
    out = [f"{key}: stage {row.get('stage') if row else '?'}, Jira "
           f"{row.get('jira_status') if row else '?'}"]  # fmt: skip
    out += [f"{_ts(ts):%m-%d %H:%M:%S}  {line}" for ts, line in rows]  # type: ignore[str-format]
    runs = store.runs_for(key)
    if runs:
        out += ["", "Agent runs:"]
        for r in runs:
            tokens = (f"{r['input_tokens'] or 0:,} in / {r['output_tokens'] or 0:,} out"
                      if r.get("input_tokens") else "tokens n/a")  # fmt: skip
            status = "ok" if r["ok"] else f"FAILED {r.get('error') or ''}"
            where, slot = r.get("repo") or "ticket", r.get("slot") or "-"
            out.append(f"  {r['role']:<14} {where:<34} {slot:<11} {r['provider']:<6}"
                       f" {duration_words(r.get('duration_s')):>8}  {tokens}  "
                       f"{status}")  # fmt: skip
            if transcripts:
                out.append(f"      {r.get('transcript_path')}")
    return "\n".join(out)
