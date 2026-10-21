"""The fixture server for external services during live tests (P10).

One local HTTP server; each external is a path prefix: `{{mock:partner_public}}` =
`http://127.0.0.1:<port>/partner_public`, so an app's `PARTNER_PUBLIC_BASE_URL` points here.
Per external (`acceptance.externals`):

- `fixtures`: answered from `$PIPELINE_HOME/fixtures/<name>/*.json` (or the configured
  directory). A request without a matching fixture gets **501** with the exact call, so a case
  that depends on it comes back *blocked*, never green on a made-up answer.
- `real_dev`: GET / HEAD are forwarded to the real dev service; any write is refused (it would
  create data the test can't delete).
- `blocked`: always refused.

A fixture file holds one fixture or a list:
`{"method": "GET", "path": "/users/{id}", "query": {"active": "true"}, "status": 200,
"body": {...}, "source": "recorded"}`; `{name}` matches one path segment, a trailing `*` the
rest. Every request is logged (`requests`), for the evidence and for "was it called" checks.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MISS = "miss"  # X-Aipl-Mock values
REFUSED = "refused"
SAFE_METHODS = frozenset({"GET", "HEAD"})


def _path_regex(pattern: str) -> re.Pattern[str]:
    pattern = "/" + pattern.strip().lstrip("/")
    out, i = "", 0
    for m in re.finditer(r"\{[^}/]+\}|\*$", pattern):
        out += re.escape(pattern[i : m.start()])
        out += ".*" if m.group(0) == "*" else "[^/]+"
        i = m.end()
    out += re.escape(pattern[i:])
    return re.compile("^" + out.rstrip("/") + "/?$")


@dataclass
class Fixture:
    method: str
    path: str
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    source: str = "authored"
    file: str = ""

    def matches(self, method: str, path: str, query: dict[str, str]) -> bool:
        if self.method.upper() != method.upper():
            return False
        if not _path_regex(self.path).match(path):
            return False
        return all(query.get(k) == str(v) for k, v in self.query.items())


def load_fixtures(directory: Path) -> list[Fixture]:
    """Every fixture in `directory` (sorted by file name; a broken file is skipped)."""
    out: list[Fixture] = []
    if not directory.is_dir():
        return out
    for file in sorted(directory.glob("*.json")):
        try:
            data = json.loads(file.read_text())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("method") and item.get("path"):
                out.append(Fixture(
                    method=str(item["method"]), path=str(item["path"]),
                    status=int(item.get("status", 200)), body=item.get("body"),
                    headers=dict(item.get("headers") or {}), query=dict(item.get("query") or {}),
                    source=str(item.get("source") or "authored"), file=file.name,
                ))  # fmt: skip
    return out


@dataclass
class External:
    name: str
    mode: str  # fixtures | real_dev | blocked
    fixtures: list[Fixture] = field(default_factory=list)
    real_url: str | None = None


class ExternalsServer:
    def __init__(self, externals: dict[str, External]) -> None:
        self.externals = externals
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet: we keep our own log
                return

            def _handle(self) -> None:
                server._answer(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="aipl-externals")  # fmt: skip
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def misses(self, since: int = 0) -> list[dict]:
        """Requests nothing could answer (no fixture / refused) since request number `since`."""
        with self._lock:
            return [r for r in self.requests[since:] if r["result"] in (MISS, REFUSED)]

    # ---------- answering ----------

    def _record(self, entry: dict) -> None:
        with self._lock:
            self.requests.append(entry)

    def _send(self, h: BaseHTTPRequestHandler, status: int, body: Any, result: str,
              headers: dict[str, str] | None = None) -> None:  # fmt: skip
        data = b"" if body is None else (body if isinstance(body, bytes) else
                                         json.dumps(body).encode())  # fmt: skip
        h.send_response(status)
        h.send_header("Content-Type", (headers or {}).get("Content-Type", "application/json"))
        for k, v in (headers or {}).items():
            if k.lower() not in ("content-type", "content-length", "transfer-encoding"):
                h.send_header(k, v)
        h.send_header("X-Aipl-Mock", result)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(data)

    def _answer(self, h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        name, _, rest = parts.path.lstrip("/").partition("/")
        path = "/" + rest
        query = dict(parse_qsl(parts.query))
        method = h.command.upper()
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        entry = {"external": name, "method": method, "path": path, "query": query,
                 "at": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
        ext = self.externals.get(name)
        if ext is None:
            entry.update(result=MISS, status=501)
            self._record(entry)
            self._send(h, 501, {"error": f"aipl: unknown external `{name}`"}, MISS)
            return
        if ext.mode == "blocked":
            entry.update(result=REFUSED, status=501)
            self._record(entry)
            self._send(h, 501, {"error": f"aipl: {name} is blocked in tests"}, REFUSED)
            return
        if ext.mode == "real_dev":
            self._forward(h, ext, method, path, parts.query, raw, entry)
            return
        found = next((f for f in ext.fixtures if f.matches(method, path, query)), None)
        if found is None:
            entry.update(result=MISS, status=501)
            self._record(entry)
            self._send(h, 501, {"error": f"aipl: no fixture for {method} {path} ({name})"}, MISS)
            return
        entry.update(result="fixture", status=found.status, fixture=found.file)
        self._record(entry)
        self._send(h, found.status, found.body, "fixture", found.headers)

    def _forward(self, h: BaseHTTPRequestHandler, ext: External, method: str, path: str,
                 query: str, raw: bytes, entry: dict) -> None:  # fmt: skip
        if method not in SAFE_METHODS or not ext.real_url:
            entry.update(result=REFUSED, status=501)
            self._record(entry)
            why = ("writes to the real service are not allowed in tests" if ext.real_url
                   else "no real URL configured")  # fmt: skip
            self._send(h, 501, {"error": f"aipl: {method} {path} to {ext.name}: {why}"}, REFUSED)
            return
        import httpx

        url = ext.real_url.rstrip("/") + path + (f"?{query}" if query else "")
        keep = {k: v for k, v in h.headers.items()
                if k.lower() in ("accept", "authorization", "x-client-id", "x-client-secret",
                                 "x-partner-id", "x-user-id", "x-trace-id")}  # fmt: skip
        try:
            resp = httpx.request(method, url, headers=keep, timeout=20)
        except httpx.HTTPError as err:
            entry.update(result=MISS, status=502, error=str(err)[:200])
            self._record(entry)
            self._send(h, 502, {"error": f"aipl: {ext.name} unreachable: {err}"}, MISS)
            return
        entry.update(result="real", status=resp.status_code)
        self._record(entry)
        self._send(h, resp.status_code, resp.content, "real",
                   {"Content-Type": resp.headers.get("content-type", "application/json")})
