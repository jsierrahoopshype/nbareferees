"""
fetch_espn_seasons.py  --  LOCAL script (run by Jorge on Windows, in cmd).
ADDITIVE. Does NOT modify the other scripts or any pre-2023 rows.

Why this exists
---------------
The Kaggle nbadb snapshot ends 2023-06-12, and stats.nba.com / cdn.nba.com are
geo-blocked from Jorge's location (VPN exits blocked too), so nba_api is
unusable. ESPN's public JSON API is NOT geo-blocked, so we source the missing
seasons from ESPN instead:

    * 2023-24, 2024-25, 2025-26  (regular season + play-in + playoffs)
    * 2000-01, 2001-02, 2002-03, 2012-13  (regular season + playoffs -- no play-in
      existed yet; the whole season in the ESPN scheme)

For each configured season we walk the ESPN scoreboard day by day to enumerate
games, then hit the summary endpoint per completed game to pull three things
from the SAME payload:
    1. a games.csv.gz result row
    2. the officials crew  -> officials.csv.gz
    3. per-player box scores -> player_logs/{season}_{RS|PO|PI}.csv.gz

  python scripts\\local\\fetch_espn_seasons.py

Endpoints (public, no auth, no key):
    scoreboard: site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
    summary:    site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=<id>

KEY DESIGN DECISIONS (also written into the freshness report)
-------------------------------------------------------------
* JOIN KEY: ESPN games have ESPN *event ids*, not NBA game_ids. Because all three
  data types for these games come from the same ESPN payloads, we store the ESPN
  event id in the `game_id` column. The ESPN era is therefore internally
  self-consistent (games <-> officials <-> player_logs all share the event id).
  ESPN event ids are ~9-digit numbers starting with a non-zero digit; NBA
  game_ids are 10-digit strings starting "00". They never collide, so the merge
  below can safely identify and replace ONLY ESPN-era rows and never touches
  pre-2023 NBA rows. (rule 5: game_ids stay strings; NBA ids keep leading zeros.)
* TEAM IDS: ESPN team ids (1..30) are NOT NBA team ids. We store the ESPN team id
  in *_team_id and normalize the ESPN abbreviation to the NBA-standard tricode in
  *_team_abbr, so the abbreviation is the cross-era join key.
* OFFICIAL IDS: ESPN payloads usually give official names but no NBA official_id.
  Where ESPN exposes an id we use it; otherwise we synthesize a stable, era-tagged
  key `espn:firstname-lastname`. These will NOT match NBA official_ids from the
  Kaggle era -- build.py must reconcile refs across eras by normalized name.
  Flagged loudly in the freshness report.
* Playoff round / Game 7 labeling is intentionally SKIPPED for ESPN-era games.

Behavior:
* 1s polite delay between calls. Resume-safe by date (a completed past date is
  recorded in source-data/_espn_progress.json and skipped on re-run).
* Before bulk-fetching, ONE probe summary call is made and the discovered
  officials field location + a sample entry are printed for verification. During
  the walk, the first 4-official playoff array is also dumped once so the
  alternate-official distinguisher can be confirmed.
* Exhibition games (All-Star etc., whose "teams" are not one of the 30 NBA
  tricodes) are dropped on fetch. Purge any that an earlier run wrote with:
      python scripts\\local\\fetch_espn_seasons.py --clean
* Outputs utf-8-sig gzip, merged additively into the existing extracts.
* Uses only the Python standard library + pandas (no nba_api, no requests).
"""

import os
import re
import sys
import glob
import gzip
import json
import time
import argparse
import datetime
import urllib.parse
import urllib.request

import pandas as pd

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
PLAYER_LOGS_DIR = os.path.join(SOURCE_DIR, "player_logs")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
OFFICIALS_PATH = os.path.join(SOURCE_DIR, "officials.csv.gz")
PROGRESS_PATH = os.path.join(SOURCE_DIR, "_espn_progress.json")

# Shared tricode normalization (single source of truth across the local scripts).
sys.path.insert(0, SCRIPT_DIR)
from nba_tricodes import ESPN_TO_NBA_ABBR, VALID_TRICODES, HISTORICAL_TRICODES  # noqa: E402

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DELAY_SECONDS = 1.0

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Seasons to pull. Dates are generous outer bounds (empty dates just return no
# games); the walk is also capped at today so we never scan the future.
# ESPN season.type: 1 = preseason, 2 = regular season, 3 = postseason,
# 4 = all-star, 5 = play-in tournament. The play-in is a SEPARATE type from the
# playoffs (that is why the play-in dates returned nothing under {2, 3}); we
# include 5 for seasons that have a play-in (2020-21+), and also detect play-in
# by name/notes as a fallback in case ESPN files it under a different id.
PLAYIN_TYPE = 5

SEASONS = [
    {"label": "2000-01", "start": "2000-10-25", "end": "2001-06-20", "types": {2, 3}},
    {"label": "2001-02", "start": "2001-10-25", "end": "2002-06-20", "types": {2, 3}},
    {"label": "2002-03", "start": "2002-10-25", "end": "2003-06-20", "types": {2, 3}},
    {"label": "2012-13", "start": "2012-10-30", "end": "2013-06-25", "types": {2, 3}},
    {"label": "2023-24", "start": "2023-10-24", "end": "2024-06-25", "types": {2, 3, 5}},
    {"label": "2024-25", "start": "2024-10-22", "end": "2025-06-25", "types": {2, 3, 5}},
    {"label": "2025-26", "start": "2025-10-21", "end": "2026-06-25", "types": {2, 3, 5}},
]

SEASON_TYPE_LABELS = {
    2: ("Regular Season", "RS"),
    3: ("Playoffs", "PO"),
    5: ("Play-In", "PI"),
}

# VALID_TRICODES (30 current franchises), HISTORICAL_TRICODES (frozen defunct /
# relocated teams like SEA/NJN/VAN/CHH/NOH), and ESPN_TO_NBA_ABBR (alias map) come
# from the shared nba_tricodes module imported above, so nbadb / ESPN / Kaggle
# converge on one tricode scheme. A game whose home or away abbreviation is not a
# real NBA franchise -- current OR historical -- is an exhibition (All-Star, etc.)
# and is dropped on fetch / purged by --clean. Both filters use ALLOWED_TRICODES
# so legitimate defunct-team games (e.g. the 2000-03 Nets/Sonics) are NOT dropped.
ALLOWED_TRICODES = VALID_TRICODES | HISTORICAL_TRICODES

# Output column sets (must match the existing extracts exactly).
GAMES_COLUMNS = [
    "game_id", "game_date", "season", "season_type",
    "home_team_id", "home_team_abbr", "away_team_id", "away_team_abbr",
    "home_pts", "away_pts", "home_win",
]
OFFICIALS_COLUMNS = ["game_id", "official_id", "official_name", "jersey_num"]
PLAYER_COLUMNS = [
    "game_id", "player_id", "player_name", "team_id", "team_abbr", "min",
    "pts", "reb", "ast", "stl", "blk", "tov", "fga", "fgm", "fg3a", "fg3m",
    "fta", "ftm", "pf", "plus_minus",
]

# ESPN_TO_NBA_ABBR (the alias table) is imported from nba_tricodes above. nba_abbr()
# below keeps the ESPN-era warning message while reusing that shared table.
_warned_abbr = set()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def name_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")


def nba_abbr(espn_abbr):
    if espn_abbr is None:
        return ""
    a = str(espn_abbr).strip().upper()
    if a in ESPN_TO_NBA_ABBR:
        return ESPN_TO_NBA_ABBR[a]
    if a and a not in _warned_abbr:
        _warned_abbr.add(a)
        print("  ! unknown ESPN abbreviation '{}' -- passing through unchanged".format(a))
    return a


def to_int_str(v):
    """'34', '34.0', 12 -> '34'; blank/dashes -> ''."""
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("", "-", "--", "None", "nan"):
        return ""
    s = re.sub(r"\.0$", "", s)
    return s


def split_made_att(v):
    """'10-18' -> ('10','18'); '--'/''/'0-0' handled; returns strings."""
    if v is None:
        return ("", "")
    s = str(v).strip()
    if "-" not in s:
        return ("", "")
    made, _, att = s.partition("-")
    return (to_int_str(made), to_int_str(att))


def daterange(start, end):
    d = start
    step = datetime.timedelta(days=1)
    while d <= end:
        yield d
        d += step


def is_nba_game_id(gid):
    """NBA game_ids are 10-digit strings starting '00'; ESPN event ids are not.
    Used to keep --clean and any ESPN-era logic away from pre-2023 NBA rows."""
    return len(gid) == 10 and gid.isdigit() and gid.startswith("00")


def is_play_in(event):
    """True if this ESPN event is a play-in tournament game: either ESPN season
    type 5, or identifiable by name / competition notes (fallback)."""
    stype = int((event.get("season") or {}).get("type", 0) or 0)
    if stype == PLAYIN_TYPE:
        return True
    blob = "{} {} {}".format(
        event.get("name", ""), event.get("shortName", ""),
        (event.get("season") or {}).get("slug", ""))
    try:
        comp = event["competitions"][0]
        blob += " " + " ".join(str(n.get("headline", "")) for n in comp.get("notes", []))
    except (KeyError, IndexError, TypeError):
        pass
    blob = blob.lower()
    return "play-in" in blob or "play in tournament" in blob


def get_json(url, params=None, retries=4):
    """GET url (with query params) and parse JSON. Retries on transient errors."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    delay = 2.0
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - retry transient failures
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError("GET failed after {} attempts: {} ({})".format(retries, url, last_err))


# --------------------------------------------------------------------------- #
# Progress (resume-safe by date)
# --------------------------------------------------------------------------- #
def load_progress():
    if os.path.exists(PROGRESS_PATH):
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("dates_done", []))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def save_progress(dates_done):
    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump({"dates_done": sorted(dates_done)}, f, indent=0)


# --------------------------------------------------------------------------- #
# Officials discovery (probe + per-game extraction)
# --------------------------------------------------------------------------- #
def discover_officials_candidates(obj, path="root"):
    """Recursively find lists keyed 'officials' whose items are dicts."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "{}.{}".format(path, k)
            if k.lower() == "officials" and isinstance(v, list) and v and isinstance(v[0], dict):
                found.append((p, v))
            found.extend(discover_officials_candidates(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(discover_officials_candidates(v, "{}[{}]".format(path, i)))
    return found


def pick_officials_list(summary):
    """Return (path, list) for the best officials array, or (None, None)."""
    cands = discover_officials_candidates(summary)
    if not cands:
        return (None, None)
    # Prefer one nested under gameInfo, else the first.
    for p, v in cands:
        if "gameinfo" in p.lower():
            return (p, v)
    return cands[0]


def official_fields(entry):
    """Extract (official_id, official_name, jersey_num) from one ESPN entry."""
    name = (entry.get("displayName") or entry.get("fullName")
            or entry.get("name") or "").strip()
    raw_id = entry.get("id") or entry.get("personId")
    if raw_id:
        oid = "espn-athlete:{}".format(str(raw_id).strip())
    elif name:
        oid = "espn:{}".format(name_slug(name))
    else:
        oid = ""
    jersey = to_int_str(entry.get("jersey") or entry.get("jerseyNum") or "")
    return (oid, name, jersey)


def probe_officials(sample_summary):
    """Print the discovered officials field location + a sample entry."""
    path, lst = pick_officials_list(sample_summary)
    print("\n==================== OFFICIALS FIELD PROBE ====================")
    if not lst:
        print("  !! NO officials array found in the probe payload.")
        print("     Top-level keys were:", list(sample_summary.keys()))
        print("     Officials will be blank until the field is located. Inspect a")
        print("     raw summary payload and adjust discover_officials_candidates().")
        print("===============================================================\n")
        return
    print("  Found officials at JSON path: {}".format(path))
    print("  Count in sample game: {}".format(len(lst)))
    print("  Raw sample entry: {}".format(json.dumps(lst[0], ensure_ascii=False)))
    oid, nm, jr = official_fields(lst[0])
    print("  Parsed -> official_id={!r}  official_name={!r}  jersey_num={!r}".format(oid, nm, jr))
    print("===============================================================\n")


# One-time dump of a real playoff officials array so the alternate (4th official)
# distinguisher can be confirmed empirically. Playoff crews list 4 officials;
# the 4th is an alternate who did not officiate (see _freshness_espn.txt).
_po_officials_probed = [False]


def probe_playoff_officials(summary):
    """Print a full playoff officials array once (only when 4+ officials, i.e.
    an alternate is present) so the order/position distinguisher is visible."""
    if _po_officials_probed[0]:
        return
    path, lst = pick_officials_list(summary)
    if not lst or len(lst) < 4:
        return
    _po_officials_probed[0] = True
    print("\n=========== PLAYOFF OFFICIALS PROBE (alternate check) ===========")
    print("  officials path: {}  count: {}".format(path, len(lst)))
    for i, entry in enumerate(lst):
        print("   [{}] {}".format(i, json.dumps(entry, ensure_ascii=False)))
    print("  -> The alternate is the LAST entry (highest 'order'); confirm via the")
    print("     'order'/'position' fields above. Extraction keeps all rows; build.py")
    print("     must NOT credit the alternate (see _freshness_espn.txt).")
    print("=================================================================\n")


# --------------------------------------------------------------------------- #
# Parsing scoreboard + summary
# --------------------------------------------------------------------------- #
def parse_game_row(event, season_label):
    """Build a games.csv.gz row from a scoreboard event. Returns (row, type_suffix)
    or (None, None) if it should be skipped."""
    try:
        comp = event["competitions"][0]
        stype = int(event.get("season", {}).get("type", comp.get("type", {}).get("id", 0)))
    except (KeyError, IndexError, ValueError, TypeError):
        return (None, None)

    label, suffix = SEASON_TYPE_LABELS.get(stype, (None, None))
    if label is None:
        return (None, None)  # not RS/PO (e.g. preseason / all-star)

    # Play-in games (still season.type 3) get their own label when detectable.
    name_blob = "{} {}".format(event.get("name", ""), event.get("shortName", "")).lower()
    notes = " ".join(str(n.get("headline", "")) for n in comp.get("notes", [])).lower()
    if "play-in" in name_blob or "play-in" in notes or "playin" in name_blob:
        label, suffix = ("Play-In", "PI")

    event_id = str(event.get("id", "")).strip()
    if not event_id:
        return (None, None)

    # Date -> YYYY-MM-DD (ESPN dates are ISO, often with Z / offset).
    raw_date = event.get("date") or comp.get("date") or ""
    game_date = ""
    if raw_date:
        try:
            game_date = pd.to_datetime(raw_date, utc=True, errors="coerce").strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            game_date = str(raw_date)[:10]

    home = away = None
    for c in comp.get("competitors", []):
        if c.get("homeAway") == "home":
            home = c
        elif c.get("homeAway") == "away":
            away = c
    if home is None or away is None:
        return (None, None)

    def team_id(c):
        return str((c.get("team") or {}).get("id", "")).strip()

    def team_abbr(c):
        return nba_abbr((c.get("team") or {}).get("abbreviation", ""))

    hp = to_int_str(home.get("score"))
    ap = to_int_str(away.get("score"))
    home_win = ""
    if home.get("winner") is True:
        home_win = "1"
    elif away.get("winner") is True:
        home_win = "0"
    elif hp != "" and ap != "":
        home_win = "1" if int(hp) > int(ap) else "0"

    row = {
        "game_id": event_id,
        "game_date": game_date,
        "season": season_label,
        "season_type": label,
        "home_team_id": team_id(home),
        "home_team_abbr": team_abbr(home),
        "away_team_id": team_id(away),
        "away_team_abbr": team_abbr(away),
        "home_pts": hp,
        "away_pts": ap,
        "home_win": home_win,
    }
    return (row, suffix)


def _resolve_stat_indices(header):
    """Map our target stat names -> column index using an ESPN box header list
    (names/labels like MIN, FG, 3PT, FT, OREB, DREB, REB, AST, STL, BLK, TO, PF,
    +/-, PTS)."""
    idx = {}
    for i, h in enumerate(header):
        n = norm(h)  # e.g. '+/-' -> '', '3pt' -> '3pt'
        raw = str(h).strip().lower()
        if raw in ("min", "minutes"):
            idx["min"] = i
        elif raw == "fg":
            idx["fg"] = i
        elif raw in ("3pt", "3p", "3ptm-a"):
            idx["3pt"] = i
        elif raw == "ft":
            idx["ft"] = i
        elif raw == "reb" or n == "reb":
            idx.setdefault("reb", i)
        elif raw == "ast" or n == "ast":
            idx["ast"] = i
        elif raw == "stl" or n == "stl":
            idx["stl"] = i
        elif raw == "blk" or n == "blk":
            idx["blk"] = i
        elif raw in ("to", "tov") or n in ("to", "tov"):
            idx["tov"] = i
        elif raw == "pf" or n == "pf":
            idx["pf"] = i
        elif raw in ("+/-", "plusminus") or n == "plusminus":
            idx["pm"] = i
        elif raw == "pts" or n == "pts":
            idx["pts"] = i
    return idx


def parse_player_rows(summary, event_id):
    """Extract per-player box rows from summary.boxscore.players."""
    rows = []
    box = summary.get("boxscore") or {}
    players = box.get("players") or []
    for team_block in players:
        team = team_block.get("team") or {}
        tid = str(team.get("id", "")).strip()
        tabbr = nba_abbr(team.get("abbreviation", ""))
        for group in team_block.get("statistics") or []:
            header = group.get("names") or group.get("labels") or group.get("keys") or []
            idx = _resolve_stat_indices(header)
            for ath in group.get("athletes") or []:
                if ath.get("didNotPlay"):
                    continue
                stats = ath.get("stats") or []
                if not stats:
                    continue
                athlete = ath.get("athlete") or {}
                pid = str(athlete.get("id", "")).strip()
                pname = (athlete.get("displayName") or athlete.get("shortName") or "").strip()

                def get(key):
                    i = idx.get(key)
                    return stats[i] if (i is not None and i < len(stats)) else ""

                fgm, fga = split_made_att(get("fg"))
                fg3m, fg3a = split_made_att(get("3pt"))
                ftm, fta = split_made_att(get("ft"))
                rows.append({
                    "game_id": event_id,
                    "player_id": pid,
                    "player_name": pname,
                    "team_id": tid,
                    "team_abbr": tabbr,
                    "min": to_int_str(get("min")),
                    "pts": to_int_str(get("pts")),
                    "reb": to_int_str(get("reb")),
                    "ast": to_int_str(get("ast")),
                    "stl": to_int_str(get("stl")),
                    "blk": to_int_str(get("blk")),
                    "tov": to_int_str(get("tov")),
                    "fga": fga, "fgm": fgm,
                    "fg3a": fg3a, "fg3m": fg3m,
                    "fta": fta, "ftm": ftm,
                    "pf": to_int_str(get("pf")),
                    "plus_minus": to_int_str(get("pm")),
                })
    return rows


def parse_officials_rows(summary, event_id):
    _, lst = pick_officials_list(summary)
    rows = []
    for entry in lst or []:
        oid, nm, jr = official_fields(entry)
        if not (oid or nm):
            continue
        rows.append({
            "game_id": event_id,
            "official_id": oid,
            "official_name": nm,
            "jersey_num": jr,
        })
    return rows


# --------------------------------------------------------------------------- #
# Additive merge helpers
# --------------------------------------------------------------------------- #
def read_existing(path, columns):
    """Read an existing gz extract as all-strings (preserves leading zeros)."""
    if not os.path.exists(path):
        return pd.DataFrame(columns=columns)
    df = pd.read_csv(path, compression="gzip", dtype=str, keep_default_na=False)
    for c in columns:
        if c not in df.columns:
            df[c] = ""
    return df[columns]


def write_gz(df, path, columns):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df[columns].to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")


def merge_rows(path, columns, new_rows, key_cols):
    """Add new_rows to the extract at path, replacing only rows whose game_id is
    in the incoming batch (ESPN ids); pre-2023 NBA rows are never touched."""
    existing = read_existing(path, columns)
    if not new_rows:
        return len(existing)
    add = pd.DataFrame(new_rows, columns=columns).astype(str)
    incoming_ids = set(add["game_id"].unique())
    kept = existing[~existing["game_id"].isin(incoming_ids)]
    combined = pd.concat([kept, add], ignore_index=True)
    combined = combined.drop_duplicates(key_cols, keep="last")
    write_gz(combined, path, columns)
    return len(combined)


def merge_player_logs(new_rows_by_file):
    """new_rows_by_file: {(season,suffix): [rows]} -> merge each into its file."""
    for (season, suffix), rows in new_rows_by_file.items():
        if not rows:
            continue
        path = os.path.join(PLAYER_LOGS_DIR, "{}_{}.csv.gz".format(season, suffix))
        merge_rows(path, PLAYER_COLUMNS, rows, ["game_id", "player_id"])


# --------------------------------------------------------------------------- #
# --clean : purge exhibition (non-NBA-team) games from already-written extracts
# --------------------------------------------------------------------------- #
def clean_exhibitions():
    """Remove All-Star / exhibition games that leaked into the extracts. A game
    is an exhibition if its home or away abbreviation is not a real NBA franchise
    -- neither a current tricode (VALID_TRICODES) nor a frozen historical one
    (HISTORICAL_TRICODES: SEA/NJN/VAN/CHH/NOH/NOK). Scoped to ESPN-era ids so
    pre-2023 NBA rows are NEVER touched; the HISTORICAL_TRICODES allow-list further
    protects legitimate ESPN-era defunct-team games (e.g. 2000-03 Nets/Sonics) that
    is_nba_game_id() does not, since those carry ESPN-scheme ids. Purges matching
    rows from games, officials, and every player_logs file, printing what it removes."""
    print("=== --clean: purging exhibition (non-NBA-team) games ===")
    if not os.path.exists(GAMES_PATH):
        print("  no games.csv.gz found; nothing to clean.")
        return

    games = pd.read_csv(GAMES_PATH, compression="gzip", dtype=str, keep_default_na=False)

    def is_exhibition(row):
        gid = row["game_id"]
        if is_nba_game_id(gid):
            return False  # pre-2023 NBA row -- never touch (protects SEA/NJN/etc.)
        return (row["home_team_abbr"] not in ALLOWED_TRICODES
                or row["away_team_abbr"] not in ALLOWED_TRICODES)

    mask = games.apply(is_exhibition, axis=1) if len(games) else pd.Series([], dtype=bool)
    exhibition_ids = set(games.loc[mask, "game_id"]) if len(games) else set()

    if not exhibition_ids:
        print("  no exhibition games found. Extracts are clean.")
        return

    print("  found {} exhibition game(s) to purge:".format(len(exhibition_ids)))
    for _, r in games[mask].iterrows():
        print("    {}  {}  {} vs {}".format(
            r["game_id"], r.get("game_date", ""),
            r["away_team_abbr"], r["home_team_abbr"]))

    # games.csv.gz
    kept = games[~mask]
    write_gz(kept, GAMES_PATH, GAMES_COLUMNS)
    print("  games.csv.gz:      {} -> {} rows".format(len(games), len(kept)))

    # officials.csv.gz
    if os.path.exists(OFFICIALS_PATH):
        off = pd.read_csv(OFFICIALS_PATH, compression="gzip", dtype=str, keep_default_na=False)
        keep_off = off[~off["game_id"].isin(exhibition_ids)]
        if len(keep_off) != len(off):
            write_gz(keep_off, OFFICIALS_PATH, OFFICIALS_COLUMNS)
        print("  officials.csv.gz:  {} -> {} rows".format(len(off), len(keep_off)))

    # player_logs/*.csv.gz
    for path in sorted(glob.glob(os.path.join(PLAYER_LOGS_DIR, "*.csv.gz"))):
        pl = pd.read_csv(path, compression="gzip", dtype=str, keep_default_na=False)
        keep_pl = pl[~pl["game_id"].isin(exhibition_ids)]
        if len(keep_pl) != len(pl):
            write_gz(keep_pl, path, PLAYER_COLUMNS)
            print("  {}: {} -> {} rows".format(os.path.basename(path), len(pl), len(keep_pl)))
    print("  purge complete.")


# --------------------------------------------------------------------------- #
# Freshness note (append, don't clobber the nbadb freshness report)
# --------------------------------------------------------------------------- #
def _officials_coverage_lines():
    """Per season+type officials coverage over ALL ESPN-era games currently in the
    extract (reads the just-written files, so it reflects real coverage, not the
    20-game probe sample). Mirrors extract_from_nbadb.py's '3-officials=X%' metric
    (share of games with exactly 3 officials) and adds the count + % of games with
    FEWER than 3 officials rows -- the actual coverage gap (e.g. the 2001 Finals
    game whose ESPN summary carried no officials array)."""
    lines = ["",
             "Officials coverage per season+type (all ESPN-era games in the extract;",
             "3-officials=X% mirrors extract_from_nbadb.py so it is comparable to the",
             "nbadb baseline; '<3' is the real gap):"]
    if not os.path.exists(GAMES_PATH):
        lines.append("  (no games.csv.gz yet)")
        return lines
    games = pd.read_csv(GAMES_PATH, compression="gzip", dtype=str, keep_default_na=False)
    # ESPN-era games only (ESPN event ids; never NBA-format '00...' ids).
    games = games[~games["game_id"].map(is_nba_game_id)].copy()
    if games.empty:
        lines.append("  (no ESPN-era games in the extract)")
        return lines
    if os.path.exists(OFFICIALS_PATH):
        off = pd.read_csv(OFFICIALS_PATH, compression="gzip", dtype=str, keep_default_na=False)
        per_game = off.groupby("game_id")["official_id"].nunique()
    else:
        per_game = pd.Series(dtype=int)
    games["_n_officials"] = games["game_id"].map(per_game).fillna(0).astype(int)
    for (season, stype), grp in sorted(games.groupby(["season", "season_type"]),
                                       key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        n = len(grp)
        n3 = int((grp["_n_officials"] == 3).sum())
        n_lt = int((grp["_n_officials"] < 3).sum())
        pct3 = 100.0 * n3 / n if n else 0.0
        pct_lt = 100.0 * n_lt / n if n else 0.0
        lines.append("  {:<8} {:<16} games={:<5} 3-officials={:.1f}%  (<3: {} games, {:.1f}%)"
                     .format(str(season), str(stype), n, pct3, n_lt, pct_lt))
    return lines


def write_espn_freshness(summary_counts):
    path = os.path.join(SOURCE_DIR, "_freshness_espn.txt")
    lines = [
        "ESPN-era ingestion report (fetch_espn_seasons.py)",
        "=" * 60,
        "",
        "Seasons pulled from ESPN (public JSON API), replacing the missing",
        "post-2023-06-12 window plus all of 2012-13 (regular season + playoffs).",
        "",
        "JOIN-KEY / IDENTITY NOTES for build.py (session 2):",
        "  * game_id column holds the ESPN EVENT ID for these rows (not an NBA",
        "    game_id). ESPN ids start with a non-zero digit and are ~9 digits;",
        "    NBA ids are 10 digits starting '00'. The eras never collide.",
        "  * team_id holds the ESPN team id (1..30), NOT the NBA team id. Join",
        "    teams across eras on the abbreviation (normalized to NBA tricodes).",
        "  * official_id is 'espn:firstname-lastname' (or 'espn-athlete:<id>')",
        "    for ESPN-era rows -- these DO NOT match NBA official_ids. build.py",
        "    must reconcile referees across eras by normalized name.",
        "  * Playoff round / Game 7 labeling is intentionally skipped here.",
        "",
        "2012-13 DOUBLE-COUNT WARNING for build.py (session 2):",
        "  * ALL of 2012-13 (regular season AND playoffs) is now sourced from ESPN,",
        "    so the whole 2012-13 season lives in the ESPN event-id scheme end to end.",
        "  * nbadb contains only a PARTIAL 2012-13 playoff fragment (~85 games) under",
        "    NBA game_ids. build.py MUST IGNORE that nbadb 2012-13 playoff fragment and",
        "    use these ESPN 2012-13 rows instead -- otherwise the 2012-13 postseason is",
        "    double-counted across two id schemes. (Run ingest_kaggle_player_logs.py with",
        "    --exclude-seasons 2012-13 so the Kaggle side never emits 2012-13 either.)",
        "",
        "PLAY-IN GAMES:",
        "  * The play-in tournament is a SEPARATE ESPN season type (5), not part of",
        "    the playoffs (type 3) -- that is why play-in dates initially returned no",
        "    games. Play-in games are now captured and labeled season_type 'Play-In'",
        "    (file suffix PI), consistent with the NBA-era play-in convention. build.py",
        "    should treat play-in as its own label (playoffs=False), per the spec.",
        "",
        "EXHIBITION FILTER:",
        "  * All-Star weekend exhibitions (teams like WEST/EAST/CHK/KEN/SHQ/CAN/STARS/",
        "    WORLD/STRIPES) leaked in as type-2 games. Any game whose home OR away",
        "    abbreviation is not a real NBA franchise -- current OR historical -- is",
        "    dropped on fetch.",
        "  * Already-written exhibition rows were purged with:  fetch_espn_seasons.py --clean",
        "    (frozen historical tricodes SEA/NJN/VAN/CHH/NOH/NOK are allow-listed, so",
        "    legitimate defunct-team games -- including ESPN-era 2000-03 -- are kept).",
        "",
        "ALTERNATE OFFICIAL (build.py REQUIREMENT):",
        "  * ESPN lists 4 officials for many PLAYOFF games; the 4th is an ALTERNATE who",
        "    did not actually officiate. Extraction keeps ALL officials rows unchanged.",
        "  * In the ESPN payload the alternate is the LAST entry of the officials array",
        "    (highest 'order'); the fetch prints a one-time PLAYOFF OFFICIALS PROBE of a",
        "    real 4-official array so this can be confirmed against the 'order'/'position'",
        "    fields. Regular crews are 3 (crew chief / referee / umpire).",
        "  * Because officials.csv.gz preserves ESPN's ordering, for an ESPN playoff",
        "    game_id with >3 officials rows, the row(s) beyond the first 3 (by row order",
        "    within that game_id) are alternates. build.py MUST NOT credit alternates as",
        "    having officiated -- count only the first 3 officials per game.",
        "",
        "Per season+type rows written this run:",
    ]
    for key in sorted(summary_counts):
        lines.append("  {:<16} games={:<5} officials={:<6} player_rows={}".format(
            key, *summary_counts[key]))
    lines.extend(_officials_coverage_lines())
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("-> wrote source-data/_freshness_espn.txt")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    os.makedirs(PLAYER_LOGS_DIR, exist_ok=True)
    today = datetime.date.today()
    dates_done = load_progress()
    print("Resume: {} dates already completed.".format(len(dates_done)))

    # ---- One-time officials probe before bulk fetching ----
    print("Probing ESPN for the officials field location...")
    probed = False
    for season in SEASONS:
        if probed:
            break
        start = datetime.date.fromisoformat(season["start"])
        end = min(datetime.date.fromisoformat(season["end"]), today)
        for d in daterange(start, end):
            sb = get_json(SCOREBOARD_URL, {"dates": d.strftime("%Y%m%d")})
            time.sleep(DELAY_SECONDS)
            for ev in sb.get("events", []):
                status = (((ev.get("status") or {}).get("type")) or {})
                if status.get("completed") or status.get("state") == "post":
                    summ = get_json(SUMMARY_URL, {"event": str(ev.get("id"))})
                    time.sleep(DELAY_SECONDS)
                    probe_officials(summ)
                    probed = True
                    break
            if probed:
                break
    if not probed:
        print("  !! Could not find a completed game to probe. Aborting before bulk fetch.")
        sys.exit(1)

    # ---- Bulk walk ----
    summary_counts = {}  # "season TYPE" -> [games, officials, player_rows]
    exhibition_skipped = []  # (game_id, away_abbr, home_abbr) dropped as exhibitions

    def bump(season, suffix, g=0, o=0, p=0):
        key = "{} {}".format(season, suffix)
        cur = summary_counts.get(key, [0, 0, 0])
        summary_counts[key] = [cur[0] + g, cur[1] + o, cur[2] + p]

    for season in SEASONS:
        label = season["label"]
        start = datetime.date.fromisoformat(season["start"])
        end = min(datetime.date.fromisoformat(season["end"]), today)
        types = season["types"]
        print("\n=== Season {} ({} .. {}), ESPN types {} ===".format(
            label, start, end, sorted(types)))

        for d in daterange(start, end):
            dkey = d.isoformat()
            if dkey in dates_done:
                continue
            try:
                sb = get_json(SCOREBOARD_URL, {"dates": d.strftime("%Y%m%d")})
            except RuntimeError as e:
                print("  {} scoreboard FAILED: {} (will retry on next run)".format(dkey, e))
                continue
            time.sleep(DELAY_SECONDS)

            events = sb.get("events", [])
            if not events:
                if d < today:
                    dates_done.add(dkey)
                continue

            game_rows, official_rows = [], []
            player_rows_by_file = {}
            all_complete = True

            for ev in events:
                stype = int((ev.get("season") or {}).get("type", 0))
                playin = is_play_in(ev)
                # Keep games of the wanted types, plus any play-in game (separate
                # ESPN season type). Preseason/all-star types are excluded here;
                # exhibition leaks are then dropped by the tricode check below.
                if stype not in types and not playin:
                    continue
                row, suffix = parse_game_row(ev, label)
                if row is None:
                    continue
                # Force the Play-In label regardless of how ESPN typed the event.
                if playin:
                    row["season_type"], suffix = ("Play-In", "PI")

                # Drop exhibition games (All-Star / Rising Stars / celebrity, etc.)
                # whose "teams" are not real NBA franchises (current OR historical).
                if (row["home_team_abbr"] not in ALLOWED_TRICODES
                        or row["away_team_abbr"] not in ALLOWED_TRICODES):
                    exhibition_skipped.append(
                        (row["game_id"], row["away_team_abbr"], row["home_team_abbr"]))
                    continue
                game_rows.append(row)

                status = (((ev.get("status") or {}).get("type")) or {})
                completed = status.get("completed") or status.get("state") == "post"
                if not completed:
                    all_complete = False
                    continue

                try:
                    summ = get_json(SUMMARY_URL, {"event": row["game_id"]})
                except RuntimeError as e:
                    print("    game {} summary FAILED: {}".format(row["game_id"], e))
                    all_complete = False
                    time.sleep(DELAY_SECONDS)
                    continue
                time.sleep(DELAY_SECONDS)

                if suffix == "PO":
                    probe_playoff_officials(summ)
                orows = parse_officials_rows(summ, row["game_id"])
                prows = parse_player_rows(summ, row["game_id"])
                official_rows.extend(orows)
                player_rows_by_file.setdefault((label, suffix), []).extend(prows)
                bump(label, suffix, g=1, o=len(orows), p=len(prows))

            # Flush this date's rows additively.
            if game_rows:
                merge_rows(GAMES_PATH, GAMES_COLUMNS, game_rows, ["game_id"])
            if official_rows:
                merge_rows(OFFICIALS_PATH, OFFICIALS_COLUMNS, official_rows,
                           ["game_id", "official_id"])
            if player_rows_by_file:
                merge_player_logs(player_rows_by_file)

            print("  {}: {} games, {} officials rows, {} completed".format(
                dkey, len(game_rows), len(official_rows),
                "all" if all_complete else "partial"))

            # Only mark a date done if it is in the past AND fully complete.
            if d < today and all_complete:
                dates_done.add(dkey)
                save_progress(dates_done)

    save_progress(dates_done)
    write_espn_freshness(summary_counts)
    print("\nDONE. Rows written this run:")
    for key in sorted(summary_counts):
        print("  {:<16} games={:<5} officials={:<6} player_rows={}".format(
            key, *summary_counts[key]))
    if exhibition_skipped:
        print("\nDropped {} exhibition (non-NBA-team) game(s) on fetch:".format(
            len(exhibition_skipped)))
        for gid, away, home in exhibition_skipped:
            print("  {}  {} vs {}".format(gid, away, home))
    print("\nExtracts updated additively; pre-2023 NBA rows untouched.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Fetch missing NBA seasons from ESPN into source-data/ extracts.")
    ap.add_argument("--clean", action="store_true",
                    help="Purge already-written exhibition (non-NBA-team) games and exit.")
    cli = ap.parse_args()
    if cli.clean:
        clean_exhibitions()
    else:
        main()
