"""
fetch_player_logs.py  --  LOCAL script (run by Jorge on Windows, in cmd).

ONLY run this if extract_from_nbadb.py printed:
    PLAYER BOX SCORES: NOT FOUND -- run fetch_player_logs.py

Pulls per-player game logs from stats.nba.com via nba_api (LeagueGameLog,
player_or_team_abbreviation="P") for every season 2000-01 -> current, both
Regular Season and Playoffs (~52 calls total). Writes one gzipped CSV per
season+type into source-data/player_logs/ using the §3.1.5 column set.

  python scripts\\local\\fetch_player_logs.py

NETWORK NOTE: stats.nba.com blocks datacenter/cloud IPs, so this must run on
Jorge's own machine. It will NOT work in a cloud session.

  pip install nba_api pandas

Behavior:
  * 2-second delay between calls (be polite; avoid rate limiting).
  * Resume-safe: seasons whose output file already exists are skipped.
  * GAME_ID forced to string (10-digit, leading zeros preserved).
  * Output encoding utf-8-sig, gzip compression.
"""

import os
import sys
import time
import datetime

import pandas as pd

try:
    from nba_api.stats.endpoints import LeagueGameLog
except ImportError:
    print("ERROR: nba_api not installed. Run:  pip install nba_api pandas")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
PLAYER_LOGS_DIR = os.path.join(REPO_ROOT, "source-data", "player_logs")

DELAY_SECONDS = 2.0

# Season types to fetch and their file suffixes (§3.2).
SEASON_TYPES = [("Regular Season", "RS"), ("Playoffs", "PO")]

# Standard nba_api browser-like headers (nba_api sets these by default, but we
# pass them explicitly so behavior is stable across versions).
NBA_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

# Map LeagueGameLog (player) columns -> the §3.1.5 output column set.
# LeagueGameLog does not return STL/BLK/TOV/PLUS_MINUS reliably in every season
# via this endpoint's default result set; where a field is absent we emit blank
# so the schema stays consistent across files.
COLUMN_MAP = {
    "GAME_ID": "game_id",
    "PLAYER_ID": "player_id",
    "PLAYER_NAME": "player_name",
    "TEAM_ID": "team_id",
    "TEAM_ABBREVIATION": "team_abbr",
    "MIN": "min",
    "PTS": "pts",
    "REB": "reb",
    "AST": "ast",
    "STL": "stl",
    "BLK": "blk",
    "TOV": "tov",
    "FGA": "fga",
    "FGM": "fgm",
    "FG3A": "fg3a",
    "FG3M": "fg3m",
    "FTA": "fta",
    "FTM": "ftm",
    "PF": "pf",
    "PLUS_MINUS": "plus_minus",
}
OUTPUT_COLUMNS = list(COLUMN_MAP.values())


def seasons_through_current():
    """
    Return season strings "2000-01" .. current. The NBA season that starts in
    calendar year Y is labeled "Y-(Y+1)". A season is considered started once
    we're at/after October of year Y.
    """
    today = datetime.date.today()
    # If we're in Jan-Sep, the current season started the previous calendar year.
    current_start = today.year if today.month >= 10 else today.year - 1
    return ["{}-{:02d}".format(y, (y + 1) % 100) for y in range(2000, current_start + 1)]


def fetch_one(season, season_type_label):
    """Fetch one (season, type) via LeagueGameLog and return a normalized frame."""
    resp = LeagueGameLog(
        season=season,
        season_type_all_star=season_type_label,
        player_or_team_abbreviation="P",
        headers=NBA_HEADERS,
        timeout=60,
    )
    # GAME_ID must stay a string; force dtype on the parsed frame.
    df = resp.get_data_frames()[0]
    if df is None or df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df["GAME_ID"] = df["GAME_ID"].astype(str).str.strip().str.zfill(10)

    out = pd.DataFrame()
    for src, dst in COLUMN_MAP.items():
        if src in df.columns:
            out[dst] = df[src]
        else:
            out[dst] = ""  # keep schema stable if endpoint omits a field
    # Ensure id columns are clean strings (no trailing .0).
    for idc in ("player_id", "team_id"):
        out[idc] = (
            out[idc].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        )
    return out[OUTPUT_COLUMNS]


def main():
    os.makedirs(PLAYER_LOGS_DIR, exist_ok=True)
    seasons = seasons_through_current()
    print("Seasons to fetch: {} .. {}  ({} seasons x {} types)".format(
        seasons[0], seasons[-1], len(seasons), len(SEASON_TYPES)))

    total = fetched = skipped = failed = 0
    for season in seasons:
        for label, suffix in SEASON_TYPES:
            total += 1
            out_path = os.path.join(PLAYER_LOGS_DIR, "{}_{}.csv.gz".format(season, suffix))
            if os.path.exists(out_path):
                print("  skip (exists): {}_{}".format(season, suffix))
                skipped += 1
                continue
            try:
                print("  fetching {} {} ...".format(season, label), end="", flush=True)
                out = fetch_one(season, label)
                out.to_csv(out_path, index=False, encoding="utf-8-sig", compression="gzip")
                print(" {} rows -> {}".format(len(out), os.path.basename(out_path)))
                fetched += 1
            except Exception as e:  # noqa: BLE001 - keep going on a single failure
                print(" FAILED: {}".format(e))
                failed += 1
            time.sleep(DELAY_SECONDS)

    print("\nDone. total={} fetched={} skipped={} failed={}".format(
        total, fetched, skipped, failed))
    if failed:
        print("Some seasons failed. Re-run to resume (existing files are skipped).")


if __name__ == "__main__":
    main()
