"""Cron expressions and the `cron` commands — pure, no server, no clock.

The centrepiece is the seconds-first contrast: `0 3 * * *` (5-field) must be
REFUSED, `0 0 3 * * *` fires daily at 03:00, and `0 3 * * * *` fires hourly at
:03. Those three lines are the reason this module exists — the middle one is what
people mean, the first is what they type, and the last is what Drone does with
it. Every test below fixes `now`; nothing here reads a clock or a socket.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dronecli import cronspec as C
from dronecli.commands import cron as cmd
from dronecli.errors import NotFoundError, OpError, ValidationError

# A Wednesday, mid-month, mid-year. Every expectation below is hand-computed
# from this instant.
NOW = datetime(2026, 7, 15, 10, 30, 0, tzinfo=timezone.utc)


def iso(*args) -> str:
    return datetime(*args, tzinfo=timezone.utc).isoformat()


# =========================================================================
# THE GUARD: 5-field detection
# =========================================================================


@pytest.mark.parametrize("expr", ["0 3 * * *", "*/15 * * * *", "0 0 * * 1", "  0   3 * * * "])
def test_five_field_crontab_is_detected(expr):
    assert C.looks_like_5_field(expr)


@pytest.mark.parametrize("expr", ["0 0 3 * * *", "@daily", "@every 1h", "0 3 * * * *"])
def test_six_field_and_macros_are_not_five_field(expr):
    assert not C.looks_like_5_field(expr)


def test_the_correction_prepends_seconds():
    assert C.to_6_field("0 3 * * *") == "0 0 3 * * *"
    assert C.to_6_field("*/15 * * * *") == "0 */15 * * * *"


def test_misread_names_the_fields_the_way_drone_shifts_them():
    """The 5-field line is not rejected by Drone — it is left-aligned onto
    SECOND. Saying so concretely is what makes the error actionable."""
    assert C.misread_as("0 3 * * *") == "second=0, minute=3, hour=*, dom=*, month=*, dow=*"


def test_explain_carries_both_readings():
    got = C.explain_5_field("0 3 * * *")
    assert got["correct_6_field"] == "0 0 3 * * *"
    assert got["fields_given"] == 5 and got["fields_drone_expects"] == 6
    assert "hour=*" in got["drone_would_read_it_as"]


# =========================================================================
# THE CONTRAST — the whole point of the module
# =========================================================================


def test_five_field_expr_is_refused_by_next_fire_times():
    """`0 3 * * *` PARSES on the server. It must not parse here: computing a
    preview for it would report the misreading as if it were the schedule."""
    with pytest.raises(ValidationError) as exc:
        C.next_fire_times("0 3 * * *", 5, now=NOW)
    msg = str(exc.value)
    assert "5-field" in msg
    assert "0 0 3 * * *" in msg, "the error must hand over the correct expression"


def test_six_field_daily_fires_daily_at_0300():
    got = C.next_fire_times("0 0 3 * * *", 3, now=NOW)
    assert [d.isoformat() for d in got] == [
        iso(2026, 7, 16, 3, 0, 0),
        iso(2026, 7, 17, 3, 0, 0),
        iso(2026, 7, 18, 3, 0, 0),
    ]
    assert {d.hour for d in got} == {3}


def test_the_same_expr_shifted_fires_hourly_at_three_past():
    """`0 3 * * * *` — one field longer, 24x more often. This is what a user who
    typed the 5-field line actually gets, and why the guard has to exist."""
    got = C.next_fire_times("0 3 * * * *", 3, now=NOW)
    assert [d.isoformat() for d in got] == [
        iso(2026, 7, 15, 11, 3, 0),
        iso(2026, 7, 15, 12, 3, 0),
        iso(2026, 7, 15, 13, 3, 0),
    ]
    assert len({d.hour for d in got}) == 3, "a different hour every time == hourly"


def test_the_two_readings_differ_24x_over_a_day():
    """Quantifying the bug: same intent, one field apart."""
    daily = C.next_fire_times("0 0 3 * * *", 24, now=NOW)
    hourly = C.next_fire_times("0 3 * * * *", 24, now=NOW)
    assert (daily[-1] - daily[0]).days == 23
    assert (hourly[-1] - hourly[0]) == timedelta(hours=23)


# =========================================================================
# next_fire_times: determinism and field support
# =========================================================================


def test_now_is_a_required_parameter():
    """A preview that reads the wall clock cannot be tested — so it is not
    allowed to. This test pins the signature, not the behaviour."""
    with pytest.raises(TypeError):
        C.next_fire_times("0 0 3 * * *", 5)  # type: ignore[call-arg]


def test_next_is_strictly_after_now_never_now_itself():
    at_three = datetime(2026, 7, 15, 3, 0, 0, tzinfo=timezone.utc)
    got = C.next_fire_times("0 0 3 * * *", 1, now=at_three)
    assert got[0] == datetime(2026, 7, 16, 3, 0, 0, tzinfo=timezone.utc)


def test_every_15_minutes_steps_within_the_hour():
    got = C.next_fire_times("0 */15 * * * *", 4, now=NOW)
    assert [d.isoformat() for d in got] == [
        iso(2026, 7, 15, 10, 45, 0),
        iso(2026, 7, 15, 11, 0, 0),
        iso(2026, 7, 15, 11, 15, 0),
        iso(2026, 7, 15, 11, 30, 0),
    ]


def test_seconds_field_is_honoured_not_ignored():
    got = C.next_fire_times("*/30 * * * * *", 3, now=NOW)
    assert [d.second for d in got] == [30, 0, 30]


def test_lists_and_ranges():
    got = C.next_fire_times("0 0 8,12,18 * * *", 4, now=NOW)
    assert [d.hour for d in got] == [12, 18, 8, 12]
    weekdays = C.next_fire_times("0 0 3 * * 1-5", 5, now=NOW)
    assert [d.isoweekday() for d in weekdays] == [4, 5, 1, 2, 3], "Sat/Sun skipped"


def test_dow_is_sunday_zero_not_pythons_monday_zero():
    """Off-by-one here silently schedules everything a day early."""
    sundays = C.next_fire_times("0 0 0 * * 0", 2, now=NOW)
    assert all(d.isoweekday() == 7 for d in sundays)
    mondays = C.next_fire_times("0 0 0 * * 1", 2, now=NOW)
    assert all(d.isoweekday() == 1 for d in mondays)


def test_dom_and_dow_are_ORed_when_both_restricted():
    """Crontab's oddest rule: `0 0 0 1 * 1` is "the 1st OR any Monday", not
    "Mondays that fall on the 1st". ANDing it fires ~12x less than asked."""
    got = C.next_fire_times("0 0 0 1 * 1", 6, now=NOW)
    for d in got:
        assert d.day == 1 or d.isoweekday() == 1
    assert any(d.day != 1 for d in got), "Mondays must be included"
    assert any(d.isoweekday() != 1 for d in got), "the 1st must be included"


def test_month_field_restricts():
    got = C.next_fire_times("0 0 0 1 1 *", 2, now=NOW)
    assert [d.isoformat() for d in got] == [iso(2027, 1, 1, 0, 0, 0), iso(2028, 1, 1, 0, 0, 0)]


def test_names_are_accepted_for_month_and_dow():
    assert C.next_fire_times("0 0 0 * * SUN", 1, now=NOW)[0].isoweekday() == 7
    assert C.next_fire_times("0 0 0 1 DEC *", 1, now=NOW)[0].month == 12


def test_macros_expand_to_six_fields():
    assert C.next_fire_times("@daily", 1, now=NOW)[0].isoformat() == iso(2026, 7, 16, 0, 0, 0)
    assert C.next_fire_times("@hourly", 1, now=NOW)[0].isoformat() == iso(2026, 7, 15, 11, 0, 0)
    assert C.next_fire_times("@weekly", 1, now=NOW)[0].isoweekday() == 7
    assert C.next_fire_times("@monthly", 1, now=NOW)[0].isoformat() == iso(2026, 8, 1, 0, 0, 0)


def test_at_every_is_an_interval_from_now_not_a_wall_clock_pattern():
    got = C.next_fire_times("@every 1h30m", 2, now=NOW)
    assert [d.isoformat() for d in got] == [iso(2026, 7, 15, 12, 0, 0), iso(2026, 7, 15, 13, 30, 0)]


def test_impossible_date_says_so_instead_of_hanging():
    """Feb 30 is legal syntax and can never occur. A bounded search must answer,
    not spin — and 'never fires' is the answer worth having."""
    with pytest.raises(ValidationError) as exc:
        C.next_fire_times("0 0 0 30 2 *", 1, now=NOW)
    assert "never fires" in str(exc.value)


def test_feb_29_finds_the_leap_year():
    got = C.next_fire_times("0 0 0 29 2 *", 1, now=NOW)
    assert got[0].isoformat() == iso(2028, 2, 29, 0, 0, 0)


def test_tzinfo_of_now_is_preserved():
    naive = datetime(2026, 7, 15, 10, 30)
    assert C.next_fire_times("0 0 3 * * *", 1, now=naive)[0].tzinfo is None
    assert C.next_fire_times("0 0 3 * * *", 1, now=NOW)[0].tzinfo is timezone.utc


# =========================================================================
# parse: refuse rather than approximate
# =========================================================================


@pytest.mark.parametrize("expr", ["", "   ", "0 0 3 * *  * *", "0 0 3 *"])
def test_wrong_field_counts_are_rejected(expr):
    with pytest.raises(ValidationError):
        C.parse(expr)


def test_dow_7_is_rejected_because_drone_rejects_it():
    """crontab users write 7 for Sunday; robfig v1 does not accept it. Accepting
    it here would preview a schedule the server will refuse."""
    with pytest.raises(ValidationError) as exc:
        C.parse("0 0 0 * * 7")
    assert "use 0" in str(exc.value).lower()


@pytest.mark.parametrize("expr", ["0 0 25 * * *", "0 60 * * * *", "0 0 0 0 * *", "0 0 0 * 13 *"])
def test_out_of_range_values_are_rejected(expr):
    with pytest.raises(ValidationError):
        C.parse(expr)


@pytest.mark.parametrize("expr", ["0 0 x * * *", "0 0 3 * * MONDAY", "0 0 */0 * * *", "0 0 9-3 * * *"])
def test_garbage_is_rejected_not_silently_dropped(expr):
    with pytest.raises(ValidationError):
        C.parse(expr)


def test_unknown_descriptor_lists_the_known_ones():
    with pytest.raises(ValidationError) as exc:
        C.parse("@fortnightly")
    assert "@daily" in str(exc.value)


def test_parse_duration():
    assert C.parse_duration("15m") == timedelta(minutes=15)
    assert C.parse_duration("1h30m") == timedelta(minutes=90)
    assert C.parse_duration("90s") == timedelta(seconds=90)
    for bad in ["", "15", "m", "1d", "abc", "0s"]:
        with pytest.raises(ValidationError):
            C.parse_duration(bad)


# =========================================================================
# from_human — so nobody hand-assembles six fields
# =========================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3am daily", "0 0 3 * * *"),
        ("daily at 3am", "0 0 3 * * *"),
        ("daily at 03:30", "0 30 3 * * *"),
        ("every day at 15:45", "0 45 15 * * *"),
        ("3pm daily", "0 0 15 * * *"),
        ("12am daily", "0 0 0 * * *"),
        ("12pm daily", "0 0 12 * * *"),
        ("midnight", "0 0 0 * * *"),
        ("noon", "0 0 12 * * *"),
        ("nightly", "0 0 3 * * *"),
        ("hourly", "0 0 * * * *"),
        ("weekly", "0 0 0 * * 0"),
        ("every 15m", "0 */15 * * * *"),
        ("every 15 minutes", "0 */15 * * * *"),
        ("every 2 hours", "0 0 */2 * * *"),
        ("every 30s", "*/30 * * * * *"),
        ("every monday at 9am", "0 0 9 * * 1"),
        ("weekdays at 6am", "0 0 6 * * 1-5"),
        ("@daily", "0 0 0 * * *"),
    ],
)
def test_from_human(text, expected):
    assert C.from_human(text) == expected


def test_everything_from_human_is_six_field_by_construction():
    """The guarantee that makes --at/--every/--preset safe to recommend."""
    for text in ["3am daily", "every 15m", "nightly", "every monday at 9am", "weekly"]:
        assert not C.looks_like_5_field(C.from_human(text))
        assert len(C.from_human(text).split()) == 6


def test_from_human_refuses_rather_than_approximating():
    for text in ["", "sometime soon", "when the build is green", "daily at 25:00", "13pm daily"]:
        with pytest.raises(ValidationError):
            C.from_human(text)


def test_every_preset_is_a_valid_six_field_expr():
    for name, expr in C.PRESETS.items():
        assert len(expr.split()) == 6, name
        assert C.next_fire_times(expr, 1, now=NOW), name


# =========================================================================
# the commands (fake client — no network, no server semantics simulated)
# =========================================================================

CRON = {
    "id": 3, "repo_id": 1, "name": "nightly", "expr": "0 0 3 * * *", "next": 1784257200,
    "prev": 0, "event": "push", "branch": "main", "disabled": False,
    "created": 1784170800, "updated": 1784170800, "version": 1,
}


class FakeClient:
    """Records calls, replays canned responses. Deliberately dumb: it must not
    model Drone's semantics, only its shapes (see the tier-1 rule in PLAN.md)."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def _answer(self, method, path):
        self.calls.append((method, path))
        key = (method, path)
        if key not in self.responses:
            raise AssertionError(f"unexpected {method} {path}")
        val = self.responses[key]
        if isinstance(val, Exception):
            raise val
        return val

    def get(self, path, **kw):
        return self._answer("GET", path)

    def post(self, path, **kw):
        self.calls_json = kw.get("json")
        return self._answer("POST", path)

    def patch(self, path, **kw):
        self.patch_json = kw.get("json")
        return self._answer("PATCH", path)

    def delete(self, path, **kw):
        return self._answer("DELETE", path)


class FakeEmitter:
    def __init__(self):
        self.emitted = []
        self.messages = []

    def emit(self, data, **kw):
        self.emitted.append(data)

    def message(self, text):
        self.messages.append(text)


def mkctx(client, *, interactive=False):
    obj = SimpleNamespace(
        emitter=FakeEmitter(),
        config=SimpleNamespace(context={}),
        interactive=interactive,
        client=lambda: client,
    )
    return SimpleNamespace(obj=obj), obj


def test_add_refuses_a_five_field_expr_and_never_calls_the_server():
    """The headline. Note the assertion on `calls`: refusing after the POST would
    be worthless — the wrong schedule would already exist."""
    client = FakeClient()
    ctx, obj = mkctx(client)
    with pytest.raises(ValidationError) as exc:
        cmd.add(ctx, name="nightly", repo="o/n", expr="0 3 * * *",
                at=None, every=None, preset=None, branch="main")
    assert "0 0 3 * * *" in str(exc.value), "must show the correct 6-field form"
    assert exc.value.detail["correct_6_field"] == "0 0 3 * * *"
    assert client.calls == [], "nothing may be created"


def test_add_posts_a_six_field_expr_and_returns_the_next_five_fires():
    client = FakeClient({("POST", "repos/o/n/cron"): CRON})
    ctx, obj = mkctx(client)
    cmd.add(ctx, name="nightly", repo="o/n", expr="0 0 3 * * *",
            at=None, every=None, preset=None, branch="main")
    out = obj.emitter.emitted[0]
    assert client.calls_json == {"name": "nightly", "expr": "0 0 3 * * *", "branch": "main"}
    assert len(out["next_fire_times"]) == 5
    assert all("T03:00:00" in t for t in out["next_fire_times"]), "daily at 03:00"


def test_add_from_human_never_produces_a_five_field_expr():
    client = FakeClient({("POST", "repos/o/n/cron"): CRON})
    ctx, obj = mkctx(client)
    cmd.add(ctx, name="nightly", repo="o/n", expr=None, at="3am daily",
            every=None, preset=None, branch="main")
    assert client.calls_json["expr"] == "0 0 3 * * *"


def test_add_defaults_the_branch_from_the_repo():
    client = FakeClient({("GET", "repos/o/n"): {"branch": "trunk"}, ("POST", "repos/o/n/cron"): CRON})
    ctx, obj = mkctx(client)
    cmd.add(ctx, name="nightly", repo="o/n", expr=None, at=None, every=None,
            preset="nightly", branch=None)
    assert client.calls_json["branch"] == "trunk"


def test_add_requires_exactly_one_way_of_saying_when():
    client = FakeClient()
    ctx, obj = mkctx(client)
    with pytest.raises(ValidationError):
        cmd.add(ctx, name="x", repo="o/n", expr=None, at=None, every=None, preset=None, branch="main")
    with pytest.raises(ValidationError) as exc:
        cmd.add(ctx, name="x", repo="o/n", expr="0 0 3 * * *", at="3am daily",
                every=None, preset=None, branch="main")
    assert "mutually exclusive" in str(exc.value)
    assert client.calls == []


def test_add_echoes_the_servers_slugified_name():
    """'Nightly Build' -> 'nightly-build' server-side; addressing it by the name
    you sent would 404 forever."""
    client = FakeClient({("POST", "repos/o/n/cron"): {**CRON, "name": "nightly-build"}})
    ctx, obj = mkctx(client)
    cmd.add(ctx, name="Nightly Build", repo="o/n", expr="0 0 3 * * *",
            at=None, every=None, preset=None, branch="main")
    out = obj.emitter.emitted[0]
    assert out["name"] == "nightly-build"
    assert "slugified" in out["note"]


def test_get_decorates_epochs_and_previews():
    client = FakeClient({("GET", "repos/o/n/cron/nightly"): CRON})
    ctx, obj = mkctx(client)
    cmd.get(ctx, name="nightly", repo="o/n")
    out = obj.emitter.emitted[0]
    assert out["next_utc"] == "2026-07-17T03:00:00+00:00", "epoch -> ISO, in UTC"
    assert out["prev_utc"] is None, "prev=0 means never ran — NOT 1970"
    assert len(out["next_fire_times"]) == 5


def test_get_diagnoses_an_existing_five_field_cron():
    """Someone else's cron, created before this CLI existed. Nothing on the wire
    says it is wrong; the expr does."""
    client = FakeClient({("GET", "repos/o/n/cron/hourly-oops"): {**CRON, "expr": "0 3 * * *"}})
    ctx, obj = mkctx(client)
    cmd.get(ctx, name="hourly-oops", repo="o/n")
    out = obj.emitter.emitted[0]
    assert "every hour" in out["warning"]
    assert "0 0 3 * * *" in out["warning"]
    assert out["next_fire_times"] == [], "we refuse to preview a misread expr"


def test_get_explains_the_slugification_on_404():
    client = FakeClient({("GET", "repos/o/n/cron/Nightly Build"): NotFoundError("not found")})
    ctx, obj = mkctx(client)
    with pytest.raises(NotFoundError) as exc:
        cmd.get(ctx, name="Nightly Build", repo="o/n")
    assert "slugified" in str(exc.value)


def test_ls_filters_and_previews_on_demand():
    rows = [CRON, {**CRON, "name": "paused", "disabled": True}]
    client = FakeClient({("GET", "repos/o/n/cron"): rows})
    ctx, obj = mkctx(client)
    cmd.ls(ctx, repo="o/n", disabled=True, preview=True)
    out = obj.emitter.emitted[0]
    assert [c["name"] for c in out] == ["paused"]
    assert len(out[0]["next_fire_times"]) == 5


def test_ls_preview_survives_an_expr_we_cannot_parse():
    """A server-legal expr we don't model must not break listing."""
    client = FakeClient({("GET", "repos/o/n/cron"): [{**CRON, "expr": "0 0 3 ? * L"}]})
    ctx, obj = mkctx(client)
    cmd.ls(ctx, repo="o/n", disabled=None, preview=True)
    assert obj.emitter.emitted[0][0]["next_fire_times"] == []


def test_next_previews_without_touching_the_server():
    ctx, obj = mkctx(FakeClient())
    cmd.next_(ctx, expr="0 0 3 * * *", at=None, every=None, preset=None, count=3)
    out = obj.emitter.emitted[0]
    assert len(out["next_fire_times"]) == 3
    assert all("T03:00:00" in t for t in out["next_fire_times"])


def test_next_refuses_five_field_too():
    ctx, obj = mkctx(FakeClient())
    with pytest.raises(ValidationError):
        cmd.next_(ctx, expr="0 3 * * *", at=None, every=None, preset=None, count=3)


def test_exec_surfaces_the_created_build_number():
    """drone-go throws this response away (`c.post(uri, nil, nil)`). It is the
    only way to know which build your trigger produced."""
    client = FakeClient({("POST", "repos/o/n/cron/nightly"): {"number": 42, "event": "cron", "target": "main"}})
    ctx, obj = mkctx(client)
    cmd.exec_(ctx, name="nightly", repo="o/n")
    out = obj.emitter.emitted[0]
    assert out["number"] == 42
    assert "#42" in out["note"] and "drone-cli wait 42" in out["note"]


def test_exec_never_invents_a_number_when_the_body_is_empty():
    client = FakeClient({("POST", "repos/o/n/cron/nightly"): None})
    ctx, obj = mkctx(client)
    cmd.exec_(ctx, name="nightly", repo="o/n")
    out = obj.emitter.emitted[0]
    assert out["number"] is None and out["status"] == "triggered"
    assert "unknown" in out["note"]


def test_update_patches_branch_and_verifies_the_response():
    client = FakeClient({("GET", "repos/o/n/cron/nightly"): CRON,
                         ("PATCH", "repos/o/n/cron/nightly"): {**CRON, "branch": "release"}})
    ctx, obj = mkctx(client)
    cmd.update(ctx, name="nightly", repo="o/n", expr=None, at=None, every=None, preset=None,
               branch="release", disabled=None, rename=None, recreate=False)
    assert client.patch_json == {"branch": "release"}
    assert obj.emitter.emitted[0]["branch"] == "release"


def test_update_fails_loudly_when_the_server_lies_with_a_200():
    """Drone's cron PATCH decodes fields it does not support, discards them, and
    returns 200 with the object unchanged. Only the diff catches it."""
    client = FakeClient({("GET", "repos/o/n/cron/nightly"): CRON,
                         ("PATCH", "repos/o/n/cron/nightly"): CRON})  # unchanged!
    ctx, obj = mkctx(client)
    with pytest.raises(OpError) as exc:
        cmd.update(ctx, name="nightly", repo="o/n", expr=None, at=None, every=None, preset=None,
                   branch="release", disabled=None, rename=None, recreate=False)
    assert "did NOT apply" in str(exc.value)
    assert exc.value.detail["server_returned"] == {"branch": "main"}
    assert obj.emitter.emitted == [], "a lie must never be emitted as a result"


def test_update_disable_is_a_real_patch():
    client = FakeClient({("GET", "repos/o/n/cron/nightly"): CRON,
                         ("PATCH", "repos/o/n/cron/nightly"): {**CRON, "disabled": True}})
    ctx, obj = mkctx(client)
    cmd.update(ctx, name="nightly", repo="o/n", expr=None, at=None, every=None, preset=None,
               branch=None, disabled=True, rename=None, recreate=False)
    assert client.patch_json == {"disabled": True}
    assert obj.emitter.emitted[0]["disabled"] is True


def test_update_expr_without_recreate_refuses_and_touches_nothing():
    """The server cannot patch expr. Doing DELETE+POST behind an innocuous verb
    would silently reset the cron's id and history — so we ask first."""
    client = FakeClient()
    ctx, obj = mkctx(client)
    with pytest.raises(OpError) as exc:
        cmd.update(ctx, name="nightly", repo="o/n", expr="0 0 4 * * *", at=None, every=None,
                   preset=None, branch=None, disabled=None, rename=None, recreate=False)
    assert "--recreate" in str(exc.value)
    assert client.calls == []


def test_update_expr_still_refuses_a_five_field_expr_before_anything_else():
    client = FakeClient()
    ctx, obj = mkctx(client)
    with pytest.raises(ValidationError) as exc:
        cmd.update(ctx, name="nightly", repo="o/n", expr="0 4 * * *", at=None, every=None,
                   preset=None, branch=None, disabled=None, rename=None, recreate=True)
    assert "0 0 4 * * *" in str(exc.value)
    assert client.calls == []


def test_update_recreate_deletes_then_posts_and_says_history_was_reset():
    client = FakeClient({
        ("GET", "repos/o/n/cron/nightly"): CRON,
        ("DELETE", "repos/o/n/cron/nightly"): None,
        ("POST", "repos/o/n/cron"): {**CRON, "id": 9, "expr": "0 0 4 * * *", "prev": 0},
    })
    ctx, obj = mkctx(client)
    cmd.update(ctx, name="nightly", repo="o/n", expr="0 0 4 * * *", at=None, every=None,
               preset=None, branch=None, disabled=None, rename=None, recreate=True)
    assert client.calls == [("GET", "repos/o/n/cron/nightly"),
                            ("DELETE", "repos/o/n/cron/nightly"),
                            ("POST", "repos/o/n/cron")]
    assert client.calls_json == {"name": "nightly", "expr": "0 0 4 * * *", "branch": "main"}
    out = obj.emitter.emitted[0]
    assert out["recreated"] is True
    assert "history is gone" in out["note"]
    assert all("T04:00:00" in t for t in out["next_fire_times"])


def test_update_recreate_carries_the_old_branch_over():
    """DELETE+POST is a create: anything not resent is LOST. Dropping the branch
    would repoint the schedule at the default branch, silently."""
    client = FakeClient({
        ("GET", "repos/o/n/cron/nightly"): {**CRON, "branch": "release"},
        ("DELETE", "repos/o/n/cron/nightly"): None,
        ("POST", "repos/o/n/cron"): {**CRON, "branch": "release"},
    })
    ctx, obj = mkctx(client)
    cmd.update(ctx, name="nightly", repo="o/n", expr=None, at="4am daily", every=None,
               preset=None, branch=None, disabled=None, rename=None, recreate=True)
    assert client.calls_json["branch"] == "release"


def test_update_needs_something_to_change():
    client = FakeClient()
    ctx, obj = mkctx(client)
    with pytest.raises(ValidationError):
        cmd.update(ctx, name="nightly", repo="o/n", expr=None, at=None, every=None, preset=None,
                   branch=None, disabled=None, rename=None, recreate=False)
    assert client.calls == []


def test_rm_without_yes_is_refused_when_not_interactive():
    client = FakeClient()
    ctx, obj = mkctx(client, interactive=False)
    with pytest.raises(OpError) as exc:
        cmd.rm(ctx, name="nightly", repo="o/n", yes=False)
    assert "--disable" in str(exc.value), "offer the non-destructive alternative"
    assert client.calls == []


def test_rm_with_yes_deletes():
    client = FakeClient({("DELETE", "repos/o/n/cron/nightly"): None})
    ctx, obj = mkctx(client, interactive=False)
    cmd.rm(ctx, name="nightly", repo="o/n", yes=True)
    assert client.calls == [("DELETE", "repos/o/n/cron/nightly")]
    assert obj.emitter.emitted[0] == {"status": "deleted", "repo": "o/n", "cron": "nightly"}


# ---- the reserved-globals rule, for this module only -------------------

def test_no_command_here_declares_a_reserved_global():
    """--output/-o/--format/-f/--fields/--columns/--dry-run/--stream are stripped
    from argv before Click sees them, so a command declaring one can never
    receive it — it silently gets the default. (A tree-wide test enforces this;
    this one keeps the failure local while cron.py is being written.)"""
    import inspect

    reserved = {"--output", "-o", "--format", "-f", "--fields", "--columns",
                "--dry-run", "--stream", "--no-context"}
    for command_info in cmd.app.registered_commands:
        for param in inspect.signature(command_info.callback).parameters.values():
            decls = set(getattr(param.default, "param_decls", None) or ())
            assert not (decls & reserved), f"{command_info.callback.__name__} declares {decls & reserved}"
