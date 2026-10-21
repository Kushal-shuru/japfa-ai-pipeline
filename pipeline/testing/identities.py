"""Who a live test call is made as (P10, `acceptance.identities`).

- `headers`: plain headers, e.g. a service's `x-client-id` / `x-client-secret` (the stack
  generated `SERVICE_CREDENTIALS`, so the pipeline knows the pair) and the partner headers.
- `seeded_session`: the gateway's logged-in state without a login. A real login ends in
  `setSession` (ws1 `utils/manageSession.js`) writing the session into Redis (connect-redis,
  key `<REDIS_KEY_PREFIX>sess:<id>`: ioredis puts the prefix before every key) and the browser
  keeping only a signed cookie `s:<id>.<HMAC-SHA256(id, SESSION_SECRET)>`. The stack generated
  `SESSION_SECRET` and runs the local Redis, so the pipeline writes the same session and signs
  the cookie itself; from there the gateway's real `isAuthenticated`, `isAuthorized` and
  `validatePartnerContext` run.

`session.obj_perms: all` grants every permission id the gateway's route schemas declare
(`security_ids`), so a ticket's new routes are reachable; `!noperm` still has none.

Variants for the auth cases (a step's `as`): `<name>` · `<name>!noperm` (the session without
the permissions the route needs) · `<name>!expired` · `none` (no auth at all).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pipeline.testing.envfile import render_value

VARIANTS = ("noperm", "expired", "own")  # own: the run's own partner (always new, swept)
_SECURITY_IDS = re.compile(r"securityIDs\s*:\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"['\"`]([^'\"`]+)['\"`]")


def security_ids(root: Path) -> list[str]:
    """Every permission id the gateway's route schemas name (`securityIDs: ['…']`), for an
    identity with `obj_perms: all`: a ticket's new routes bring their own."""
    found: set[str] = set()
    if root.is_dir():
        for file in root.rglob("*.js"):
            text = file.read_text(errors="replace")
            for block in _SECURITY_IDS.findall(text):
                found |= set(_QUOTED.findall(block))
    noise = {"Array", "Date", "Object", "String", "Number", "Boolean", "Integer"}
    return sorted(i for i in found if i not in noise and not any(c.isspace() for c in i))


def sign_cookie(sid: str, secret: str) -> str:
    """express-session's cookie value: `s:<sid>.<signature>` (cookie-signature, base64 without
    padding), URL-encoded as the browser sends it."""
    mac = hmac.new(secret.encode(), sid.encode(), hashlib.sha256).digest()
    sig = base64.b64encode(mac).decode().rstrip("=")
    return quote(f"s:{sid}.{sig}", safe="")


def split_variant(name: str) -> tuple[str, str]:
    base, _, variant = name.partition("!")
    return base, variant


def session_body(fields: dict[str, Any], variant: str, ttl_s: int,
                 now: datetime | None = None) -> dict:  # fmt: skip
    """The session JSON in `setSession`'s shape, plus the cookie block express-session needs to
    load it. `fields` uses plain names (user_pk, user_uuid, user_id, user_name, app_pk, roles,
    obj_perms, allowed_units, partner_id, aes_key); anything else is copied as it is."""
    now = now or datetime.now(UTC)
    known = {"user_pk", "user_uuid", "user_id", "user_name", "app_pk", "roles", "obj_perms",
             "allowed_units", "partner_id", "aes_key"}  # fmt: skip
    expires = now + timedelta(seconds=ttl_s)
    session_expires = now - timedelta(minutes=1) if variant == "expired" else expires
    body = {
        "cookie": {"originalMaxAge": ttl_s * 1000, "expires": expires.isoformat(),
                   "secure": False, "httpOnly": True, "path": "/"},
        "sessionExpires": session_expires.isoformat(),
        "nUserPk": fields.get("user_pk"),
        "cUserUuid": fields.get("user_uuid"),
        "cUserId": fields.get("user_id"),
        "cUserName": fields.get("user_name"),
        "nAppPk": fields.get("app_pk"),
        "listRoles": list(fields.get("roles") or []),
        "listObjPerms": [] if variant == "noperm" else list(fields.get("obj_perms") or []),
        "listAllowedUnits": list(fields.get("allowed_units") or []),
        "isChangePasswordRequired": False,
        "isForceLogoutRequired": False,
        "aesKey": fields.get("aes_key"),
        "closioV1Data": {"partnerId": fields.get("partner_id")},
    }  # fmt: skip
    body.update({k: v for k, v in fields.items() if k not in known})
    return body


class Identities:
    """Builds the auth headers for a step. `redis`: the stack's `RespClient` (seeded sessions);
    `env_of(repo)`: the env the repo's app runs with (cookie name, secret, key prefix);
    `variables`: the run's template values."""

    def __init__(self, config: Any, redis: Any, env_of: Any, variables: dict,
                 all_perms: Any = None) -> None:  # fmt: skip
        self.config = config  # AcceptanceConfig
        self.redis = redis
        self.env_of = env_of
        self.variables = variables
        self.all_perms = all_perms or (lambda _repo: [])  # repo → every securityID it declares
        self._cookies: dict[str, str] = {}

    def names(self) -> list[str]:
        return list(self.config.identities)

    def resolve(self, repo: str, name: str) -> str:
        if name in ("", "default"):
            return self.config.default_identity.get(repo, "none")
        return name

    def known(self, name: str) -> bool:
        base, variant = split_variant(name)
        return name == "none" or (base in self.config.identities
                                  and variant in ("", *VARIANTS))  # fmt: skip

    def headers(self, repo: str, name: str) -> dict[str, str]:
        always = {k: str(v) for k, v in
                  render_value(self.config.repo_headers.get(repo, {}), self.variables).items()}
        name = self.resolve(repo, name)
        if name == "none":
            return always
        base, variant = split_variant(name)
        ident = self.config.identities[base]
        variables = self._variables(variant)
        out = {**always,
               **{k: str(v) for k, v in render_value(ident.headers, variables).items()}}
        if ident.kind == "seeded_session":
            out["Cookie"] = self._session_cookie(base, variant, ident)
        return out

    def _variables(self, variant: str) -> dict:
        """The templates for an identity; `!own` swaps the partner for the run's own one."""
        if variant != "own":
            return self.variables
        run = {**self.variables.get("run", {})}
        run["partner_id"] = run.get("own_partner", run.get("partner_id"))
        return {**self.variables, "run": run}

    def _session_cookie(self, base: str, variant: str, ident: Any) -> str:
        cache = f"{base}!{variant}"
        if cache not in self._cookies:
            env = self.env_of(ident.repo)
            sid = secrets.token_urlsafe(24)
            fields = render_value(ident.session, self._variables(variant))
            if fields.get("obj_perms") == "all":
                fields["obj_perms"] = self.all_perms(ident.repo)
            body = session_body(fields, variant, ident.ttl_s)
            key = env.get(ident.key_prefix_env, "") + ident.session_key.format(sid=sid)
            self.redis.call("SET", key, json.dumps(body), "EX", str(ident.ttl_s))
            name = env.get(ident.cookie_name_env) or "connect.sid"
            self._cookies[cache] = f"{name}={sign_cookie(sid, env[ident.secret_env])}"
        return self._cookies[cache]
