"""This tool's identity, and the shared services derived from it.

Everything tool-specific about the shared chassis (`agentcli`) is these strings.
The config directory, the keyring service and every env var follow from them.

On the env-var namespace — a deliberate, slightly awkward split:

* ``DRONE_SERVER`` / ``DRONE_TOKEN`` are the **ecosystem standard**. The official
  Go CLI reads them, so users and their CI already export them. We honour them.
* ``DRONECLI_*`` is for everything *we* invent (``DRONECLI_CONFIG_DIR``,
  ``DRONECLI_FORMAT``, …) and takes precedence for the token.

Why not just use ``DRONE_*`` for everything? Because this CLI runs **inside
Drone**, where ``DRONE_*`` is the runner's injected namespace — a build step
already has ``DRONE_COMMIT``, ``DRONE_BRANCH``, ``DRONE_REPO`` and friends set.
Inventing ``DRONE_FORMAT`` there would be squatting on someone else's namespace.
"""

from __future__ import annotations

from agentcli import AppSpec, Credentials

SPEC = AppSpec(
    name="drone-cli",
    env_prefix="DRONECLI",
    # The official CLI's variable. Honoured after DRONECLI_TOKEN.
    token_env_aliases=("DRONE_TOKEN",),
)

credentials = Credentials(SPEC)


def token_url(server: str) -> str:
    """Where a human gets their API token, given a server URL.

    Drone shows the personal token on ``<server>/account``. Deriving this from
    what the operator just typed — rather than telling them to "go find it" —
    is the difference between a 10-second login and a support question.
    """
    return server.rstrip("/") + "/account"
