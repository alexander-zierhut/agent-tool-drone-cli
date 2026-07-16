"""`context`, `raw` and `install claude` — pure unit tests, no server, no network.

The centrepiece is the KNOWN_KEYS ↔ command-tree contract at the top. Sticky
context is wired through Click's ``default_map``, which matches a context key to
an option by **name** and says nothing when it fails to match. So a key nobody
consumes is not a warning, a log line, or a 400 — it is a feature that quietly
does nothing forever. OpenProject shipped exactly that: its ``KNOWN_KEYS`` is
defined once and imported nowhere, so nothing ever checked it.

These tests are the check.
"""

from __future__ import annotations

import io
import json
import re
import sys

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from dronecli.appctx import AppContext
from dronecli.cli import _context_default_map, app
from dronecli.commands import context as context_cmd
from dronecli.commands import install, raw
from dronecli.commands.context import KNOWN_KEYS
from dronecli.config import Config

runner = CliRunner()

# The groups cli.py refuses to feed from the context. `context` itself is the big
# one: `context set` declares --repo/--owner/--branch, so counting it as a
# consumer would make the contract test below pass vacuously — every key would
# match the very command that sets it.
SKIP = {"context", "settings", "guide", "install"}


def _walk(cmd, path: str = ""):
    """Yield (command_path, command) for every leaf in the tree.

    Duck-typed on purpose: Typer vendors Click privately (`typer._click`) and has
    moved it before, so `isinstance(x, click.Group)` is a liability. cli.py's own
    `_context_default_map` walks the tree the same way.
    """
    subs = getattr(cmd, "commands", None)
    if subs:
        for name, sub in subs.items():
            yield from _walk(sub, f"{path} {name}".strip())
    else:
        yield path, cmd


def _leaves():
    return list(_walk(get_command(app)))


def _consumers(key: str) -> list[str]:
    """Commands that would actually receive *key* from the sticky context.

    Mirrors `_context_default_map` exactly, including the `param_type_name ==
    "option"` filter — that filter is load-bearing safety (Click's default_map
    will happily satisfy a REQUIRED POSITIONAL, which would turn a bare
    `repo rm` into silent destruction), and its side effect is that a key
    matching only a positional silently does nothing. Both halves matter, so the
    test must use the same rule the runtime does, not a looser one.
    """
    out = []
    for path, cmd in _leaves():
        if not path or path.split()[0] in SKIP:
            continue
        for p in cmd.params:
            if getattr(p, "param_type_name", "") == "option" and p.name == key:
                out.append(path)
                break
    return out


# ---- the contract: every context key must have a consumer -------------


def test_the_tree_actually_has_commands():
    # Guard the guard: if introspection breaks, every test below passes
    # vacuously and we learn nothing.
    leaves = _leaves()
    assert len(leaves) > 15, f"expected the full command tree, walked only {len(leaves)}"


@pytest.mark.parametrize("key", KNOWN_KEYS)
def test_every_known_key_has_a_consumer(key):
    """A context key with no matching option is a silent no-op, not an error.

    This is the whole reason KNOWN_KEYS is imported here rather than admired in
    place: nothing else in the system will ever tell you the key is dead. Note
    the match is on the parameter's **name**, not its flag — `orgsecret ls`
    declares `owner: str = typer.Option(None, "--org")`, so `context set --owner`
    scopes it even though the flag reads `--org`. Renaming that *parameter* (not
    the flag) is enough to silently unhook the key, which is precisely the kind
    of change nobody thinks to test.
    """
    assert _consumers(key), (
        f"context key '{key}' matches no option NAMED '{key}' on any command outside "
        f"{sorted(SKIP)}. Click's default_map matches on the parameter name, so this key "
        f"is stored, echoed by `context show`, and then ignored by every command — no "
        f"error, ever. Fix one of three ways: rename the consuming option's parameter to "
        f"'{key}', register the group that owns it in cli.py, or drop the key.\n"
        f"(Only params with param_type_name == 'option' count: a key matching a positional "
        f"is also a no-op — deliberately, since default_map WOULD fill a required positional "
        f"and turn a bare `repo rm` into silent destruction.)"
    )


def test_context_set_declares_every_known_key():
    """`context set`'s signature is driven from KNOWN_KEYS — keep them in lockstep.

    Without this, adding a key to the list gives you a key you cannot set, and
    adding an option to `set` gives you a flag `--set` would reject.
    """
    cmd = dict(_walk(get_command(app)))
    params = {p.name for p in cmd["context set"].params if getattr(p, "param_type_name", "") == "option"}
    missing = [k for k in KNOWN_KEYS if k not in params]
    assert not missing, f"KNOWN_KEYS entries with no --flag on `context set`: {missing}"


def test_default_map_really_injects_a_sticky_repo():
    """End-to-end on the mechanism itself, with no server: the key name we store
    is the name Click looks up."""
    dmap = _context_default_map(get_command(app), {"repo": "octocat/hello"}, skip=SKIP)
    assert dmap["build"]["ls"]["repo"] == "octocat/hello"
    assert dmap["wait"]["repo"] == "octocat/hello"
    assert "context" not in dmap, "the group that writes the context must never be fed by it"


def test_default_map_never_fills_a_positional():
    """The safety refusal, pinned. `build info NUMBER` takes the build as a
    positional; a context key named `number` must not satisfy it, or a bare
    command silently acts on a stale target."""
    dmap = _context_default_map(get_command(app), {"number": 999}, skip=SKIP)
    assert dmap == {}, f"a positional was fed from the context: {dmap}"


# ---- context CRUD -----------------------------------------------------


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Relocate the whole config dir — hermetic, never touches the real one."""
    monkeypatch.setenv("DRONECLI_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("DRONECLI_NO_CONTEXT", raising=False)
    monkeypatch.setenv("DRONECLI_CLI_FORMAT", "json")
    return tmp_path


@pytest.fixture
def ctx_app():
    a = typer.Typer()
    a.add_typer(context_cmd.app, name="context")
    return a


def _json_out(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_set_and_show_round_trip(cfg_home, ctx_app):
    _json_out(runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello"]))
    out = _json_out(runner.invoke(ctx_app, ["context", "show"]))
    assert out["context"] == {"repo": {"value": "octocat/hello", "from": "saved"}}
    assert out["applies"] is True
    assert out["knownKeys"] == KNOWN_KEYS


def test_show_reports_where_every_value_came_from(cfg_home, ctx_app):
    """AGENTS.md — the shipped machine contract — promises `context show` gives
    "each value + where it came from", and `show`'s own job is answering "why is
    this scoped wrong?". A bare map answers the first half and calls it done.

    "saved" is the only rung that exists today; the point is the SHAPE. An agent
    that learns to read `.context.repo.value` keeps working the day an env
    override or a repo-local file becomes a second source — and the contract
    stops being a lie in the meantime.
    """
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello", "--branch", "main"])
    out = _json_out(runner.invoke(ctx_app, ["context", "show"]))
    assert out["context"] == {
        "repo": {"value": "octocat/hello", "from": "saved"},
        "branch": {"value": "main", "from": "saved"},
    }
    assert all(set(v) == {"value", "from"} for v in out["context"].values())


def test_show_of_an_empty_context_is_an_empty_map(cfg_home, ctx_app):
    out = _json_out(runner.invoke(ctx_app, ["context", "show"]))
    assert out["context"] == {}
    assert out["applies"] is False, "nothing saved is nothing applied"


def test_show_reports_that_no_context_suspends_it(cfg_home, ctx_app, monkeypatch):
    """`--no-context` is popped into an env var before Click runs. `show` must
    report the truth about the CURRENT invocation, not just what's on disk —
    this command exists to answer 'why is my scoping wrong?'.

    Note `applies` is the ONLY thing that says so: the values are still `from:
    saved`, because that is where they came from. Provenance is not the same
    question as whether they are in force, and collapsing the two would make
    `--no-context` invisible.
    """
    _json_out(runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello"]))
    monkeypatch.setenv("DRONECLI_NO_CONTEXT", "1")
    out = _json_out(runner.invoke(ctx_app, ["context", "show"]))
    assert out["context"] == {"repo": {"value": "octocat/hello", "from": "saved"}}, "still saved..."
    assert out["applies"] is False, "...but not in force right now"


def test_set_merges_rather_than_replaces(cfg_home, ctx_app):
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello"])
    out = _json_out(runner.invoke(ctx_app, ["context", "set", "--branch", "main"]))
    assert out["context"] == {"repo": "octocat/hello", "branch": "main"}


def test_set_rejects_an_unknown_key(cfg_home, ctx_app):
    """The no-op key, refused at the door. `--set project=x` would otherwise
    save, show up in `context show`, and scope precisely nothing."""
    res = runner.invoke(ctx_app, ["context", "set", "--set", "project=webshop"])
    assert res.exit_code != 0
    assert "unknown context key" in str(res.exception or res.output).lower()


def test_set_rejects_a_repo_that_is_not_a_slug(cfg_home, ctx_app):
    """Validate here, not in Click's converter: a bad context value otherwise
    surfaces on some unrelated command, blaming a flag the caller never typed."""
    res = runner.invoke(ctx_app, ["context", "set", "--repo", "hello"])
    assert res.exit_code != 0
    assert "owner/name" in str(res.exception or res.output)


def test_set_rejects_an_owner_with_a_slash(cfg_home, ctx_app):
    res = runner.invoke(ctx_app, ["context", "set", "--owner", "octocat/hello"])
    assert res.exit_code != 0


def test_set_with_nothing_is_an_error_not_a_no_op(cfg_home, ctx_app):
    res = runner.invoke(ctx_app, ["context", "set"])
    assert res.exit_code != 0
    assert "nothing to set" in str(res.exception or res.output)


def test_unset_and_clear(cfg_home, ctx_app):
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello", "--branch", "main"])
    out = _json_out(runner.invoke(ctx_app, ["context", "unset", "branch"]))
    assert out["context"] == {"repo": "octocat/hello"}, "the writers echo the raw map they wrote"
    _json_out(runner.invoke(ctx_app, ["context", "clear"]))
    assert _json_out(runner.invoke(ctx_app, ["context", "show"]))["context"] == {}


def test_unset_an_absent_key_is_not_an_error(cfg_home, ctx_app):
    """Idempotent by design: an agent retrying a cleanup must not fail on the
    second run."""
    out = _json_out(runner.invoke(ctx_app, ["context", "unset", "branch"]))
    assert out["status"] == "unset"


def test_save_use_list_rm(cfg_home, ctx_app):
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello"])
    _json_out(runner.invoke(ctx_app, ["context", "save", "proj-a"]))
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/other"])
    _json_out(runner.invoke(ctx_app, ["context", "save", "proj-b"]))

    assert [r["name"] for r in _json_out(runner.invoke(ctx_app, ["context", "list"]))] == ["proj-a", "proj-b"]

    out = _json_out(runner.invoke(ctx_app, ["context", "use", "proj-a"]))
    assert out["context"] == {"repo": "octocat/hello"}

    _json_out(runner.invoke(ctx_app, ["context", "rm", "proj-b"]))
    assert [r["name"] for r in _json_out(runner.invoke(ctx_app, ["context", "list"]))] == ["proj-a"]


def test_use_is_a_copy_not_an_alias(cfg_home, ctx_app):
    """Editing the active context must not silently rewrite the saved one — a
    saved context is a checkpoint you can return to."""
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello"])
    runner.invoke(ctx_app, ["context", "save", "proj"])
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/other"])
    out = _json_out(runner.invoke(ctx_app, ["context", "use", "proj"]))
    assert out["context"] == {"repo": "octocat/hello"}


def test_use_an_unknown_name_lists_what_exists(cfg_home, ctx_app):
    runner.invoke(ctx_app, ["context", "set", "--repo", "octocat/hello"])
    runner.invoke(ctx_app, ["context", "save", "proj"])
    res = runner.invoke(ctx_app, ["context", "use", "nope"])
    assert res.exit_code != 0
    assert "proj" in str(res.exception or res.output), "an error about names must name the names"


def test_save_an_empty_context_is_refused(cfg_home, ctx_app):
    res = runner.invoke(ctx_app, ["context", "save", "empty"])
    assert res.exit_code != 0


# ---- raw --------------------------------------------------------------


class FakeClient:
    """Records calls. Small on purpose — a MockTransport here would be testing
    httpx's URL joining, not this module's decisions."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"ok": True}

    def request(self, method, path, *, params=None, json=None, **kw):
        self.calls.append({"method": method, "path": path, "params": params, "json": json})
        return self.result


class FakeEmitter:
    def __init__(self):
        self.emitted = []

    def emit(self, data, **kw):
        self.emitted.append(data)

    def message(self, text):
        pass


class FakeObj:
    def __init__(self, client):
        self._client = client
        self.emitter = FakeEmitter()

    def client(self):
        return self._client


class FakeCtx:
    """Stands in for typer.Context; `ctx_obj` reads `.obj` and nothing else."""

    def __init__(self, obj):
        self.obj = obj


@pytest.mark.parametrize(
    "given,expected",
    [
        ("user", "user"),
        ("/user", "user"),
        ("api/user", "user"),
        ("/api/user", "user"),
        ("  /api/repos/o/n/builds  ", "repos/o/n/builds"),
        ("api", ""),
    ],
)
def test_normalize_strips_a_pasted_api_prefix(given, expected):
    """`client._url` joins onto <server>/api unconditionally, so a path copied
    from a doc page would become /api/api/user — a 404 that reads like the
    endpoint doesn't exist."""
    assert raw._normalize(given) == expected


def test_normalize_keeps_a_real_api_path_that_starts_with_apiary_like_names():
    """Only the `api` segment goes — `apikeys` must survive intact."""
    assert raw._normalize("apikeys/1") == "apikeys/1"


def test_normalize_leaves_an_absolute_url_alone():
    assert raw._normalize("http://drone.example.com/version") == "http://drone.example.com/version"


def test_raw_get_passes_params_and_emits_the_payload():
    client = FakeClient(result=[{"number": 1}])
    obj = FakeObj(client)
    raw.get(FakeCtx(obj), "/api/repos/o/n/builds", ["page=2", "per_page=5"])
    assert client.calls == [
        {"method": "GET", "path": "repos/o/n/builds", "params": {"page": "2", "per_page": "5"}, "json": None}
    ]
    assert obj.emitter.emitted == [[{"number": 1}]]


def test_raw_post_sends_a_json_body():
    client = FakeClient()
    obj = FakeObj(client)
    raw.post(FakeCtx(obj), "repos/o/n/secrets", '{"name": "token", "data": "x"}', None, None)
    assert client.calls[0]["json"] == {"name": "token", "data": "x"}


def test_raw_post_reads_a_body_from_a_file(tmp_path):
    f = tmp_path / "body.json"
    f.write_text('{"name": "from-file"}')
    client = FakeClient()
    raw.post(FakeCtx(FakeObj(client)), "repos/o/n/secrets", None, f, None)
    assert client.calls[0]["json"] == {"name": "from-file"}


def test_raw_data_file_wins_over_data(tmp_path):
    """One body, one winner. Reading both and merging would invent a request the
    caller never described."""
    f = tmp_path / "body.json"
    f.write_text('{"from": "file"}')
    client = FakeClient()
    raw.post(FakeCtx(FakeObj(client)), "x", '{"from": "flag"}', f, None)
    assert client.calls[0]["json"] == {"from": "file"}


def test_raw_bad_json_is_a_clean_error_not_a_traceback():
    from agentcli.errors import OpError

    with pytest.raises(OpError) as exc:
        raw.post(FakeCtx(FakeObj(FakeClient())), "x", "{not json", None, None)
    assert "invalid JSON for --data" in str(exc.value)


def test_raw_bad_param_names_the_offender():
    from agentcli.errors import OpError

    with pytest.raises(OpError) as exc:
        raw.get(FakeCtx(FakeObj(FakeClient())), "user", ["oops"])
    assert "key=value" in str(exc.value)


def test_raw_param_value_may_contain_an_equals_sign():
    """`--param filter=a=b` splits once. Splitting greedily would silently
    truncate the value."""
    client = FakeClient()
    raw.get(FakeCtx(FakeObj(client)), "user", ["expr=a=b"])
    assert client.calls[0]["params"] == {"expr": "a=b"}


def test_raw_delete_sends_no_body():
    client = FakeClient()
    raw.delete(FakeCtx(FakeObj(client)), "repos/o/n/builds/1", None)
    assert client.calls[0] == {
        "method": "DELETE", "path": "repos/o/n/builds/1", "params": {}, "json": None,
    }


def test_raw_get_help_warns_about_the_web_root_endpoints():
    """The three endpoints people reach for first are the three `raw` cannot
    serve. If the help doesn't say so, `raw get version` looks like a broken
    server rather than a wrong root."""
    doc = raw.get.__doc__ or ""
    for token in ("/version", "/healthz", "/varz", "server version"):
        assert token in doc, f"raw get help must mention {token}"


def test_raw_declares_no_reserved_global():
    """-o/-f/--output/--fields/--dry-run are stripped from argv before Click sees
    them; a command declaring one could never receive it."""
    reserved = {"--format", "-f", "--output", "-o", "--fields", "--columns", "--dry-run", "--stream", "--no-context"}
    for path, cmd in _walk(get_command(raw.app), "raw"):
        for p in cmd.params:
            assert not (set(getattr(p, "opts", [])) & reserved), f"{path} declares a reserved global"


# ---- install claude ---------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: home))
    return home


def _frontmatter(text: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must open with a YAML frontmatter block"
    return m.group(1)


def _description(text: str) -> str:
    fm = _frontmatter(text)
    m = re.search(r"description: >-\n(.*)", fm, re.S)
    assert m, "frontmatter must carry a description"
    return " ".join(line.strip() for line in m.group(1).splitlines())


def test_skill_frontmatter_name_matches_the_directory():
    """Claude matches the skill by directory + frontmatter; a mismatch is a skill
    that never loads."""
    assert re.search(rf"^name: {install.SKILL_NAME}$", _frontmatter(install.SKILL_MD), re.M)


def test_skill_embeds_the_real_version():
    from dronecli import __version__

    assert f"v{__version__}" in install.SKILL_MD


def test_every_build_trigger_is_anchored_to_the_product_noun():
    """The description is the ENTIRE matching surface.

    A bare "build" fires this skill on "my webpack build is broken" and every
    other unrelated build question. Over-firing is how a skill earns distrust and
    stops being loaded at all, so every trigger must carry the product noun.
    """
    desc = _description(install.SKILL_MD)
    unanchored = [
        m.group(0)
        for m in re.finditer(r"(\w+)\s+(builds?)\b", desc)
        if m.group(1).lower() not in {"drone"}
    ]
    assert not unanchored, f"unanchored build triggers in the description: {unanchored}"


def test_description_names_drone_and_the_command():
    desc = _description(install.SKILL_MD)
    assert "Drone CI" in desc
    assert "drone-cli" in desc


def test_skill_body_states_the_contract_and_leads_with_commit_addressing():
    body = install.SKILL_MD
    assert "drone-cli guide" in body, "the skill must point at the tool's own manual"
    assert "drone-cli wait --commit HEAD" in body
    assert "logs are raw text" in body, "the output contract's carve-out, or agents json.loads a log"
    # The exit-code table is a published contract; the skill is one of its homes.
    for code in ("`0` ok", "`4` auth", "`5` not found", "`10`"):
        assert code in body, f"exit code line missing: {code}"


def test_install_writes_the_skill_and_reports_the_path(fake_home):
    path = install.write_skill()
    assert path == fake_home / ".claude" / "skills" / "drone-ci" / "SKILL.md"
    assert path.read_text() == install.SKILL_MD
    assert install.skill_installed()


def test_uninstall_removes_only_our_marked_memory_block(fake_home):
    mem = fake_home / ".claude" / "CLAUDE.md"
    mem.parent.mkdir(parents=True)
    mem.write_text("# My notes\n\nKeep me.\n")

    install.write_memory_hint()
    text = mem.read_text()
    assert "Keep me." in text and install._MEM_START in text

    assert install._remove_memory_hint() is True
    after = mem.read_text()
    assert "Keep me." in after, "the user's own memory file must survive intact"
    assert install._MEM_START not in after and install._MEM_END not in after


def test_memory_hint_is_idempotent(fake_home):
    install.write_memory_hint()
    install.write_memory_hint()
    assert (fake_home / ".claude" / "CLAUDE.md").read_text().count(install._MEM_START) == 1


def test_remove_memory_hint_when_there_is_nothing_to_remove(fake_home):
    assert install._remove_memory_hint() is False
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "CLAUDE.md").write_text("just the user's notes\n")
    assert install._remove_memory_hint() is False


def test_claude_available_detects_the_dot_claude_dir(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _: None)
    assert install.claude_available() is False
    (fake_home / ".claude").mkdir()
    assert install.claude_available() is True


def test_install_refuses_without_claude_unless_forced(fake_home, monkeypatch):
    from agentcli.errors import OpError

    monkeypatch.setattr(install, "claude_available", lambda: False)
    obj = FakeObj(FakeClient())
    with pytest.raises(OpError) as exc:
        install.claude(FakeCtx(obj), False, False, False, False, False)
    assert "--force" in str(exc.value)

    install.claude(FakeCtx(obj), False, False, True, False, False)
    assert obj.emitter.emitted[-1]["status"] == "installed"
    assert install.skill_installed()


def test_uninstall_reports_what_it_removed(fake_home, monkeypatch):
    monkeypatch.setattr(install, "claude_available", lambda: True)
    obj = FakeObj(FakeClient())
    install.claude(FakeCtx(obj), False, True, False, False, False)
    assert install.skill_installed()

    install.claude(FakeCtx(obj), False, False, False, True, False)
    out = obj.emitter.emitted[-1]
    assert out["status"] == "uninstalled"
    assert len(out["removed"]) == 2, out
    assert not install.skill_installed()


def test_project_scope_writes_into_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = install.write_skill(project=True)
    assert path == tmp_path / ".claude" / "skills" / "drone-ci" / "SKILL.md"


# ---- the first-run offer (appctx) -------------------------------------
#
# `install claude` is what makes Claude reach for this CLI at all, and it was
# discoverable only by someone who already knew it existed. The offer is the fix
# — but an offer that fires twice is worse than one that never fires, so most of
# what follows is about it firing exactly once.


@pytest.fixture
def offer(cfg_home, fake_home, monkeypatch):
    """A first run with Claude present, a TTY, and a scripted answer.

    `cfg_home` relocates the config dir and pins DRONECLI_CLI_FORMAT=json, which
    also keeps the *format* prompt from firing and eating our stdin.
    """

    def run(answer: str = "y", *, interactive: bool = True, available: bool = True):
        monkeypatch.setattr(install, "claude_available", lambda: available)
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
        AppContext(interactive=interactive)
        return err.getvalue()

    return run


def test_the_offer_installs_the_skill_when_accepted(offer):
    err = offer("y\n")
    assert install.skill_installed()
    assert "Drone CI" in err
    assert "install claude --uninstall" in err, "an offer must say how to undo itself"
    assert Config.load().claude_prompted is True


def test_the_offer_never_fires_twice(offer, fake_home):
    """The flag exists for exactly this. A CLI that re-asks on every command gets
    `2>/dev/null`'d — and then its real errors are invisible too, which is a much
    worse outcome than a skill nobody installed."""
    offer("y\n")
    (fake_home / ".claude" / "skills" / "drone-ci" / "SKILL.md").unlink()

    err = offer("y\n")
    assert err == "", "asked once means asked forever, even with the skill gone"
    assert not install.skill_installed(), "and it must not silently reinstall"


def test_declining_still_counts_as_asked(offer):
    """The whole point of saving BEFORE the prompt. 'No' is an answer."""
    err = offer("n\n")
    assert not install.skill_installed()
    assert Config.load().claude_prompted is True
    assert "drone-cli install claude" in err, "declining must not be a dead end"
    assert offer("y\n") == "", "and it is never asked again"


def test_a_bare_enter_is_not_consent(offer):
    """[y/N]: the default must not write files into the user's ~/.claude."""
    offer("\n")
    assert not install.skill_installed()
    assert Config.load().claude_prompted is True


def test_the_offer_prompts_on_stderr_only(offer, capsys):
    """stdout is the machine channel — a question in it is a parse error for
    whatever is reading the JSON."""
    monkeypatched_err = offer("y\n")
    assert "Install it?" in monkeypatched_err
    assert capsys.readouterr().out == "", "not one byte of the prompt on stdout"


def test_no_offer_without_a_tty(offer):
    """Same gate as the format prompt. A prompt in CI hangs a build until timeout."""
    assert offer("y\n", interactive=False) == ""
    assert not install.skill_installed()
    assert Config.load().claude_prompted is False, "never asked -> ask later, on a real terminal"


def test_no_offer_when_claude_is_not_installed(offer):
    """And crucially: does NOT burn the one prompt. Someone who installs Claude
    next month should still get offered the skill."""
    assert offer("y\n", available=False) == ""
    assert Config.load().claude_prompted is False


def test_no_offer_when_the_skill_is_already_installed(offer, fake_home):
    install.write_skill()
    assert offer("y\n") == ""


def test_the_offer_never_breaks_a_real_command(offer, monkeypatch):
    """It runs before EVERY command. A first-run nicety that can fail one is a
    liability, so the swallow is load-bearing, not lazy."""
    def boom():
        raise RuntimeError("keyring on fire")

    monkeypatch.setattr(install, "skill_installed", boom)
    assert offer("y\n") == ""  # constructed fine, said nothing
