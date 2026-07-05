"""
ingest_kaggle_player_logs.py  --  LOCAL script (run by Jorge on Windows, in cmd).
ADDITIVE. Does NOT modify the other scripts.

Why this exists
---------------
The nbadb SQLite extract may lack per-player box scores (extract_from_nbadb.py
prints PLAYER BOX SCORES: NOT FOUND), and nba_api is unusable from Jorge's
location (stats.nba.com / cdn.nba.com geo-blocked). This script normalizes a
separately-downloaded Kaggle *bulk player box score CSV set* into the project's
player_logs schema for seasons 2000-01 .. 2022-23. (2023-24+ and 2012-13 RS come
from ESPN via fetch_espn_seasons.py.)

  python scripts\\local\\ingest_kaggle_player_logs.py "C:\\path\\to\\kaggle_boxscores.csv"
  python scripts\\local\\ingest_kaggle_player_logs.py "C:\\path\\to\\folder_of_csvs"

The Kaggle schema is not known ahead of time, so the script:
  1. Reads the file (or every *.csv / *.csv.gz in the folder) as all-strings.
  2. Introspects the headers and prints a PROPOSED column mapping
     (source column -> our target column) plus any unmapped source columns.
  3. Waits for confirmation before converting (or pass --yes to skip the prompt;
     --dry-run stops after printing the proposal).

Output: source-data/player_logs/{season}_{RS|PO|PI}.csv.gz, merged additively
(dedupe on game_id + player_id), utf-8-sig gzip.

rule 5: game_id stays a STRING. NBA-format ids are zfill(10) so leading zeros
survive. Season + season type are derived from the NBA game_id where possible
(digit 3: 2=RS, 4=PO, 5=play-in; digits 4-5 = season start year).

Overlap note: 2012-13 is ALSO pulled from ESPN. If your Kaggle set includes
2012-13, exclude it here to avoid a double-source under two id schemes:
  --exclude-seasons 2012-13
"""

import os
import re
import sys
import glob
import argparse

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
PLAYER_LOGS_DIR = os.path.join(REPO_ROOT, "source-data", "player_logs")

MIN_START_YEAR = 2000   # 2000-01
MAX_START_YEAR = 2022   # 2022-23
KEEP_TYPES = {"RS", "PO", "PI"}

# Target schema (must match the existing player_logs files exactly).
PLAYER_COLUMNS = [
    "game_id", "player_id", "player_name", "team_id", "team_abbr", "min",
    "pts", "reb", "ast", "stl", "blk", "tov", "fga", "fgm", "fg3a", "fg3m",
    "fta", "ftm", "pf", "plus_minus",
]

# target column -> candidate normalized source-name patterns (exact-first, then
# substring). Order within a list is preference order.
COLUMN_PATTERNS = {
    "game_id": ["gameid", "gamekey", "gid"],
    "player_id": ["playerid", "personid"],
    "player_name": ["playername", "player", "displayname", "name"],
    "team_id": ["teamid"],
    "team_abbr": ["teamabbreviation", "teamabbr", "tricode", "teamcode"],
    "min": ["min", "minutes"],
    "pts": ["pts", "points"],
    "reb": ["reb", "totreb", "treb", "rebounds"],
    "ast": ["ast", "assists"],
    "stl": ["stl", "steals"],
    "blk": ["blk", "blocks"],
    "tov": ["tov", "to", "turnovers"],
    "fga": ["fga", "fieldgoalsattempted"],
    "fgm": ["fgm", "fieldgoalsmade"],
    "fg3a": ["fg3a", "fg3pa", "tpa", "threepa", "threepointersattempted"],
    "fg3m": ["fg3m", "fg3pm", "tpm", "threepm", "threepointersmade"],
    "fta": ["fta", "freethrowsattempted"],
    "ftm": ["ftm", "freethrowsmade"],
    "pf": ["pf", "personalfouls", "fouls"],
    "plus_minus": ["plusminus", "plusminuspoints"],
}

# Columns whose values are counting stats -> cleaned to integer-like strings.
NUMERIC_TARGETS = {
    "pts", "reb", "ast", "stl", "blk", "tov", "fga", "fgm", "fg3a", "fg3m",
    "fta", "ftm", "pf", "plus_minus",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def find_col(columns, patterns):
    """First column matching a pattern (exact normalized, then substring)."""
    ncols = {c: norm(c) for c in columns}
    for pat in patterns:
        for c, nc in ncols.items():
            if nc == pat:
                return c
    for pat in patterns:
        for c, nc in ncols.items():
            if pat in nc:
                return c
    return None


def clean_id_str(v, width=None):
    if v is None:
        return ""
    s = str(v).strip()
    s = re.sub(r"\.0$", "", s)
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    if width and s:
        s = s.zfill(width)
    return s


def clean_num_str(v):
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", "<na>", "-", "--"):
        return ""
    s = re.sub(r"\.0$", "", s)
    return s


def season_start_year_from_gid(gid):
    if not gid or len(gid) < 5:
        return None
    try:
        yy = int(gid[3:5])
    except ValueError:
        return None
    return 1900 + yy if yy >= 46 else 2000 + yy


def season_str_from_start_year(year):
    if year is None:
        return None
    return "{}-{:02d}".format(year, (year + 1) % 100)


def type_suffix_from_gid(gid):
    if not gid or len(gid) < 3:
        return None
    return {"2": "RS", "4": "PO", "5": "PI"}.get(gid[2])


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def load_frames(path):
    """Return a single concatenated DataFrame (all-strings) from a file or dir."""
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.csv"))
                       + glob.glob(os.path.join(path, "*.csv.gz")))
        if not files:
            print("ERROR: no *.csv / *.csv.gz files in {}".format(path))
            sys.exit(1)
    elif os.path.isfile(path):
        files = [path]
    else:
        print("ERROR: path not found: {}".format(path))
        sys.exit(1)

    frames = []
    for f in files:
        comp = "gzip" if f.endswith(".gz") else "infer"
        df = pd.read_csv(f, dtype=str, keep_default_na=False, compression=comp)
        print("  read {} ({} rows, {} cols)".format(os.path.basename(f), len(df), len(df.columns)))
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True, sort=False).fillna("")
    return combined


# --------------------------------------------------------------------------- #
# Mapping proposal
# --------------------------------------------------------------------------- #
def propose_mapping(columns):
    mapping = {tgt: find_col(columns, pats) for tgt, pats in COLUMN_PATTERNS.items()}
    print("\n==================== PROPOSED COLUMN MAPPING ====================")
    print("  {:<14} <- {}".format("TARGET", "SOURCE"))
    for tgt in PLAYER_COLUMNS:
        src = mapping[tgt]
        flag = "" if src else "   *** MISSING ***"
        print("  {:<14} <- {}{}".format(tgt, src if src else "(none)", flag))
    used = {v for v in mapping.values() if v}
    unmapped = [c for c in columns if c not in used]
    print("\n  Unmapped source columns ({}): {}".format(
        len(unmapped), ", ".join(unmapped) if unmapped else "(none)"))
    print("================================================================\n")
    return mapping


# --------------------------------------------------------------------------- #
# Convert
# --------------------------------------------------------------------------- #
def convert(df, mapping, exclude_seasons):
    gid_src = mapping["game_id"]
    if not gid_src:
        print("ERROR: could not find a game_id column; cannot derive season/type.")
        print("       Inspect the proposed mapping above and rename the source column,")
        print("       or extend COLUMN_PATTERNS['game_id'].")
        sys.exit(2)

    out = pd.DataFrame()
    out["game_id"] = df[gid_src].map(lambda v: clean_id_str(v, width=10))
    for tgt in PLAYER_COLUMNS:
        if tgt == "game_id":
            continue
        src = mapping[tgt]
        if not src:
            out[tgt] = ""
        elif tgt in ("player_id", "team_id"):
            out[tgt] = df[src].map(clean_id_str)
        elif tgt in NUMERIC_TARGETS:
            out[tgt] = df[src].map(clean_num_str)
        else:  # player_name, team_abbr, min -> trimmed strings
            out[tgt] = df[src].astype(str).str.strip()

    # Derive season + type from the (zero-padded) NBA game_id.
    out["_start_year"] = out["game_id"].map(season_start_year_from_gid)
    out["_suffix"] = out["game_id"].map(type_suffix_from_gid)
    out["_season"] = out["_start_year"].map(season_str_from_start_year)

    before = len(out)
    keep = (
        out["_start_year"].notna()
        & (out["_start_year"] >= MIN_START_YEAR)
        & (out["_start_year"] <= MAX_START_YEAR)
        & out["_suffix"].isin(KEEP_TYPES)
        & (out["game_id"] != "")
    )
    out = out[keep]
    if exclude_seasons:
        out = out[~out["_season"].isin(exclude_seasons)]
    print("  rows after season/type filter (2000-01..2022-23): {} of {}".format(len(out), before))
    return out


def merge_write(out):
    os.makedirs(PLAYER_LOGS_DIR, exist_ok=True)
    written = {}
    for (season, suffix), grp in out.groupby(["_season", "_suffix"]):
        path = os.path.join(PLAYER_LOGS_DIR, "{}_{}.csv.gz".format(season, suffix))
        new = grp[PLAYER_COLUMNS].astype(str)
        if os.path.exists(path):
            existing = pd.read_csv(path, compression="gzip", dtype=str, keep_default_na=False)
            for c in PLAYER_COLUMNS:
                if c not in existing.columns:
                    existing[c] = ""
            combined = pd.concat([existing[PLAYER_COLUMNS], new], ignore_index=True)
            combined = combined.drop_duplicates(["game_id", "player_id"], keep="last")
        else:
            combined = new.drop_duplicates(["game_id", "player_id"], keep="last")
        combined.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")
        written["{}_{}".format(season, suffix)] = len(combined)
    return written


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Normalize a Kaggle player box CSV set into player_logs.")
    ap.add_argument("path", help="Path to the Kaggle CSV file or a folder of CSVs.")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    ap.add_argument("--dry-run", action="store_true", help="Print the mapping proposal and exit.")
    ap.add_argument("--exclude-seasons", default="",
                    help="Comma-separated season labels to skip, e.g. '2012-13' (ESPN covers it).")
    args = ap.parse_args()

    exclude = {s.strip() for s in args.exclude_seasons.split(",") if s.strip()}
    if exclude:
        print("Excluding seasons: {}".format(", ".join(sorted(exclude))))

    print("Loading Kaggle CSV set from: {}".format(args.path))
    df = load_frames(args.path)
    print("Combined: {} rows, {} columns".format(len(df), len(df.columns)))

    mapping = propose_mapping(list(df.columns))

    if args.dry_run:
        print("--dry-run: stopping after the mapping proposal. No files written.")
        return

    if not args.yes:
        try:
            reply = input("Proceed with this mapping and convert? [y/N] ").strip().lower()
        except EOFError:
            reply = "n"
        if reply not in ("y", "yes"):
            print("Aborted. Re-run with --yes to skip this prompt, or adjust COLUMN_PATTERNS.")
            return

    out = convert(df, mapping, exclude)
    if out.empty:
        print("No rows in range 2000-01..2022-23 after filtering. Nothing written.")
        return

    written = merge_write(out)
    print("\nDONE. Player-log files written/updated additively:")
    for key in sorted(written):
        print("  {:<12} {} rows".format(key, written[key]))
    total = sum(written.values())
    print("Total rows across {} files: {}".format(len(written), total))


if __name__ == "__main__":
    main()
