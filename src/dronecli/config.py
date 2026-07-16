"""Non-secret configuration: connection profiles and settings.

Config is a plain JSON file (``~/.config/drone-cli/config.json`` by default). It
never contains the API token — that lives in the OS keyring (see :mod:`spec`).

Environment overrides:

* ``DRONE_SERVER`` / ``DRONECLI_SERVER`` — the server URL (``DRONECLI_`` wins).
* ``DRONECLI_PROFILE`` — selects the active profile.
* ``DRONE_TOKEN`` / ``DRONECLI_TOKEN`` — the token directly.
* ``DRONECLI_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` — relocate this directory.

Every setting below has a **sane default** or is **asked once on first run** —
there is no silent half-configured state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcli.errors import ConfigError

from .spec import SPEC

DEFAULT_PROFILE = "default"

#: Where `promote`/`rollback` send a build when `--to` is omitted. Settable via
#: `drone-cli settings set-promote-target`.
DEFAULT_PROMOTE_TARGET = "prod"

#: How to build a commit's web URL from the repo's web URL.
#: Verified live against Gitea/Forgejo: `{repo}/commit/{sha}` -> 200, while
#: GitLab's `/-/commit/` -> 404 and Bitbucket's `/commits/` -> 303. Drone's own
#: `repo.scm` field is an EMPTY STRING even on a synced repo, so the provider is
#: not discoverable — we default to the Gitea/Forgejo/GitHub shape and let the
#: operator override rather than guess wrong.
COMMIT_URL_PATTERNS = {
    "gitea": "{repo}/commit/{sha}",      # also Forgejo
    "forgejo": "{repo}/commit/{sha}",
    "github": "{repo}/commit/{sha}",
    "gitlab": "{repo}/-/commit/{sha}",
    "bitbucket": "{repo}/commits/{sha}",
}
DEFAULT_SCM_FLAVOUR = "gitea"


def config_dir() -> Path:
    return SPEC.config_dir()


def config_path() -> Path:
    return SPEC.config_file()


@dataclass
class Profile:
    name: str
    base_url: str
    username: str | None = None  # informational; the login of the API user
    verify_ssl: bool = True

    def api_root(self) -> str:
        # Drone's REST API is unversioned and mounted at /api. Note NOT everything
        # useful lives under it: /version, /healthz and /varz sit on the web root.
        return self.base_url.rstrip("/") + "/api"


@dataclass
class Config:
    current_profile: str = DEFAULT_PROFILE
    profiles: dict[str, Profile] = field(default_factory=dict)

    # --- settings (sane default, or asked on first run) ---
    default_format: str | None = None       # None = never chosen -> ask once
    promote_target: str = DEFAULT_PROMOTE_TARGET
    scm_flavour: str = DEFAULT_SCM_FLAVOUR  # picks a COMMIT_URL_PATTERNS entry
    scm_base_url: str | None = None         # override when repo.link is unusable
    claude_prompted: bool = False

    # sticky session context (see `drone-cli context`)
    context: dict = field(default_factory=dict)
    contexts: dict = field(default_factory=dict)

    # ---- persistence -------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            profiles = {
                name: Profile(
                    name=name,
                    base_url=p["base_url"],
                    username=p.get("username"),
                    verify_ssl=p.get("verify_ssl", True),
                )
                for name, p in raw.get("profiles", {}).items()
            }
            return cls(
                current_profile=raw.get("current_profile", DEFAULT_PROFILE),
                profiles=profiles,
                default_format=raw.get("default_format"),
                promote_target=raw.get("promote_target") or DEFAULT_PROMOTE_TARGET,
                scm_flavour=raw.get("scm_flavour") or DEFAULT_SCM_FLAVOUR,
                scm_base_url=raw.get("scm_base_url"),
                claude_prompted=bool(raw.get("claude_prompted", False)),
                context=raw.get("context") or {},
                contexts=raw.get("contexts") or {},
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ConfigError(f"malformed config at {path}: {exc}") from exc

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "current_profile": self.current_profile,
            "default_format": self.default_format,
            "promote_target": self.promote_target,
            "scm_flavour": self.scm_flavour,
            "scm_base_url": self.scm_base_url,
            "claude_prompted": self.claude_prompted,
            "context": self.context,
            "contexts": self.contexts,
            "profiles": {
                name: {
                    "base_url": p.base_url,
                    "username": p.username,
                    "verify_ssl": p.verify_ssl,
                }
                for name, p in self.profiles.items()
            },
        }
        path.write_text(json.dumps(data, indent=2) + "\n")

    # ---- resolution --------------------------------------------------
    def active_profile_name(self) -> str:
        return SPEC.getenv("PROFILE") or self.current_profile

    def _env_server(self) -> str | None:
        """DRONECLI_SERVER wins over the ecosystem's DRONE_SERVER."""
        return SPEC.getenv("SERVER") or os.environ.get("DRONE_SERVER")

    def resolve(self) -> Profile:
        """The effective profile, applying env overrides.

        A profile can be synthesised entirely from the environment with no config
        file on disk, so ``DRONE_SERVER`` + ``DRONE_TOKEN`` are enough to run
        headless — which is exactly how this gets used inside CI.
        """
        name = self.active_profile_name()
        env_url = self._env_server()

        prof = self.profiles.get(name)
        if prof is None:
            if env_url:
                return Profile(name=name, base_url=env_url)
            raise ConfigError(
                f"no profile '{name}' configured. Run `drone-cli auth login` "
                f"or set DRONE_SERVER + DRONE_TOKEN."
            )
        if env_url:
            return Profile(
                name=prof.name,
                base_url=env_url,
                username=prof.username,
                verify_ssl=prof.verify_ssl,
            )
        return prof

    def upsert_profile(self, prof: Profile, make_current: bool = True) -> None:
        self.profiles[prof.name] = prof
        if make_current:
            self.current_profile = prof.name

    # ---- derived settings --------------------------------------------
    def commit_url(self, repo_link: str, sha: str) -> str | None:
        """Build a web URL for *sha*, or None if we have nothing to build it from.

        `build.link` is NOT usable for this (verified live): for a push it is a
        **compare** URL (`/compare/before...after`) and for an API-triggered
        build it is an **API** URL (`/api/v1/.../git/commits/{sha}`) that renders
        as JSON. Drone passes through whatever the SCM handed it and never
        normalises. So we derive from the repo's web link instead.
        """
        base = (self.scm_base_url or repo_link or "").rstrip("/")
        if not base or not sha:
            return None
        pattern = COMMIT_URL_PATTERNS.get(self.scm_flavour, COMMIT_URL_PATTERNS[DEFAULT_SCM_FLAVOUR])
        return pattern.format(repo=base, sha=sha)
