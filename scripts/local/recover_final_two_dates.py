"""
recover_final_two_dates.py  --  LOCAL, single-purpose FINAL recovery step
(run by Jorge on Windows).

The untruncated seasontype=3 probe response for 2001-06-08 surfaced ESPN's own
calendar array for the 2001 postseason, which explicitly lists 2001-05-22 and
2001-05-24 as valid dates -- the real East Finals (MIL/PHI) Games 1 and 2.
Neither date carries a game in games.csv.gz.

Per code review of recover_2000_01_playoffs.py: both dates fall inside the
four incomplete series' unioned, padded query window (2001-04-29..2001-06-20),
and neither is in present_dates (confirmed empirically -- zero rows in
games.csv.gz on either date, from ANY series). So under the existing logic
they SHOULD have been queried and, if empty, logged. If the previous run's
printed log did not show them, that is most likely console/paste truncation of
a long scrolling log rather than a code skip -- this script is the direct,
authoritative test either way.

This is the LAST test: it queries ONLY 2001-05-22 and 2001-05-24, with the
plain `dates` param (no seasontype -- the prior probe showed adding it changed
nothing), prints the full raw result for each, and -- if either returns a real
completed game matching one of the four incomplete series -- fetches its
summary and merges it in additively (same machinery as
recover_2000_01_playoffs.py: parse_game_row / parse_officials_rows /
parse_player_rows / merge_rows / merge_player_logs), then re-runs the 2000-01
completeness audit and reports the final state.

ESPN's calendar array only extends to 2001-06-15 and these are its only two
entries not yet in the extract -- no further window walking happens here.
Whatever series are still incomplete after this get game_num nulled with round
kept (option 1); the recovery effort ends with this script.

  python scripts\\local\\recover_final_two_dates.py
"""

import os
import sys
import datetime

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from fetch_espn_seasons import GAMES_PATH, parse_game_row  # noqa: E402
from recover_2000_01_playoffs import (  # noqa: E402
    SEASON,
    analyze_series,
    teams_label,
    raw_scoreboard,
    recover,
    report,
)

TARGET_DATES = [datetime.date(2001, 5, 22), datetime.date(2001, 5, 24)]


def main():
    print("Final two-date recovery (recover_final_two_dates.py)")
    print("=" * 70)
    print("Querying only 2001-05-22 and 2001-05-24 (plain dates param), per "
          "ESPN's own calendar array from the seasontype probe. No further "
          "window walking after this.")

    games = pd.read_csv(GAMES_PATH, dtype=str, keep_default_na=False)
    before = analyze_series(games, SEASON)
    incomplete = [s for s in before if not s["complete"]]
    pairs = {s["teams"] for s in incomplete}

    candidates = {}
    for d in TARGET_DATES:
        events, diag = raw_scoreboard(d)
        print("\n--- %s (dates only) ---" % d.isoformat())
        if diag:
            print("  diag: %s" % diag)
        print("  events: %d" % len(events))
        for ev in events:
            row, suffix = parse_game_row(ev, SEASON)
            if row is None:
                continue
            ser = frozenset({row["home_team_abbr"], row["away_team_abbr"]})
            status = (((ev.get("status") or {}).get("type")) or {})
            completed = bool(status.get("completed") or status.get("state") == "post")
            flag = " <-- matches an incomplete series" if ser in pairs else ""
            print("    id=%s  %s @ %s  date=%s  season_type=%s  completed=%s%s"
                  % (row["game_id"], row["away_team_abbr"], row["home_team_abbr"],
                     row["game_date"], row["season_type"], completed, flag))
            if ser in pairs and completed and suffix == "PO":
                candidates[row["game_id"]] = (ev, row)

    print("\ncandidates to merge: %d" % len(candidates))
    recovered = recover(candidates)

    games2 = pd.read_csv(GAMES_PATH, dtype=str, keep_default_na=False)
    after = analyze_series(games2, SEASON)
    report(before, after, recovered)


if __name__ == "__main__":
    main()
