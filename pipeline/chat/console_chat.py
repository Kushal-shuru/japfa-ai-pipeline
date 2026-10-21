"""ChatPort for the CLI / dev: prints `[DEMO-1] …` messages, renders cards as numbered actions,
appends everything to `$PIPELINE_HOME/logs/chat.log`, and keeps a buffer that the local server
serves to `pipeline say` (GET /local/chat)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from pipeline.chat.cards import Card, render_text
from pipeline.logging import mask_text


def _now() -> datetime:
    return datetime.now(UTC)


class ConsoleChat:
    def has_own_thread(self, key: str) -> bool:
        """One stream for everything: no separate main channel (P8 2.1)."""
        return False

    def __init__(
        self,
        log_path: Path | None = None,
        console: Console | None = None,
        store: Any = None,
        clock: Callable[[], datetime] = _now,
        echo: bool = True,
        command_prefix: str = 'pipeline say "',
    ) -> None:
        self.log_path = log_path
        self.console = console or Console()
        self.store = store
        self.clock = clock
        self.echo = echo
        self.command_prefix = command_prefix  # "" in `pipeline cli`: type the command as shown
        self.messages: list[dict] = []  # {"seq", "id", "key", "text", "kind"}
        self._lock = threading.Lock()
        self._next = 0

    # ---------- ChatPort ----------

    def ensure_thread(self, key: str, origin: dict | None = None) -> None:
        if self.store is not None and self.store.get_chat_thread(key) is None:
            self.store.save_chat_thread(key, "console", origin, None)

    def post(self, key: str | None, text_md: str, card: dict | None = None) -> str:
        text_md, card = mask_text(text_md), mask_text(card)  # 1.8
        body = text_md.rstrip()
        if card:
            rendered = render_text(Card.from_dict(card), self.command_prefix)
            body = f"{body}\n\n{rendered}" if body else rendered
        msg_id = self._emit(key, body, "post")
        if self.store is not None and key:
            self.store.record_event(key, "chat_post", data={"message_id": msg_id})
        return msg_id

    def update(self, message_id: str, text_md: str | None = None, card: dict | None = None) -> None:
        text_md, card = mask_text(text_md), mask_text(card)
        key = next((m["key"] for m in self.messages if m["id"] == message_id), None)
        body = text_md or (render_text(Card.from_dict(card), self.command_prefix) if card else "")
        self._emit(key, f"(updated {message_id}) {body}".rstrip(), "update", message_id)

    # ---------- buffer for `pipeline say` ----------

    def since(self, seq: int, key: str | None = None) -> list[dict]:
        with self._lock:
            return [m for m in self.messages if m["seq"] > seq and (key is None or m["key"] == key)]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._next

    # ---------- internals ----------

    def _emit(self, key: str | None, body: str, kind: str, msg_id: str | None = None) -> str:
        with self._lock:
            self._next += 1
            seq = self._next
            msg_id = msg_id or f"console-{seq}"
            self.messages.append({"seq": seq, "id": msg_id, "key": key, "text": body,
                                  "kind": kind})  # fmt: skip
        prefix = f"[{key}]" if key else "[pipeline]"
        if self.echo:
            self.console.rule(f"{prefix} {self.clock():%H:%M:%S}", align="left", style="dim")
            self.console.print(Markdown(body))
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{self.clock().isoformat()} {prefix} {body}\n")
        return msg_id
