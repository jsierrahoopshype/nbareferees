"""
drop_corrupt_rows.py  --  LOCAL, re-runnable maintenance script (run by Jorge).

Why this exists
----------------
Some individual ESPN rows have been diagnosed as genuinely corrupt upstream
data -- not a coverage GAP (dedupe_backfilled_seasons.py's job: removing old
nbadb rows once a full ESPN replacement lands) and not a bug in our own
fetch/parsing code, but a bad row ESPN's own archive returned (e.g. the wrong
team_id, a duplicate event) that has no replacement -- it's just wrong and
needs to go.

Driven by CORRUPT_GAME_IDS below: an explicit table of game_id -> reason, so
every removal is documented and reviewable, and this stays a general,
re-runnable tool rather than a one-off hardcoded script. To prune another bad
row once it's been diagnosed, add a new entry (with a pointer to whatever
diagnosis established it's genuinely corrupt) -- do not delete old entries
after they're cleaned, so this file stays a durable audit trail of what's
been pruned from the extracts and why.

What it does
------------
For every game_id in CORRUPT_GAME_IDS, removes matching rows from
games.csv.gz, officials.csv.gz, and every player_logs/*.csv.gz file (scanned
directly -- a corrupt game_id may or may not have made it into officials or
player_logs at all, so this doesn't assume which file it landed in). Prints
what it removes, from which file, and why. Idempotent: a second run finds
nothing left for any entry and reports "already clean".

  python scripts\\local\\drop_corrupt_rows.py
"""

import os
import glob

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
OFFICIALS_PATH = os.path.join(SOURCE_DIR, "officials.csv.gz")
PLAYER_LOGS_DIR = os.path.join(SOURCE_DIR, "player_logs")

# Documented-corrupt game_ids: upstream ESPN rows confirmed bad by a real
# diagnosis, never a mere suspicion. Add new entries here as more are found;
# never remove an entry once it's been cleaned, so the history of what's
# been pruned (and the evidence for why) stays visible in this file.
CORRUPT_GAME_IDS = {
    "150611014": (
        "1994-95 Playoffs: duplicate/ghost ESPN event for the real 1995 NBA "
        "Finals Game 3. Carries home_team_id=14 (MIA) instead of the correct "
        "10 (HOU); away team, date, and final score (106-103) are identical "
        "to game_id 150611010, the correctly-recorded real game -- only "
        "home_team_id differs. This created a phantom 'MIA/ORL' series in "
        "the structural round derivation (fetch_espn_round_labels.py), which "
        "flagged it as an incomplete series claiming an impossible round 5. "
        "Diagnosed in source-data/_diagnose_1990s_flags.txt (Part 1) -- an "
        "ESPN-side archival data error (both team_id -> abbreviation "
        "mappings are individually correct), not a normalization bug and "
        "not a structural-derivation bug."
    ),
}


def load_gz(path):
    return pd.read_csv(path, compression="gzip", dtype=str,
                       keep_default_na=False, encoding="utf-8-sig")


def process_file(path, label, target_ids, always_print=True):
    """Remove rows whose game_id is in target_ids. Returns rows removed.
    Only rewrites the file if something was actually removed."""
    if not os.path.exists(path):
        if always_print:
            print("  {:<32} MISSING ({}) -- skipping".format(label, path))
        return 0
    df = load_gz(path)
    before = len(df)
    mask = df["game_id"].isin(target_ids)
    if not mask.any():
        if always_print:
            print("  {:<32} {} rows (0 removed)".format(label, before))
        return 0
    kept = df[~mask]
    kept.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")
    removed = before - len(kept)
    print("  {:<32} {} -> {} rows (removed {})".format(label, before, len(kept), removed))
    return removed


def main():
    print("Drop documented-corrupt rows (drop_corrupt_rows.py)")
    print("=" * 68)
    print("{} documented corrupt game_id(s):".format(len(CORRUPT_GAME_IDS)))
    for gid, reason in sorted(CORRUPT_GAME_IDS.items()):
        print("\n  game_id {}:".format(gid))
        print("    {}".format(reason))

    target_ids = set(CORRUPT_GAME_IDS)
    total_removed = 0

    print("\nRemoving from extracts:")
    total_removed += process_file(GAMES_PATH, "games.csv.gz:", target_ids)
    total_removed += process_file(OFFICIALS_PATH, "officials.csv.gz:", target_ids)

    # player_logs: scan every file directly rather than guessing which
    # season/type a corrupt game_id belongs to -- cheap (small files) and
    # robust to a game_id that never made it into player_logs at all, or
    # one already removed from games.csv.gz by an earlier partial run.
    logs_removed = 0
    for path in sorted(glob.glob(os.path.join(PLAYER_LOGS_DIR, "*.csv.gz"))):
        logs_removed += process_file(
            path, "player_logs/{}:".format(os.path.basename(path)),
            target_ids, always_print=False)
    total_removed += logs_removed
    if logs_removed == 0:
        print("  player_logs/*.csv.gz            0 removed (no documented-corrupt "
             "game_id was present in any player_logs file)")

    print("\n" + "=" * 68)
    if total_removed == 0:
        print("Already clean: 0 rows removed for any documented-corrupt game_id.")
    else:
        print("Removed {} row(s) total across all extracts.".format(total_removed))


if __name__ == "__main__":
    main()
