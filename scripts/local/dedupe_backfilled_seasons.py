"""
dedupe_backfilled_seasons.py  --  LOCAL, one-time migration (run by Jorge, cmd).

Why this exists
---------------
source-data/officials.csv.gz (and games.csv.gz) still carry a few NBA-scheme
game_id rows -- 10-digit, '00'-prefixed ids from nbadb's original sparse
coverage -- for 2000-01, 2001-02, 2002-03, and 2012-13. Those seasons are now
sourced ENTIRELY from ESPN (fetch_espn_seasons.py), which stores ESPN event ids
in game_id. So the old NBA-scheme rows duplicate real games under a second,
NON-JOINABLE id scheme and must be removed. (2012-13 is the ~85-game playoff
fragment flagged in _freshness_espn.txt's "2012-13 DOUBLE-COUNT WARNING" -- it
was documented for build.py to ignore but never actually removed until now.)

What it does
------------
Removes EXACTLY the rows whose game_id is NBA-scheme (10 digits, starts '00')
AND whose season -- decoded with the SAME logic as extract_from_nbadb.py (reused
here by import, not reinvented) -- is one of {2000-01, 2001-02, 2002-03, 2012-13},
from BOTH games.csv.gz and officials.csv.gz. It touches:
  * no ESPN-scheme rows (their ids are not NBA-scheme),
  * no other season,
  * no player_logs files (nbadb never had player box scores).

It is idempotent: a second run finds nothing and prints "already clean".

  python scripts\\local\\dedupe_backfilled_seasons.py
"""

import os
import sys

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
OFFICIALS_PATH = os.path.join(SOURCE_DIR, "officials.csv.gz")

# Reuse the EXACT season-decoding logic from extract_from_nbadb.py (importing it
# is side-effect-safe: its work is behind __main__).
sys.path.insert(0, SCRIPT_DIR)
from extract_from_nbadb import (  # noqa: E402
    season_start_year_from_gid,
    season_str_from_start_year,
    season_type_from_gid,
)

TARGET_SEASONS = {"2000-01", "2001-02", "2002-03", "2012-13"}


def is_nba_scheme(gid):
    """NBA game_ids are 10-digit strings starting '00'. ESPN event ids are not."""
    return len(gid) == 10 and gid.isdigit() and gid.startswith("00")


def is_stale_backfill_row(gid):
    """True iff gid is an NBA-scheme id AND decodes to one of the target seasons."""
    if not is_nba_scheme(gid):
        return False
    season = season_str_from_start_year(season_start_year_from_gid(gid))
    return season in TARGET_SEASONS


def load_gz(path):
    """Read a gz extract as all-strings. utf-8-sig strips the BOM the extracts
    are written with, so the first column stays 'game_id' (not '\\ufeffgame_id')."""
    return pd.read_csv(path, compression="gzip", dtype=str,
                       keep_default_na=False, encoding="utf-8-sig")


def process_file(path, label):
    """Remove stale rows from one extract. Returns (removed_game_ids, before, after).
    Only rewrites the file if something was actually removed."""
    if not os.path.exists(path):
        print("  {:<17} MISSING ({}) -- skipping".format(label, path))
        return [], 0, 0
    df = load_gz(path)
    before = len(df)
    mask = df["game_id"].map(is_stale_backfill_row)
    removed_ids = df.loc[mask, "game_id"].tolist()
    if not mask.any():
        print("  {:<17} {} rows (0 removed)".format(label, before))
        return [], before, before
    kept = df[~mask]
    after = len(kept)
    kept.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")
    print("  {:<17} {} -> {} rows (removed {})".format(label, before, after, before - after))
    return removed_ids, before, after


def count_remaining_stale():
    """Re-read both files and count any surviving NBA-scheme target-season rows."""
    remaining = 0
    for path in (GAMES_PATH, OFFICIALS_PATH):
        if os.path.exists(path):
            df = load_gz(path)
            remaining += int(df["game_id"].map(is_stale_backfill_row).sum())
    return remaining


def main():
    seasons_str = "/".join(sorted(TARGET_SEASONS))
    print("Dedupe stale nbadb-scheme rows for {} (now fully ESPN-sourced)".format(seasons_str))
    print("=" * 68)

    g_removed, g_before, g_after = process_file(GAMES_PATH, "games.csv.gz:")
    o_removed, o_before, o_after = process_file(OFFICIALS_PATH, "officials.csv.gz:")

    all_removed_ids = sorted(set(g_removed) | set(o_removed))
    total_rows_removed = (g_before - g_after) + (o_before - o_after)

    if not all_removed_ids:
        print("\nAlready clean: 0 NBA-scheme rows for {} -- nothing removed.".format(seasons_str))
    else:
        # Group the DISTINCT removed game_ids by season + type (reused decoders).
        groups = {}
        for gid in all_removed_ids:
            season = season_str_from_start_year(season_start_year_from_gid(gid))
            suffix, _label = season_type_from_gid(gid)
            groups.setdefault((season, suffix), []).append(gid)
        print("\nRemoved {} distinct game_id(s) ({} rows across both files), by season+type:"
              .format(len(all_removed_ids), total_rows_removed))
        for season, suffix in sorted(groups):
            ids = groups[(season, suffix)]
            print("  {} {}: {} game(s)".format(season, suffix, len(ids)))
            print("      {}".format(", ".join(ids)))

    # Final confirmation: re-read and prove nothing stale survived.
    remaining = count_remaining_stale()
    print("\n" + "=" * 68)
    if remaining == 0:
        print("CONFIRMED: 0 NBA-scheme rows remain for {} in games.csv.gz or officials.csv.gz."
              .format(seasons_str))
    else:
        print("!! WARNING: {} NBA-scheme row(s) for {} STILL remain -- investigate."
              .format(remaining, seasons_str))


if __name__ == "__main__":
    main()
