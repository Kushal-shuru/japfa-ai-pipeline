"""Gate reminders (Phase 8 1.5), run by the server's scheduler.

- A ticket waiting for a person (a manual gate, or Needs Human) longer than
  `chat.reminder_hours` gets **one** reminder in its thread, naming who started it. A new wait
  (the next gate, or the same gate again later) can get its own reminder.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pipeline.chat.notifier import gate_label
from pipeline.report import _ts, duration_words

# waits where a person in the ticket's thread has to decide (not merges / QA: other people)
REMINDED = frozenset({"plan_approval", "review", "fix", "re_review", "deploy_doc", "qa_fix",
                      "needs_human"})  # fmt: skip


def wait_started(store: Any, key: str, gate: str) -> datetime | None:
    """When the current wait on `gate` began (its last `gate_wait`, or Needs Human's halt)."""
    if gate == "needs_human":
        evs = [e for e in store.events_for(key, "halt") if e.get("node") == "needs_human"]
    else:
        evs = [e for e in store.events_for(key, "gate_wait") if e.get("node") == gate]
    return _ts(evs[-1]["ts"]) if evs else None


class GateReminder:
    def __init__(self, deps: Any, runtime: Any) -> None:
        self.deps = deps
        self.runtime = runtime

    def poll(self) -> list[str]:
        """Post the reminders that are due; returns the tickets reminded."""
        deps = self.deps
        hours = deps.config.chat.reminder_hours
        if not hours or hours <= 0:
            return []
        now = _ts(deps.clock())
        assert now is not None
        done = []
        for row in deps.store.list_tickets():
            key = row["key"]
            if row.get("finished_at"):
                continue
            info = self.runtime.snapshot(key)
            gate = info.waiting
            if info.busy or gate not in REMINDED:
                continue
            since = wait_started(deps.store, key, gate)
            if since is None or (now - since).total_seconds() < hours * 3600:
                continue
            tag = f"{gate}@{since.isoformat()}"
            posts = deps.store.events_for(key, "chat_post")
            if any((e.get("data") or {}).get("reminder") == tag for e in posts):
                continue
            who = row.get("started_by") or info.values.get("started_by") or "everyone"
            text = (f"@{who}, a reminder: {key} has been waiting for {gate_label(gate)} for "
                    f"{duration_words((now - since).total_seconds())}. Just answer here in your "
                    "own words.")  # fmt: skip
            msg = deps.notifier.progress(key, text, important=True)
            deps.store.record_event(key, "chat_post", data={"message_id": msg, "reminder": tag})
            done.append(key)
        return done
