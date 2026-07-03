"""
extract_from_nbadb.py  --  LOCAL script (run by Jorge on Windows, in cmd).

Reads the raw nbadb SQLite (downloaded manually from Kaggle) and writes slim,
gzipped CSV extracts into source-data/. The raw DB never enters the repo.

  python scripts\\local\\extract_from_nbadb.py "C:\\Users\\Jorge Sierra\\Downloads\\nba.sqlite"

Design constraints from docs/PHASE1_SPEC.md:
  * Never assume table names. The nbadb schema was overhauled in 2025-26 into a
    star-schema warehouse; old names ("game", "officials") may be gone. This
    script INTROSPECTS the schema and locates tables by column signature.
  * game_id is a 10-digit STRING with leading zeros ("0022300001"). Every read
    forces string ids and zero-pads game_id to width 10. Dropping a leading zero
    silently breaks every downstream join. This is the #1 project risk.
  * Output CSVs use utf-8-sig encoding (Windows/Excel), gzip compression.
  * NO NETWORK. This script only touches the local SQLite file.

Outputs:
  source-data/officials.csv.gz
  source-data/games.csv.gz
  source-data/player_logs/{season}_{RS|PO|PI}.csv.gz   (only if player box found)
  source-data/_schema_report.txt
  source-data/_freshness.txt
"""

import os
import re
import sys
import sqlite3

import pandas as pd

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
PLAYER_LOGS_DIR = os.path.join(SOURCE_DIR, "player_logs")

# Season filter: keep 1996-97 onward (start year >= 1996). The site covers
# 2000-01+, but we extract a little earlier so player-season baselines near the
# 2000-01 boundary are computable.
MIN_SEASON_START_YEAR = 1996

# Season-type digit (game_id index 2) -> (file/label suffix, human label)
SEASON_TYPE_MAP = {
    "1": ("PRE", "Pre Season"),
    "2": ("RS", "Regular Season"),
    "3": ("AS", "All-Star"),
    "4": ("PO", "Playoffs"),
    "5": ("PI", "Play-In"),
}
# Season types we keep in the extracts (drop preseason / all-star noise).
KEEP_TYPES = {"RS", "PO", "PI"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def norm(name):
    """Normalize a column/table name for fuzzy matching: lowercase, alnum only."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def clean_id(series, width=None):
    """
    Coerce an id column to clean strings.

    Handles ints, floats (that picked up a trailing '.0' or NaN), and text.
    Optionally zero-pads to a fixed width (used for game_id -> width 10) so
    leading zeros are restored even if the DB stored the id as an integer.
    """
    s = series
    if pd.api.types.is_float_dtype(s) or pd.api.types.is_integer_dtype(s):
        s = s.astype("Int64").astype("string")
    else:
        s = s.astype("string").str.strip()
        s = s.str.replace(r"\.0$", "", regex=True)
    s = s.replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA, "": pd.NA})
    out = s.fillna("")
    if width:
        out = out.where(out == "", out.str.zfill(width))
    return out


def season_start_year_from_gid(gid):
    """Digits 4-5 of the game_id give the season start year (pivot at 46)."""
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


def season_type_from_gid(gid):
    """Return (suffix, label) from game_id index 2; ('??','Unknown') if unknown."""
    if not gid or len(gid) < 3:
        return ("??", "Unknown")
    return SEASON_TYPE_MAP.get(gid[2], ("??", "Unknown"))


def find_col(columns, patterns, exclude=None):
    """
    Return the first column whose normalized name matches any pattern.

    patterns: list of substrings (already normalized) to look for.
    exclude:  list of normalized substrings that disqualify a column.
    Preference: exact normalized match first, then substring containment.
    """
    exclude = exclude or []
    ncols = {c: norm(c) for c in columns}
    # Exact match pass.
    for pat in patterns:
        for c, nc in ncols.items():
            if nc == pat and not any(x in nc for x in exclude):
                return c
    # Substring pass.
    for pat in patterns:
        for c, nc in ncols.items():
            if pat in nc and not any(x in nc for x in exclude):
                return c
    return None


# --------------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------------- #
def introspect(conn):
    """
    Return {table_name: {"cols": [...], "rows": int}} and write _schema_report.txt.
    """
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]

    info = {}
    lines = ["nbadb schema report", "=" * 60, ""]
    for t in tables:
        try:
            cur.execute('SELECT COUNT(*) FROM "{}"'.format(t))
            nrows = cur.fetchone()[0]
        except sqlite3.Error as e:
            nrows = -1
            print("  ! could not count rows in {}: {}".format(t, e))
        cur.execute('PRAGMA table_info("{}")'.format(t))
        cols = [row[1] for row in cur.fetchall()]
        info[t] = {"cols": cols, "rows": nrows}

        header = "TABLE {}  ({} rows, {} cols)".format(t, nrows, len(cols))
        lines.append(header)
        lines.append("-" * len(header))
        lines.append("  " + ", ".join(cols))
        lines.append("")
        print(header)
        print("  " + ", ".join(cols))

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(os.path.join(SOURCE_DIR, "_schema_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n-> wrote source-data/_schema_report.txt")
    return info


def read_table(conn, table, columns=None):
    """Read a whole table (or a subset of columns) into a DataFrame."""
    if columns:
        col_sql = ", ".join('"{}"'.format(c) for c in columns)
    else:
        col_sql = "*"
    return pd.read_sql_query('SELECT {} FROM "{}"'.format(col_sql, table), conn)


# --------------------------------------------------------------------------- #
# Locate the officials data
# --------------------------------------------------------------------------- #
GAME_ID_PATS = ["gameid", "gamekey", "gamesk", "gameidfk", "gid", "gmeid"]
OFFICIAL_ID_PATS = ["officialid", "refereeid", "refid", "officialpersonid"]
PERSON_ID_PATS = ["personid", "officialid", "refereeid", "refid", "id"]
JERSEY_PATS = ["jerseynum", "jerseynumber", "jersey", "number", "num"]


def locate_officials(conn, info):
    """
    Find the officials assignment data. Two supported shapes:
      A) one table with game_id + official_id (+ name/jersey)
      B) dimension (official_id + name) + bridge/fact (game_id + official_id)

    Returns a DataFrame: game_id, official_id, official_name, jersey_num
    or None if nothing plausible is found.
    """
    print("\n== Locating officials ==")

    # Candidate tables mention 'official' or 'referee' somewhere in name/cols.
    def mentions_official(t):
        nt = norm(t)
        if "official" in nt or "referee" in nt or "ref" in nt:
            return True
        return any("official" in norm(c) or "referee" in norm(c)
                   for c in info[t]["cols"])

    candidates = [t for t in info if mentions_official(t)]
    print("  candidate tables:", candidates or "(none)")

    fact = None       # has game_id + official_id
    fact_cols = None
    for t in candidates:
        cols = info[t]["cols"]
        gid = find_col(cols, GAME_ID_PATS)
        oid = find_col(cols, OFFICIAL_ID_PATS) or find_col(cols, PERSON_ID_PATS)
        if gid and oid:
            fact, fact_cols = t, cols
            print("  -> officials fact/bridge table: {} (game_id={}, official_id={})"
                  .format(t, gid, oid))
            break

    if fact is None:
        print("  !! could not find a table with both game_id and official_id.")
        return None

    gid_c = find_col(fact_cols, GAME_ID_PATS)
    oid_c = find_col(fact_cols, OFFICIAL_ID_PATS) or find_col(fact_cols, PERSON_ID_PATS)
    name_c = find_col(fact_cols, ["officialname", "refereename", "name"], exclude=["first", "last"])
    first_c = find_col(fact_cols, ["firstname", "first", "fname"])
    last_c = find_col(fact_cols, ["lastname", "last", "lname"])
    jer_c = find_col(fact_cols, JERSEY_PATS)

    df = read_table(conn, fact)
    out = pd.DataFrame()
    out["game_id"] = clean_id(df[gid_c], width=10)
    out["official_id"] = clean_id(df[oid_c])

    have_name = False
    if name_c is not None:
        out["official_name"] = df[name_c].astype("string").str.strip()
        have_name = out["official_name"].str.len().fillna(0).gt(0).any()
    if not have_name and first_c and last_c:
        out["official_name"] = (
            df[first_c].astype("string").str.strip().fillna("")
            + " "
            + df[last_c].astype("string").str.strip().fillna("")
        ).str.strip()
        have_name = True
    if jer_c is not None:
        out["jersey_num"] = clean_id(df[jer_c])
    else:
        out["jersey_num"] = ""

    # Shape B: names live in a separate dimension table -> join on official_id.
    if not have_name:
        print("  names absent from fact table; looking for an official dimension...")
        dim = None
        for t in candidates:
            if t == fact:
                continue
            cols = info[t]["cols"]
            oid2 = find_col(cols, OFFICIAL_ID_PATS) or find_col(cols, PERSON_ID_PATS)
            nm = find_col(cols, ["officialname", "refereename", "name"], exclude=["first", "last"])
            fn = find_col(cols, ["firstname", "first", "fname"])
            ln = find_col(cols, ["lastname", "last", "lname"])
            if oid2 and (nm or (fn and ln)):
                dim = t
                dim_oid, dim_nm, dim_fn, dim_ln = oid2, nm, fn, ln
                break
        if dim:
            print("  -> official dimension table: {}".format(dim))
            ddf = read_table(conn, dim)
            ddf["_oid"] = clean_id(ddf[dim_oid])
            if dim_nm:
                ddf["_name"] = ddf[dim_nm].astype("string").str.strip()
            else:
                ddf["_name"] = (
                    ddf[dim_fn].astype("string").str.strip().fillna("")
                    + " "
                    + ddf[dim_ln].astype("string").str.strip().fillna("")
                ).str.strip()
            if dim_jer := find_col(ddf.columns, JERSEY_PATS):
                ddf["_jersey"] = clean_id(ddf[dim_jer])
            name_map = ddf.drop_duplicates("_oid").set_index("_oid")["_name"]
            out["official_name"] = out["official_id"].map(name_map).fillna("")
            if "_jersey" in ddf.columns and (out["jersey_num"] == "").all():
                jer_map = ddf.drop_duplicates("_oid").set_index("_oid")["_jersey"]
                out["jersey_num"] = out["official_id"].map(jer_map).fillna("")
        else:
            print("  !! no official-name dimension found; names will be blank.")
            out["official_name"] = ""

    out = out[["game_id", "official_id", "official_name", "jersey_num"]]
    out = out[(out["game_id"] != "") & (out["official_id"] != "")]
    out = out.drop_duplicates(["game_id", "official_id"])
    print("  officials rows (pre season-filter): {}".format(len(out)))
    return out


# --------------------------------------------------------------------------- #
# Locate the games data
# --------------------------------------------------------------------------- #
def locate_games(conn, info):
    """
    Find the games table by column signature and return the spec's game columns.
    Returns DataFrame with:
      game_id, game_date, season, season_type, home_team_id, home_team_abbr,
      away_team_id, away_team_abbr, home_pts, away_pts, home_win
    """
    print("\n== Locating games ==")

    # Score every table on how many game-level signals it carries. The games
    # table is per-game (one row per game_id), NOT per-player, so it must have
    # a game id, a date, and home/away team columns but NO player id.
    best, best_score = None, -1
    for t, meta in info.items():
        cols = meta["cols"]
        if find_col(cols, PERSON_ID_PATS) and find_col(cols, ["playerid"]):
            continue  # per-player table; not the games table
        if find_col(cols, ["playerid"]):
            continue
        gid = find_col(cols, GAME_ID_PATS)
        if not gid:
            continue
        score = 0
        score += bool(find_col(cols, ["gamedate", "date"]))
        score += bool(find_col(cols, ["hometeamid", "teamidhome"]))
        score += bool(find_col(cols, ["awayteamid", "visitorteamid", "teamidaway"]))
        score += bool(find_col(cols, ["homepts", "ptshome", "homescore"]))
        score += bool(find_col(cols, ["awaypts", "visitorpts", "ptsaway", "awayscore"]))
        score += bool(find_col(cols, ["season"]))
        if score > best_score:
            best, best_score = t, score

    if best is None or best_score < 3:
        print("  !! could not confidently identify a games table (best={}, score={})"
              .format(best, best_score))
        if best is None:
            return None
    print("  -> games table: {} (signal score {})".format(best, best_score))

    cols = info[best]["cols"]
    df = read_table(conn, best)

    gid_c = find_col(cols, GAME_ID_PATS)
    date_c = find_col(cols, ["gamedate", "date"])
    season_c = find_col(cols, ["seasonid", "season", "seasonyear"])
    stype_c = find_col(cols, ["seasontype", "gametype"])
    h_id = find_col(cols, ["hometeamid", "teamidhome"])
    a_id = find_col(cols, ["awayteamid", "visitorteamid", "teamidaway"])
    h_abbr = find_col(cols, ["teamabbreviationhome", "hometeamabbreviation", "hometeamabbr",
                             "teamabbrhome", "hometeamtricode", "teamtricodehome"])
    a_abbr = find_col(cols, ["teamabbreviationaway", "awayteamabbreviation", "visitorteamabbreviation",
                             "awayteamabbr", "teamabbraway", "awayteamtricode", "teamtricodeaway"])
    h_pts = find_col(cols, ["homepts", "ptshome", "homescore"])
    a_pts = find_col(cols, ["awaypts", "visitorpts", "ptsaway", "awayscore"])
    wl_home = find_col(cols, ["wlhome", "homewl", "homewin"])

    out = pd.DataFrame()
    out["game_id"] = clean_id(df[gid_c], width=10)

    # Date -> ISO yyyy-mm-dd string.
    if date_c:
        out["game_date"] = pd.to_datetime(df[date_c], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        out["game_date"] = ""

    # Season / season_type: prefer source columns, fall back to game_id digits.
    derived_year = out["game_id"].map(season_start_year_from_gid)
    derived_season = derived_year.map(season_str_from_start_year)
    derived_type = out["game_id"].map(lambda g: season_type_from_gid(g)[1])

    if season_c:
        src_season = df[season_c].astype("string").str.strip()
        out["season"] = src_season.where(src_season.str.len().fillna(0) > 0, derived_season)
    else:
        out["season"] = derived_season
    # Normalize numeric-year seasons (e.g. "2023") to "2023-24".
    def norm_season(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return v
        s = str(v).strip()
        m = re.fullmatch(r"(\d{4})", s)
        if m:
            y = int(m.group(1))
            return season_str_from_start_year(y)
        # nbadb season_id like "22023" -> 2023
        m = re.fullmatch(r"[2-5](\d{4})", s)
        if m:
            return season_str_from_start_year(int(m.group(1)))
        return s
    out["season"] = out["season"].map(norm_season)
    out["season"] = out["season"].fillna(derived_season)

    if stype_c:
        src_type = df[stype_c].astype("string").str.strip()
        out["season_type"] = src_type.where(src_type.str.len().fillna(0) > 0, derived_type)
    else:
        out["season_type"] = derived_type

    out["home_team_id"] = clean_id(df[h_id]) if h_id else ""
    out["away_team_id"] = clean_id(df[a_id]) if a_id else ""
    out["home_team_abbr"] = df[h_abbr].astype("string").str.strip() if h_abbr else ""
    out["away_team_abbr"] = df[a_abbr].astype("string").str.strip() if a_abbr else ""

    # If abbreviations are missing, try to join a team dimension by team_id.
    if (h_abbr is None or a_abbr is None):
        team_map = _build_team_abbr_map(conn, info)
        if team_map is not None:
            if h_abbr is None and h_id:
                out["home_team_abbr"] = out["home_team_id"].map(team_map).fillna("")
            if a_abbr is None and a_id:
                out["away_team_abbr"] = out["away_team_id"].map(team_map).fillna("")

    out["home_pts"] = pd.to_numeric(df[h_pts], errors="coerce") if h_pts else pd.NA
    out["away_pts"] = pd.to_numeric(df[a_pts], errors="coerce") if a_pts else pd.NA

    # home_win: prefer an explicit W/L column, else derive from points.
    if wl_home:
        wl = df[wl_home].astype("string").str.upper().str.strip()
        out["home_win"] = wl.map({"W": 1, "L": 0})
    else:
        out["home_win"] = pd.NA
    need = out["home_win"].isna()
    derivable = need & out["home_pts"].notna() & out["away_pts"].notna()
    out.loc[derivable, "home_win"] = (
        out.loc[derivable, "home_pts"] > out.loc[derivable, "away_pts"]
    ).astype(int)
    out["home_win"] = out["home_win"].astype("Int64")

    out = out[out["game_id"] != ""].drop_duplicates("game_id")
    print("  games rows (pre season-filter): {}".format(len(out)))
    return out


def _build_team_abbr_map(conn, info):
    """team_id -> abbreviation, from any table that looks like a team dimension."""
    for t, meta in info.items():
        cols = meta["cols"]
        tid = find_col(cols, ["teamid", "id"])
        abbr = find_col(cols, ["abbreviation", "abbr", "tricode"])
        looks_team = "team" in norm(t)
        if tid and abbr and looks_team:
            df = read_table(conn, t, [tid, abbr])
            df["_id"] = clean_id(df[tid])
            m = df.drop_duplicates("_id").set_index("_id")[abbr].astype("string").str.strip()
            print("  -> team abbreviation dimension: {}".format(t))
            return m
    return None


# --------------------------------------------------------------------------- #
# Locate player box scores
# --------------------------------------------------------------------------- #
# Output column set (§3.1.5): the value is a list of normalized source patterns.
PLAYER_COLS = {
    "game_id": GAME_ID_PATS,
    "player_id": ["playerid", "personid"],
    "player_name": ["playername", "player", "name", "displayname"],
    "team_id": ["teamid"],
    "team_abbr": ["teamabbreviation", "teamabbr", "tricode"],
    "min": ["min", "minutes"],
    "pts": ["pts", "points"],
    "reb": ["reb", "totreb", "rebounds", "treb"],
    "ast": ["ast", "assists"],
    "stl": ["stl", "steals"],
    "blk": ["blk", "blocks"],
    "tov": ["tov", "to", "turnovers"],
    "fga": ["fga"],
    "fgm": ["fgm"],
    "fg3a": ["fg3a", "fg3pa", "tpa", "threepa"],
    "fg3m": ["fg3m", "fg3pm", "tpm", "threepm"],
    "fta": ["fta"],
    "ftm": ["ftm"],
    "pf": ["pf", "personalfouls", "fouls"],
    "plus_minus": ["plusminus", "plusminuspoints"],
}


def locate_player_box(conn, info):
    """
    Detect a per-player-per-game box score table. Returns (table, df) or (None, None).
    Verdict is printed clearly per §3.1.4.
    """
    print("\n== Detecting player box scores ==")
    best, best_score = None, -1
    for t, meta in info.items():
        cols = meta["cols"]
        gid = find_col(cols, GAME_ID_PATS)
        pid = find_col(cols, ["playerid", "personid"])
        if not (gid and pid):
            continue
        # Require the core scoring signals to distinguish from lineup tables.
        signals = ["pts", "reb", "ast", "fta", "fga", "min"]
        score = sum(bool(find_col(cols, PLAYER_COLS[s])) for s in signals)
        if score > best_score:
            best, best_score = t, score

    if best is None or best_score < 4:
        print("PLAYER BOX SCORES: NOT FOUND -- run fetch_player_logs.py")
        return None, None

    print("PLAYER BOX SCORES: FOUND (table {})".format(best))
    cols = info[best]["cols"]
    resolved = {out_c: find_col(cols, pats) for out_c, pats in PLAYER_COLS.items()}
    df = read_table(conn, best)

    out = pd.DataFrame()
    for out_c, src_c in resolved.items():
        if src_c is None:
            out[out_c] = pd.NA
            continue
        if out_c in ("game_id",):
            out[out_c] = clean_id(df[src_c], width=10)
        elif out_c in ("player_id", "team_id"):
            out[out_c] = clean_id(df[src_c])
        elif out_c in ("player_name", "team_abbr", "min"):
            out[out_c] = df[src_c].astype("string").str.strip()
        else:
            out[out_c] = pd.to_numeric(df[src_c], errors="coerce")
    out = out[list(PLAYER_COLS.keys())]
    out = out[out["game_id"] != ""]
    print("  player box rows (pre season-filter): {}".format(len(out)))
    return best, out


# --------------------------------------------------------------------------- #
# Season filtering + writing
# --------------------------------------------------------------------------- #
def add_season_keys(df):
    """Attach _start_year and _type_suffix derived from game_id."""
    df = df.copy()
    df["_start_year"] = df["game_id"].map(season_start_year_from_gid)
    df["_type_suffix"] = df["game_id"].map(lambda g: season_type_from_gid(g)[0])
    df["_season_str"] = df["_start_year"].map(season_str_from_start_year)
    return df


def season_filter(df):
    """Keep season >= MIN_SEASON_START_YEAR and season type in KEEP_TYPES."""
    df = add_season_keys(df)
    keep = (
        df["_start_year"].notna()
        & (df["_start_year"] >= MIN_SEASON_START_YEAR)
        & df["_type_suffix"].isin(KEEP_TYPES)
    )
    return df[keep]


def write_csv_gz(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")
    print("  wrote {} ({} rows)".format(os.path.relpath(path, REPO_ROOT), len(df)))


# --------------------------------------------------------------------------- #
# Freshness report
# --------------------------------------------------------------------------- #
def freshness_report(games, officials):
    """Print and write source-data/_freshness.txt (§3.1.6)."""
    print("\n== Freshness report ==")
    lines = ["nbadb freshness report", "=" * 60, ""]

    g = add_season_keys(games)
    max_date = g["game_date"].dropna().max() if "game_date" in g else None
    lines.append("Max game_date overall: {}".format(max_date))
    print("  max game_date:", max_date)

    # officials per game -> coverage
    off_counts = (
        officials.groupby("game_id")["official_id"].nunique()
        if len(officials) else pd.Series(dtype=int)
    )
    g["_n_officials"] = g["game_id"].map(off_counts).fillna(0).astype(int)

    lines.append("")
    lines.append("Per-season: games count and officials coverage (% with 3 officials)")
    lines.append("season      games   %3-officials")
    warnings = []
    for season, grp in sorted(g.groupby("_season_str"), key=lambda kv: str(kv[0])):
        n_games = len(grp)
        pct3 = 100.0 * (grp["_n_officials"] == 3).mean() if n_games else 0.0
        lines.append("{:<10}  {:>5}   {:>6.1f}%".format(str(season), n_games, pct3))
        print("  {:<10} games={:<5} 3-officials={:.1f}%".format(str(season), n_games, pct3))
        # Flag coverage below 95% for seasons 2000-01 onward.
        sy = grp["_start_year"].iloc[0]
        if sy is not None and sy >= 2000 and pct3 < 95.0:
            warnings.append(
                "LOW OFFICIALS COVERAGE: {} at {:.1f}% (<95%) -- consider backfill_officials.py"
                .format(season, pct3)
            )

    # Latest-season completeness heuristic: an RS should have ~1230 games
    # (30 teams x 82 / 2). Flag if the newest season is well short.
    rs = g[g["_type_suffix"] == "RS"]
    if len(rs):
        latest_year = rs["_start_year"].max()
        latest = rs[rs["_start_year"] == latest_year]
        n_latest = len(latest)
        if n_latest < 1100:
            warnings.append(
                "LATEST SEASON MAY BE INCOMPLETE: {} has {} RS games (< 1100 expected ~1230)"
                .format(season_str_from_start_year(int(latest_year)), n_latest)
            )

    lines.append("")
    if warnings:
        lines.append("WARNINGS:")
        for w in warnings:
            lines.append("  * " + w)
            print("  !! " + w)
    else:
        lines.append("No freshness warnings.")
        print("  no freshness warnings.")

    with open(os.path.join(SOURCE_DIR, "_freshness.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("-> wrote source-data/_freshness.txt")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if len(sys.argv) != 2:
        print('Usage: python extract_from_nbadb.py "PATH\\TO\\nba.sqlite"')
        sys.exit(1)
    db_path = sys.argv[1]
    if not os.path.isfile(db_path):
        print("ERROR: file not found: {}".format(db_path))
        sys.exit(1)

    os.makedirs(SOURCE_DIR, exist_ok=True)
    os.makedirs(PLAYER_LOGS_DIR, exist_ok=True)

    print("Opening SQLite: {}".format(db_path))
    conn = sqlite3.connect(db_path)
    try:
        info = introspect(conn)

        officials = locate_officials(conn, info)
        if officials is None:
            print("\nFATAL: no officials data found. Inspect _schema_report.txt.")
            sys.exit(2)

        games = locate_games(conn, info)
        if games is None:
            print("\nFATAL: no games table found. Inspect _schema_report.txt.")
            sys.exit(2)

        box_table, player_box = locate_player_box(conn, info)

        # ---- Season filter + write officials / games ----
        print("\n== Writing extracts ==")
        officials_f = season_filter(officials)[
            ["game_id", "official_id", "official_name", "jersey_num"]
        ]
        write_csv_gz(officials_f, os.path.join(SOURCE_DIR, "officials.csv.gz"))

        games_f = season_filter(games)[
            [
                "game_id", "game_date", "season", "season_type",
                "home_team_id", "home_team_abbr", "away_team_id", "away_team_abbr",
                "home_pts", "away_pts", "home_win",
            ]
        ]
        write_csv_gz(games_f, os.path.join(SOURCE_DIR, "games.csv.gz"))

        # ---- Write player logs, split per season + type ----
        if player_box is not None:
            pb = season_filter(player_box)
            written = 0
            for (season, suffix), grp in pb.groupby(["_season_str", "_type_suffix"]):
                out = grp[list(PLAYER_COLS.keys())]
                fname = "{}_{}.csv.gz".format(season, suffix)
                write_csv_gz(out, os.path.join(PLAYER_LOGS_DIR, fname))
                written += 1
            print("  wrote {} player-log files.".format(written))
        else:
            print("  (no player box scores in DB -- run fetch_player_logs.py next)")

        # ---- Freshness report over the FILTERED games/officials ----
        freshness_report(games_f, officials_f)

        print("\nDONE. Review source-data/_schema_report.txt and _freshness.txt.")
        if player_box is None:
            print("NEXT: PLAYER BOX SCORES were NOT FOUND -- run fetch_player_logs.py.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
