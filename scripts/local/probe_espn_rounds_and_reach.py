"""
probe_espn_rounds_and_reach.py  --  LOCAL, READ-ONLY probe (run by Jorge on Windows).

Two questions, one script. WRITES NOTHING to the extracts (games.csv.gz /
officials.csv.gz / player_logs). It only prints to the console and writes one
additive report:
    source-data/_probe_rounds_reach.txt

It reuses (does not duplicate) the ESPN plumbing from fetch_espn_seasons.py,
exactly like probe_espn_historical.py does:
    get_json, SCOREBOARD_URL, SUMMARY_URL,
    discover_officials_candidates, official_fields, nba_abbr

  python scripts\\local\\probe_espn_rounds_and_reach.py

------------------------------------------------------------------------------
(1) ROUND / GAME LABELING  -- can we recover playoff round + game number from
    ESPN so the known Phase-1 gap (ESPN-era Finals/Game-7 labels) can be filled?
    For 8 fixed playoff dates spanning the four ESPN-sourced eras (2000-01,
    2012-13, 2023-24, 2025-26) it fetches the scoreboard and ONE summary each,
    then dumps every field that plausibly carries series / round / game-number
    info -- event notes, competition notes/series/type, event name/shortName,
    season block, summary header notes/series/type, seasonseries, headlines,
    plus a recursive scan for any key named note(s)/series/round/headline(s)/
    gameNote -- as raw JSON snippets so we can pick an extraction path.

(2) BACKWARD REACH  -- how far back does ESPN's public API actually carry NBA
    games, officials and player box scores? For 5 fixed pre-2000 dates it prints
    the scoreboard event count and every event id, and for ONE completed game
    per date reports whether officials and boxscore.players exist in the summary.
------------------------------------------------------------------------------
"""

import os
import sys
import json
import time
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_rounds_reach.txt")

# Reuse the ESPN plumbing already written and tested in fetch_espn_seasons.py.
# (Importing that module is side-effect-safe: its work is behind __main__.)
sys.path.insert(0, SCRIPT_DIR)
from fetch_espn_seasons import (  # noqa: E402
    get_json,
    SCOREBOARD_URL,
    SUMMARY_URL,
    discover_officials_candidates,
    official_fields,
    nba_abbr,
)

DELAY_SECONDS = 1.0
SNIPPET_MAXLEN = 1200          # truncate raw JSON dumps to keep the report readable

# (1) Playoff dates spanning the four ESPN-sourced eras: 2000-01, 2012-13,
#     2023-24, 2025-26 -- an early-round date and a Finals date in each.
ROUND_DATES = [
    "2001-04-22", "2001-06-08",   # 2000-01 playoffs (1st round / Finals)
    "2013-05-05", "2013-06-13",   # 2012-13 playoffs
    "2024-04-22", "2024-06-09",   # 2023-24 playoffs
    "2026-05-05", "2026-06-08",   # 2025-26 playoffs
]

# (2) Pre-2000 dates: how far back does ESPN reach?
REACH_DATES = [
    "1999-11-02", "1999-02-06", "1997-11-01", "1995-11-03", "1993-11-06",
]

# Recursive scan targets: key names that plausibly carry round / series / game
# labeling. A match records (path, value) and does not recurse further into it.
INTEREST_KEYS = {
    "notes", "note", "series", "headlines", "headline",
    "gamenote", "round", "seriessummary",
}

# Collected output lines (printed live AND written to the report file).
_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


def is_completed(event):
    status = (((event.get("status") or {}).get("type")) or {})
    return bool(status.get("completed") or status.get("state") == "post")


def event_matchup(event):
    """'AWY @ HOM' via the shared nba_abbr normalization; falls back to shortName."""
    try:
        comps = event["competitions"][0]["competitors"]
    except (KeyError, IndexError, TypeError):
        return event.get("shortName") or event.get("name") or ""
    home = away = ""
    for c in comps:
        abbr = nba_abbr((c.get("team") or {}).get("abbreviation", ""))
        if c.get("homeAway") == "home":
            home = abbr
        elif c.get("homeAway") == "away":
            away = abbr
    if home or away:
        return "{} @ {}".format(away or "?", home or "?")
    return event.get("shortName") or event.get("name") or ""


def count_box_athletes(summary):
    """Total athlete rows across summary.boxscore.players (informational)."""
    players = ((summary.get("boxscore") or {}).get("players")) or []
    n = 0
    for team_block in players:
        for group in team_block.get("statistics") or []:
            n += len(group.get("athletes") or [])
    return n


def snippet(obj):
    """Compact JSON snippet, truncated so the report stays readable."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(obj)
    if len(s) > SNIPPET_MAXLEN:
        s = s[:SNIPPET_MAXLEN] + " ...[+{} chars]".format(len(s) - SNIPPET_MAXLEN)
    return s


def dump_field(label, obj):
    """Emit one 'label: <json>' line only when the value is present/non-empty."""
    if obj in (None, "", [], {}):
        return
    emit("     {}: {}".format(label, snippet(obj)))


def collect_interesting(obj, path="root", out=None):
    """Find keys named note(s)/series/round/headline(s)/gameNote anywhere in obj;
    record (path, value) and don't recurse into a matched value."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "{}.{}".format(path, k)
            if k.lower() in INTEREST_KEYS and v not in (None, "", [], {}):
                out.append((p, v))
            else:
                collect_interesting(v, p, out)
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            collect_interesting(v, "{}[{}]".format(path, idx), out)
    return out


def dump_interesting(label, obj):
    hits = collect_interesting(obj)
    emit("     {} -- {} interesting key(s):".format(label, len(hits)))
    if not hits:
        emit("       (none)")
    for p, v in hits:
        emit("       {} = {}".format(p, snippet(v)))


def pick_event(events):
    """First completed event, else the first event (so future/incomplete dates
    still yield something to dump)."""
    for ev in events:
        if is_completed(ev):
            return ev
    return events[0] if events else None


# --------------------------------------------------------------------------- #
# (1) Round / game labeling
# --------------------------------------------------------------------------- #
def probe_rounds():
    emit("\n" + "#" * 72)
    emit("# (1) ROUND / GAME LABELING  -- fields that may carry series/round/game")
    emit("#" * 72)

    for dstr in ROUND_DATES:
        ymd = datetime.date.fromisoformat(dstr).strftime("%Y%m%d")
        emit("\n=== {}  (scoreboard dates={}) ===".format(dstr, ymd))

        try:
            sb = get_json(SCOREBOARD_URL, {"dates": ymd})
        except Exception as e:  # noqa: BLE001
            emit("  scoreboard FAILED: {}".format(e))
            time.sleep(DELAY_SECONDS)
            continue
        time.sleep(DELAY_SECONDS)

        events = sb.get("events", []) or []
        emit("  events: {}".format(len(events)))
        for ev in events:
            season = ev.get("season") or {}
            emit("    event id={!r}  {}  season.type={}  completed={}  name={!r}".format(
                ev.get("id"), event_matchup(ev), season.get("type"),
                is_completed(ev), ev.get("shortName") or ev.get("name")))

        ev = pick_event(events)
        if not ev:
            emit("  (no events to dump)")
            continue
        eid = str(ev.get("id", "")).strip()
        comp = (ev.get("competitions") or [{}])[0]

        emit("  -- SCOREBOARD event {} field dump --".format(eid))
        dump_field("event.name", ev.get("name"))
        dump_field("event.shortName", ev.get("shortName"))
        dump_field("event.season", ev.get("season"))
        dump_field("event.notes", ev.get("notes"))
        dump_field("competitions[0].notes", comp.get("notes"))
        dump_field("competitions[0].series", comp.get("series"))
        dump_field("competitions[0].type", comp.get("type"))
        dump_field("competitions[0].status.type", (comp.get("status") or {}).get("type"))
        dump_interesting("scoreboard event scan", ev)

        emit("  -- SUMMARY for event {} --".format(eid))
        try:
            summ = get_json(SUMMARY_URL, {"event": eid})
        except Exception as e:  # noqa: BLE001
            emit("     summary FAILED: {}".format(e))
            time.sleep(DELAY_SECONDS)
            continue
        time.sleep(DELAY_SECONDS)

        header = summ.get("header") or {}
        hcomp = ((header.get("competitions") or [{}])[0]) if header else {}
        dump_field("header.season", header.get("season"))
        dump_field("header.competitions[0].series", hcomp.get("series"))
        dump_field("header.competitions[0].notes", hcomp.get("notes"))
        dump_field("header.competitions[0].type", hcomp.get("type"))
        dump_field("header.competitions[0].status.type", (hcomp.get("status") or {}).get("type"))
        dump_field("seasonseries", summ.get("seasonseries"))
        dump_field("summary.notes", summ.get("notes"))
        dump_field("summary.headlines", summ.get("headlines"))
        gi = summ.get("gameInfo") or {}
        dump_field("gameInfo.status", gi.get("status"))
        dump_interesting("summary scan", summ)


# --------------------------------------------------------------------------- #
# (2) Backward reach
# --------------------------------------------------------------------------- #
def probe_reach():
    emit("\n" + "#" * 72)
    emit("# (2) BACKWARD REACH  -- event counts/ids + officials/boxscore presence")
    emit("#" * 72)

    for dstr in REACH_DATES:
        ymd = datetime.date.fromisoformat(dstr).strftime("%Y%m%d")
        emit("\n=== {}  (scoreboard dates={}) ===".format(dstr, ymd))

        try:
            sb = get_json(SCOREBOARD_URL, {"dates": ymd})
        except Exception as e:  # noqa: BLE001
            emit("  scoreboard FAILED: {}".format(e))
            time.sleep(DELAY_SECONDS)
            continue
        time.sleep(DELAY_SECONDS)

        events = sb.get("events", []) or []
        emit("  events: {}".format(len(events)))
        for ev in events:
            season = ev.get("season") or {}
            emit("    event id={!r}  {}  season.type={}  completed={}".format(
                ev.get("id"), event_matchup(ev), season.get("type"), is_completed(ev)))
        if not events:
            emit("  -> ESPN returns NO events for this date.")
            continue

        ev = pick_event(events)
        eid = str(ev.get("id", "")).strip()
        emit("  -- summary for event {} ({}) --".format(eid, event_matchup(ev)))
        try:
            summ = get_json(SUMMARY_URL, {"event": eid})
        except Exception as e:  # noqa: BLE001
            emit("     summary FAILED: {}".format(e))
            time.sleep(DELAY_SECONDS)
            continue
        time.sleep(DELAY_SECONDS)

        cands = discover_officials_candidates(summ)
        if cands:
            path, lst = cands[0]
            emit("     officials: YES at {} ({} entries)".format(path, len(lst)))
            for entry in lst:
                oid, nm, jr = official_fields(entry)
                emit("       parsed -> id={!r} name={!r} jersey={!r}".format(oid, nm, jr))
        else:
            emit("     officials: NO")
        n_ath = count_box_athletes(summ)
        emit("     boxscore.players: {} (athlete rows: {})".format(
            "yes" if n_ath > 0 else "no", n_ath))


def main():
    emit("ESPN round-labeling + backward-reach probe (probe_espn_rounds_and_reach.py)")
    emit("=" * 74)
    emit("Read-only. Writes nothing to the extracts; only console + {}.".format(
        os.path.relpath(OUT_PATH, REPO_ROOT)))
    emit("(1) round/game dates ({}): {}".format(len(ROUND_DATES), ", ".join(ROUND_DATES)))
    emit("(2) backward-reach dates ({}): {}".format(len(REACH_DATES), ", ".join(REACH_DATES)))

    probe_rounds()
    probe_reach()

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    print("\n-> wrote {}".format(os.path.relpath(OUT_PATH, REPO_ROOT)))


if __name__ == "__main__":
    main()
