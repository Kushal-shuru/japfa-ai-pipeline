"""A private Redis for one test run (P10).

The repos' own `.env` points Redis at the shared ElastiCache, whose BullMQ queues the deployed
dev workers listen on: a test job would run there with the old code (and the crons send
emails), and a local worker could pick up the team's jobs. So each run gets its own
`redis-server`: a free port, no persistence, a generated password and (both apps' clients use
`rediss://`) TLS with a throw-away certificate. `RespClient` speaks just enough RESP to seed a
session and check it's up.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import time
from pathlib import Path

from pipeline.agents.providers.children import register, unregister


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class RedisError(RuntimeError):
    pass


class RespClient:
    """A minimal RESP client (AUTH, PING, SET, GET, DEL) over TCP or TLS."""

    def __init__(self, host: str, port: int, password: str | None, tls: bool,
                 timeout: float = 5) -> None:  # fmt: skip
        self.host, self.port, self.password, self.tls = host, port, password, tls
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        if self.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # our own throw-away certificate
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        return sock

    @staticmethod
    def _encode(*args: str | bytes) -> bytes:
        out = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode()
            out.append(f"${len(b)}\r\n".encode() + b + b"\r\n")
        return b"".join(out)

    @staticmethod
    def _read(f: object) -> object:
        line = f.readline()  # type: ignore[attr-defined]
        if not line:
            raise RedisError("connection closed")
        kind, rest = line[:1], line[1:].rstrip(b"\r\n")
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RedisError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = f.read(n + 2)  # type: ignore[attr-defined]
            return data[:-2].decode()
        if kind == b"*":
            return [RespClient._read(f) for _ in range(int(rest))]
        raise RedisError(f"unexpected reply {line!r}")

    def call(self, *args: str | bytes) -> object:
        with self._connect() as sock, sock.makefile("rb") as f:
            if self.password:
                sock.sendall(self._encode("AUTH", "default", self.password))
                self._read(f)
            sock.sendall(self._encode(*args))
            return self._read(f)

    def ping(self) -> bool:
        try:
            return self.call("PING") == "PONG"
        except (OSError, RedisError):
            return False


def _certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 (the apps don't check it: rejectUnauthorized
    false), made with the system `openssl`."""
    key, cert = directory / "redis.key", directory / "redis.crt"
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RedisError("`openssl` isn't installed, so the local Redis can't use TLS")
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "2", "-subj", "/CN=localhost"],
        check=True, capture_output=True, timeout=30,
    )  # fmt: skip
    return key, cert


class LocalRedis:
    """`redis-server` in its own process group, registered in `$PIPELINE_HOME/children` so a
    crashed pipeline's leftovers are killed at the next start (P8 1.4)."""

    def __init__(self, directory: Path, tls: bool = True, children: Path | None = None) -> None:
        self.directory = directory
        self.tls = tls
        self.children = children
        self.port = 0
        self.password = secrets.token_urlsafe(18)
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    def client(self) -> RespClient:
        return RespClient(self.host, self.port, self.password, self.tls)

    def start(self, timeout_s: float = 15) -> None:
        binary = shutil.which("redis-server")
        if binary is None:
            raise RedisError("`redis-server` isn't installed (brew install redis)")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        args = [binary, "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
                "--dir", str(self.directory), "--requirepass", self.password,
                "--protected-mode", "yes"]  # fmt: skip
        if self.tls:
            key, cert = _certificate(self.directory)
            args += ["--port", "0", "--tls-port", str(self.port), "--tls-cert-file", str(cert),
                     "--tls-key-file", str(key), "--tls-ca-cert-file", str(cert),
                     "--tls-auth-clients", "no"]  # fmt: skip
        else:
            args += ["--port", str(self.port)]
        log = open(self.directory / "redis.log", "ab")  # noqa: SIM115 (lives with the process)
        self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)  # fmt: skip
        register(self.children, self.proc.pid)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RedisError(f"redis-server exited: {self.log_tail()}")
            if self.client().ping():
                return
            time.sleep(0.2)
        raise RedisError(f"redis-server didn't answer within {timeout_s:.0f}s: {self.log_tail()}")

    def log_tail(self, lines: int = 8) -> str:
        path = self.directory / "redis.log"
        if not path.is_file():
            return ""
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])

    def stop(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(5)
        unregister(self.children, self.proc.pid)
        self.proc = None
