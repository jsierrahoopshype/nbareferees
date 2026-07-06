"""
probe_espn_historical.py  --  LOCAL, READ-ONLY probe (run by Jorge on Windows).

Question: does ESPN's public API carry usable OFFICIATING data for the early
2000s (2000-01 .. 2002-03)? nbadb barely covers officials for that range (~1-3%)
and fetch_espn_seasons.py does not currently pull it, so before deciding whether
to extend the ESPN fetch back that far we spot-check a fixed set of dates.

This script WRITES NOTHING to games.csv.gz / officials.csv.gz / player_logs. It
only prints to the console and writes one additive summary file:
    source-data/_probe_espn_historical.txt

It reuses (does not duplicate) the ESPN plumbing from fetch_espn_seasons.py:
    get_json, SCOREBOARD_URL, SUMMARY_URL,
    discover_officials_candidates, official_fields, nba_abbr

  python scripts\\local\\probe_espn_historical.py

For each of 12 spot-check dates it calls the scoreboard once (1s delay), prints
the event count and every event's RAW id (the early-2000s id format is known to
differ from the modern 400-million range, and we need to see it). For up to the
first 2 COMPLETED games per date it calls summary (1s delay) and:
  (a) runs discover_officials_candidates and prints the path + full raw officials
      list if found, or NOT FOUND;
  (b) reports yes/no + athlete count for summary.boxscore.players (informational
      only -- Kaggle already covers player logs for this range).
Ends with: "Officials found in X of Y checked games."
"""

import os
import sys
import json
import time
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_espn_historical.txt")

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
MAX_SUMMARIES_PER_DATE = 2

# Fixed spot-check dates spanning 2000-01 .. 2002-03 (regular season + playoffs).
DATES = [
    "2000-11-01", "2001-01-15", "2001-04-15", "2001-05-10", "2001-06-15",
    "2001-11-01", "2002-01-15", "2002-05-10", "2002-06-10", "2002-11-01",
    "2003-01-15", "2003-04-20",
]

# Collected output lines (printed live AND written to the summary file).
_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


def is_completed(event):
    status = (((event.get("status") or {}).get("type")) or {})
    return bool(status.get("completed") or status.get("state") == "post")


def event_matchup(event):
    """'AWY @ HOM' using the shared nba_abbr normalization (surfaces this era's
    tricodes, e.g. VAN/SEA/NJN). Falls back to shortName if competitors absent."""
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


def probe_summary(summary, event_id):
    """Report officials + player-box presence for one game. Returns True if a
    non-empty officials array was found."""
    cands = discover_officials_candidates(summary)
    found = bool(cands)
    if found:
        for path, lst in cands:
            emit("     OFFICIALS FOUND at {}  ({} entries)".format(path, len(lst)))
            emit("       raw: {}".format(json.dumps(lst, ensure_ascii=False)))
            for entry in lst:
                oid, nm, jr = official_fields(entry)
                emit("         parsed -> id={!r}  name={!r}  jersey={!r}".format(oid, nm, jr))
    else:
        emit("     OFFICIALS: NOT FOUND")

    n_ath = count_box_athletes(summary)
    emit("     boxscore.players: {} (athlete rows: {})".format(
        "yes" if n_ath > 0 else "no", n_ath))
    return found


def main():
    emit("ESPN historical officiating probe (probe_espn_historical.py)")
    emit("=" * 64)
    emit("Read-only. Checks whether ESPN has usable officials data for")
    emit("2000-01 .. 2002-03. Writes nothing to the extracts.")
    emit("Dates checked ({}): {}".format(len(DATES), ", ".join(DATES)))

    checked_games = 0
    games_with_officials = 0

    for dstr in DATES:
        d = datetime.date.fromisoformat(dstr)
        ymd = d.strftime("%Y%m%d")
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

        # Print every event's RAW id (this era's id format is what we're after).
        for ev in events:
            season = ev.get("season") or {}
            emit("    event id={!r}  {}  season.type={}  completed={}".format(
                ev.get("id"), event_matchup(ev), season.get("type"), is_completed(ev)))

        completed = [ev for ev in events if is_completed(ev)]
        for ev in completed[:MAX_SUMMARIES_PER_DATE]:
            eid = str(ev.get("id", "")).strip()
            if not eid:
                continue
            checked_games += 1
            emit("  -- summary for event {} --".format(eid))
            try:
                summ = get_json(SUMMARY_URL, {"event": eid})
            except Exception as e:  # noqa: BLE001
                emit("     summary FAILED: {}".format(e))
                time.sleep(DELAY_SECONDS)
                continue
            time.sleep(DELAY_SECONDS)
            if probe_summary(summ, eid):
                games_with_officials += 1

    emit("\n" + "=" * 64)
    verdict = "Officials found in {} of {} checked games.".format(
        games_with_officials, checked_games)
    emit(verdict)

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    print("-> wrote {}".format(os.path.relpath(OUT_PATH, REPO_ROOT)))


if __name__ == "__main__":
    main()
