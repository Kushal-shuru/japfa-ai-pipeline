"""Runs ticket threads: the `TicketRuntime` behind the command bus (Phase 3 8.1).

- `snapshot(key)` reads the checkpoint: pending interrupt (what the thread waits for), finished.
- `start` / `resume` mark the ticket busy immediately (so a second click is rejected) and hand
  the graph step to a worker thread (`sync=False`, the server) or run it inline (`sync=True`,
  `pipeline say` without a server, `pipeline run`, tests). A step runs until the next
  interrupt or END. One step per ticket at a time.
- Workers are daemon threads: stopping the server doesn't wait for a running agent. The thread
  keeps its last checkpoint; `recover()` on the next start resumes steps that were cut off
  (the interrupted node runs again; nodes are idempotent).
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from langgraph.types import Command

from pipeline.agents.providers.children import CHILDREN_DIR, kill_leftovers
from pipeline.chat.commands import ThreadInfo
from pipeline.chat.summaries import step_words
from pipeline.graph.build import build_graph
from pipeline.graph.wrapper import STOP_PREFIX, halt
from pipeline.logging import get_logger

# cut off here, a second restart just resumes (nothing expensive runs again)
NO_HALT = frozenset({"needs_human", "needs_human_wait", "qa_done", ""})


def thread_config(key: str) -> dict:
    return {"configurable": {"thread_id": key}}


class GraphRuntime:
    def __init__(self, deps: Any, checkpointer: Any, workers: int = 4, sync: bool = False) -> None:
        self.deps = deps
        self.checkpointer = checkpointer
        self.graph = build_graph(deps, checkpointer)
        self.sync = sync
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self._done: dict[str, threading.Event] = {}
        self._jobs: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._stopping = False
        if not sync:
            for n in range(workers):
                t = threading.Thread(target=self._worker, name=f"ticket-{n}", daemon=True)
                t.start()
                self._workers.append(t)

    # ---------- TicketRuntime ----------

    def snapshot(self, key: str) -> ThreadInfo:
        st = self.graph.get_state(thread_config(key))
        exists = bool(st.values)
        waiting = None
        if st.interrupts:
            value = st.interrupts[0].value
            waiting = value.get("gate") if isinstance(value, dict) else None
        with self._lock:
            busy = key in self._busy
        return ThreadInfo(
            exists=exists,
            finished=exists and not st.next,
            waiting=waiting,
            busy=busy,
            values=dict(st.values),
        )

    def start(self, key: str, state: dict) -> None:
        self._mark_busy(key)
        self.checkpointer.delete_thread(key)  # a finished earlier run starts over
        self.deps.store.upsert_ticket(key, halted_reason=None, finished_at=None, stage="starting")
        self._submit(key, state)

    def resume(self, key: str, payload: dict) -> None:
        self._mark_busy(key)
        self._submit(key, Command(resume=payload))

    def request_stop(self, key: str, by: str) -> None:
        self.deps.store.upsert_ticket(key, halted_reason=f"{STOP_PREFIX} by {by}")

    def continue_to_review(self, key: str, by: str) -> None:
        """A thread that finished right after `open_mrs` (the Phase 3/4 end, before the review
        gate existed): record the review gate as passed by `by` and continue the same thread
        into `review`. Nothing is re-coded; the open MRs are reviewed as they are."""
        self.continue_from(key, "review_gate", "review", by)

    def continue_from(self, key: str, gate_node: str, action: str, by: str) -> None:
        """Continue a thread that ended where an older pipeline version stopped: record gate
        `gate_node` as passed by `by` with `action` and run on from there (D108; Phase 6: a
        thread that ended at `approve` continues into the deployment note)."""
        gate = gate_node.removesuffix("_gate")
        self._mark_busy(key)
        try:
            event = {"type": "gate", "gate": gate, "action": action, "by": by, "args": {}}
            self.graph.update_state(thread_config(key),
                                    {"waiting_for": None, "last_event": event},
                                    as_node=gate_node)  # fmt: skip
        except Exception:
            with self._lock:
                self._busy.discard(key)
            raise
        self.deps.store.upsert_ticket(key, finished_at=None, stage=gate_node)
        self.deps.store.record_event(key, "gate_pass", node=gate,
                                     data={"by": by, "action": action,
                                           "note": "continued a finished thread"})  # fmt: skip
        self._submit(key, None)

    # ---------- helpers ----------

    def busy(self, key: str) -> bool:
        with self._lock:
            return key in self._busy

    def wait(self, key: str, timeout: float | None = None) -> bool:
        """Block until the ticket's current step is done (server mode). False on timeout."""
        done = self._done.get(key)
        return True if done is None else done.wait(timeout)

    def recover(self) -> list[str]:
        """Startup (1.4, K18): kill agent processes a previous server left behind, then resume
        threads cut off mid-step (not paused at an interrupt, not finished) — once. The thread
        says so in the chat. A step cut off again before it finished goes to Needs Human
        instead of running a third time."""
        kill_leftovers(self.deps.home / CHILDREN_DIR)
        resumed = []
        for row in self.deps.store.list_tickets():
            key = row["key"]
            info = self.snapshot(key)
            if not (info.exists and not info.finished and info.waiting is None
                    and not info.busy):  # fmt: skip
                continue
            nxt = self.graph.get_state(thread_config(key)).next
            node = nxt[0] if nxt else ""
            words = step_words(node)
            store = self.deps.store
            store.record_event(key, "resume", node=node or None,
                               data={"recovered": True, "again": self._cut_twice(key)})  # fmt: skip
            self._mark_busy(key)
            if store.events_for(key, "resume")[-1]["data"]["again"] and node not in NO_HALT:
                reason = (f"The pipeline was restarted twice while working on {words}, so I "
                          "stopped instead of starting it a third time. Tell me to try again "
                          "when the machine is stable.")  # fmt: skip
                try:
                    self.graph.update_state(thread_config(key), halt(node, reason), as_node=node)
                except Exception:
                    with self._lock:
                        self._busy.discard(key)
                    raise
            else:
                self._say(key, f"The pipeline was restarted while I was working on {words}. "
                          "I'm picking it up again from there.")  # fmt: skip
            self._submit(key, None)  # None = continue from the last checkpoint
            resumed.append(key)
        return resumed

    def _cut_twice(self, key: str) -> bool:
        """True when the last recovery of `key` never got to finish a node."""
        last_resume = last_end = -1
        for n, ev in enumerate(self.deps.store.events_for(key)):
            if ev["type"] == "resume" and (ev.get("data") or {}).get("recovered"):
                last_resume = n
            elif ev["type"] == "node_end":
                last_end = n
        return last_resume > last_end

    def _say(self, key: str, text: str) -> None:
        try:
            self.deps.notifier.progress(key, text)
        except Exception:  # a chat hiccup must not stop the recovery
            get_logger(ticket=key).exception("recover_message_failed")

    def shutdown(self) -> None:
        """Graceful drain (K18): steps not started yet are dropped (the next start resumes
        them); running ones are cut off with the process (daemon threads) and agent child
        processes are killed, so nothing keeps working on a ticket nobody watches."""
        self._stopping = True
        for _ in self._workers:
            self._jobs.put(None)
        kill_leftovers(self.deps.home / CHILDREN_DIR)

    def _mark_busy(self, key: str) -> None:
        with self._lock:
            if key in self._busy:
                raise RuntimeError(f"{key} is already running a step")
            self._busy.add(key)
            self._done[key] = threading.Event()

    def _submit(self, key: str, graph_input: Any) -> None:
        if self.sync:
            self._run(key, graph_input)
        else:
            self._jobs.put((key, graph_input))

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            if self._stopping:  # draining: leave it for `recover()` on the next start
                continue
            self._run(*job)

    def _run(self, key: str, graph_input: Any) -> None:
        log = get_logger(ticket=key)
        try:
            self.graph.invoke(graph_input, thread_config(key))
        except Exception as err:  # nodes never raise; this is a bug or a checkpoint failure
            log.exception("graph_step_crashed")
            try:
                self.deps.notifier.error(
                    key, f"Something went wrong on my side while working on {key}. A developer "
                    f"can find the details in the pipeline log.\n\nError: {err}",
                )  # fmt: skip
            except Exception:
                log.exception("error_report_failed")
        finally:
            with self._lock:
                self._busy.discard(key)
                done = self._done.get(key)
            if done is not None:
                done.set()
