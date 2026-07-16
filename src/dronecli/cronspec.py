"""Cron expressions for Drone — pure logic, no server, no clock, no I/O.

**Drone's cron is SECONDS-FIRST.** It parses with robfig/cron v1, whose spec is
six fields:

    SECOND MINUTE HOUR DAY-OF-MONTH MONTH DAY-OF-WEEK
      0-59   0-59  0-23     1-31      1-12     0-6

The trap this module exists for: the ordinary 5-field crontab line ``0 3 * * *``
is **accepted** by that parser — it just shifts every field left by one and reads
as ``second=0 minute=3 hour=*``, i.e. **every hour at :03**, not daily at 03:00.
Nothing warns. Nothing 400s. The schedule is simply 24x wrong, forever, and the
API cannot even show you: ``next`` is computed server-side only *after* the cron
is persisted. Detecting that is the highest-value thing this CLI does with crons,
so the logic lives here — pure and fully unit-testable — rather than in a command.

``next_fire_times`` takes ``now`` as a required parameter. That is not a style
preference: a schedule preview that reads the wall clock cannot be tested, and an
untested cron preview is worse than none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import ValidationError

#: How far ahead we are willing to search before declaring a schedule dead.
#: Five years comfortably covers every legal periodic expression (the rarest,
#: ``0 0 0 29 2 *``, hits within four) while making an impossible one — ``0 0 0
#: 30 2 *`` — terminate with an answer instead of spinning.
_HORIZON_DAYS = 366 * 5

FIELD_NAMES = ("second", "minute", "hour", "dom", "month", "dow")

_BOUNDS = {
    "second": (0, 59),
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),
}

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

_DOW_LONG = {
    "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3,
    "thursday": 4, "friday": 5, "saturday": 6,
}

#: robfig/cron v1's descriptors, expanded to the six-field form it expands them
#: to internally. Note ``@weekly`` is Sunday (dow=0), matching crontab.
MACROS = {
    "@yearly": "0 0 0 1 1 *",
    "@annually": "0 0 0 1 1 *",
    "@monthly": "0 0 0 1 * *",
    "@weekly": "0 0 0 * * 0",
    "@daily": "0 0 0 * * *",
    "@midnight": "0 0 0 * * *",
    "@hourly": "0 0 * * * *",
}

#: Named schedules for `--preset`. Kept separate from MACROS: these are ours to
#: choose (an agent asking for "nightly" wants a sane hour, not 00:00 exactly on
#: the hour every server hits at once), MACROS are Drone's to define.
PRESETS = {
    "nightly": "0 0 3 * * *",       # 03:00 — after midnight batch jobs, before work
    "hourly": "0 0 * * * *",        # on the hour
    "daily": "0 0 0 * * *",         # midnight
    "midnight": "0 0 0 * * *",
    "weekly": "0 0 0 * * 0",        # Sunday 00:00
    "monthly": "0 0 0 1 * *",       # 1st of the month, 00:00
    "yearly": "0 0 0 1 1 *",
    "every-15m": "0 */15 * * * *",
    "every-5m": "0 */5 * * * *",
    "workdays": "0 0 3 * * 1-5",    # 03:00, Mon-Fri
}


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------


def field_count(expr: str) -> int:
    """Number of whitespace-separated fields, or 0 for a macro/@every."""
    text = (expr or "").strip()
    if not text or text.startswith("@"):
        return 0
    return len(text.split())


def looks_like_5_field(expr: str) -> bool:
    """True when *expr* is a standard crontab line that Drone will MISREAD.

    This is the whole point of the module. Five fields is not a syntax error to
    Drone — it is a silently different schedule, because robfig v1 treats the
    trailing day-of-week as optional and left-aligns what it gets against
    SECOND. So the check is purely structural: five fields, no macro.
    """
    return field_count(expr) == 5


def to_6_field(expr: str) -> str:
    """The 6-field expression a 5-field crontab line was *meant* to be.

    Prefixing ``0 `` pins seconds to :00 and slides every other field back into
    the position the author intended.
    """
    if not looks_like_5_field(expr):
        raise ValidationError(f"{expr!r} is not a 5-field crontab expression.")
    return "0 " + " ".join(expr.split())


def misread_as(expr: str) -> str:
    """How Drone would actually read a 5-field *expr*, in prose.

    Built by shifting the fields onto Drone's names — which is exactly what the
    server does, so this is a description of real behaviour, not a warning.
    """
    parts = expr.split()
    if len(parts) != 5:
        raise ValidationError(f"{expr!r} is not a 5-field crontab expression.")
    return ", ".join(f"{name}={val}" for name, val in zip(FIELD_NAMES, parts + ["*"]))


def explain_5_field(expr: str) -> dict:
    """A full report on a misread 5-field expression, for an error's `detail`."""
    fixed = to_6_field(expr)
    return {
        "given": " ".join(expr.split()),
        "fields_given": 5,
        "fields_drone_expects": 6,
        "drone_would_read_it_as": misread_as(expr),
        "correct_6_field": fixed,
    }


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CronSchedule:
    """A parsed 6-field expression as sets of permitted values."""

    second: frozenset
    minute: frozenset
    hour: frozenset
    dom: frozenset
    month: frozenset
    dow: frozenset
    dom_restricted: bool
    dow_restricted: bool

    def day_matches(self, when: datetime) -> bool:
        """Crontab's OR rule for the two day fields.

        When BOTH day-of-month and day-of-week are restricted they are ORed, not
        ANDed — ``0 0 0 1 * 1`` means "the 1st, and every Monday". Getting this
        backwards is the classic cron bug; robfig v1 follows the OR rule.
        """
        # Python's Monday=0 vs cron's Sunday=0.
        dow = (when.weekday() + 1) % 7
        if not self.dom_restricted and not self.dow_restricted:
            return True
        if not self.dow_restricted:
            return when.day in self.dom
        if not self.dom_restricted:
            return dow in self.dow
        return when.day in self.dom or dow in self.dow


@dataclass(frozen=True)
class EverySchedule:
    """``@every 1h30m`` — a fixed interval, not a wall-clock pattern."""

    delta: timedelta


def _parse_int(token: str, field: str, names: dict | None) -> int:
    key = token.strip().lower()
    if names and key in names:
        return names[key]
    if not key.isdigit():
        raise ValidationError(
            f"bad value {token!r} in the {field} field: expected a number"
            + (f" or a name ({', '.join(sorted(names))})" if names else "")
            + "."
        )
    return int(key)


def _parse_field(text: str, field: str) -> tuple[frozenset, bool]:
    """One field -> (permitted values, is_restricted).

    Supports the subset robfig v1 supports and we are willing to stand behind:
    ``*``, ``n``, ``a-b``, ``a,b,c``, ``*/k`` and ``a-b/k``, plus month/day
    names. Anything else raises rather than guessing — a preview that quietly
    drops a field it did not understand would be the same class of lie as the
    5-field bug.
    """
    lo, hi = _BOUNDS[field]
    names = _MONTH_NAMES if field == "month" else (_DOW_NAMES if field == "dow" else None)
    text = text.strip()
    if not text:
        raise ValidationError(f"empty {field} field.")

    restricted = text != "*"
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValidationError(f"empty item in the {field} field ({text!r}).")
        rng, sep, step_text = part.partition("/")
        step = 1
        if sep:
            if not step_text.strip().isdigit() or int(step_text) < 1:
                raise ValidationError(f"bad step {step_text!r} in the {field} field.")
            step = int(step_text)
        rng = rng.strip()
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start = _parse_int(a, field, names)
            end = _parse_int(b, field, names)
        else:
            start = end = _parse_int(rng, field, names)
            if sep:  # `5/15` means "from 5, every 15" -- robfig accepts it.
                end = hi
        if start > end:
            raise ValidationError(f"reversed range {rng!r} in the {field} field.")
        for v in (start, end):
            if not lo <= v <= hi:
                # dow=7 is the loudest case: crontab users write it for Sunday,
                # robfig v1 rejects it. Accepting it here would preview a
                # schedule the server will refuse -- the opposite of our job.
                raise ValidationError(
                    f"{v} is out of range for the {field} field (allowed {lo}-{hi})."
                    + (" Drone's parser does not accept 7 for Sunday — use 0." if field == "dow" and v == 7 else "")
                )
        values.update(range(start, end + 1, step))
    if not values:
        raise ValidationError(f"the {field} field {text!r} matches nothing.")
    return frozenset(values), restricted


_DURATION_RE = re.compile(r"(\d+)\s*(h|m|s)", re.I)


def parse_duration(text: str) -> timedelta:
    """``1h30m`` / ``15m`` / ``90s`` / ``2h`` -> timedelta (Go's duration subset)."""
    t = "".join((text or "").split()).lower()
    if not re.fullmatch(r"(?:\d+[hms])+", t or ""):
        raise ValidationError(f"bad duration {text!r} — expected e.g. 15m, 1h30m, 90s.")
    total = sum(int(num) * {"h": 3600, "m": 60, "s": 1}[unit] for num, unit in _DURATION_RE.findall(t))
    if total <= 0:
        raise ValidationError(f"duration {text!r} is zero — a schedule must advance.")
    return timedelta(seconds=total)


def parse(expr: str) -> CronSchedule | EverySchedule:
    """Parse a Drone cron expression. Raises ValidationError with the fix."""
    text = " ".join((expr or "").split())
    if not text:
        raise ValidationError("an expression is required, e.g. '0 0 3 * * *' (daily at 03:00).")

    low = text.lower()
    if low.startswith("@every"):
        return EverySchedule(parse_duration(low[len("@every"):]))
    if low.startswith("@"):
        macro = MACROS.get(low)
        if macro is None:
            raise ValidationError(
                f"unknown descriptor {text!r}. Known: {', '.join(sorted(MACROS))}, @every <duration>."
            )
        text = macro

    parts = text.split()
    if len(parts) == 5:
        raise ValidationError(
            f"{text!r} is a 5-field crontab expression, but Drone's cron is SECONDS-FIRST "
            f"(second minute hour dom month dow). Drone would accept this and read it as "
            f"{misread_as(text)} — not what you meant. Use {to_6_field(text)!r}.",
            detail=explain_5_field(text),
        )
    if len(parts) != 6:
        raise ValidationError(
            f"expected 6 fields (second minute hour dom month dow), got {len(parts)} in {text!r}. "
            f"Example: '0 0 3 * * *' = every day at 03:00:00."
        )

    fields = {}
    restricted = {}
    for name, raw in zip(FIELD_NAMES, parts):
        fields[name], restricted[name] = _parse_field(raw, name)
    return CronSchedule(
        second=fields["second"],
        minute=fields["minute"],
        hour=fields["hour"],
        dom=fields["dom"],
        month=fields["month"],
        dow=fields["dow"],
        dom_restricted=restricted["dom"],
        dow_restricted=restricted["dow"],
    )


# ---------------------------------------------------------------------------
# the preview
# ---------------------------------------------------------------------------


def _bump_month(when: datetime) -> datetime:
    year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
    return when.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_after(sched: CronSchedule, start: datetime) -> datetime | None:
    """First fire strictly after *start*.

    Field-by-field with resets rather than ticking one second at a time: a
    second-by-second scan of ``0 0 0 1 1 *`` is 31 million iterations per answer.
    """
    when = start.replace(microsecond=0) + timedelta(seconds=1)
    horizon = start + timedelta(days=_HORIZON_DAYS)
    while when <= horizon:
        if when.month not in sched.month:
            when = _bump_month(when)
            continue
        if not sched.day_matches(when):
            when = (when + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            continue
        if when.hour not in sched.hour:
            when = (when + timedelta(hours=1)).replace(minute=0, second=0)
            continue
        if when.minute not in sched.minute:
            when = (when + timedelta(minutes=1)).replace(second=0)
            continue
        if when.second not in sched.second:
            when = when + timedelta(seconds=1)
            continue
        return when
    return None


def next_fire_times(expr: str, n: int = 5, *, now: datetime) -> list[datetime]:
    """The next *n* times *expr* fires after *now*.

    ``now`` is required and never read from the clock — the whole feature is
    "show me what this schedule will really do", and a function that consults the
    wall clock cannot be pinned by a test.

    Returned datetimes carry whatever tzinfo *now* carries. Drone evaluates crons
    in the **server's** timezone, so callers should hand in a time in that zone
    (UTC for a stock container) rather than assume the operator's.
    """
    if n < 1:
        raise ValidationError("n must be at least 1.")
    sched = parse(expr)

    if isinstance(sched, EverySchedule):
        # @every is relative to when the schedule was registered, so this is a
        # projection from `now`, not from an anchor we can know.
        return [now.replace(microsecond=0) + sched.delta * (i + 1) for i in range(n)]

    out: list[datetime] = []
    cursor = now
    for _ in range(n):
        nxt = _next_after(sched, cursor)
        if nxt is None:
            if out:
                break
            raise ValidationError(
                f"{expr!r} never fires — no matching date within {_HORIZON_DAYS // 366} years. "
                f"(A date like Feb 30 is a legal expression that can never occur.)"
            )
        out.append(nxt)
        cursor = nxt
    return out


# ---------------------------------------------------------------------------
# human input
# ---------------------------------------------------------------------------

_EVERY_RE = re.compile(
    r"^(?:every\s+)?(\d+)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$"
)
_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?![\d:])")


def _unit(word: str) -> str:
    w = word.lower()
    if w.startswith("s"):
        return "s"
    if w.startswith("d"):
        return "d"
    if w.startswith("h"):
        return "h"
    return "m"


def from_human(text: str) -> str:
    """``"3am daily"`` / ``"every 15m"`` / ``"nightly"`` -> a correct 6-field expr.

    Exists so no one — human or agent — ever hand-assembles the six fields and
    lands on the 5-field trap. Everything it returns is 6-field by construction.
    Anything it cannot parse raises rather than approximating: a schedule that is
    *nearly* what you asked for is the failure mode this module was written to
    eliminate.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValidationError("nothing to interpret — try '3am daily', 'every 15m', 'nightly'.")
    low = " ".join(raw.lower().split())

    if low in PRESETS:
        return PRESETS[low]
    if low.startswith("@"):
        parse(low)  # validate; raises with a good message
        return MACROS.get(low, low)

    # "every 15m" / "every 2 hours" / "15m"
    m = _EVERY_RE.match(low)
    if m:
        count, unit = int(m.group(1)), _unit(m.group(2))
        if count < 1:
            raise ValidationError(f"interval must be at least 1, got {count}.")
        if unit == "s":
            return f"*/{count} * * * * *"
        if unit == "m":
            return f"0 */{count} * * * *"
        if unit == "h":
            return f"0 0 */{count} * * *"
        return f"0 0 0 */{count} * *"  # days: day-of-month stepping, per crontab

    # "every monday at 9am", "3am daily", "daily at 03:30", "weekdays at 6am"
    dow_field = "*"
    for name, num in {**_DOW_LONG, **_DOW_NAMES}.items():
        if re.search(rf"\b{name}s?\b", low):
            dow_field = str(num)
            break
    else:
        if re.search(r"\b(weekdays?|workdays?|business days?)\b", low):
            dow_field = "1-5"
        elif re.search(r"\bweekends?\b", low):
            dow_field = "0,6"

    if re.search(r"\b(midnight)\b", low):
        return f"0 0 0 * * {dow_field}"
    if re.search(r"\b(noon|midday)\b", low):
        return f"0 0 12 * * {dow_field}"

    tm = _TIME_RE.search(low)
    periodic = bool(
        re.search(r"\b(daily|nightly|every day|each day|weekly|every week)\b", low)
    ) or dow_field != "*"
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
        ampm = tm.group(3)
        if ampm:
            if hour < 1 or hour > 12:
                raise ValidationError(f"{hour}{ampm} is not a valid 12-hour time.")
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValidationError(f"{raw!r} is not a valid time of day.")
        if not periodic and not re.search(r"\bat\b|am|pm|:", low):
            raise ValidationError(
                f"could not interpret {raw!r}. Try '3am daily', 'daily at 03:30', "
                f"'every monday at 9am', 'every 15m', or a preset: {', '.join(sorted(PRESETS))}."
            )
        return f"0 {minute} {hour} * * {dow_field}"

    if re.search(r"\b(daily|nightly)\b", low):
        return PRESETS["nightly"] if "nightly" in low else PRESETS["daily"]
    if re.search(r"\b(hourly|every hour)\b", low):
        return PRESETS["hourly"]
    if re.search(r"\b(weekly|every week)\b", low):
        return PRESETS["weekly"]
    if re.search(r"\b(monthly|every month)\b", low):
        return PRESETS["monthly"]

    raise ValidationError(
        f"could not interpret {raw!r}. Try '3am daily', 'daily at 03:30', "
        f"'every monday at 9am', 'every 15m', or a preset: {', '.join(sorted(PRESETS))}."
    )


def describe(expr: str) -> str:
    """A short, honest gloss of what an expression does. Best-effort, never lies."""
    text = " ".join((expr or "").split())
    sched = parse(text)
    if isinstance(sched, EverySchedule):
        return f"every {int(sched.delta.total_seconds())}s, counted from when the cron is created"
    parts = text.lower().split() if not text.startswith("@") else MACROS[text.lower()].split()
    sec, minute, hour = parts[0], parts[1], parts[2]
    if hour == "*" and minute.isdigit() and sec.isdigit():
        return f"every hour at {int(minute):02d}:{int(sec):02d}"
    if hour.isdigit() and minute.isdigit() and sec.isdigit():
        return f"at {int(hour):02d}:{int(minute):02d}:{int(sec):02d} on matching days"
    return "see the next fire times"
