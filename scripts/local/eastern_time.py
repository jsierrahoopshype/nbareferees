"""
eastern_time.py  --  UTC instant -> the local calendar date an NBA game was
played on. No timezone database, no third-party dependency.

  python scripts/local/eastern_time.py      # runs the self-test

WHY THIS EXISTS AT ALL, RATHER THAN tz_convert OR ZoneInfo.

Both of the obvious answers fail on the machine these scripts actually run on:

  ZoneInfo("America/New_York")   needs the IANA database. Windows does not
                                 ship one, so this raises there unless the
                                 `tzdata` package happens to be installed.

  pandas .tz_convert("US/Eastern")
                                 pandas 3 resolves zone names through zoneinfo
                                 rather than the pytz it used to bundle, so it
                                 inherits the same problem -- AND "US/Eastern"
                                 is a legacy pytz alias that zoneinfo does not
                                 carry even where the database IS present. It
                                 raises ZoneInfoNotFoundError on a stock
                                 install. That was shipped once, inside a
                                 try/except whose fallback was the UTC date,
                                 so it silently reinstated the exact defect it
                                 was written to fix. Hence this module, and
                                 hence it raises rather than falling back.

WHY EASTERN IS THE RIGHT TARGET, AND WHY NO PER-ARENA TABLE IS NEEDED. No NBA
game tips after midnight Eastern -- the latest start is about 10:30pm ET, which
is 7:30pm on the west coast -- so for every game in the league the Eastern
calendar date and the home arena's calendar date are the same day. Converting
to Eastern therefore yields the arena-local date without knowing where the
arena is, and it matches what stats.nba.com's GAME_DATE holds, so both eras of
games.csv.gz land on one convention. No relocations to track, and no special
case for Arizona not observing daylight saving.

THE TWO DAYLIGHT-SAVING RULES. This repo covers 1993-94 onward, which straddles
a change in US law, and getting it wrong moves a date whenever a tip lands
within an hour of midnight Eastern:

  1987-2006   DST begins the FIRST Sunday in April, ends the LAST Sunday in
              October.
  2007-       DST begins the SECOND Sunday in March, ends the FIRST Sunday in
              November.  (Energy Policy Act of 2005.)

Both are implemented. Anything before 1987 raises rather than being converted
under a rule that did not apply to it.
"""

import re
import datetime

DST_RULE_FLOOR_YEAR = 1987
NEW_RULE_FROM_YEAR = 2007


class EasternTimeError(ValueError):
    """A timestamp that cannot be converted. Never swallow this into a UTC date."""


def _nth_weekday(year, month, weekday, n):
    """The nth given weekday of a month, n starting at 1. weekday: Mon=0..Sun=6."""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    """The last given weekday of a month."""
    if month == 12:
        nxt = datetime.date(year + 1, 1, 1)
    else:
        nxt = datetime.date(year, month + 1, 1)
    last = nxt - datetime.timedelta(days=1)
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


SUNDAY = 6


def dst_bounds_utc(year):
    """(start, end) of Eastern daylight time that year, as naive UTC datetimes.

    Expressed in UTC because that is what they are compared against: DST begins
    2:00am local STANDARD time (07:00 UTC) and ends 2:00am local DAYLIGHT time
    (06:00 UTC).
    """
    if year < DST_RULE_FLOOR_YEAR:
        raise EasternTimeError(
            "year %d predates the %d daylight-saving rules implemented here"
            % (year, DST_RULE_FLOOR_YEAR))
    if year >= NEW_RULE_FROM_YEAR:
        start_day = _nth_weekday(year, 3, SUNDAY, 2)      # second Sunday in March
        end_day = _nth_weekday(year, 11, SUNDAY, 1)       # first Sunday in November
    else:
        start_day = _nth_weekday(year, 4, SUNDAY, 1)      # first Sunday in April
        end_day = _last_weekday(year, 10, SUNDAY)         # last Sunday in October
    return (datetime.datetime.combine(start_day, datetime.time(7, 0)),
            datetime.datetime.combine(end_day, datetime.time(6, 0)))


def eastern_offset_hours(utc_dt):
    """-4 during Eastern daylight time, -5 during Eastern standard time."""
    start, end = dst_bounds_utc(utc_dt.year)
    return -4 if start <= utc_dt < end else -5


# One regex for every shape these feeds actually emit. Written out rather than
# leaning on fromisoformat, whose tolerance for "Z" and for odd fraction widths
# varies by Python version.
#
#   date       1993-11-06                  (refused -- see below)
#   separator  T or a space, either case
#   time       00:30      SECONDS OPTIONAL -- the 1990s feed omits them
#              00:30:00
#   fraction   .9 .999 .999999             any width
#   zone       Z  z  +00:00  -05:00  -0500  +00   or absent
_TS_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})"                 # 1 y   2 m   3 d
    r"(?:[Tt ]"
    r"(\d{2}):(\d{2})(?::(\d{2}))?"                 # 4 H   5 M   6 S (optional)
    r"(?:[.,]\d+)?"                                  # fractional seconds, discarded
    r"\s*([Zz]|[+-]\d{2}:?(?:\d{2})?)?"             # 7 zone (optional)
    r")?\s*$"
)


def parse_utc(ts):
    """A naive UTC datetime from an ISO-8601 timestamp, or None if unparseable.

    Accepts every shape ESPN has been seen to emit across 1993 to today. The
    seconds-less form is the one that matters: ESPN's 1990s archive emits
    `1993-11-06T00:30Z`, and an earlier version of this function rejected it
    outright on a length check, so a whole re-walk recovered nothing.

    A DATE-ONLY value is refused, not defaulted. `1993-11-06` carries no time
    of day, so there is no instant to convert and no way to know which local
    date it belongs to. Returning midnight would silently produce a date that
    might be right and might be a day out, which is the class of bug this
    module exists to prevent.

    A timestamp with NO zone is read as UTC. This field is UTC by contract and
    every sample carries Z; refusing it would mean recovering nothing from a
    43-minute walk over a shape that is almost certainly UTC anyway. `--dates-
    only` prints a census of the shapes it actually saw, so if a bare form ever
    does turn up it is visible rather than assumed away.
    """
    m = _TS_RE.match(ts or "")
    if not m:
        return None
    year, month, day, hh, mm, ss, zone = m.groups()
    if hh is None:
        return None                         # date-only: no instant to convert
    try:
        base = datetime.datetime(int(year), int(month), int(day),
                                 int(hh), int(mm), int(ss or 0))
    except ValueError:                      # e.g. month 13, day 32, hour 25
        return None
    if zone is None or zone in ("Z", "z"):
        return base
    sign = 1 if zone[0] == "+" else -1
    digits = zone[1:].replace(":", "")
    try:
        off_h = int(digits[0:2])
        off_m = int(digits[2:4]) if len(digits) >= 4 else 0
    except ValueError:
        return None
    return base - sign * datetime.timedelta(hours=off_h, minutes=off_m)


def is_date_only(ts):
    """True for a well-formed date with no time of day, e.g. '1993-11-06'."""
    m = _TS_RE.match(ts or "")
    return bool(m) and m.group(4) is None


def shape_of(ts):
    """A timestamp's SHAPE, digits blanked to 9: '1993-11-06T00:30Z' ->
    '9999-99-99T99:99Z'. Used to census what a feed actually emits."""
    return re.sub(r"\d", "9", (ts or "").strip()) or "<empty>"


def local_date_from_utc(utc_dt):
    """The Eastern calendar date of a UTC instant."""
    return (utc_dt + datetime.timedelta(hours=eastern_offset_hours(utc_dt))).date()


def game_date(ts):
    """ISO game date from an ISO-8601 UTC timestamp. Raises, never guesses."""
    dt = parse_utc(ts)
    if dt is None:
        if is_date_only(ts):
            raise EasternTimeError(
                "date-only value with no time of day, so the local date cannot "
                "be derived: %r" % (ts,))
        raise EasternTimeError("unparseable timestamp: %r" % (ts,))
    return local_date_from_utc(dt).isoformat()


# --------------------------------------------------------------------------- #
SELF_TEST = [
    # (timestamp, expected Eastern date, what it is)
    ("2023-12-25T17:00:00Z", "2023-12-25", "MIL@NYK noon ET, EST, no rollover"),
    ("2023-12-26T01:00:00Z", "2023-12-25", "PHI@MIA 8pm ET, EST, rolled over"),
    ("2023-12-26T03:30:00Z", "2023-12-25", "DAL@PHX 10:30pm ET, EST, rolled over"),
    ("2026-06-14T00:30:00Z", "2026-06-13", "Finals, EDT, rolled over"),
    # modern rule boundaries
    ("2024-03-10T06:59:00Z", "2024-03-10", "one minute before EDT begins (2007 rule)"),
    ("2024-03-10T07:00:00Z", "2024-03-10", "the instant EDT begins (2007 rule)"),
    ("2024-11-03T05:59:00Z", "2024-11-03", "one minute before EST returns"),
    ("2024-11-03T06:00:00Z", "2024-11-03", "the instant EST returns"),
    # pre-2007 rule: 1996 DST ran Apr 7 -> Oct 27
    ("1996-04-07T06:59:00Z", "1996-04-07", "1996: just before EDT begins (Apr 7)"),
    ("1996-04-07T07:00:00Z", "1996-04-07", "1996: the instant EDT begins"),
    ("1996-10-27T05:59:00Z", "1996-10-27", "1996: just before EST returns (Oct 27)"),
    ("1996-10-27T06:00:00Z", "1996-10-27", "1996: the instant EST returns"),
    # the case the two rules disagree about: late March 1996 is still EST, so a
    # 00:30Z tip is 7:30pm on the 30th. Under the 2007 rule it would be EDT and
    # 8:30pm -- same date here, but the rule is still the one being exercised.
    ("1996-03-31T00:30:00Z", "1996-03-30", "1996-03-30 evening, EST under the old rule"),
    ("2012-03-31T00:30:00Z", "2012-03-30", "2012-03-30 evening, EDT under the new rule"),
    # midnight ET either side
    ("2024-01-01T04:59:00Z", "2023-12-31", "11:59pm ET New Year's Eve"),
    ("2024-01-01T05:00:00Z", "2024-01-01", "midnight ET New Year"),
    # ---- shape variants -------------------------------------------------- #
    # The 1990s ESPN archive omits seconds. An earlier parser rejected this
    # form on a length check, so a full re-walk recovered nothing at all.
    ("1993-11-06T00:30Z", "1993-11-05", "NO SECONDS: 7:30pm ET tip, rolled over"),
    ("1993-11-06T00:30:00Z", "1993-11-05", "same instant with seconds, must agree"),
    ("1996-01-20T18:00Z", "1996-01-20", "NO SECONDS: 1pm ET afternoon, no rollover"),
    ("1999-02-05T01:00Z", "1999-02-04", "NO SECONDS: 8pm ET tip, rolled over"),
    ("2002-04-14T23:00Z", "2002-04-14", "NO SECONDS: 7pm EDT, no rollover"),
    ("2023-10-25T00:10:41.9Z", "2023-10-24", "fractional seconds, one digit"),
    ("2023-10-25T00:10:41.123456Z", "2023-10-24", "fractional seconds, six digits"),
    ("2023-10-25T00:10:41,5Z", "2023-10-24", "comma as the decimal separator"),
    ("2023-10-25T00:10.5Z", "2023-10-24", "fractional MINUTES, no seconds field"),
    ("2023-10-24T20:10:41-04:00", "2023-10-24", "explicit offset instead of Z"),
    ("2023-10-24T20:10-04:00", "2023-10-24", "explicit offset AND no seconds"),
    ("1993-11-05T19:30-05:00", "1993-11-05", "1990s shape with an EST offset"),
    ("1993-11-05T19:30-0500", "1993-11-05", "offset without the colon"),
    ("2023-10-25T00:10:41+00:00", "2023-10-24", "+00:00 rather than Z"),
    ("2023-10-25T00:10:41+00", "2023-10-24", "hours-only offset"),
    ("2023-10-25t00:10:41z", "2023-10-24", "lowercase t and z"),
    ("2023-10-25 00:10:41Z", "2023-10-24", "space instead of T"),
    ("  2023-10-25T00:10:41Z  ", "2023-10-24", "surrounding whitespace"),
    ("1993-11-06T00:30", "1993-11-05", "no zone at all, read as UTC"),
]

# Values that MUST be refused. Every one of these would otherwise produce a
# date that could silently be a day out.
SELF_TEST_REFUSE = [
    ("1993-11-06", "date-only: no time of day, so no local date exists"),
    ("1993-11", "a month is not a date"),
    ("", "empty"),
    (None, "missing"),
    ("not a timestamp", "garbage"),
    ("1993-13-06T00:30Z", "month 13"),
    ("1993-11-31T00:30Z", "November has 30 days"),
    ("1993-11-06T25:30Z", "hour 25"),
    ("1993-11-06T00:30Q", "unknown zone letter"),
]


def self_test(verbose=True):
    bad = 0
    for ts, expected, why in SELF_TEST:
        try:
            got = game_date(ts)
        except EasternTimeError as exc:
            got = "RAISED %s" % exc
        ok = got == expected
        bad += not ok
        if verbose:
            print("  %-28s -> %-12s %-6s %s" % (ts, got, "ok" if ok else "FAIL", why))
    # the DST bounds themselves, against dates that are a matter of record
    checks = [
        (1996, datetime.date(1996, 4, 7), datetime.date(1996, 10, 27)),
        (2006, datetime.date(2006, 4, 2), datetime.date(2006, 10, 29)),
        (2007, datetime.date(2007, 3, 11), datetime.date(2007, 11, 4)),
        (2024, datetime.date(2024, 3, 10), datetime.date(2024, 11, 3)),
    ]
    for year, want_start, want_end in checks:
        s, e = dst_bounds_utc(year)
        ok = s.date() == want_start and e.date() == want_end
        bad += not ok
        if verbose:
            print("  DST %d: %s -> %s  %s" % (year, s.date(), e.date(),
                                              "ok" if ok else "FAIL expected %s -> %s"
                                              % (want_start, want_end)))
    try:
        dst_bounds_utc(1986)
        bad += 1
        if verbose:
            print("  FAIL: 1986 was accepted")
    except EasternTimeError:
        if verbose:
            print("  pre-1987 correctly refused")
    for ts, why in SELF_TEST_REFUSE:
        try:
            got = game_date(ts)
            bad += 1
            if verbose:
                print("  REFUSE %-22r -> FAIL, returned %s (%s)" % (ts, got, why))
        except EasternTimeError:
            if verbose:
                print("  REFUSE %-22r -> ok, raises (%s)" % (ts, why))
    if verbose:
        print("\n%d case(s) failed." % bad)
    return bad == 0


if __name__ == "__main__":
    raise SystemExit(0 if self_test() else 1)
