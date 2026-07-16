"""The guide and the Claude skill must only promise commands that EXIST.

Docs that describe a CLI drift from it silently: nothing executes prose, so a
command invented in a cheat-sheet costs nothing at write time and everything at
read time. Two live defects motivated this file, both found by hand:

  * `drone-cli guide` advertised `drone-cli fields` under DISCOVER. No such
    command was ever implemented — the discovery trio was planned, documented,
    and never built.
  * The installed SKILL.md pointed Claude at `drone-cli guide <topic>` for
    ".../gotchas/...". There was no `gotchas` topic, so it exited 2 — and it is
    the most inviting name on the list, so an agent reaches for it FIRST and the
    only broken one is the one it tries.

Both are the same bug: text making a promise the tree does not keep. So these
tests resolve every `drone-cli ...` string in the guide and the skill against the
REAL Typer tree, and cross-check the topic list against the topics that exist.

Hermetic: introspection only. No network, no client, no config.
"""

from __future__ import annotations

import re

import pytest
import typer
from typer.main import get_command

from dronecli.cli import app
from dronecli.commands import guide as G
from dronecli.commands import install as I

# ---------------------------------------------------------------------------
# the real tree
# ---------------------------------------------------------------------------


def _walk(cmd, path: str = ""):
    """Yield the path of every leaf command.

    Duck-typed on `commands` rather than isinstance(click.Group): Typer vendors
    Click privately (`typer._click`) and has moved it before.
    """
    subs = getattr(cmd, "commands", None)
    if subs:
        for name, sub in subs.items():
            yield from _walk(sub, f"{path} {name}".strip())
    elif path:
        yield path


COMMANDS = frozenset(_walk(get_command(app)))
# Top-level leaves (`wait`, `promote`, `guide`) take positional args, so
# "drone-cli wait --commit HEAD" must resolve on its first word alone.
TOP_LEVEL = frozenset(p for p in COMMANDS if " " not in p)

# Prose that is not a command name and never can be. Kept SHORT and explicit —
# every entry is a hole in the check, so each one has to earn its place.
ALLOWED_NON_COMMANDS = frozenset({
    "raw",  # documented as `raw get|post|patch|delete <path>`; the pipe defeats the regex
})

# Matches `drone-cli foo` / `drone-cli foo bar`, and deliberately NOT:
#   `drone-cli --help`, `drone-cli <group> ...`  -> the (?![-<]) lookahead
#   `drone-cli v0.4.0` (the skill's title line)  -> the trailing (?![\w.-])
_CMD_RX = re.compile(r"drone-cli ((?![-<])[a-z][a-z-]*(?: [a-z][a-z-]*)?)(?![\w.-])")


def _cited_commands(text: str) -> set[str]:
    return set(_CMD_RX.findall(text))


def _resolves(cited: str) -> bool:
    """Is this citation a real command?

    `build ls` must match exactly — a two-word citation under a GROUP is only
    valid if the pair exists, which is what catches `drone-cli build bogus`. A
    top-level leaf (`wait`, `promote`) takes arguments, so its first word is
    enough.
    """
    return (
        cited in COMMANDS
        or cited.split()[0] in TOP_LEVEL
        or cited in ALLOWED_NON_COMMANDS
        or cited.split()[0] in ALLOWED_NON_COMMANDS
    )


# Every doc string that makes promises, named for a legible failure message.
DOC_SOURCES: dict[str, str] = {
    "guide.OVERVIEW": G.OVERVIEW,
    "install.SKILL_MD": I.SKILL_MD,
    **{f"guide.TOPICS[{k!r}]": v for k, v in G.TOPICS.items()},
}


# ---------------------------------------------------------------------------
# guard the guard
# ---------------------------------------------------------------------------


def test_the_tree_actually_has_commands():
    """If introspection breaks, every assertion below passes vacuously."""
    assert len(COMMANDS) > 15, f"expected the full command tree, walked only {len(COMMANDS)}"
    assert {"build ls", "wait", "guide"} <= COMMANDS


def test_the_regex_actually_finds_citations():
    """A regex that matches nothing would make this file a no-op."""
    assert len(_cited_commands(G.OVERVIEW)) > 5
    assert len(_cited_commands(I.SKILL_MD)) > 5


def test_the_regex_catches_an_invented_command():
    """The exact defect: `drone-cli fields` was documented, never implemented."""
    assert not _resolves("fields")
    assert "fields" in _cited_commands("DISCOVER\n  drone-cli --help · drone-cli fields\n")
    assert not _resolves("build bogus"), "a bogus subcommand under a real group must not pass"


def test_the_regex_ignores_prose_that_is_not_a_command():
    assert _cited_commands("run `drone-cli <group> --help` or `drone-cli --help`") == set()
    assert _cited_commands("# Drone CI CLI (agent-tool-drone-cli v0.4.0)") == set()


# ---------------------------------------------------------------------------
# the actual contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", sorted(DOC_SOURCES))
def test_every_documented_command_exists(source):
    """Parametrised per doc source (~14), not per citation (~100): a failure
    should name the file to open, and one broken doc should not paint the run
    red 30 times over."""
    bogus = sorted(c for c in _cited_commands(DOC_SOURCES[source]) if not _resolves(c))
    assert not bogus, (
        f"{source} documents commands that do not exist: {bogus}.\n"
        f"An agent reading this will run them and get exit 2. Either implement them "
        f"or delete the promise."
    )


def test_skill_md_only_names_real_guide_topics():
    """SKILL.md is what Claude reads. Every `guide <topic>` it lists must work.

    This is the `gotchas` defect, in one assertion: the skill listed
    builds/logs/repos/secrets/context/gotchas and only `gotchas` did not exist.
    A subset check, not equality — the skill advertises a sample ("…"), while
    OVERVIEW is the complete index (asserted exactly below).
    """
    listed = _skill_topics()
    assert listed, "found no topics named in SKILL_MD — this test would pass vacuously"
    unknown = sorted(listed - set(G.TOPICS))
    assert not unknown, (
        f"SKILL_MD sends Claude to `drone-cli guide <topic>` for {unknown}, which "
        f"exit(2). Known topics: {sorted(G.TOPICS)}"
    )


def _skill_topics() -> set[str]:
    """The topics SKILL_MD names, from its `builds/logs/repos/...` slash list."""
    line = re.search(r"`drone-cli guide <topic>` for\s*\n?\s*([a-z/\s]+?)…", I.SKILL_MD)
    assert line, "SKILL_MD no longer names guide topics in the shape this test parses"
    return {t.strip() for t in line.group(1).split("/") if t.strip()}


def test_overview_topics_line_matches_the_real_topics():
    """The TOPICS: index is the discovery surface — if it drifts, a topic either
    cannot be found or exits 2 when tried. Exact match, both directions."""
    line = re.search(r"^TOPICS:\s*(.+)$", G.OVERVIEW, re.M)
    assert line, "OVERVIEW no longer has a TOPICS: line"
    listed = {t.strip() for t in line.group(1).split("·") if t.strip()}
    assert listed == set(G.TOPICS), (
        f"OVERVIEW's TOPICS: line and guide.TOPICS disagree.\n"
        f"  listed but not implemented: {sorted(listed - set(G.TOPICS))}\n"
        f"  implemented but unlisted:   {sorted(set(G.TOPICS) - listed)}"
    )


def test_gotchas_is_a_real_topic():
    """Named explicitly because it is the one an agent reaches for first."""
    assert "gotchas" in G.TOPICS
    body = G.TOPICS["gotchas"]
    # The verified facts this topic exists to carry. If someone rewrites it, the
    # rewrite must still say these things.
    for fact in ("SECONDS-FIRST", "NUMBER", "restart", "custom", "Unauthorized", "/version"):
        assert fact in body, f"the gotchas topic no longer mentions {fact!r}"


def test_every_topic_is_reachable_and_prints(capsys):
    for name in G.TOPICS:
        G.guide(topic=name)
        out = capsys.readouterr().out
        assert out.strip(), f"topic {name!r} printed nothing"


def test_topic_lookup_is_case_and_space_insensitive():
    G.guide(topic="  GoTcHaS  ")


def test_no_topic_prints_the_overview(capsys):
    G.guide(topic=None)
    assert "OUTPUT CONTRACT" in capsys.readouterr().out


def test_unknown_topic_exits_2_and_lists_the_real_ones(capsys):
    """Exit 2 = usage error. It must also SAY what the topics are: an agent that
    guessed wrong needs the list, not just a non-zero code."""
    with pytest.raises(typer.Exit) as exc:
        G.guide(topic="webhooks")
    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "unknown topic 'webhooks'" in out
    assert "gotchas" in out


def test_guide_needs_no_config_client_or_network():
    """The guide is what you run when nothing else works — including before auth.

    It takes no `ctx`, builds no AppContext and calls no client, so it cannot
    fail with a config/auth error. Asserted structurally, on the signature.
    """
    import inspect

    params = inspect.signature(G.guide).parameters
    assert list(params) == ["topic"], "guide() grew a dependency; it must stay standalone"
