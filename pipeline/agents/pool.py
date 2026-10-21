"""Named agent slots (CLAUDE.md §3.7): the unit of concurrency.

A slot is held only while an agent runs. Waiting tickets queue FIFO per pool; a ticket prefers
its previous slot (sticky), else takes any free one. In-memory state is authoritative inside
the server process; `agent_slots` mirrors it for `pipeline slots` / `status` (reset on start).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pipeline.agents.roles import ROLE_POOL
from pipeline.logging import get_logger


@dataclass(frozen=True)
class Slot:
    name: str
    pool: str
    provider: str


class SlotCancelled(Exception):
    """Waiting for a slot was cancelled (stop / pause)."""


class AgentPool:
    def __init__(self, config: Any, store: Any) -> None:
        self.store = store
        self.slots: dict[str, Slot] = {
            s.name: Slot(s.name, pool, s.provider)
            for pool, slots in config.agent_pool.items()
            for s in slots
        }
        self._busy: dict[str, tuple[str, str]] = {}  # slot name → (key, role)
        self._queues: dict[str, deque[str]] = {pool: deque() for pool in config.agent_pool}
        self._cond = threading.Condition()
        for s in self.slots.values():
            store.register_slot(s.name, s.pool, s.provider)
        store.free_all_slots()  # §8: all slots free on startup

    # ---------- queries ----------

    @staticmethod
    def pool_for(role: str) -> str | None:
        if role not in ROLE_POOL:
            raise KeyError(f"unknown agent role {role!r}")
        return ROLE_POOL[role]

    def queue_position(self, key: str, pool: str) -> int | None:
        """1-based position in the pool's queue, or None if not queued."""
        with self._cond:
            q = self._queues[pool]
            return q.index(key) + 1 if key in q else None

    def queues(self) -> dict[str, list[str]]:
        with self._cond:
            return {pool: list(q) for pool, q in self._queues.items()}

    def busy(self) -> dict[str, tuple[str, str]]:
        with self._cond:
            return dict(self._busy)

    # ---------- acquire / release ----------

    def _free_slot(self, pool: str, preferred: str | None,
                   provider: str | None = None) -> Slot | None:  # fmt: skip
        """A free slot of `pool`, the preferred one first. With `provider`, only slots of that
        provider (one provider per run, whatever is configured, D129), unless the pool has
        none of it at all."""
        slots = [s for s in self.slots.values() if s.pool == pool]
        if provider is not None and any(s.provider == provider for s in slots):
            slots = [s for s in slots if s.provider == provider]
        free = [s for s in slots if s.name not in self._busy]
        for s in free:
            if s.name == preferred:
                return s
        return free[0] if free else None

    def acquire(self, key: str, role: str, preferred: str | None = None,
                provider: str | None = None) -> Slot | None:  # fmt: skip
        """Take a free slot now, or queue `key` (FIFO) and return None."""
        pool = self.pool_for(role)
        if pool is None:
            return None
        with self._cond:
            return self._try(key, role, pool, preferred, provider)

    def _try(self, key: str, role: str, pool: str, preferred: str | None,
             provider: str | None = None) -> Slot | None:  # fmt: skip
        q = self._queues[pool]
        if q and q[0] != key:  # someone queued earlier goes first
            if key not in q:
                q.append(key)
            return None
        slot = self._free_slot(pool, preferred, provider)
        if slot is None:
            if key not in q:
                q.append(key)
            return None
        if q and q[0] == key:
            q.popleft()
        self._busy[slot.name] = (key, role)
        self.store.set_slot_busy(slot.name, key, role)
        get_logger(ticket=key, slot=slot.name).info("slot_acquired", role=role)
        return slot

    def release(self, slot: Slot) -> None:
        with self._cond:
            if self._busy.pop(slot.name, None) is not None:
                self.store.set_slot_free(slot.name)
            self._cond.notify_all()

    def leave_queue(self, key: str, pool: str) -> None:
        with self._cond:
            if key in self._queues[pool]:
                self._queues[pool].remove(key)
            self._cond.notify_all()

    @contextmanager
    def slot(
        self,
        key: str,
        role: str,
        preferred: str | None = None,
        on_queued: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        poll_s: float = 1.0,
        provider: str | None = None,
    ) -> Iterator[Slot | None]:
        """Block until a slot is free; release it on exit (also on exceptions).
        `on_queued(position)` is called once if the ticket has to wait. Roles outside the pool
        (intent) yield None."""
        pool = self.pool_for(role)
        if pool is None:
            yield None
            return
        slot: Slot | None = None
        told = False
        with self._cond:
            while slot is None:
                slot = self._try(key, role, pool, preferred, provider)
                if slot is not None:
                    break
                if not told and on_queued is not None:
                    told = True
                    position = self._queues[pool].index(key) + 1
                    self._cond.release()
                    try:
                        on_queued(position)
                    finally:
                        self._cond.acquire()
                    continue
                if cancelled is not None and cancelled():
                    if key in self._queues[pool]:
                        self._queues[pool].remove(key)
                    self._cond.notify_all()
                    raise SlotCancelled(f"{key}: stopped while waiting for a {pool} slot")
                self._cond.wait(timeout=poll_s)
        try:
            yield slot
        finally:
            self.release(slot)
