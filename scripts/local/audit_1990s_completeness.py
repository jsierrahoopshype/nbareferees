"""
audit_1990s_completeness.py  --  LOCAL, READ-ONLY audit (run by Jorge, cmd).

Why this exists
----------------
fetch_espn_seasons.py's SEASONS list now includes seven new 1990s seasons
(1993-94 through 1999-00). Before any round-labeling work happens for them,
we need to know how COMPLETE the fetched playoff data actually is -- ESPN's
archive that far back may have gaps that don't show up just by looking at
row counts.

This script reuses (imports, does NOT reimplement) the exact win-based
series-completeness check fetch_espn_round_labels.py already uses to audit
2000-01 and later: derive_structural() groups each season's ESPN-scheme
playoff games into series, derives round/game_num structurally, and flags
any series where NEITHER team's win count reached that round's clinch
number (INCOMPLETE -- missing games) or exceeded the format's max games
(OVER-LONG). Calling the same function guarantees an identical report
format to the established 2000-01 audit.

What it does NOT do
--------------------
  * No network calls -- pure pandas over the already-fetched games.csv.gz.
  * No round_labels.csv.gz is written. This is a read-only survey of data
    shape, not the round-labeling step -- that's a deliberate follow-up
    once we know how complete these seasons really are.
  * No recovery attempt for anything flagged incomplete -- just surfaced.

Output
------
Prints the audit to the console AND writes the same text (additive report,
overwritten each run -- it reflects the CURRENT state of games.csv.gz, not
a running log) to source-data/_completeness_1990s.txt.

  python scripts\\local\\audit_1990s_completeness.py
"""

import os
import io
import sys
import contextlib

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
OUT_PATH = os.path.join(SOURCE_DIR, "_completeness_1990s.txt")

# Reuse the exact completeness-audit logic from fetch_espn_round_labels.py
# (importing it is side-effect-safe: its work is behind __main__). No reverse
# import exists -- fetch_espn_round_labels.py must never import this module.
sys.path.insert(0, SCRIPT_DIR)
from fetch_espn_round_labels import derive_structural  # noqa: E402

NEW_SEASONS = {"1993-94", "1994-95", "1995-96", "1996-97",
              "1997-98", "1998-99", "1999-00"}


class _Tee:
    """Write to multiple streams at once (console + an in-memory buffer),
    so the report file ends up with exactly what was printed."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)

    def flush(self):
        for st in self._streams:
            st.flush()


def main():
    buf = io.StringIO()
    with contextlib.redirect_stdout(_Tee(sys.stdout, buf)):
        print("1990s backfill completeness audit (audit_1990s_completeness.py)")
        print("=" * 70)
        print("Seasons in scope: {}".format(", ".join(sorted(NEW_SEASONS))))
        print("Read-only. Does NOT write round_labels.csv.gz -- no round-labeling")
        print("for these seasons yet; this only surfaces how complete the fetched")
        print("playoff data actually is, using the SAME win-based completeness")
        print("check fetch_espn_round_labels.py runs for 2000-01 onward (imported,")
        print("not reimplemented).")
        print()

        if not os.path.exists(GAMES_PATH):
            print("games.csv.gz not found at {} -- nothing to audit.".format(GAMES_PATH))
        else:
            games = pd.read_csv(GAMES_PATH, dtype=str)
            subset = games[games["season"].isin(NEW_SEASONS)].copy()
            present = sorted(set(subset["season"].unique()) & NEW_SEASONS)
            missing = sorted(NEW_SEASONS - set(subset["season"].unique()))

            if missing:
                print("Not yet present in games.csv.gz (run fetch_espn_seasons.py "
                      "first): {}".format(", ".join(missing)))
            if not present:
                print("\nNo in-scope season data found -- nothing to audit yet.")
            else:
                print("Auditing {} season(s) present: {}\n"
                      .format(len(present), ", ".join(present)))
                _df, ok = derive_structural(subset)
                print()
                if ok:
                    print("All {} audited season(s) PASSED the completeness check."
                          .format(len(present)))
                else:
                    print("One or more audited seasons have FLAGGED (incomplete or "
                          "over-long) series -- see above. No recovery attempted in "
                          "this pass; this is a survey only.")

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    print("\n-> wrote {}".format(os.path.relpath(OUT_PATH, REPO_ROOT)))


if __name__ == "__main__":
    main()
