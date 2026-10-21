"""Problems of the machine the pipeline runs on, not of the code (Phase 8 1.9, K29).

A check that fails with `ECONNREFUSED 127.0.0.1:6379` needs someone to start Redis, not a
coder run: `machine_problem()` recognises such output and says the cause and the fix in plain
words. Verify stops at once on them (no coder attempt spent); every other halt reason gets the
same plain lead (`graph.wrapper.halt`).
"""

from __future__ import annotations

import re

# well-known local services, by port
SERVICES: dict[int, str] = {
    6379: "Redis", 5432: "PostgreSQL", 3306: "MySQL", 27017: "MongoDB", 5672: "RabbitMQ",
    9092: "Kafka", 11211: "Memcached", 9200: "Elasticsearch",
}  # fmt: skip

LEAD = "This is a problem on the machine that runs the pipeline, not in the code:"

_REFUSED = re.compile(r"ECONNREFUSED\s+(?:\[?([\w.:-]+?)\]?):(\d{2,5})\b|ECONNREFUSED")
_NOT_FOUND = re.compile(
    r"(?:getaddrinfo\s+(?:ENOTFOUND|EAI_AGAIN)\s+([\w.-]+))"
    r"|(?:Could not resolve host:?\s*([\w.-]+))"
)
_NO_SPACE = re.compile(r"ENOSPC|No space left on device", re.I)
_IN_USE = re.compile(r"EADDRINUSE[^\n]*?:(\d{2,5})\b|address already in use[^\n]*?:(\d{2,5})\b")
_DOCKER = re.compile(r"Cannot connect to the Docker daemon", re.I)
_FILES = re.compile(r"EMFILE|too many open files", re.I)
_NPM_NETWORK = re.compile(r"npm ERR! (?:code )?(?:ETIMEDOUT|ECONNRESET|network)", re.I)


def machine_problem(text: str | None) -> str | None:
    """One plain sentence (cause + what to do) when `text` shows a machine problem, else None."""
    if not text:
        return None
    if m := _REFUSED.search(text):
        host, port = m.group(1), m.group(2)
        if port:
            service = SERVICES.get(int(port))
            where = f"{host}:{port}" if host else f"port {port}"
            if service:
                return (f"{service} isn't running (nothing answers on {where}). Start {service} "
                        f"on port {port}, then tell me to try again.")  # fmt: skip
            return (f"nothing answers on {where}. Start the service that should listen there, "
                    "then tell me to try again.")  # fmt: skip
        return ("a local service the checks need isn't running (connection refused). Start it, "
                "then tell me to try again.")  # fmt: skip
    if m := _NOT_FOUND.search(text):
        host = m.group(1) or m.group(2) or "a host"
        return (f"{host} can't be reached (the name doesn't resolve). Check the network or VPN, "
                "then tell me to try again.")  # fmt: skip
    if _NO_SPACE.search(text):
        return "the disk is full. Free some space, then tell me to try again."
    if m := _IN_USE.search(text):
        port = m.group(1) or m.group(2)
        return (f"port {port} is already in use by another program. Stop that program, then "
                "tell me to try again.")  # fmt: skip
    if _DOCKER.search(text):
        return "Docker isn't running. Start Docker, then tell me to try again."
    if _FILES.search(text):
        return ("the machine ran out of open files. Restart the pipeline (or raise the limit), "
                "then tell me to try again.")  # fmt: skip
    if _NPM_NETWORK.search(text):
        return ("npm couldn't reach its registry (network problem). Check the network, then "
                "tell me to try again.")  # fmt: skip
    return None


def plain_reason(reason: str) -> tuple[str, bool]:
    """`reason` with the machine problem's plain cause on top; (reason, is_machine)."""
    if reason.startswith(LEAD):
        return reason, True
    cause = machine_problem(reason)
    if cause is None:
        return reason, False
    return f"{LEAD} {cause}\n\n{reason}", True
