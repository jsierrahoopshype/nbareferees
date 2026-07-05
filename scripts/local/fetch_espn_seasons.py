"""
fetch_espn_seasons.py  --  LOCAL script (run by Jorge on Windows, in cmd).
ADDITIVE. Does NOT modify the other scripts or any pre-2023 rows.

Why this exists
---------------
The Kaggle nbadb snapshot ends 2023-06-12, and stats.nba.com / cdn.nba.com are
geo-blocked from Jorge's location (VPN exits blocked too), so nba_api is
unusable. ESPN's public JSON API is NOT geo-blocked, so we source the missing
seasons from ESPN instead:

    * 2023-24, 2024-25, 2025-26  (regular season + playoffs)
    * 2012-13  (regular season + playoffs -- the whole season in the ESPN scheme)

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
  officials field location + a sample entry are printed for verification.
* Outputs utf-8-sig gzip, merged additively into the existing extracts.
* Uses only the Python standard library + pandas (no nba_api, no requests).
"""

import os
import re
import sys
import gzip
import json
import time
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
# ESPN season.type: 2 = regular season, 3 = postseason (playoffs / play-in).
SEASONS = [
    {"label": "2012-13", "start": "2012-10-30", "end": "2013-06-25", "types": {2, 3}},
    {"label": "2023-24", "start": "2023-10-24", "end": "2024-06-25", "types": {2, 3}},
    {"label": "2024-25", "start": "2024-10-22", "end": "2025-06-25", "types": {2, 3}},
    {"label": "2025-26", "start": "2025-10-21", "end": "2026-06-25", "types": {2, 3}},
]

SEASON_TYPE_LABELS = {2: ("Regular Season", "RS"), 3: ("Playoffs", "PO")}

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

# ESPN abbreviation -> NBA-standard tricode (cross-era join key). Unknown
# abbreviations pass through unchanged with a printed warning.
ESPN_TO_NBA_ABBR = {
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "BRK": "BKN", "CHA": "CHA",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN", "DET": "DET",
    "GS": "GSW", "GSW": "GSW", "HOU": "HOU", "IND": "IND", "LAC": "LAC",
    "LAL": "LAL", "MEM": "MEM", "MIA": "MIA", "MIL": "MIL", "MIN": "MIN",
    "NO": "NOP", "NOP": "NOP", "NY": "NYK", "NYK": "NYK", "OKC": "OKC",
    "ORL": "ORL", "PHI": "PHI", "PHX": "PHX", "PHO": "PHX", "POR": "POR",
    "SAC": "SAC", "SA": "SAS", "SAS": "SAS", "TOR": "TOR", "UTAH": "UTA",
    "UTA": "UTA", "WSH": "WAS", "WAS": "WAS",
}
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
# Freshness note (append, don't clobber the nbadb freshness report)
# --------------------------------------------------------------------------- #
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
        "Per season+type rows written this run:",
    ]
    for key in sorted(summary_counts):
        lines.append("  {:<16} games={:<5} officials={:<6} player_rows={}".format(
            key, *summary_counts[key]))
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
                if stype not in types:
                    continue
                row, suffix = parse_game_row(ev, label)
                if row is None:
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
    print("\nExtracts updated additively; pre-2023 NBA rows untouched.")


if __name__ == "__main__":
    main()
