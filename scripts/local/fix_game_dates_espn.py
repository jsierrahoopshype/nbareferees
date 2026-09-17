"""
fix_game_dates_espn.py  --  LOCAL, INGEST-SIDE fix for the UTC-vs-local
game_date defect in source-data/games.csv.gz.

  python scripts\\local\\fix_game_dates_espn.py            (dry run, writes nothing)
  python scripts\\local\\fix_game_dates_espn.py --apply    (rewrites games.csv.gz)
  python scripts\\local\\fix_game_dates_espn.py --self-test (date arithmetic only)

THE DEFECT, established by scripts/local/probe_game_date_timezone.py. Every row
in games.csv.gz that came from the ESPN feed carries the UTC calendar date of
the tip-off instant, not the local date the game was played on. The cause is a
single line in fetch_espn_seasons.py:

    game_date = pd.to_datetime(raw_date, utc=True, ...).strftime("%Y-%m-%d")

ESPN's event.date is a UTC instant. Formatting it in UTC files an 8:00pm ET
game under the following day. 17,349 rows across 14 seasons are affected; the
probe's fitted rollover rate puts the large majority of them on the wrong day.

WHAT THIS SCRIPT FIXES, AND WHAT IT DOES NOT. It corrects the 2023-24 through
2025-26 rows -- the ~3,941 games the game-id bridge covers -- exactly, from a
real timestamp, and marks every other row for what it is. It does NOT fix the
1993-94..2002-03 and 2012-13 blocks: no timestamp for those games exists
anywhere in this repo, so correcting them needs fetch_espn_seasons.py re-run
with the date bug fixed. Those rows come out of here honestly labelled
date_is_local=0 rather than silently left looking correct.

WHY EASTERN TIME IS THE RIGHT CONVERSION, AND WHY NO PER-TEAM TIMEZONE TABLE
IS NEEDED. The obvious implementation converts each tip-off into the HOME
ARENA's zone. That turns out to be unnecessary: no NBA game tips after midnight
Eastern -- the latest regular start is about 10:30pm ET, which is 7:30pm on the
west coast -- so for every game in the league the Eastern calendar date and the
home arena's calendar date are the SAME day. Converting to Eastern therefore
gives the arena-local date without needing to know where the arena is, and it
matches what stats.nba.com's own GAME_DATE holds, which is what the 19
NBA-scheme seasons in this file are already dated by. One conversion, one
convention, no table to keep in sync with relocations.

    2023-12-25 22:30 ET  (DAL@PHX, 10:30pm tip)  ->  UTC 2023-12-26 03:30
    back to Eastern                              ->  2023-12-25, correct

NO tzdata DEPENDENCY. zoneinfo needs the IANA database, which Windows does not
ship, so a plain `ZoneInfo("America/New_York")` fails on exactly the machine
this script is meant to run on. The US daylight-saving rule has been fixed
since 2007 -- second Sunday in March to first Sunday in November -- and this
window is 2023-26, so the offset is computed from that rule directly. The
script refuses any timestamp before 2007 rather than apply the rule where it
does not hold.

TIP-OFF IS THE MINIMUM timeActual, NOT THE FIRST ROW ENCOUNTERED AND NOT THE
MAXIMUM. cdn.nba.com play-by-play stamps every action with a real UTC
timestamp, so the earliest one in a game is the tip. The maximum would be the
final buzzer, which for a 10:30pm ET start is after midnight Eastern and would
re-introduce exactly the off-by-one this script exists to remove.

SAFETY. Dry run by default. --apply writes through a temporary file and only
replaces games.csv.gz once the new file has been read back and verified. It
refuses to write at all if it corrected nothing, so a missing archive cache
cannot quietly downgrade the extract to "everything flagged unverified".
"""

import os
import sys
import csv
import gzip
import shutil
import datetime
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data_source as nds  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
BRIDGE_PATH = os.path.join(SOURCE_DIR, "game_id_bridge.csv.gz")
OUT_REPORT = os.path.join(SOURCE_DIR, "_fix_game_dates_espn.txt")

# The cdn archives that carry timeActual, same keys build_game_id_bridge.py uses.
CDN_KEYS = [
    "cdnnba_2023", "cdnnba_po_2023",
    "cdnnba_2024", "cdnnba_po_2024",
    "cdnnba_2025", "cdnnba_po_2025",
]

# The US daylight-saving rule this script relies on took its current form in
# 2007. Refuse rather than silently misapply it to older timestamps.
DST_RULE_FROM_YEAR = 2007

_out = []


def emit(msg=""):
    print(msg)
    _out.append(msg)


def section(title):
    emit()
    emit("=" * 78)
    emit(title)
    emit("=" * 78)


# --------------------------------------------------------------------------- #
# Eastern time, from the rule rather than from a timezone database
# --------------------------------------------------------------------------- #
def _nth_sunday(year, month, n):
    """The nth Sunday of a month, n starting at 1."""
    first = datetime.date(year, month, 1)
    # date.weekday(): Monday 0 ... Sunday 6
    offset = (6 - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def eastern_offset_hours(utc_dt):
    """-4 during Eastern daylight time, -5 during Eastern standard time.

    Boundaries are expressed in UTC: DST begins 2:00am local standard, which
    is 07:00 UTC, and ends 2:00am local daylight, which is 06:00 UTC.
    """
    if utc_dt.year < DST_RULE_FROM_YEAR:
        raise ValueError(
            "timestamp %s predates the %d US daylight-saving rule this script "
            "implements; it must not be converted here" % (utc_dt, DST_RULE_FROM_YEAR))
    y = utc_dt.year
    dst_start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    dst_end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if dst_start <= utc_dt < dst_end else -5


def parse_utc(ts):
    """A naive UTC datetime from an ISO-8601 timestamp, or None.

    Handles the shapes cdn.nba.com actually emits -- a trailing Z, fractional
    seconds of any length, and an explicit +/-HH:MM offset -- without relying
    on fromisoformat, whose tolerance for 'Z' and for odd fraction widths
    varies by Python version.
    """
    s = (ts or "").strip()
    if len(s) < 19:
        return None
    try:
        base = datetime.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]),
                                 int(s[11:13]), int(s[14:16]), int(s[17:19]))
    except (ValueError, IndexError):
        return None
    tail = s[19:]
    # Drop fractional seconds, whatever their width.
    if tail.startswith("."):
        i = 1
        while i < len(tail) and tail[i].isdigit():
            i += 1
        tail = tail[i:]
    if tail in ("", "Z", "z"):
        return base
    if len(tail) >= 6 and tail[0] in "+-" and tail[3] == ":":
        try:
            sign = 1 if tail[0] == "+" else -1
            delta = datetime.timedelta(hours=int(tail[1:3]), minutes=int(tail[4:6]))
        except ValueError:
            return None
        return base - sign * delta          # to UTC
    return None


def local_date_from_utc(utc_dt):
    """The calendar date the game was played on, Eastern."""
    return (utc_dt + datetime.timedelta(hours=eastern_offset_hours(utc_dt))).date()


# --------------------------------------------------------------------------- #
def self_test():
    """Date arithmetic only -- needs neither the cache nor the extract.

    Cases are hand-computed, and deliberately include both sides of each
    daylight-saving boundary and the tip-off times that make the Christmas
    slate split across two UTC days.
    """
    section("SELF-TEST: Eastern conversion")
    cases = [
        # (UTC timestamp, expected local date, what it is)
        ("2023-12-25T17:00:00Z", "2023-12-25", "MIL@NYK 12:00pm ET, EST, no rollover"),
        ("2023-12-25T22:00:00Z", "2023-12-25", "BOS@LAL 5:00pm ET, EST, no rollover"),
        ("2023-12-26T01:00:00Z", "2023-12-25", "PHI@MIA 8:00pm ET, EST, rolled over"),
        ("2023-12-26T03:30:00Z", "2023-12-25", "DAL@PHX 10:30pm ET, EST, rolled over"),
        ("2026-06-14T00:30:00Z", "2026-06-13", "NYK@SAS Finals, EDT, rolled over"),
        ("2024-03-10T06:59:00Z", "2024-03-10", "one minute before EDT begins (EST, -5)"),
        ("2024-03-10T07:00:00Z", "2024-03-10", "the instant EDT begins (-4)"),
        ("2024-11-03T05:59:00Z", "2024-11-03", "one minute before EST returns (EDT, -4)"),
        ("2024-11-03T06:00:00Z", "2024-11-03", "the instant EST returns (-5)"),
        ("2024-01-01T04:59:00Z", "2023-12-31", "New Year's Eve 11:59pm ET"),
        ("2024-01-01T05:00:00Z", "2024-01-01", "New Year midnight ET"),
        ("2023-10-25T00:10:41.9Z", "2023-10-24", "fractional seconds, one digit"),
        ("2023-10-24T20:10:41-04:00", "2023-10-24", "explicit offset instead of Z"),
    ]
    bad = 0
    for ts, expected, why in cases:
        dt = parse_utc(ts)
        got = local_date_from_utc(dt).isoformat() if dt else "(unparsed)"
        ok = got == expected
        bad += not ok
        emit("  %-28s -> %-12s %-8s %s" % (ts, got, "ok" if ok else "FAIL", why))
    emit("")
    try:
        eastern_offset_hours(datetime.datetime(2006, 6, 1))
        emit("  FAIL: a pre-2007 timestamp was accepted")
        bad += 1
    except ValueError:
        emit("  pre-2007 timestamp correctly refused")
    emit("")
    emit("%d case(s) failed." % bad)
    return bad == 0


# --------------------------------------------------------------------------- #
def load_bridge():
    """espn_game_id -> nba_game_id for the bridged window."""
    if not os.path.exists(BRIDGE_PATH):
        return {}
    out = {}
    with gzip.open(BRIDGE_PATH, "rt", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            espn = (rec.get("espn_game_id") or "").strip()
            nba = (rec.get("nba_game_id") or "").strip()
            if espn and nba:
                out[espn] = nba
    return out


def collect_tipoffs(no_download):
    """nba_game_id -> naive UTC datetime of tip-off, from cached cdn archives."""
    section("READING TIP-OFF TIMESTAMPS FROM THE cdn ARCHIVES")
    index = nds.load_index(emit=emit, no_download=no_download)
    tip = {}
    missing = []
    for key in CDN_KEYS:
        path = nds.ensure_dataset(key, index, emit=emit, no_download=no_download)
        if not path:
            missing.append(key)
            continue
        seen = 0
        for row in nds.iter_csv_rows(path):
            gid = (row.get("gameId") or row.get("GAME_ID") or "").strip()
            dt = parse_utc(row.get("timeActual"))
            if not gid or dt is None:
                continue
            gid = gid.zfill(10)
            # Earliest action in the game IS the tip-off. See the module
            # docstring on why the maximum would reintroduce the bug.
            if gid not in tip or dt < tip[gid]:
                tip[gid] = dt
            seen += 1
        emit("  %-18s %7d timestamped row(s), %d game(s) known so far"
             % (key, seen, len(tip)))
    if missing:
        emit("")
        emit("  NOT AVAILABLE: %s" % ", ".join(missing))
        emit("  Games only in those archives cannot be corrected on this run.")
    emit("")
    emit("tip-off timestamps for %d game(s)" % len(tip))
    return tip


def load_games_rows():
    with gzip.open(GAMES_PATH, "rt", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        field_order = list(reader.fieldnames or [])
    return rows, field_order


def is_espn_scheme(gid):
    g = (gid or "").strip()
    return not (len(g) == 10 and g.startswith("00"))


def main():
    args = set(sys.argv[1:])
    apply_changes = "--apply" in args
    no_download = "--no-download" in args

    emit("game_date UTC->local fix (fix_game_dates_espn.py)")
    emit("Run at (local clock): %s"
         % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("mode: %s" % ("APPLY (games.csv.gz will be rewritten)" if apply_changes
                       else "DRY RUN (nothing will be written)"))

    if "--self-test" in args:
        ok = self_test()
        with open(OUT_REPORT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_out) + "\n")
        raise SystemExit(0 if ok else 1)

    if not self_test():
        raise SystemExit("self-test failed; refusing to touch the extract")

    rows, field_order = load_games_rows()
    gid_col = [k for k in field_order if k.endswith("game_id")][0]
    emit("")
    emit("games.csv.gz: %d row(s), columns %s" % (len(rows), field_order))

    bridge = load_bridge()
    emit("bridge: %d espn->nba pairing(s)" % len(bridge))

    tip = collect_tipoffs(no_download)

    section("APPLYING")
    stats = collections.Counter()
    changed_examples = []
    by_season = collections.defaultdict(collections.Counter)

    for r in rows:
        gid = (r[gid_col] or "").strip()
        season = r.get("season") or "?"
        if not is_espn_scheme(gid):
            # stats.nba.com publishes a local GAME_DATE, and the probe's
            # Christmas test found zero Dec-24 games in any of these seasons.
            r["date_is_local"] = "1"
            stats["nba scheme, already local"] += 1
            by_season[season]["already local"] += 1
            continue
        nba_gid = bridge.get(gid)
        dt = tip.get(nba_gid) if nba_gid else None
        if dt is None:
            r["date_is_local"] = "0"
            stats["espn scheme, no timestamp available"] += 1
            by_season[season]["unfixed"] += 1
            continue
        old = r["game_date"]
        new = local_date_from_utc(dt).isoformat()
        r["game_date"] = new
        r["date_is_local"] = "1"
        if new != old:
            stats["espn scheme, date CORRECTED"] += 1
            by_season[season]["corrected"] += 1
            if len(changed_examples) < 10:
                changed_examples.append((gid, season, old, new,
                                         r.get("away_team_abbr"), r.get("home_team_abbr")))
        else:
            stats["espn scheme, date confirmed unchanged"] += 1
            by_season[season]["confirmed"] += 1

    for label, n in stats.most_common():
        emit("  %-42s %6d" % (label, n))

    emit("")
    emit("%-9s %10s %10s %10s %10s" % ("season", "corrected", "confirmed",
                                       "already loc", "unfixed"))
    for season in sorted(by_season):
        c = by_season[season]
        emit("%-9s %10d %10d %10d %10d"
             % (season, c["corrected"], c["confirmed"], c["already local"], c["unfixed"]))

    if changed_examples:
        emit("")
        emit("sample corrections:")
        for gid, season, old, new, away, home in changed_examples:
            emit("  %-10s %-8s %s -> %s   %s @ %s" % (gid, season, old, new, away, home))

    # ---- verification, before anything is written -------------------------
    section("VERIFICATION")
    failures = []

    corrected_seasons = {s for s, c in by_season.items() if c["corrected"] or c["confirmed"]}
    xmas_eve = collections.Counter()
    for r in rows:
        if r.get("date_is_local") != "1":
            continue
        if r["game_date"][5:] == "12-24":
            xmas_eve[r.get("season")] += 1
    emit("Christmas Eve games among rows now claiming a local date: %d"
         % sum(xmas_eve.values()))
    if xmas_eve:
        for s, n in sorted(xmas_eve.items()):
            emit("  %s: %d" % (s, n))
        failures.append("%d local-dated game(s) still fall on December 24"
                        % sum(xmas_eve.values()))
    else:
        emit("  none -- the league plays no games that day, so this is the")
        emit("  signature the defect left behind, and it is gone.")

    if "2023-24" in corrected_seasons:
        xmas = [r for r in rows
                if r.get("season") == "2023-24" and r["game_date"] == "2023-12-25"]
        emit("")
        emit("2023-24 Christmas Day slate after the fix: %d game(s) (expect 5)"
             % len(xmas))
        for r in sorted(xmas, key=lambda x: x[gid_col]):
            emit("  %s @ %s" % (r.get("away_team_abbr"), r.get("home_team_abbr")))
        if len(xmas) != 5:
            failures.append("2023-24 Christmas Day has %d games, expected 5" % len(xmas))

    if not stats["espn scheme, date CORRECTED"]:
        failures.append("nothing was corrected -- refusing to rewrite the extract "
                        "(is source-data/_nba_data_cache populated?)")

    emit("")
    if failures:
        emit("VERIFICATION FAILED")
        for f in failures:
            emit("  FAIL: %s" % f)
    else:
        emit("verification passed")

    # ---- write ------------------------------------------------------------
    section("OUTPUT")
    if not apply_changes:
        emit("Dry run: games.csv.gz untouched. Re-run with --apply to write.")
    elif failures:
        emit("Refusing to write because verification failed.")
    else:
        out_fields = list(field_order)
        if "date_is_local" not in out_fields:
            out_fields.append("date_is_local")
        tmp = GAMES_PATH + ".part"
        backup = GAMES_PATH + ".bak"
        with gzip.open(tmp, "wt", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=out_fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in out_fields})
        # Read the new file back before trusting it.
        with gzip.open(tmp, "rt", encoding="utf-8-sig") as fh:
            check = list(csv.DictReader(fh))
        if len(check) != len(rows):
            os.remove(tmp)
            raise SystemExit("readback mismatch: %d rows written, %d expected"
                             % (len(check), len(rows)))
        shutil.copy2(GAMES_PATH, backup)
        os.replace(tmp, GAMES_PATH)
        emit("wrote %s (%d rows, %d columns)" % (GAMES_PATH, len(check), len(out_fields)))
        emit("previous file kept at %s" % backup)
        emit("")
        emit("NEXT: re-run scripts/build.py, then scripts/render_pages.py.")
        emit("The Christmas Eve QA gate in build.py will now also police this.")

    with open(OUT_REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    print()
    print("-> %s" % OUT_REPORT)
    raise SystemExit(1 if failures and apply_changes else 0)


if __name__ == "__main__":
    main()
