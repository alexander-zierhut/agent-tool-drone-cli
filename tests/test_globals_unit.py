"""The reserved global-flag namespace, and the sticky-context key contract.

`cli.py::_pop_globals` strips a small set of flags from ANYWHERE on the command
line so they work before or after a subcommand. That power has a price: those
names are **reserved**, and any command declaring one of its own can never
receive it — the popper eats it first, silently, with no error.

This is not hypothetical. The sibling OpenProject CLI shipped exactly this bug
for four releases: `attach download --output f.pdf` had its path swallowed as an
output *format*, degraded to json, and wrote the file to the working directory
under a different name — exit 0. Its only test used the short `-O` alias, so CI
never saw it. These tests are how that class of bug stays dead here.
"""

from __future__ import annotations

from typer.main import get_command

from dronecli.cli import _BOOL_FLAGS, _FIELDS_FLAGS, _FORMAT_FLAGS, _pop_globals, app

# The reserved namespace is EXACTLY what _pop_globals strips — no more. Derived
# from the real tuples so it widens automatically if someone pops another flag.
#
# Deliberately NOT reserved: --version/-V, --profile/-p, --no-color. Those are
# ordinary root options; Click never removes them from a subcommand's argv, so a
# subcommand may legitimately shadow them. Reserving them flags working commands
# as broken (verified on the OpenProject tree: it produced four false positives).
RESERVED = set(_FORMAT_FLAGS) | set(_FIELDS_FLAGS) | set(_BOOL_FLAGS)


def _walk(cmd, path: str = ""):
    """Yield (command_path, command) for every leaf in the tree.

    Duck-typed on purpose: Typer vendors Click privately (`typer._click`) and has
    moved it before, so `isinstance(x, click.Group)` is a liability.
    """
    subs = getattr(cmd, "commands", None)
    if subs:
        for name, sub in subs.items():
            yield from _walk(sub, f"{path} {name}".strip())
    else:
        yield path, cmd


def _leaf_commands():
    return list(_walk(get_command(app)))


def test_the_tree_actually_has_commands():
    # Guard the guard: if introspection breaks, the reservation test below would
    # pass vacuously and we would learn nothing.
    leaves = _leaf_commands()
    assert len(leaves) > 15, f"expected the full command tree, walked only {len(leaves)}"


def test_no_command_declares_a_reserved_global():
    """One test, not one-per-command: it is a single rule, and parametrising it
    over ~50 commands would inflate the suite count while asserting the same
    thing. The message names every offender, so a failure is just as actionable."""
    offenders = []
    for path, cmd in _leaf_commands():
        if not path:
            continue  # the root callback legitimately DEFINES the globals
        for param in cmd.params:
            if getattr(param, "param_type_name", "") != "option":
                continue
            clashes = sorted(set(param.opts) & RESERVED)
            if clashes:
                offenders.append(f"  `drone-cli {path}` declares {clashes}")

    assert not offenders, (
        "These options can never be received — _pop_globals strips the reserved "
        "globals from anywhere on the line before Click parses them:\n"
        + "\n".join(offenders)
        + "\n\nRename the local option (e.g. --output -> --out), or stop popping the flag."
    )


def test_context_keys_all_match_a_real_option():
    """Every sticky-context key must correspond to a real option somewhere.

    The bug this prevents (inherited from opcli, where KNOWN_KEYS is defined once
    and imported nowhere): rename an option and its context key silently becomes
    a no-op. The user keeps setting `context set --repo x`, nothing applies it,
    and there is no error anywhere.
    """
    from dronecli.commands.context import KNOWN_KEYS

    all_opts: set[str] = set()
    for _path, cmd in _leaf_commands():
        for param in cmd.params:
            if getattr(param, "param_type_name", "") == "option":
                all_opts.add(param.name)

    orphans = [k for k in KNOWN_KEYS if k not in all_opts]
    assert not orphans, (
        f"context keys {orphans} match no option in the command tree, so setting them "
        f"would silently do nothing. Either wire an option named that, or drop the key."
    )


# ---- _pop_globals itself ----------------------------------------------

def test_pops_format_from_anywhere():
    for argv in (["-o", "table", "build", "ls"], ["build", "ls", "-o", "table"]):
        fmt, _f, _b, rest = _pop_globals(argv)
        assert fmt == "table"
        assert rest == ["build", "ls"]


def test_pops_equals_form():
    fmt, _f, _b, rest = _pop_globals(["build", "ls", "--format=csv"])
    assert fmt == "csv"
    assert rest == ["build", "ls"]


def test_pops_bool_globals():
    _fmt, _f, bools, rest = _pop_globals(["promote", "--commit", "HEAD", "--dry-run"])
    assert "dry-run" in bools
    assert rest == ["promote", "--commit", "HEAD"]


def test_double_dash_stops_parsing():
    """After `--`, a literal `-o` belongs to the command, not to us."""
    fmt, _f, _b, rest = _pop_globals(["raw", "get", "--", "-o", "weird"])
    assert fmt is None
    assert rest == ["raw", "get", "--", "-o", "weird"]


def test_fields_flag():
    _fmt, fields, _b, rest = _pop_globals(["build", "ls", "--fields", "number,status"])
    assert fields == "number,status"
    assert rest == ["build", "ls"]


def test_untouched_argv_passes_through():
    fmt, fields, bools, rest = _pop_globals(["wait", "--commit", "HEAD", "--timeout", "5m"])
    assert (fmt, fields, bools) == (None, None, set())
    assert rest == ["wait", "--commit", "HEAD", "--timeout", "5m"]
