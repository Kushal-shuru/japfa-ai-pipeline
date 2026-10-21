"""Tracking tables (CLAUDE.md §8) on SQLAlchemy Core. Checkpoints win if the two disagree."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

from pipeline.schemas import AgentRun, Command

metadata = sa.MetaData()

tickets = sa.Table(
    "tickets",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
    sa.Column("repos", sa.JSON, nullable=False, default=list),
    sa.Column("stage", sa.String),
    sa.Column("waiting_for", sa.String, nullable=True),
    sa.Column("jira_status", sa.String),
    sa.Column("review_rounds", sa.Integer, nullable=False, default=0),
    sa.Column("qa_rounds", sa.Integer, nullable=False, default=0),
    sa.Column("replans", sa.Integer, nullable=False, default=0),
    sa.Column("total_cost_usd", sa.Float, nullable=False, default=0.0),
    sa.Column("started_by", sa.String),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("halted_reason", sa.String),
)

ticket_mrs = sa.Table(
    "ticket_mrs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("key", sa.String, nullable=False, index=True),
    sa.Column("repo", sa.String, nullable=False),
    sa.Column("round_kind", sa.String, nullable=False),  # 'initial' | 'qa<n>'
    sa.Column("branch", sa.String),
    sa.Column("mr_iid", sa.Integer),
    sa.Column("mr_url", sa.String),
    sa.Column("state", sa.String),  # 'opened' | 'merged' | 'closed'
    sa.Column("opened_at", sa.DateTime(timezone=True)),
    sa.Column("merged_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("key", "repo", "round_kind"),
)

EVENT_TYPES = frozenset(
    {
        "node_start",
        "node_end",
        "gate_wait",
        "gate_pass",
        "transition",
        "comment",
        "chat_post",
        "human_command",
        "resume",
        "error",
        "halt",
        "llm_call",  # Chat LLM call (P4b, D89): provider, model, latency, tokens, ok/error
        "env_check",  # P10: the preflight's result (ok, problems)
        "acceptance",  # P10: a live run (ok, counts, pending migrations, cleanup, duration)
        "migrate",  # P10: a person ran `pipeline migrate` (repo, migrations applied, ok)
    }
)

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("key", sa.String, nullable=False, index=True),
    sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    sa.Column("type", sa.String, nullable=False),
    sa.Column("node", sa.String),
    sa.Column("repo", sa.String, nullable=True),
    sa.Column("data", sa.JSON),
)

agent_runs = sa.Table(
    "agent_runs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("key", sa.String, nullable=False, index=True),
    sa.Column("repo", sa.String, nullable=True),
    sa.Column("role", sa.String, nullable=False),
    sa.Column("slot", sa.String, nullable=True),
    sa.Column("run_no", sa.Integer, nullable=False),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("auth_mode", sa.String, nullable=False),
    sa.Column("model", sa.String, nullable=True),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("duration_s", sa.Float),
    sa.Column("ok", sa.Boolean, nullable=False),
    sa.Column("turns", sa.Integer, nullable=True),
    sa.Column("input_tokens", sa.Integer, nullable=True),
    sa.Column("output_tokens", sa.Integer, nullable=True),
    sa.Column("cost_usd", sa.Float, nullable=True),
    sa.Column("transcript_path", sa.String),
    sa.Column("error", sa.String, nullable=True),
)

commands = sa.Table(
    "commands",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    sa.Column("key", sa.String, nullable=True, index=True),
    sa.Column("verb", sa.String, nullable=False),
    sa.Column("args", sa.JSON),
    sa.Column("actor_id", sa.String),
    sa.Column("actor_name", sa.String),
    sa.Column("source", sa.String),
    sa.Column("raw", sa.String),
    sa.Column("interpreted", sa.Boolean, nullable=False, default=False),
    sa.Column("accepted", sa.Boolean, nullable=False),
    sa.Column("reason", sa.String, nullable=True),
)

agent_slots = sa.Table(
    "agent_slots",
    metadata,
    sa.Column("name", sa.String, primary_key=True),
    sa.Column("pool", sa.String, nullable=False),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("busy_key", sa.String, nullable=True),
    sa.Column("busy_role", sa.String, nullable=True),
    sa.Column("busy_since", sa.DateTime(timezone=True), nullable=True),
)

chat_threads = sa.Table(
    "chat_threads",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
    sa.Column("channel_id", sa.String),
    sa.Column("conversation_ref", sa.JSON),
    sa.Column("root_message_id", sa.String),
    sa.Column("created_at", sa.DateTime(timezone=True)),
)

# [P9] small per-ticket records the pipeline keeps between restarts: the Jira comment cursor,
# the Jira delivery summary (name → JSON)
ticket_notes = sa.Table(
    "ticket_notes",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
    sa.Column("name", sa.String, primary_key=True),
    sa.Column("value", sa.JSON),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
)

TICKET_FIELDS = frozenset(c.name for c in tickets.columns) - {"key"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Store:
    """Tracking DB. `url` e.g. `sqlite:///~/.ai-pipeline/pipeline.db` or `sqlite://` (memory)."""

    def __init__(self, url: str, clock: Callable[[], datetime] = _utcnow) -> None:
        self.url = url
        self.clock = clock
        self.engine = self._make_engine(url)
        # In-memory SQLite shares one connection: serialise its use across threads (parallel
        # coders / reviewers, D129). File databases get a connection per thread.
        self._lock: Any = (
            threading.RLock() if self.engine.pool.__class__ is StaticPool else nullcontext()
        )

    @staticmethod
    def _make_engine(url: str) -> Engine:
        parsed = sa.make_url(url)
        if parsed.get_backend_name() != "sqlite":
            return sa.create_engine(url)
        memory = parsed.database in (None, "", ":memory:")
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if memory:
            kwargs["poolclass"] = StaticPool  # one shared connection keeps the tables alive
        engine = sa.create_engine(url, **kwargs)

        @sa.event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            if not memory:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        return engine

    @classmethod
    def for_home(cls, home: Any) -> Store:
        return cls(f"sqlite:///{home}/pipeline.db")

    def init(self) -> None:
        metadata.create_all(self.engine)

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self._lock, self.engine.begin() as conn:
            yield conn

    @contextmanager
    def _conn(self) -> Iterator[Connection]:
        with self._lock, self.engine.connect() as conn:
            yield conn

    def journal_mode(self) -> str:
        with self._conn() as conn:
            return str(conn.exec_driver_sql("PRAGMA journal_mode").scalar())

    # ---------- tickets ----------

    def upsert_ticket(self, key: str, **fields: Any) -> None:
        unknown = set(fields) - TICKET_FIELDS
        if unknown:
            raise ValueError(f"unknown ticket fields {sorted(unknown)}")
        now = self.clock()
        fields.setdefault("updated_at", now)
        with self._tx() as conn:
            exists = conn.execute(sa.select(tickets.c.key).where(tickets.c.key == key)).first()
            if exists:
                conn.execute(tickets.update().where(tickets.c.key == key).values(**fields))
            else:
                fields.setdefault("started_at", now)
                fields.setdefault("repos", [])
                conn.execute(tickets.insert().values(key=key, **fields))

    def get_ticket(self, key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(tickets).where(tickets.c.key == key)).mappings().first()
            return dict(row) if row else None

    def list_tickets(self, stage: str | None = None) -> list[dict]:
        q = sa.select(tickets).order_by(tickets.c.started_at)
        if stage is not None:
            q = q.where(tickets.c.stage == stage)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def delete_ticket(self, key: str) -> None:
        """Remove a ticket's tracking rows. The `commands` audit log is kept."""
        with self._tx() as conn:
            for table in (tickets, ticket_mrs, events, agent_runs, chat_threads, ticket_notes):
                conn.execute(table.delete().where(table.c.key == key))

    def get_note(self, key: str, name: str) -> Any:
        q = sa.select(ticket_notes.c.value).where(ticket_notes.c.key == key,
                                                   ticket_notes.c.name == name)  # fmt: skip
        with self._conn() as conn:
            return conn.execute(q).scalar_one_or_none()

    def set_note(self, key: str, name: str, value: Any) -> None:
        where = (ticket_notes.c.key == key) & (ticket_notes.c.name == name)
        with self._tx() as conn:
            if conn.execute(sa.select(ticket_notes.c.key).where(where)).first():
                conn.execute(ticket_notes.update().where(where)
                             .values(value=value, updated_at=self.clock()))  # fmt: skip
            else:
                conn.execute(ticket_notes.insert().values(key=key, name=name, value=value,
                                                          updated_at=self.clock()))  # fmt: skip

    def delete_commands(self, key: str) -> None:
        """The `commands` audit rows of a ticket (demo-reset only; `reset` keeps them)."""
        with self._tx() as conn:
            conn.execute(commands.delete().where(commands.c.key == key))

    # ---------- events ----------

    def record_event(
        self,
        key: str,
        type: str,
        node: str | None = None,
        repo: str | None = None,
        data: dict | None = None,
    ) -> None:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}; valid: {sorted(EVENT_TYPES)}")
        with self._tx() as conn:
            conn.execute(
                events.insert().values(
                    key=key, ts=self.clock(), type=type, node=node, repo=repo, data=data or {}
                )
            )

    def events_for(self, key: str, type: str | None = None) -> list[dict]:
        q = sa.select(events).where(events.c.key == key).order_by(events.c.id)
        if type is not None:
            q = q.where(events.c.type == type)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ---------- MRs ----------

    def upsert_mr(
        self,
        key: str,
        repo: str,
        round_kind: str = "initial",
        *,
        branch: str | None = None,
        mr_iid: int | None = None,
        mr_url: str | None = None,
        state: str | None = None,
        opened_at: datetime | None = None,
        merged_at: datetime | None = None,
    ) -> None:
        values = {
            k: v
            for k, v in dict(
                branch=branch,
                mr_iid=mr_iid,
                mr_url=mr_url,
                state=state,
                opened_at=opened_at,
                merged_at=merged_at,
            ).items()
            if v is not None
        }
        if state == "merged" and merged_at is None:
            values["merged_at"] = self.clock()
        where = sa.and_(
            ticket_mrs.c.key == key,
            ticket_mrs.c.repo == repo,
            ticket_mrs.c.round_kind == round_kind,
        )
        with self._tx() as conn:
            row = conn.execute(sa.select(ticket_mrs.c.id, ticket_mrs.c.merged_at).where(where))
            existing = row.first()
            if existing:
                if existing.merged_at is not None:
                    values.pop("merged_at", None)
                if values:
                    conn.execute(ticket_mrs.update().where(where).values(**values))
            else:
                values.setdefault("opened_at", self.clock())
                conn.execute(
                    ticket_mrs.insert().values(key=key, repo=repo, round_kind=round_kind, **values)
                )

    def mrs_for(self, key: str, round_kind: str | None = None) -> list[dict]:
        q = sa.select(ticket_mrs).where(ticket_mrs.c.key == key).order_by(ticket_mrs.c.id)
        if round_kind is not None:
            q = q.where(ticket_mrs.c.round_kind == round_kind)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ---------- agent runs ----------

    def record_agent_run(self, key: str, run: AgentRun, started_at: datetime | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                agent_runs.insert().values(
                    key=key, started_at=started_at or self.clock(), **run.model_dump()
                )
            )
            if run.cost_usd:
                conn.execute(
                    tickets.update()
                    .where(tickets.c.key == key)
                    .values(total_cost_usd=tickets.c.total_cost_usd + run.cost_usd)
                )

    def ticket_cost(self, key: str) -> float:
        q = sa.select(sa.func.coalesce(sa.func.sum(agent_runs.c.cost_usd), 0.0)).where(
            agent_runs.c.key == key
        )
        with self._conn() as conn:
            return float(conn.execute(q).scalar_one())

    def ticket_run_count(self, key: str) -> int:
        q = sa.select(sa.func.count()).select_from(agent_runs).where(agent_runs.c.key == key)
        with self._conn() as conn:
            return int(conn.execute(q).scalar_one())

    def runs_for(self, key: str) -> list[dict]:
        q = sa.select(agent_runs).where(agent_runs.c.key == key).order_by(agent_runs.c.id)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ---------- commands ----------

    def record_command(self, cmd: Command, accepted: bool, reason: str | None = None) -> None:
        now = self.clock()
        with self._tx() as conn:
            conn.execute(
                commands.insert().values(
                    ts=now,
                    key=cmd.key,
                    verb=cmd.verb,
                    args=cmd.args,
                    actor_id=cmd.actor_id,
                    actor_name=cmd.actor_name,
                    source=cmd.source,
                    raw=cmd.raw,
                    interpreted=cmd.interpreted,
                    accepted=accepted,
                    reason=reason,
                )
            )
            if cmd.key:
                conn.execute(
                    events.insert().values(
                        key=cmd.key,
                        ts=now,
                        type="human_command",
                        node=None,
                        data={
                            "verb": cmd.verb,
                            "args": cmd.args,
                            "actor": cmd.actor_name,
                            "source": cmd.source,
                            "accepted": accepted,
                            "reason": reason,
                        },
                    )
                )

    def commands_for(self, key: str | None = None) -> list[dict]:
        q = sa.select(commands).order_by(commands.c.id)
        if key is not None:
            q = q.where(commands.c.key == key)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ---------- agent slots ----------

    def register_slot(self, name: str, pool: str, provider: str) -> None:
        """Create or update a slot from config (keeps its busy state)."""
        with self._tx() as conn:
            exists = conn.execute(
                sa.select(agent_slots.c.name).where(agent_slots.c.name == name)
            ).first()
            if exists:
                conn.execute(
                    agent_slots.update()
                    .where(agent_slots.c.name == name)
                    .values(pool=pool, provider=provider)
                )
            else:
                conn.execute(agent_slots.insert().values(name=name, pool=pool, provider=provider))

    def set_slot_busy(self, name: str, key: str, role: str) -> bool:
        """Mark a free slot busy. Returns False if it is already busy (or unknown)."""
        with self._tx() as conn:
            result = conn.execute(
                agent_slots.update()
                .where(agent_slots.c.name == name, agent_slots.c.busy_key.is_(None))
                .values(busy_key=key, busy_role=role, busy_since=self.clock())
            )
            return result.rowcount == 1

    def set_slot_free(self, name: str) -> None:
        with self._tx() as conn:
            conn.execute(
                agent_slots.update()
                .where(agent_slots.c.name == name)
                .values(busy_key=None, busy_role=None, busy_since=None)
            )

    def free_all_slots(self) -> None:
        with self._tx() as conn:
            conn.execute(
                agent_slots.update().values(busy_key=None, busy_role=None, busy_since=None)
            )

    def list_slots(self, pool: str | None = None) -> list[dict]:
        q = sa.select(agent_slots).order_by(agent_slots.c.pool, agent_slots.c.name)
        if pool is not None:
            q = q.where(agent_slots.c.pool == pool)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ---------- chat threads ----------

    def save_chat_thread(
        self,
        key: str,
        channel_id: str | None,
        conversation_ref: dict | None,
        root_message_id: str | None,
    ) -> None:
        values = dict(
            channel_id=channel_id,
            conversation_ref=conversation_ref,
            root_message_id=root_message_id,
        )
        with self._tx() as conn:
            exists = conn.execute(
                sa.select(chat_threads.c.key).where(chat_threads.c.key == key)
            ).first()
            if exists:
                conn.execute(
                    chat_threads.update().where(chat_threads.c.key == key).values(**values)
                )
            else:
                conn.execute(
                    chat_threads.insert().values(key=key, created_at=self.clock(), **values)
                )

    def get_chat_thread(self, key: str) -> dict | None:
        q = sa.select(chat_threads).where(chat_threads.c.key == key)
        with self._conn() as conn:
            row = conn.execute(q).mappings().first()
            return dict(row) if row else None

    def list_chat_threads(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sa.select(chat_threads)).mappings()]

    def delete_chat_thread(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute(chat_threads.delete().where(chat_threads.c.key == key))

    # ---------- stats ----------

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            counts = {
                t.name: int(conn.execute(sa.select(sa.func.count()).select_from(t)).scalar_one())
                for t in metadata.sorted_tables
            }
            cost = conn.execute(
                sa.select(sa.func.coalesce(sa.func.sum(agent_runs.c.cost_usd), 0.0))
            ).scalar_one()
            stages = dict(
                conn.execute(
                    sa.select(tickets.c.stage, sa.func.count()).group_by(tickets.c.stage)
                ).all()
            )
        return {"counts": counts, "total_cost_usd": float(cost), "stages": stages}
