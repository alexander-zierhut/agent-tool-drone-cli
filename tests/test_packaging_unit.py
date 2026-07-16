"""Packaging, docs generation and the doc↔code contract — pure, no network.

These tests guard the things that rot silently: a PyInstaller flag list that
drifts from what CI calls, a docs generator nothing runs, an exit-code table that
says one thing while the code does another, and a README that claims a test count.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import typer

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    """Import a script from scripts/ — it is not on the package path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_binary = _load("build_binary", ROOT / "scripts" / "build_binary.py")
gen_docs = _load("gen_docs", ROOT / "scripts" / "gen_docs.py")

README = (ROOT / "README.md").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text()
MAKEFILE = (ROOT / "Makefile").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()


# ---- build_binary: the flags that only fail in the shipped artifact ----

def _flags(system: str) -> list[str]:
    return build_binary.pyinstaller_args("drone-cli", system=system, launcher="l.py")


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_keyring_is_collected_on_every_platform(system):
    """Without --collect-all keyring the binary builds, runs, and cannot read the
    token you stored. It fails only in the artifact, never in a dev checkout."""
    args = _flags(system)
    assert "--collect-all" in args
    assert "keyring" in args[args.index("--collect-all") + 1:]


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_always_onefile_and_finds_the_source_tree(system):
    args = _flags(system)
    assert "--onefile" in args
    assert args[args.index("--paths") + 1] == "src"
    assert args[args.index("--name") + 1] == "drone-cli"
    assert args[-1] == "l.py", "the launcher script is the last positional"


def test_platform_backends_are_the_ones_that_exist():
    """Each OS's keyring backend is a different module; a frozen app drops them
    all unless named. Getting this wrong is invisible until someone downloads it."""
    assert "keyring.backends.SecretService" in build_binary.platform_extras("Linux")
    assert "secretstorage" in build_binary.platform_extras("Linux")
    assert "keyring.backends.macOS" in build_binary.platform_extras("Darwin")
    assert "keyring.backends.Windows" in build_binary.platform_extras("Windows")
    assert build_binary.platform_extras("Haiku") == [], "unknown OS must not crash the build"


def test_windows_asset_name_does_not_double_its_suffix():
    """release.yml asks for `drone-cli-windows-x86_64.exe`; PyInstaller appends
    `.exe` itself, so the name handed to it must not already carry one."""
    assert build_binary.build_name("drone-cli-windows-x86_64.exe", "Windows") == "drone-cli-windows-x86_64"
    assert build_binary.build_name("drone-cli-windows-x86_64", "Windows") == "drone-cli-windows-x86_64"
    assert build_binary.build_name("drone-cli-linux-x86_64", "Linux") == "drone-cli-linux-x86_64"


def test_a_dotted_name_on_unix_is_left_alone():
    """Only Windows appends .exe. Stripping a suffix on Linux would rename the
    asset out from under the release upload."""
    assert build_binary.build_name("drone-cli.exe", "Linux") == "drone-cli.exe"


def test_find_built_accepts_either_suffix(tmp_path):
    assert build_binary.find_built(tmp_path, "drone-cli") is None
    (tmp_path / "drone-cli.exe").write_text("x")
    assert build_binary.find_built(tmp_path, "drone-cli").name == "drone-cli.exe"


def test_release_workflow_calls_this_script_the_way_it_parses():
    """The contract with release.yml: `--output NAME`. If that flag is renamed,
    every tagged release silently loses its binaries."""
    assert "python scripts/build_binary.py --output" in RELEASE
    assert build_binary.parse_args(["--output", "drone-cli-macos-arm64"]).output == "drone-cli-macos-arm64"
    assert build_binary.parse_args([]).output == "drone-cli", "release.yml's default asset"


# ---- gen_docs: renders from the real tree, without needing the real tree ----

def _fake_app() -> typer.Typer:
    app = typer.Typer()
    sub = typer.Typer()

    @sub.command("ls")
    def ls(
        repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
        limit: int = typer.Option(25, "--limit", "-n", help="Max rows."),
    ) -> None:
        """List builds, newest first."""

    @sub.command("info")
    def info(number: int = typer.Argument(..., help="Build NUMBER.")) -> None:
        """Show one build."""

    app.add_typer(sub, name="build", help="Builds: list, inspect.")
    return app


def test_render_emits_groups_commands_and_options():
    md = gen_docs.render(typer.main.get_command(_fake_app()))
    assert "# Command reference" in md
    assert "### `drone-cli build ls`" in md
    assert "List builds, newest first." in md
    assert "`--repo`, `-r`" in md
    assert "| Option | Description |" in md


def test_render_marks_arguments_separately_from_options():
    """Arguments are positional and have no flag — listing them in the option
    table would document a flag that does not exist."""
    md = gen_docs.render(typer.main.get_command(_fake_app()))
    assert "**Arguments:** `number` (required)" in md
    assert "| `number`" not in md


def test_render_escapes_pipes_so_the_tables_survive():
    app = typer.Typer()

    @app.command("x")
    def x(fmt: str = typer.Option("json", "--fmt", help="json|table")) -> None:
        """Thing."""

    @app.command("y")
    def y() -> None:
        """Other thing — a second command, so Typer yields a group, not a bare command."""

    md = gen_docs.render(typer.main.get_command(app))
    assert "json\\|table" in md, "an unescaped pipe silently breaks the markdown table"


def test_gen_docs_helpers_import_without_the_command_tree():
    """The renderers must be importable without httpx/keyring/the whole app —
    that is what makes this test hermetic, and the CI docs job cheap."""
    assert not hasattr(gen_docs, "app"), "the app import belongs inside main()"


def test_ci_regenerates_docs_and_fails_on_drift():
    """The gap the sibling CLI left: it has a gen_docs.py that nothing runs."""
    assert "python scripts/gen_docs.py" in CI
    assert "git diff --exit-code docs/" in CI
    assert "git add -N docs/" in CI, "untracked docs diff clean; -N is what closes that hole"


# ---- the doc/code contract ----

def test_agents_exit_table_matches_the_code():
    """AGENTS.md is a published contract. If errors.py renumbers and the table
    doesn't, an agent branches on a lie."""
    from dronecli import errors as E

    documented = {int(m) for m in re.findall(r"^  \| (\d+) \|", AGENTS, re.M)}
    for cls in (E.OpError, E.ConfigError, E.AuthError, E.NotFoundError,
                E.ValidationError, E.NotImplementedOnServer, E.BuildNotFinished,
                E.CommitNotBuilt):
        assert cls.exit_code in documented, f"{cls.__name__} -> {cls.exit_code} is undocumented"
    assert documented == {0, 1, 3, 4, 5, 7, 8, 9, 10, 130}


def test_six_is_reserved_and_never_reused():
    """Family-wide, 6 means conflict. Drone has no optimistic locking, so it is a
    hole — not a free number. An agent that learned 6 from a sibling CLI must
    never meet a different meaning here."""
    assert "| 6 |" not in AGENTS
    assert "reserved family-wide" in AGENTS
    from dronecli import errors as E
    codes = [c.exit_code for c in (E.NotImplementedOnServer, E.BuildNotFinished, E.CommitNotBuilt)]
    assert 6 not in codes


def test_docs_do_not_claim_a_test_count():
    """Counts rot on every commit. The sibling README claimed "56 tests" against
    a real 233 for months — nobody noticed, because nothing could."""
    for name, text in (("README.md", README), ("AGENTS.md", AGENTS)):
        assert not re.search(r"\b\d+\s+(?:tests?|passed|skipped)\b", text), \
            f"{name} states a test count; it will be wrong within a week"


def test_readme_leads_development_with_the_contributor_promise():
    """A Development section that opens with `docker compose up` is a barrier that
    loses drive-by contributors. The two-line promise comes first, always."""
    dev = README[README.index("## Contributing / Development"):]
    promise = dev.index("pip install -e '.[test]'")
    assert "You do not need Drone, Docker" in dev[:promise]
    for heavy in ("docker", "compose", "Forgejo"):
        pos = dev.lower().find(heavy.lower())
        assert pos == -1 or pos > promise or "do not need" in dev[max(0, pos - 60):pos], \
            f"{heavy!r} appears before the contributor promise"
    assert "deeper tier" in dev


def test_readme_states_the_command_name_and_that_drone_is_not_claimed():
    assert "`drone-cli`, not `drone`" in README
    assert "deliberately does not claim it" in README
    assert "coexist" in README


def test_readme_leads_features_with_the_two_reasons_the_tool_exists():
    why = README[README.index("### Why this Drone CLI?"):README.index("**Docs:**")]
    bullets = [ln for ln in why.splitlines() if ln.startswith("- ")]
    assert "wait --commit HEAD" in bullets[0], "commit-addressed waiting leads"
    assert "log failed" in bullets[1]


def test_readme_documents_the_api_limitations_we_cannot_fix():
    limits = README[README.index("## Known limitations"):]
    assert "no duration field" in limits.lower()
    assert "never be read back" in limits          # secret values
    assert "No cross-repo build search" in limits
    assert "seconds-first" in limits
    assert "not a commit link" in limits


def test_makefile_test_unit_uses_the_marker_not_a_file_list():
    """`pytest tests/test_unit.py` ran 30 of 144 in the sibling repo while its
    help claimed to run them all. The marker grows with the suite; a path does not."""
    body = MAKEFILE[MAKEFILE.index("test-unit:"):MAKEFILE.index("lint:")]
    recipe = [ln for ln in body.splitlines() if ln.startswith("\t") and not ln.lstrip().startswith("#")]
    assert any('pytest -m "not integration"' in ln for ln in recipe)
    assert not any("tests/" in ln for ln in recipe), "a file list silently stops growing"


def test_makefile_has_every_promised_target():
    for target in ("help", "install", "test", "test-unit", "lint", "docs", "clean"):
        assert re.search(rf"^{re.escape(target)}:", MAKEFILE, re.M), f"missing target {target}"


def test_ci_runs_the_hermetic_suite_on_the_supported_matrix():
    for version in ("3.10", "3.11", "3.12"):
        assert f'"{version}"' in CI
    assert 'pytest -m "not integration"' in CI


def test_ci_smoke_tests_the_wheel_from_a_clean_venv():
    """`pip install -e .` in the job venv makes a broken wheel look installable —
    the entry point resolves from the source tree either way."""
    assert "python -m venv /tmp/clean" in CI
    assert "/tmp/clean/bin/pip install dist/*.whl" in CI
    assert "/tmp/clean/bin/drone-cli --version" in CI


def test_ci_has_no_live_integration_job():
    """The Forgejo+Drone stack is not committed. A job that pretends otherwise is
    a red build with no fix available to the person who broke it."""
    jobs_block = CI[CI.index("\njobs:"):]
    jobs = set(re.findall(r"^  ([a-z][a-z-]*):$", jobs_block, re.M))
    assert jobs == {"unit", "build", "docs"}
