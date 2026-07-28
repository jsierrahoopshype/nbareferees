"""
dedupe_backfilled_seasons.py  --  LOCAL, one-time migration (run by Jorge, cmd).

Why this exists
---------------
source-data/officials.csv.gz (and games.csv.gz) still carry a few NBA-scheme
game_id rows -- 10-digit, '00'-prefixed ids from nbadb's original sparse
coverage -- for 2000-01, 2001-02, 2002-03, 2012-13, and (as of the 1990s ESPN
backfill) 1996-97, 1997-98, 1998-99, and 1999-00. Those seasons are now sourced
ENTIRELY from ESPN (fetch_espn_seasons.py), which stores ESPN event ids in
game_id. So the old NBA-scheme rows duplicate real games under a second,
NON-JOINABLE id scheme and must be removed. (2012-13 is the ~85-game playoff
fragment flagged in _freshness_espn.txt's "2012-13 DOUBLE-COUNT WARNING" -- it
was documented for build.py to ignore but never actually removed until now.
1996-97 through 1999-00 also have nbadb NBA-scheme rows that the 1990s ESPN
fetch will duplicate -- and, on inspection, these are NOT "sparse" the way
that phrase is normally used in this project: nbadb's GAME rows for those
seasons are essentially complete full schedules (e.g. 1189 rows = a full
82-game/29-team season; 725 for the lockout-shortened 1998-99 = exactly
29 teams x 50 games / 2). What's actually sparse is OFFICIALS coverage for
those same games (~1-3%), matching the already-documented early-2000s
pattern. Either way the old rows are non-joinable duplicates once ESPN's
full replacement lands, so the removal logic is unchanged.
1993-94/1994-95/1995-96 apparently have no nbadb rows at all to begin with,
so they're left out of this list; if a future run finds otherwise,
count_remaining_stale() below will catch it.)

Pre-flight safety check
------------------------
TARGET_SEASONS is a permanent, shared list -- nothing stops this script from
being run before fetch_espn_seasons.py has actually populated a given
season+type's ESPN replacement (that's exactly what almost happened here:
this script was widened to include the 1990s seasons before their ESPN fetch
had run). So before removing NBA-scheme rows for ANY (season, type) pair --
old seasons included, not just the new ones -- this script first checks that
ESPN-scheme rows already exist in games.csv.gz for that EXACT season+type. If
they don't, that pair is SKIPPED with a loud printed warning, not a silent
no-op, since deleting it would remove the only data source that season+type
has. This is a permanent guard, not a one-time migration step.

What it does
------------
Removes EXACTLY the rows whose game_id is NBA-scheme (10 digits, starts '00'),
AND whose season -- decoded with the SAME logic as extract_from_nbadb.py (reused
here by import, not reinvented) -- is one of TARGET_SEASONS, AND whose
(season, type) pair already has an ESPN-scheme replacement in games.csv.gz
(the pre-flight check above), from BOTH games.csv.gz and officials.csv.gz. It
touches:
  * no ESPN-scheme rows (their ids are not NBA-scheme),
  * no other season,
  * no season+type pair lacking an ESPN replacement yet,
  * no player_logs files (nbadb never had player box scores).

It is idempotent: a second run finds nothing left to remove and prints
"already clean" (any pairs still blocked on a missing ESPN replacement are
reported every run, not silently re-skipped).

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
    SEASON_TYPE_MAP,
    season_start_year_from_gid,
    season_str_from_start_year,
    season_type_from_gid,
)

TARGET_SEASONS = {"2000-01", "2001-02", "2002-03", "2012-13",
                  "1996-97", "1997-98", "1998-99", "1999-00"}

# games.csv.gz's season_type column spells this out in full words ("Regular
# Season", "Playoffs", ...); SEASON_TYPE_MAP (gid-index-2 -> (suffix, label))
# already has the label side of that mapping, so build the reverse once.
_LABEL_TO_SUFFIX = {label: suffix for suffix, label in SEASON_TYPE_MAP.values()}


def is_nba_scheme(gid):
    """NBA game_ids are 10-digit strings starting '00'. ESPN event ids are not."""
    return len(gid) == 10 and gid.isdigit() and gid.startswith("00")


def season_type_pair(gid):
    """(season, suffix) decoded from an NBA-scheme gid, or (None, None) if gid
    isn't NBA-scheme or doesn't decode to a TARGET_SEASONS season."""
    if not is_nba_scheme(gid):
        return None, None
    season = season_str_from_start_year(season_start_year_from_gid(gid))
    if season not in TARGET_SEASONS:
        return None, None
    suffix, _label = season_type_from_gid(gid)
    return season, suffix


def is_stale_backfill_row(gid, allowed_pairs):
    """True iff gid is an NBA-scheme id, decodes to one of TARGET_SEASONS, AND
    its exact (season, type) pair is in allowed_pairs -- i.e. it has already
    passed the pre-flight ESPN-coverage check and is actually safe to remove."""
    season, suffix = season_type_pair(gid)
    return season is not None and (season, suffix) in allowed_pairs


def load_gz(path):
    """Read a gz extract as all-strings. utf-8-sig strips the BOM the extracts
    are written with, so the first column stays 'game_id' (not '\\ufeffgame_id')."""
    return pd.read_csv(path, compression="gzip", dtype=str,
                       keep_default_na=False, encoding="utf-8-sig")


def preflight_check(games_df, officials_df):
    """Determine which (season, type) pairs among TARGET_SEASONS are actually
    safe to remove: every pair that has stale NBA-scheme rows in EITHER
    extract file, split into `allowed` (ESPN-scheme rows for that exact pair
    already exist in games.csv.gz) and `blocked` (they don't -- removing this
    pair would delete the only data source it has)."""
    stale_pairs = set()
    for df in (games_df, officials_df):
        if df is None:
            continue
        for gid in df["game_id"]:
            season, suffix = season_type_pair(gid)
            if season is not None:
                stale_pairs.add((season, suffix))

    covered_pairs = set()
    espn_rows = games_df[~games_df["game_id"].map(is_nba_scheme)]
    for season, season_type in zip(espn_rows["season"], espn_rows["season_type"]):
        suffix = _LABEL_TO_SUFFIX.get(season_type)
        if suffix:
            covered_pairs.add((season, suffix))

    allowed = stale_pairs & covered_pairs
    blocked = stale_pairs - covered_pairs
    return allowed, blocked


def process_file(path, label, allowed_pairs):
    """Remove stale-and-allowed rows from one extract. Returns (removed_game_ids,
    before, after). Only rewrites the file if something was actually removed."""
    if not os.path.exists(path):
        print("  {:<17} MISSING ({}) -- skipping".format(label, path))
        return [], 0, 0
    df = load_gz(path)
    before = len(df)
    mask = df["game_id"].map(lambda g: is_stale_backfill_row(g, allowed_pairs))
    removed_ids = df.loc[mask, "game_id"].tolist()
    if not mask.any():
        print("  {:<17} {} rows (0 removed)".format(label, before))
        return [], before, before
    kept = df[~mask]
    after = len(kept)
    kept.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")
    print("  {:<17} {} -> {} rows (removed {})".format(label, before, after, before - after))
    return removed_ids, before, after


def count_remaining_stale(allowed_pairs):
    """Re-read both files and count any surviving NBA-scheme rows among the
    ALLOWED pairs only -- these should be zero after a successful run. Rows
    for BLOCKED pairs are expected to remain (that's the pre-flight check
    working as intended) and are deliberately not counted here."""
    remaining = 0
    for path in (GAMES_PATH, OFFICIALS_PATH):
        if os.path.exists(path):
            df = load_gz(path)
            remaining += int(df["game_id"].map(
                lambda g: is_stale_backfill_row(g, allowed_pairs)).sum())
    return remaining


def main():
    seasons_str = "/".join(sorted(TARGET_SEASONS))
    print("Dedupe stale nbadb-scheme rows for {} (now fully ESPN-sourced)".format(seasons_str))
    print("=" * 68)

    if not os.path.exists(GAMES_PATH):
        print("\n!! ABORTING: games.csv.gz not found at {} -- cannot run the "
              "pre-flight ESPN-coverage check without it, so nothing is safe "
              "to remove.".format(GAMES_PATH))
        return

    games_df = load_gz(GAMES_PATH)
    officials_df = load_gz(OFFICIALS_PATH) if os.path.exists(OFFICIALS_PATH) else None

    allowed, blocked = preflight_check(games_df, officials_df)

    if blocked:
        print("\n!! PRE-FLIGHT WARNING: {} season+type pair(s) have stale NBA-scheme "
              "rows but NO ESPN-scheme replacement in games.csv.gz yet -- SKIPPING "
              "these, since removing them would delete the only data that "
              "season+type has:".format(len(blocked)))
        for season, suffix in sorted(blocked):
            print("  SKIPPED  {} {} -- no ESPN-scheme rows found for this pair yet.".format(
                season, suffix))

    if not allowed:
        print("\nNothing is safe to remove yet -- no season+type pair in {} has an "
              "ESPN-scheme replacement in games.csv.gz. Exiting without touching "
              "any file.".format(seasons_str))
        return

    print("\n{} season+type pair(s) have an ESPN replacement and will be deduped:"
          .format(len(allowed)))
    for season, suffix in sorted(allowed):
        print("  {} {}".format(season, suffix))
    print()

    g_removed, g_before, g_after = process_file(GAMES_PATH, "games.csv.gz:", allowed)
    o_removed, o_before, o_after = process_file(OFFICIALS_PATH, "officials.csv.gz:", allowed)

    all_removed_ids = sorted(set(g_removed) | set(o_removed))
    total_rows_removed = (g_before - g_after) + (o_before - o_after)

    if not all_removed_ids:
        print("\nAlready clean: 0 removable NBA-scheme rows found -- nothing removed.")
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

    # Final confirmation: re-read and prove nothing ALLOWED-and-stale survived.
    remaining = count_remaining_stale(allowed)
    print("\n" + "=" * 68)
    if remaining == 0:
        print("CONFIRMED: 0 removable NBA-scheme rows remain in games.csv.gz or "
              "officials.csv.gz (any rows still present belong to season+type "
              "pairs the pre-flight check blocked above).")
    else:
        print("!! WARNING: {} NBA-scheme row(s) that SHOULD have been removed "
              "still remain -- investigate.".format(remaining))


if __name__ == "__main__":
    main()
