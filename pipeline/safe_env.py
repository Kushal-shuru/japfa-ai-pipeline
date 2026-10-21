"""Environments for processes the pipeline starts outside an agent (Phase 9, audit F4).

- `command_env`: repo scripts (install, lint, tests, build) are code from the repo and its
  dependencies, so they get a **positive allowlist** of variables (paths, locale, Node/npm
  settings) plus `verify.env_passthrough`. Never the pipeline's credentials, never anything that
  looks like a credential unless the config names it. The repo's own `.env` file (the coder's
  test settings, D69) is read by the repo itself, as before.
- `without_credentials`: the orchestrator's own git keeps the rest of the environment (git
  config, ssh agent) but never sees the pipeline's credentials; the push token goes in through
  `GIT_ASKPASS` only for network calls (workspace).

This is not a sandbox: files the pipeline's OS user can read stay readable. Full isolation
(a disposable container per run) is the audit's W06, in the Backlog.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

from pipeline.agents.providers.base import PIPELINE_SECRET_ENV, PROVIDER_KEYS

CREDENTIAL_ENV = frozenset(
    {
        *PIPELINE_SECRET_ENV,
        *(k for keys in PROVIDER_KEYS.values() for k in keys),
        "GOOGLE_OAUTH_CLIENT_PATH",
        "GOOGLE_TOKEN_PATH",
    }  # fmt: skip
)
ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TMPDIR", "TEMP", "TMP",
    "TERM", "TZ", "CI", "COREPACK_HOME", "VOLTA_HOME", "PNPM_HOME", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "DOCKER_HOST", "COLORTERM", "FORCE_COLOR", "NO_COLOR",
})  # fmt: skip
ALLOW_PREFIXES = ("LC_", "NODE_", "NPM_CONFIG_", "npm_config_", "NVM_", "XDG_")
CREDENTIAL_LIKE = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|PRIVATE|CREDENTIAL|AUTH", re.I)


def command_env(passthrough: Iterable[str] = (),
                base: Mapping[str, str] | None = None) -> dict[str, str]:  # fmt: skip
    src = os.environ if base is None else base
    named = set(passthrough) - CREDENTIAL_ENV
    out = {k: v for k, v in src.items()
           if k in named or ((k in ALLOW or k.startswith(ALLOW_PREFIXES))
                             and not CREDENTIAL_LIKE.search(k))}  # fmt: skip
    out.setdefault("PATH", os.defpath)
    return out


def without_credentials(base: Mapping[str, str] | None = None) -> dict[str, str]:
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if k not in CREDENTIAL_ENV}
