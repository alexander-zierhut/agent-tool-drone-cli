"""Build selection, terminal logic and commit resolution — pure, no server.

Fixtures below are shaped from real responses captured during the 2026-07-16
spike against a live gitea + drone + runner stack.
"""

from __future__ import annotations

import pytest

from dronecli import builds as B

SHA_A = "42f7a46ed69c9cdd53a6b44fe98c0d55986b19a5"
SHA_B = "3d0e0b4d860bdc9c0e0a866fca324794f9f5128b"


def mk(number, status, event="push", after=SHA_A, deploy_to="", **kw):
    return {"number": number, "status": status, "event": event, "after": after,
            "deploy_to": deploy_to, **kw}


# ---- terminal logic ---------------------------------------------------

@pytest.mark.parametrize("status", sorted(B.IN_FLIGHT))
def test_in_flight_is_not_done(status):
    assert not B.is_done(mk(1, status))


@pytest.mark.parametrize("status", sorted(B.TERMINAL))
def test_terminal_is_done(status):
    assert B.is_done(mk(1, status))


def test_blocked_counts_as_done_deliberately():
    """The documented deviation from Drone's own IsDone().

    A blocked build waits on a human and will sit for hours. Treating it as
    "still running" makes `wait` hang until timeout on every approval-gated
    pipeline — the exact failure this feature exists to prevent.
    """
    b = mk(1, B.STATUS_BLOCKED)
    assert B.is_done(b), "blocked must terminate a wait"
    assert B.is_blocked(b), "...but it must be reported as its own outcome"
    assert not B.succeeded(b), "and it is certainly not a success"


def test_skipped_is_good_but_failure_is_not():
    assert B.succeeded(mk(1, B.STATUS_SUCCESS))
    assert B.succeeded(mk(1, B.STATUS_SKIPPED))
    assert not B.succeeded(mk(1, B.STATUS_FAILURE))
    assert not B.succeeded(mk(1, B.STATUS_KILLED))


# ---- commit matching --------------------------------------------------

def test_matches_full_and_short_sha():
    b = mk(1, "success", after=SHA_A)
    assert B.sha_matches(b, SHA_A)
    assert B.sha_matches(b, SHA_A[:8]), "short SHAs are what humans paste"
    assert B.sha_matches(b, SHA_A.upper()), "case must not matter"
    assert not B.sha_matches(b, SHA_B)


def test_never_matches_the_parent_commit():
    """`before` is the parent. If it matched, a push would also match its
    predecessor's build and an agent could wait on the wrong one."""
    b = {"number": 1, "status": "success", "after": SHA_A, "before": SHA_B}
    assert not B.sha_matches(b, SHA_B)


def test_empty_sha_matches_nothing():
    assert not B.sha_matches(mk(1, "success"), "")
    assert not B.sha_matches({"after": ""}, SHA_A)


# ---- selection: one commit, many builds -------------------------------

def test_selects_all_builds_for_a_commit_newest_first():
    builds = [
        mk(5, "success", event="promote", deploy_to="prod"),
        mk(4, "failure", event="push"),
        mk(3, "success", event="push", after=SHA_B),   # someone else's commit
        mk(2, "success", event="push"),
    ]
    got = B.select_for_commit(builds, SHA_A)
    assert [b["number"] for b in got] == [5, 4, 2], "newest first, only our commit"


def test_selects_by_event():
    """The race this whole design exists for: a commit has a push build AND a
    promote build. 'Did my push pass?' must not be answered by the promote."""
    builds = [mk(9, "success", event="promote", deploy_to="prod"), mk(8, "failure", event="push")]
    got = B.select_for_commit(builds, SHA_A, event=B.EVENT_PUSH)
    assert [b["number"] for b in got] == [8]
    assert got[0]["status"] == "failure"


def test_selects_by_target():
    builds = [
        mk(7, "success", event="promote", deploy_to="staging"),
        mk(6, "success", event="promote", deploy_to="prod"),
    ]
    got = B.select_for_commit(builds, SHA_A, event=B.EVENT_PROMOTE, target="prod")
    assert [b["number"] for b in got] == [6]


def test_no_builds_for_unknown_commit():
    assert B.select_for_commit([mk(1, "success")], "deadbeef") == []


# ---- "which commit is on prod?" ---------------------------------------

def test_latest_promotion_picks_newest_successful():
    builds = [
        mk(10, "failure", event="promote", after=SHA_B, deploy_to="prod"),  # newer but FAILED
        mk(9, "success", event="promote", after=SHA_A, deploy_to="prod"),
        mk(8, "success", event="promote", after=SHA_B, deploy_to="staging"),
    ]
    live = B.latest_promotion(builds, "prod")
    assert live is not None and live["number"] == 9
    assert live["after"] == SHA_A


def test_a_failed_promote_never_counts_as_deployed():
    """The most dangerous wrong answer this tool could give."""
    builds = [mk(10, "failure", event="promote", deploy_to="prod")]
    assert B.latest_promotion(builds, "prod") is None


def test_latest_promotion_respects_target():
    builds = [mk(9, "success", event="promote", deploy_to="staging")]
    assert B.latest_promotion(builds, "prod") is None
    assert B.latest_promotion(builds, "staging")["number"] == 9


def test_rollback_counts_as_a_deployment():
    """A rollback IS what is live now — ignoring it reports a stale commit."""
    builds = [
        mk(11, "success", event="rollback", after=SHA_B, deploy_to="prod"),
        mk(10, "success", event="promote", after=SHA_A, deploy_to="prod"),
    ]
    assert B.latest_promotion(builds, "prod")["after"] == SHA_B


# ---- "has my commit been promoted?" -----------------------------------

def test_promotions_of_a_commit():
    builds = [
        mk(9, "success", event="promote", deploy_to="prod"),
        mk(8, "failure", event="promote", deploy_to="staging"),
        mk(7, "success", event="push"),
    ]
    got = B.promotions_of(builds, SHA_A)
    assert [b["number"] for b in got] == [9, 8], "push is not a promotion"
    assert [b["number"] for b in B.promotions_of(builds, SHA_A, target="prod")] == [9]


# ---- failed steps -----------------------------------------------------

def test_failed_steps_returns_1_based_ordinals():
    build = {
        "number": 2,
        "status": "failure",
        "stages": [
            {"number": 1, "name": "default", "steps": [
                {"number": 1, "name": "clone", "status": "success", "exit_code": 0},
                {"number": 2, "name": "test", "status": "failure", "exit_code": 1},
            ]}
        ],
    }
    got = B.failed_steps(build)
    assert got == [{"stage": 1, "step": 2, "stage_name": "default", "step_name": "test",
                    "status": "failure", "exit_code": 1}]


def test_failed_steps_survives_null_stages():
    """A pending build has stages: null (not []), which NPEs naive iteration."""
    assert B.failed_steps({"number": 1, "status": "pending", "stages": None}) == []
    assert B.failed_steps({"number": 1, "status": "pending"}) == []


def test_failed_steps_empty_when_green():
    build = {"stages": [{"number": 1, "steps": [{"number": 1, "status": "success"}]}]}
    assert B.failed_steps(build) == []


# ---- derived time (there is no duration field in the API) -------------

def test_duration_and_queue():
    b = {"created": 100, "started": 110, "finished": 175}
    assert B.duration_seconds(b) == 65
    assert B.queue_seconds(b) == 10


def test_unfinished_has_no_duration():
    """finished == 0 means running, NOT 1970. Treating it as an epoch yields a
    56-year build and poisons every average."""
    assert B.duration_seconds({"started": 100, "finished": 0}) is None
    assert B.duration_seconds({"started": 0, "finished": 0}) is None


def test_nonsense_ordering_is_none_not_negative():
    assert B.duration_seconds({"started": 200, "finished": 100}) is None
    assert B.queue_seconds({"created": 200, "started": 100}) is None


# ---- commit resolution ------------------------------------------------

def test_resolve_commit_passes_a_literal_sha_through():
    assert B.resolve_commit(SHA_A) == SHA_A
    assert B.resolve_commit("  " + SHA_A + " ") == SHA_A


def test_resolve_commit_none():
    assert B.resolve_commit(None) is None
    assert B.resolve_commit("") is None


def test_resolve_head_uses_git(monkeypatch):
    monkeypatch.setattr(B, "git_head", lambda cwd=None: SHA_A)
    assert B.resolve_commit("HEAD") == SHA_A
    assert B.resolve_commit("head") == SHA_A


def test_git_head_outside_a_repo_is_none(tmp_path):
    """Must not raise: `--commit HEAD` in a non-repo is a normal user error."""
    assert B.git_head(cwd=str(tmp_path)) is None
