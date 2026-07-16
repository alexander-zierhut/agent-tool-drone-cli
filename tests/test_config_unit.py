"""Config, settings and commit-link derivation — hermetic."""

from __future__ import annotations

import pytest

from dronecli.config import COMMIT_URL_PATTERNS, Config, Profile
from dronecli.spec import SPEC, token_url

SHA = "42f7a46ed69c9cdd53a6b44fe98c0d55986b19a5"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("DRONECLI_CONFIG_DIR", str(tmp_path))
    for v in ("DRONE_SERVER", "DRONECLI_SERVER", "DRONE_TOKEN", "DRONECLI_TOKEN", "DRONECLI_PROFILE"):
        monkeypatch.delenv(v, raising=False)


# ---- the token URL (requirement: show the user where to get a token) ----

def test_token_url_is_derived_from_the_server():
    assert token_url("https://drone.zierhut-it.de") == "https://drone.zierhut-it.de/account"
    assert token_url("https://drone.zierhut-it.de/") == "https://drone.zierhut-it.de/account"


# ---- profiles / env ----------------------------------------------------

def test_api_root_is_unversioned():
    """Drone's API is /api -- there is no /api/v3 equivalent."""
    assert Profile(name="d", base_url="https://d.example.com/").api_root() == "https://d.example.com/api"


def test_env_server_synthesises_a_profile(monkeypatch):
    """DRONE_SERVER + DRONE_TOKEN must be enough with no config file at all --
    that is exactly how this runs inside CI."""
    monkeypatch.setenv("DRONE_SERVER", "https://drone.example.com")
    prof = Config().resolve()
    assert prof.base_url == "https://drone.example.com"


def test_our_server_var_beats_the_ecosystem_one(monkeypatch):
    monkeypatch.setenv("DRONE_SERVER", "https://ecosystem.example.com")
    monkeypatch.setenv("DRONECLI_SERVER", "https://ours.example.com")
    assert Config().resolve().base_url == "https://ours.example.com"


def test_env_overrides_a_saved_profile(monkeypatch):
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://saved.example.com"))
    monkeypatch.setenv("DRONE_SERVER", "https://env.example.com")
    assert cfg.resolve().base_url == "https://env.example.com"


def test_no_profile_and_no_env_is_a_config_error():
    from agentcli.errors import ConfigError

    with pytest.raises(ConfigError) as e:
        Config().resolve()
    assert "auth login" in str(e.value), "the error must say how to fix it"


def test_roundtrip_persists_settings():
    cfg = Config()
    cfg.upsert_profile(Profile(name="default", base_url="https://d.example.com"))
    cfg.promote_target = "staging"
    cfg.scm_flavour = "gitlab"
    cfg.default_format = "table"
    cfg.save()

    again = Config.load()
    assert again.promote_target == "staging"
    assert again.scm_flavour == "gitlab"
    assert again.default_format == "table"
    assert again.profiles["default"].base_url == "https://d.example.com"


def test_defaults_are_sane_with_no_config_file():
    """Every setting must have a default; nothing may be half-configured."""
    cfg = Config()
    assert cfg.promote_target == "prod", "promote must default to prod"
    assert cfg.scm_flavour in COMMIT_URL_PATTERNS
    assert cfg.default_format is None, "None = never chosen -> ask once, then json"


def test_malformed_config_is_a_clean_error():
    from agentcli.errors import ConfigError

    SPEC.config_file().parent.mkdir(parents=True, exist_ok=True)
    SPEC.config_file().write_text("{not json")
    with pytest.raises(ConfigError):
        Config.load()


# ---- commit links ------------------------------------------------------

def test_commit_url_default_is_the_gitea_forgejo_shape():
    """Verified live against Gitea/Forgejo: {repo}/commit/{sha} -> 200."""
    cfg = Config()
    assert cfg.commit_url("http://git.example.com/acme/api", SHA) == (
        f"http://git.example.com/acme/api/commit/{SHA}"
    )


def test_commit_url_respects_the_scm_flavour():
    cfg = Config()
    cfg.scm_flavour = "gitlab"
    assert cfg.commit_url("https://gl.example.com/a/b", SHA) == f"https://gl.example.com/a/b/-/commit/{SHA}"
    cfg.scm_flavour = "bitbucket"
    assert cfg.commit_url("https://bb.example.com/a/b", SHA) == f"https://bb.example.com/a/b/commits/{SHA}"


def test_scm_base_url_overrides_the_repo_link():
    """For when repo.link points at an internal hostname you can't reach."""
    cfg = Config()
    cfg.scm_base_url = "https://public.example.com/acme/api"
    assert cfg.commit_url("http://internal:3000/acme/api", SHA) == (
        f"https://public.example.com/acme/api/commit/{SHA}"
    )


def test_commit_url_is_none_when_there_is_nothing_to_build_from():
    """repo.link can be empty; returning a broken URL would be worse than none."""
    cfg = Config()
    assert cfg.commit_url("", SHA) is None
    assert cfg.commit_url("https://git.example.com/a/b", "") is None


def test_unknown_flavour_falls_back_rather_than_crashing():
    cfg = Config()
    cfg.scm_flavour = "svn-from-1999"
    assert cfg.commit_url("https://g.example.com/a/b", SHA) == f"https://g.example.com/a/b/commit/{SHA}"
