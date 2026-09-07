#!/usr/bin/env python3
"""
build.py -- NBA Referee Database aggregation (cloud session).

Pure pandas over already-committed source-data/. No network calls.
Reads:
    source-data/games.csv.gz
    source-data/officials.csv.gz
    source-data/player_logs/*.csv.gz
    data/referee_identity_overrides.csv        (optional escape hatch, see below)
Writes:
    data/referees.json
    data/leaderboards.json
    data/referees/{official_id}.json

Design anchors (docs/PHASE1_SPEC.md + docs/BUILD_SPEC.md):
  * All game_ids are strings; every read forces dtype={"game_id": str}.
    NBA scheme  = 10 digits starting '00'.
    ESPN scheme = anything else (9-digit date-encoded or sequential event id).
    The two never collide; scheme is detected from the id, never inferred
    from the season.
  * Two id schemes, structurally disjoint, partitioned by season. Team joins
    use normalized tricodes (nba_tricodes), NEVER team_id -- the two schemes
    number teams differently.
  * BUILD_SPEC section 3 -- referee identity: NBA official_id is numeric,
    ESPN official_id is 'espn:first-last'; they share no key, so referees are
    reconciled on a normalized-name ref_key, with a manual override escape
    hatch (data/referee_identity_overrides.csv).
  * BUILD_SPEC section 4 -- alternate officials: for any game with >3
    officials rows, only the first 3 (by row order as written) officiated;
    the rest are alternates. Verified to occur in BOTH eras, so applied to
    both.
  * BUILD_SPEC section 5 -- round/Game-7 labeling: NBA scheme parses from the
    game_id; ESPN scheme is not derivable and is intentionally skipped (known
    Phase-1 gap for 2000-03, 2012-13, 2023-26).

Run from the repo root:  python scripts/build.py
"""

import os
import sys
import re
import json
import glob
import datetime
import unicodedata
from collections import defaultdict

import pandas as pd
import numpy as np

# Shared tricode normalization lives with the local scripts.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "local"))
import nba_tricodes  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "source-data")
DATA = os.path.join(REPO, "data")
OVERRIDE_CSV = os.path.join(DATA, "referee_identity_overrides.csv")

SEASON_FLOOR_YEAR = 1993          # 1993-94 is the first in-scope season (ESPN 1990s backfill)
CURRENT_SEASON = "2025-26"        # "active" == worked this season
OT_MIN_THRESHOLD = 505            # total player-minutes/game; clean gap in data at 505
SWING_MIN_GAMES = 15              # min games under a ref to report a player swing
SWING_TOP_N = 50
PO_BASELINE_MIN = 5               # min playoff games in a season to trust a PO baseline
TOP_PERF_N = 25
LEADERBOARD_MIN_GAMES = 200       # RS + career-total leaderboards (unchanged)
# Playoff games are inherently scarcer than regular-season games per ref
# career -- reusing LEADERBOARD_MIN_GAMES (200) for PO whistle-profile
# qualification left only 3 of 96 playoff-experienced refs colored/ranked.
# Empirically, 75 clears about a third of the playoff-experienced pool
# (~32 refs) without reaching into single-digit-game noise. RS keeps 200.
PO_LEADERBOARD_MIN_GAMES = 75
# Whistle-profile stats eligible for percentile ranking + a dedicated
# /leaderboard/{slug}/ page. n_column is which whistle_profile[kind] count a
# ref must clear the min-games threshold on to qualify -- "n" for stats
# derived straight from the game row, "n_boxscore" for stats that need
# box-score data (FTA/PF/OT), matching how each stat is actually computed
# above. RS uses LEADERBOARD_MIN_GAMES; PO uses PO_LEADERBOARD_MIN_GAMES.
WHISTLE_STATS = [
    # (key, n_column, label, slug)
    ("avg_total_points", "n", "Combined points", "combined-points"),
    ("avg_total_fta", "n_boxscore", "Combined free-throw attempts", "combined-fta"),
    ("avg_total_pf", "n_boxscore", "Combined personal fouls", "combined-fouls"),
    ("avg_abs_margin", "n", "Avg. margin of victory", "avg-margin"),
    ("home_win_pct", "n", "Home team win rate", "home-win-rate"),
    ("ot_rate", "n_boxscore", "Games to overtime", "ot-rate"),
]
# Recent form (docs/RECENT_FORM_SPEC.md): the same six WHISTLE_STATS keys,
# mapped to the raw per-game column each is rolled up from (needs_box marks
# the three that require box-score data and so can have fewer usable games
# than the window itself).
RECENT_FORM_STATS = [
    # (key, raw_col, needs_box)
    ("avg_total_points", "total_pts", False),
    ("avg_total_fta", "box_fta", True),
    ("avg_total_pf", "box_pf", True),
    ("avg_abs_margin", "abs_margin", False),
    ("home_win_pct", "home_win_f", False),
    ("ot_rate", "is_ot_f", True),
]
# Officiating "quality score" (DASHBOARD_SPEC §2): weight every game a ref
# worked by how much responsibility it represents -- 1 point per regular-
# season game, then doubling with each round of the playoffs the game
# belongs to (per BUILD_SPEC round labels / label_rounds -- this includes
# every game in ESPN_GAME_NUM_UNRECOVERABLE with round kept but game_num
# nulled, since round is all this needs). Play-in games are not separately
# weighted (no round label applies to them) and so contribute 0, same as
# before this scale was widened to include the regular season.
QUALITY_RS_POINTS = 1
QUALITY_PO_POINTS = {1: 2, 2: 4, 3: 8, 4: 16}   # round 1 (first round) .. round 4 (Finals)
QUALITY_MIN_SEASONS = 3           # min seasons_active for the PER-SEASON ranking only
# Subset of WHISTLE_STATS eligible for a spotlight "signature line" (DASHBOARD_
# SPEC §1) -- excludes avg_abs_margin, which the spec's spotlight list omits.
SPOTLIGHT_STAT_KEYS = ["home_win_pct", "avg_total_fta", "avg_total_pf", "ot_rate", "avg_total_points"]
TEAM_REF_MIN_GAMES = 10           # min games of a team under a ref to list on team pages
# /matchup/ (team x referee lookup, docs/MATCHUP_SPEC.md): a two-tier sample-
# size policy applied uniformly at both career and per-season scope, since a
# single team-referee pairing is a much finer cut than any other split on the
# site and per-season samples are naturally tiny (~1-4 games, teams meet a
# handful of times a season). Below MATCHUP_SUPPRESS_MIN, rate/average stats
# (win%, margins, FTA/PF) are suppressed outright -- games/W-L still show,
# since a raw count is never misleading, but a percentage over 1-2 games is.
# Between the two, stats still show but carry a small-sample flag.
MATCHUP_FLAG_MIN = TEAM_REF_MIN_GAMES   # reuse the existing team-page listing bar (10)
MATCHUP_SUPPRESS_MIN = 3
PLAYER_TOP_GAMES = 10             # best scoring games shown on a player page

# ---- Recent form (docs/RECENT_FORM_SPEC.md) --------------------------------
# Rolling windows over each referee's own chronological RS+PO game log (Play-In
# excluded, same as everywhere else a league baseline is involved). Four
# window types: three fixed game counts, one calendar window that goes empty
# off-season by construction (it's anchored to the actual build date, not to
# the referee's own last game -- a retired official's "last 5 games" would
# otherwise never look empty).
RECENT_FORM_WINDOWS = [5, 10, 25]
RECENT_FORM_CAL_DAYS = 30
# Box-score-dependent stats (FTA/PF/OT) can have fewer usable games than the
# window itself (box data isn't universal, especially older ESPN-era games) --
# below this many box-available games in a window, that stat is suppressed
# (explicit reason shown, not a blank cell), matching /matchup/'s policy.
RECENT_FORM_MIN_BOX = MATCHUP_SUPPRESS_MIN
# A referee only CONTRIBUTES samples to the league-wide pooled distribution
# (what "normal variance" means for a window of this size) once their own
# career is comfortably larger than the window -- otherwise the window and
# its own baseline overlap too much and the "deviation" is artificially
# small, which would narrow the normal-range band and make everyone ELSE's
# genuine deviations look more unusual than they are. This only gates who
# helps DEFINE normal; every referee's own current window is still compared
# against the resulting distribution regardless of their career length.
RECENT_FORM_POOL_MULT = 2         # count windows: need career >= MULT * window size
RECENT_FORM_POOL_MIN_CAL_CAREER = 20   # calendar window: flat career-game floor
# "Normal variance" = the middle 90% of the pooled deviation distribution for
# a window of that size and stat; outside the 5th-95th percentile band is
# flagged as outside typical range. An 80% band (10/90) was tried first and
# rejected: with 4 window types x 6 stats shown per referee, an 80% band
# flagged >=1 combination for 143 of 166 referees (86%) on the actual data --
# technically correct (percentiles guarantee ~20% of individual samples fall
# outside a p10-p90 band) but it makes "outside" look like the common case on
# any one referee's page, undermining the whole point of the flag. Tightening
# to a 90% band is a real fix to that, not just cosmetic -- it's still purely
# a threshold choice, not a change to what's being measured.
# Also NOT "any window outside the band" for the dashboard widget -- with 24
# combinations per referee, nearly everyone clears even a tight bar on SOME
# combination by chance alone. The widget instead ranks by how far outside
# (percentile distance from the median), across the whole pool of (ref,
# window, stat) triples, and only surfaces genuine tail cases.
RECENT_FORM_NORMAL_LO, RECENT_FORM_NORMAL_HI = 5, 95
RECENT_FORM_DASHBOARD_TOP_N = 6

# ---- Tier C (docs/TIER_C_SPEC.md) ------------------------------------------
CREW_TOP_N = 40                   # full /crews/ page length; index widget shows CREW_TOP_N[:5]
SWINGS_ALL_CAP = 250              # top/bottom N pairs kept in data/swings_all.json
ERA_LEADERS_TOP_N = 25            # per (decade, category) leader list length
# Decade buckets for era_leaders: (label, start_year, end_year, partial). 1990s
# is explicitly partial -- the dataset only goes back to 1993-94, not 1990-91.
# 2020s is also incomplete in a trivial sense (still in progress) but the spec
# only asks to flag the 1990s case, so that's the only one labeled partial.
DECADES = [
    ("1990s", 1993, 1999, True),
    ("2000s", 2000, 2009, False),
    ("2010s", 2010, 2019, False),
    ("2020s", 2020, 2029, False),
]

ALLOWED_TRICODES = nba_tricodes.VALID_TRICODES | nba_tricodes.HISTORICAL_TRICODES
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

OVERRIDE_CSV_PLAYER = os.path.join(DATA, "player_identity_overrides.csv")
PLAYER_AUDIT_TXT = os.path.join(SRC, "_player_identity_audit.txt")


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def is_espn_scheme(game_id):
    """NBA scheme is 10 digits starting '00'; everything else is ESPN."""
    g = str(game_id)
    return not (len(g) == 10 and g.startswith("00"))


def season_start_year(season):
    """'2015-16' -> 2015."""
    return int(str(season)[:4])


def season_decade(season):
    """'1996-97' -> '1990s'; None if the season falls outside every configured
    DECADES bucket (shouldn't happen given SEASON_FLOOR_YEAR, but a season
    beyond the last configured decade end_year would fall through here)."""
    yr = season_start_year(season)
    for label, lo, hi, _partial in DECADES:
        if lo <= yr <= hi:
            return label
    return None


def norm_ref_key(name):
    """
    Canonical referee key: lowercase, drop accents, strip periods/apostrophes,
    collapse whitespace and hyphens to single hyphens, drop Jr/Sr/II/III... .

    Deliberately conservative: it does NOT strip middle initials, because that
    would risk merging genuinely different people (BUILD_SPEC section 3 -- the
    audit surfaces near-duplicates like 'eddie-f-rush' vs 'eddie-rush' for a
    human to resolve via the override file, rather than the script guessing).
    """
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = s.lower().replace(".", " ").replace("'", "")
    s = re.sub(r"[^a-z0-9\s-]", " ", s).replace("-", " ")
    toks = [t for t in s.split() if t and t not in NAME_SUFFIXES]
    return "-".join(toks)


def clean_num(x):
    """Convert to a JSON-safe number (None for NaN), rounding floats."""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(x, float):
        return round(x, 2)
    return x


def pct_str(x, places=1):
    """0.605 -> '60.5%'; used only for the small, fixed dashboard.records list
    (build.py stores a human display string there rather than a raw number --
    render_pages.py's own pct()/dec() handle every other, more general table)."""
    return "—" if x is None else "%.*f%%" % (places, float(x) * 100)


def dec_str(x, places=1):
    return "—" if x is None else "%.*f" % (places, float(x))


def int_str(x):
    return "—" if x is None else "{:,}".format(int(x))


def assert_no_nan(obj, path="root"):
    """Recursively verify no NaN leaked into a structure destined for JSON."""
    if isinstance(obj, float):
        if obj != obj:  # NaN
            raise AssertionError("NaN leak at %s" % path)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            assert_no_nan(v, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_no_nan(v, "%s[%d]" % (path, i))


# ----------------------------------------------------------------------------
# load & era-tag
# ----------------------------------------------------------------------------
def load_games():
    gm = pd.read_csv(os.path.join(SRC, "games.csv.gz"), dtype={"game_id": str})
    gm["yr"] = gm["season"].map(season_start_year)
    before = len(gm)
    gm = gm[gm["yr"] >= SEASON_FLOOR_YEAR].copy()
    print("games: %d rows (dropped %d rows before the %d-%02d season)"
          % (len(gm), before - len(gm), SEASON_FLOOR_YEAR, (SEASON_FLOOR_YEAR + 1) % 100))
    gm["era"] = gm["game_id"].map(lambda g: "espn" if is_espn_scheme(g) else "nba")
    gm["home_team_abbr"] = gm["home_team_abbr"].map(nba_tricodes.to_nba_tricode)
    gm["away_team_abbr"] = gm["away_team_abbr"].map(nba_tricodes.to_nba_tricode)
    gm["home_pts"] = pd.to_numeric(gm["home_pts"], errors="coerce")
    gm["away_pts"] = pd.to_numeric(gm["away_pts"], errors="coerce")
    gm["home_win"] = pd.to_numeric(gm["home_win"], errors="coerce")
    return gm


def load_officials(valid_game_ids):
    off = pd.read_csv(os.path.join(SRC, "officials.csv.gz"), dtype={"game_id": str})
    off["row_order"] = range(len(off))          # preserve as-written order (alternates rule)
    off = off[off["game_id"].isin(valid_game_ids)].copy()
    off["era"] = off["game_id"].map(lambda g: "espn" if is_espn_scheme(g) else "nba")
    off["official_id"] = off["official_id"].astype(str)
    print("officials: %d rows (in-scope games)" % len(off))
    return off


def load_player_logs(valid_game_ids):
    frames = []
    for f in sorted(glob.glob(os.path.join(SRC, "player_logs", "*.csv.gz"))):
        frames.append(pd.read_csv(f, dtype={"game_id": str}))
    pl = pd.concat(frames, ignore_index=True)
    pl = pl[pl["game_id"].isin(valid_game_ids)].copy()
    pl["team_abbr"] = pl["team_abbr"].map(nba_tricodes.to_nba_tricode)
    for c in ["min", "pts", "fta", "pf", "reb", "ast"]:
        pl[c] = pd.to_numeric(pl[c], errors="coerce").fillna(0)
    # player_id arrives as float in the CSVs (e.g. 1018.0); coerce to a clean
    # integer string so ids don't leak a spurious ".0" into the output JSON.
    pid = pd.to_numeric(pl["player_id"], errors="coerce")
    before = len(pl)
    pl = pl[pid.notna()].copy()
    pl["player_id"] = pid[pid.notna()].astype("int64").astype(str)
    if before != len(pl):
        print("player_logs: dropped %d rows with no player_id" % (before - len(pl)))
    print("player_logs: %d rows across %d games" % (len(pl), pl["game_id"].nunique()))
    return pl


# ----------------------------------------------------------------------------
# alternate-official exclusion (BUILD_SPEC section 4)
# ----------------------------------------------------------------------------
def exclude_alternates(off):
    hr("SECTION 4  Alternate-official exclusion")

    dup = off.duplicated(subset=["game_id", "official_id"]).sum()
    if dup:
        print("dropping %d exact-duplicate (game_id, official_id) rows" % dup)
        off = off.drop_duplicates(subset=["game_id", "official_id"], keep="first")

    off = off.sort_values(["game_id", "row_order"])
    per_game = off.groupby("game_id")["official_id"].size()

    for era in ("nba", "espn"):
        era_games = off[off["era"] == era]["game_id"].unique()
        counts = per_game.loc[era_games]
        over = counts[counts > 3]
        print("%-4s scheme: max officials/game = %d ; games with >3 rows = %d"
              % (era, counts.max(), len(over)))
        if len(over):
            print("        -> first-3-by-row-order rule applies (trimming %d games)"
                  % len(over))

    # keep only the first 3 officials (by written order) of every game.
    off["rank_in_game"] = off.groupby("game_id").cumcount()
    trimmed = int((off["rank_in_game"] >= 3).sum())
    kept = off[off["rank_in_game"] < 3].copy()
    print("total alternate rows excluded: %d ; officiating rows kept: %d"
          % (trimmed, len(kept)))

    # coverage after trimming: distribution of officials-per-game
    post = kept.groupby("game_id")["official_id"].size()
    print("post-trim officials-per-game distribution: %s"
          % post.value_counts().sort_index().to_dict())
    return kept


# ----------------------------------------------------------------------------
# referee identity reconciliation (BUILD_SPEC section 3)
# ----------------------------------------------------------------------------
def _collapse_initials(key):
    """Drop single-letter tokens -- used only to *surface* audit candidates."""
    return "-".join(t for t in key.split("-") if len(t) > 1)


def _lev(a, b):
    m, n = len(a), len(b)
    if abs(m - n) > 2:
        return 3
    d = list(range(n + 1))
    for i in range(1, m + 1):
        prev, d[0] = d[0], i
        for j in range(1, n + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
            prev = cur
    return d[n]


def load_overrides():
    """
    data/referee_identity_overrides.csv columns:
        raw_name_or_id, canonical_ref_key, canonical_display_name
    Empty/absent is fine -- auto-matching is the default. Matches a row when
    raw_name_or_id equals (case-insensitively) either the raw official_name or
    the raw official_id.
    """
    if not os.path.exists(OVERRIDE_CSV):
        print("no override file (%s) -- auto-matching only" % os.path.relpath(OVERRIDE_CSV, REPO))
        return {}
    ov = pd.read_csv(OVERRIDE_CSV, dtype=str, comment="#").fillna("")
    ov = ov[ov["raw_name_or_id"].str.strip() != ""]
    mapping = {}
    for _, r in ov.iterrows():
        mapping[r["raw_name_or_id"].strip().lower()] = (
            r["canonical_ref_key"].strip(),
            r["canonical_display_name"].strip(),
        )
    print("loaded %d referee identity override(s)" % len(mapping))
    return mapping


def reconcile_referees(off):
    hr("SECTION 3  Referee identity reconciliation")

    # Introspect the NBA-scheme official_id format before assuming anything.
    nba_ids = off[off["era"] == "nba"]["official_id"].dropna().unique()
    espn_ids = off[off["era"] == "espn"]["official_id"].dropna().unique()
    print("NBA-scheme official_id samples : %s  (%d distinct, all-numeric=%s)"
          % (sorted(nba_ids)[:5], len(nba_ids),
             all(str(x).isdigit() for x in nba_ids)))
    print("ESPN-scheme official_id samples: %s  (%d distinct)"
          % (sorted(espn_ids)[:3], len(espn_ids)))

    off = off.copy()
    off["auto_key"] = off["official_name"].map(norm_ref_key)

    overrides = load_overrides()

    def apply_override(row):
        for probe in (str(row["official_name"]).strip().lower(),
                      str(row["official_id"]).strip().lower()):
            if probe in overrides:
                return overrides[probe][0]
        return row["auto_key"]

    off["ref_key"] = off.apply(apply_override, axis=1)

    # canonical display name per ref_key: an override display wins; otherwise the
    # most common raw name (tie-break: longest, then alphabetical).
    override_display = {}
    for _rk, disp in overrides.values():
        if disp:
            override_display[_rk] = disp

    display = {}
    raw_ids = defaultdict(set)
    raw_names = defaultdict(set)
    eras = defaultdict(set)
    for rk, grp in off.groupby("ref_key"):
        raw_ids[rk] = set(grp["official_id"].unique())
        raw_names[rk] = set(grp["official_name"].unique())
        eras[rk] = set(grp["era"].unique())
        if rk in override_display:
            display[rk] = override_display[rk]
        else:
            counts = grp["official_name"].value_counts()
            top = counts[counts == counts.max()].index.tolist()
            display[rk] = sorted(top, key=lambda s: (-len(s), s))[0]

    # ---- full audit list -----------------------------------------------------
    audit_rows = []
    for rk in sorted(raw_ids):
        audit_rows.append({
            "ref_key": rk,
            "display": display[rk],
            "eras": "+".join(sorted(eras[rk])),
            "n_ids": len(raw_ids[rk]),
            "ids": sorted(raw_ids[rk]),
            "raw_names": sorted(raw_names[rk]),
        })

    hr("REFEREE IDENTITY AUDIT  (%d canonical referees)" % len(audit_rows))
    print("%-26s %-22s %-9s %s" % ("ref_key", "display", "eras", "raw ids / names"))
    print("-" * 100)
    for a in audit_rows:
        names = a["raw_names"]
        name_note = "" if names == [a["display"]] else "  names=%s" % names
        print("%-26s %-22s %-9s %s%s"
              % (a["ref_key"], a["display"], a["eras"], a["ids"], name_note))

    # ---- near-duplicate candidates (surface, do NOT auto-merge) --------------
    keys = sorted(raw_ids)
    seen = set()
    candidates = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            reason = None
            if _collapse_initials(a) and _collapse_initials(a) == _collapse_initials(b):
                reason = "same-after-dropping-initials"
            elif _lev(a, b) <= 2:
                reason = "edit-distance<=2"
            if reason:
                candidates.append((a, b, reason))
                seen.add(a)
                seen.add(b)

    hr("NEAR-DUPLICATE CANDIDATES  (for human review -- NOT auto-merged)")
    if not candidates:
        print("none")
    else:
        for a, b, reason in candidates:
            merged = off.loc[off["ref_key"].isin([a, b])]
            a_e = "+".join(sorted(eras[a]))
            b_e = "+".join(sorted(eras[b]))
            already = " [ALREADY MERGED via override]" if a == b else ""
            print("  %-24s (%s, n=%d)  <->  %-24s (%s, n=%d)   [%s]%s"
                  % (a, a_e, (off["ref_key"] == a).sum(),
                     b, b_e, (off["ref_key"] == b).sum(), reason, already))
        print("\nTo merge any pair, add a line to data/referee_identity_overrides.csv:")
        print("    <raw name or id>,<canonical_ref_key>,<canonical_display_name>")

    # ---- single-era referees (candidates for a missed cross-era match) ------
    single = [(rk, "+".join(sorted(eras[rk])), (off["ref_key"] == rk).sum())
              for rk in keys if len(eras[rk]) == 1]
    both = sum(1 for rk in keys if len(eras[rk]) == 2)
    print("\ncross-era match summary: %d referees span BOTH eras; %d appear in one era only"
          % (both, len(single)))

    return off, display, raw_ids, eras


# ----------------------------------------------------------------------------
# playoff round / Game-7 labeling (BUILD_SPEC section 5)
# ----------------------------------------------------------------------------
# Games belonging to playoff series that scripts/local/fetch_espn_round_labels
# .py's win-based completeness audit found genuinely missing games for. Round
# is still correct for these games (derived from each team's series-sequence
# order, independent of how many games are present in any one series), but
# game_num is NOT -- it's the chronological rank among the games we have,
# which is wrong once a game is missing (whether from the middle, or the end
# where it would silently understate how long the series actually ran).
# Honest fallback: game_num is nulled for exactly these games; round is kept.
# This nulling MUST happen here, downstream of round_labels.csv.gz, not in
# fetch_espn_round_labels.py's own output -- label_rounds() below verifies
# game_num is in 1-7 across ALL ESPN seasons at once, BEFORE any nulling; a
# null baked into round_labels.csv.gz itself would fail that check globally
# and silently skip round-labeling for every ESPN season, not just this one.
#
# 2000-01, after a real recovery attempt (scripts/local/
# recover_2000_01_playoffs.py + recover_final_two_dates.py both came back
# empty for every remaining gap date):
#   R2 CHA/MIL (5 present, series score 3-2, needed 4):
#     210510003 210513003 210515015 210517003 210520015
#   R3 LAL/SAS West Finals (3 present, series score 3-0, needed 4):
#     210519024 210521024 210525013
#   R3 MIL/PHI East Finals (4 present, series score 2-2, needed 4):
#     210522020 210524020 210526015 210528015
#   R4 LAL/PHI Finals (1 present, series score 1-0, needed 4):
#     210615020
#
# 1995-96, diagnosed genuinely absent (not mis-scored or misfiled -- see
# source-data/_diagnose_1990s_flags.txt Part 2); no recovery attempted yet:
#   R2 CHI/NYK (4 present, series score 3-1, needed 4 -- real record is a
#   Bulls series win 4-1, so Game 5 is the missing game):
#     160505004 160511018 160512018 160514004
ESPN_GAME_NUM_UNRECOVERABLE = {
    "210510003", "210513003", "210515015", "210517003", "210520015",
    "210519024", "210521024", "210525013",
    "210522020", "210524020", "210526015", "210528015",
    "210615020",
    "160505004", "160511018", "160512018", "160514004",
}


def label_rounds(gm):
    hr("SECTION 5  Playoff round / Game-7 labeling")
    gm = gm.copy()
    gm["po_round"] = None
    gm["po_game_num"] = None

    nba_po = gm[(gm["season_type"] == "Playoffs") & (gm["era"] == "nba")].copy()
    if len(nba_po):
        rnd = nba_po["game_id"].str[7].astype(int)
        gnum = nba_po["game_id"].str[9].astype(int)
        # Verify before trusting: rounds must be 1-4, games 1-7, and each season
        # must have exactly one round-4 (Finals) series of 4-7 games.
        ok_ranges = rnd.between(1, 4).all() and gnum.between(1, 7).all()
        finals = nba_po[rnd == 4].groupby("season").size()
        ok_finals = finals.between(4, 7).all() and len(finals) > 0
        print("NBA-scheme playoff games: %d ; rounds in 1-4: %s ; games in 1-7: %s ; "
              "one 4-7 game Finals/season: %s"
              % (len(nba_po), rnd.between(1, 4).all(), gnum.between(1, 7).all(), ok_finals))
        if ok_ranges and ok_finals:
            gm.loc[nba_po.index, "po_round"] = rnd.values
            gm.loc[nba_po.index, "po_game_num"] = gnum.values
            print("verification PASSED -> round/Game-7 labels trusted for NBA scheme")
        else:
            print("verification FAILED -> skipping round labeling rather than guessing")

    espn_po = gm[(gm["season_type"] == "Playoffs") & (gm["era"] == "espn")]
    labels_path = os.path.join(SRC, "round_labels.csv.gz")
    if len(espn_po) and os.path.exists(labels_path):
        labels = pd.read_csv(labels_path, dtype={"game_id": str})
        labels["round"] = pd.to_numeric(labels["round"], errors="coerce")
        labels["game_num"] = pd.to_numeric(labels["game_num"], errors="coerce")
        # Verify before trusting: same shape checks as the NBA-scheme branch,
        # applied to the labels that actually match in-scope ESPN playoff games.
        matched = espn_po.merge(labels, on="game_id", how="inner")
        ok_ranges = matched["round"].between(1, 4).all() and matched["game_num"].between(1, 7).all()
        finals = matched[matched["round"] == 4].groupby("season").size()
        ok_finals = finals.between(1, 7).all() and len(finals) > 0
        by_source = labels.set_index("game_id").loc[matched["game_id"], "source"].value_counts().to_dict()
        print("ESPN-scheme playoff games: %d ; labels matched: %d (%s) ; "
              "rounds in 1-4: %s ; games in 1-7: %s ; one Finals series/season: %s"
              % (len(espn_po), len(matched), by_source,
                 matched["round"].between(1, 4).all(), matched["game_num"].between(1, 7).all(),
                 ok_finals))
        if ok_ranges and ok_finals:
            idx = espn_po.set_index("game_id").index
            lab = labels.set_index("game_id").reindex(idx)
            gm.loc[espn_po.index, "po_round"] = lab["round"].values
            gm.loc[espn_po.index, "po_game_num"] = lab["game_num"].values
            nulled_mask = gm["game_id"].isin(ESPN_GAME_NUM_UNRECOVERABLE)
            n_nulled = nulled_mask.sum()
            nulled_seasons = sorted(gm.loc[nulled_mask, "season"].unique())
            gm.loc[nulled_mask, "po_game_num"] = None
            print("verification PASSED -> round/Game-7 labels trusted for ESPN scheme")
            print("game_num nulled for %d games in known-incomplete series across %s "
                  "(round kept -- see ESPN_GAME_NUM_UNRECOVERABLE)" % (n_nulled, nulled_seasons))
        else:
            print("verification FAILED -> skipping round labeling rather than guessing")
    elif len(espn_po):
        print("ESPN-scheme playoff games: %d (source-data/round_labels.csv.gz not found -- "
              "skipping round labeling rather than guessing)" % len(espn_po))
    return gm


# ----------------------------------------------------------------------------
# team-game stats (single source of truth: player_logs)
# ----------------------------------------------------------------------------
def build_team_game(pl):
    tg = pl.groupby(["game_id", "team_abbr"], as_index=False).agg(
        team_pts=("pts", "sum"),
        team_fta=("fta", "sum"),
        team_pf=("pf", "sum"),
        team_min=("min", "sum"),
    )
    # per-game totals (both teams) for whistle profile + OT detection
    game_tot = tg.groupby("game_id", as_index=False).agg(
        box_fta=("team_fta", "sum"),
        box_pf=("team_pf", "sum"),
        box_min=("team_min", "sum"),
        n_teams=("team_abbr", "nunique"),
    )
    game_tot["is_ot"] = game_tot["box_min"] >= OT_MIN_THRESHOLD
    return tg, game_tot


# ----------------------------------------------------------------------------
# per-referee aggregation
# ----------------------------------------------------------------------------
def season_type_label(row):
    st = row["season_type"]
    if st == "Playoffs":
        return "PO"
    if st == "Play-In":
        return "PI"
    return "RS"


def _whistle_stat_values(sub, got):
    """Compute the six WHISTLE_STATS raw values (+ n / n_boxscore) for one
    game-row slice (must carry total_pts/home_win/abs_margin and be indexed
    by game_id). Shared by build_league_baselines (the league-wide
    population), the per-referee career whistle_profile, and the per-referee
    per-season splits (docs/LEAGUE_CONTEXT_SPEC.md) -- the same formula at
    every granularity, so league baselines and referee stats are directly
    comparable and a season-split row's numbers reconcile with the career
    row's weighted-in inputs."""
    n = len(sub)
    vals = {"n": n, "n_boxscore": 0, "avg_total_points": None, "avg_total_fta": None,
           "avg_total_pf": None, "home_win_pct": None, "avg_abs_margin": None,
           "ot_games": None, "ot_rate": None}
    if not n:
        return vals
    vals["avg_total_points"] = clean_num(sub["total_pts"].mean())
    vals["home_win_pct"] = clean_num((sub["home_win"] == 1).mean())
    vals["avg_abs_margin"] = clean_num(sub["abs_margin"].mean())
    box = got.reindex(sub.index)
    box = box[box["n_teams"] == 2]
    nb = len(box)
    vals["n_boxscore"] = nb
    if nb:
        vals["avg_total_fta"] = clean_num(box["box_fta"].mean())
        vals["avg_total_pf"] = clean_num(box["box_pf"].mean())
        vals["ot_games"] = int(box["is_ot"].sum())
        vals["ot_rate"] = clean_num(box["is_ot"].mean())
    return vals


def _agg_matchup(grp):
    """/matchup/ (docs/MATCHUP_SPEC.md): aggregate one team's record from a
    slice of that team's own "melted" perspective rows (one row per game the
    team appears in, already resolved to that team's own win/pts_for/
    pts_against/fta_for/fta_against/pf_for/pf_against/is_home -- see the
    home_persp/away_persp melt in aggregate()). Games/W-L are raw counts and
    always populated; rate stats are None below MATCHUP_SUPPRESS_MIN so the
    renderer never has to re-derive the suppression policy from n itself."""
    games = len(grp)
    out = {"games": games, "wins": 0, "losses": 0, "win_pct": None,
           "home_games": 0, "home_wins": 0, "away_games": 0, "away_wins": 0,
           "avg_pts_for": None, "avg_pts_against": None, "avg_margin": None,
           "n_box": 0, "avg_team_fta": None, "avg_opp_fta": None,
           "avg_team_pf": None, "avg_opp_pf": None}
    if not games:
        return out
    wins = int(grp["win"].sum())
    home_games = int(grp["is_home"].sum())
    home_wins = int((grp["is_home"] & grp["win"]).sum())
    out.update({"wins": wins, "losses": games - wins,
                "home_games": home_games, "home_wins": home_wins,
                "away_games": games - home_games, "away_wins": wins - home_wins})
    if games >= MATCHUP_SUPPRESS_MIN:
        out["win_pct"] = clean_num(wins / games)
        out["avg_pts_for"] = clean_num(grp["pts_for"].mean())
        out["avg_pts_against"] = clean_num(grp["pts_against"].mean())
        out["avg_margin"] = clean_num((grp["pts_for"] - grp["pts_against"]).mean())
        box = grp[grp["fta_for"].notna()]
        nb = len(box)
        out["n_box"] = nb
        if nb >= MATCHUP_SUPPRESS_MIN:
            out["avg_team_fta"] = clean_num(box["fta_for"].mean())
            out["avg_opp_fta"] = clean_num(box["fta_against"].mean())
            out["avg_team_pf"] = clean_num(box["pf_for"].mean())
            out["avg_opp_pf"] = clean_num(box["pf_against"].mean())
    return out


def build_league_baselines(gm, game_tot):
    """League Context (docs/LEAGUE_CONTEXT_SPEC.md section 1): per season +
    season_type, the league-wide average for every WHISTLE_STATS stat, plus n
    (games). This is the raw material every referee's era-adjusted expected
    baseline is built from (section 2) -- reuses season_type_label (the exact
    classifier aggregate() uses for the per-referee side) and
    _whistle_stat_values (the exact formula used at every other granularity),
    so league and referee numbers are computed on an identical population
    definition and are directly comparable."""
    hr("League baselines (League Context)")
    games_meta = gm.copy()
    games_meta["kind"] = games_meta.apply(season_type_label, axis=1)
    games_meta["abs_margin"] = (games_meta["home_pts"] - games_meta["away_pts"]).abs()
    games_meta["total_pts"] = games_meta["home_pts"] + games_meta["away_pts"]
    games_meta = games_meta.set_index("game_id")
    got = game_tot.set_index("game_id")

    baselines = {}
    for (season, kind), sub in games_meta.groupby(["season", "kind"]):
        if kind not in ("RS", "PO"):
            continue  # whistle profiles (and era adjustment) only cover RS/PO
        baselines.setdefault(season, {})[kind.lower()] = _whistle_stat_values(sub, got)

    assert_no_nan(baselines, "league_baselines")
    with open(os.path.join(DATA, "league_baselines.json"), "w", encoding="utf-8") as fh:
        json.dump(baselines, fh, ensure_ascii=False, indent=2)
    print("league_baselines: %d seasons (RS/PO each) -> data/league_baselines.json"
          % len(baselines))
    return baselines


def build_player_baselines(pl, games_meta):
    """(player_id, season, kind) -> per-game means, kind in {RS, PO}. Keyed on
    (player_id, season): a season is single-era, so this is already era-safe even
    though numeric player_ids are reused across eras for different people."""
    m = pl.merge(games_meta[["game_id", "season", "kind"]], on="game_id", how="inner")
    m = m[m["kind"].isin(["RS", "PO"])]
    grp = m.groupby(["player_id", "season", "kind"]).agg(
        pts=("pts", "mean"), fta=("fta", "mean"), pf=("pf", "mean"),
        reb=("reb", "mean"), ast=("ast", "mean"), n=("game_id", "size"),
    )
    base = {}
    for (pid, season, kind), r in grp.iterrows():
        base[(pid, season, kind)] = (r["pts"], r["fta"], r["pf"], int(r["n"]),
                                     r["reb"], r["ast"])
    return base


# ----------------------------------------------------------------------------
# player identity reconciliation (teams/players round -- TEAMS_PLAYERS_SPEC §1)
# ----------------------------------------------------------------------------
def norm_player_key(name):
    """
    Canonical PLAYER key. Unlike norm_ref_key it deliberately KEEPS Jr/Sr/II/III
    suffixes: Tim Hardaway Sr./Jr., Gary Payton / Payton II, Larry Nance / Nance
    Jr. are different people whose careers both touch this dataset.
    """
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = s.lower().replace(".", " ").replace("'", "")
    s = re.sub(r"[^a-z0-9\s-]", " ", s).replace("-", " ")
    return "-".join(t for t in s.split() if t)


def load_player_overrides():
    """data/player_identity_overrides.csv -- same columns/semantics as the ref
    overrides (raw_name_or_id, canonical_ref_key, canonical_display_name).
    Shipped empty; auto-matching is the default."""
    if not os.path.exists(OVERRIDE_CSV_PLAYER):
        print("no player override file -- auto-matching only")
        return {}
    ov = pd.read_csv(OVERRIDE_CSV_PLAYER, dtype=str, comment="#").fillna("")
    ov = ov[ov["raw_name_or_id"].str.strip() != ""]
    mp = {}
    for _, r in ov.iterrows():
        mp[r["raw_name_or_id"].strip().lower()] = (
            r["canonical_ref_key"].strip(), r["canonical_display_name"].strip())
    print("loaded %d player identity override(s)" % len(mp))
    return mp


def reconcile_players(pl, gm, overrides):
    """Reconcile players across id schemes by name, suffix-preserving key, with
    adjacency-gated cross-era merging (gap <= 1 season). Returns:
      seg_to_entity: "{era}:{player_id}" -> {slug, display, key, entity}
      entities:      slug -> {display, key, seg_ids, eras, seasons, teams}
    Writes the full merge/no-merge audit to source-data/_player_identity_audit.txt.
    """
    hr("PLAYER identity reconciliation")
    m = pl.merge(gm[["game_id", "season", "era"]], on="game_id", how="inner")
    m["seg_id"] = m["era"] + ":" + m["player_id"]

    segs = {}
    for seg_id, grp in m.groupby("seg_id"):
        era, pid = seg_id.split(":", 1)
        disp = grp["player_name"].value_counts().index[0]
        starts = {int(str(s)[:4]) for s in grp["season"].unique()}
        segs[seg_id] = {
            "seg_id": seg_id, "era": era, "player_id": pid, "display": disp,
            "key": norm_player_key(disp), "override_display": None,
            "smin": min(starts), "smax": max(starts),
            "seasons": sorted(grp["season"].unique()),
            "teams": sorted(set(grp["team_abbr"])), "games": int(grp["game_id"].nunique()),
        }

    # overrides: force a segment onto a canonical key (matched by name or id)
    for s in segs.values():
        for probe in (s["display"].strip().lower(), s["player_id"].strip().lower()):
            if probe in overrides:
                s["key"] = overrides[probe][0]
                s["override_display"] = overrides[probe][1]
                break

    # union different-era segments sharing a key when season ranges are adjacent
    # or overlapping (gap <= 1 season => hi.smin <= lo.smax + 2). Same-era
    # same-key segments never auto-merge (two different people would overlap).
    parent = {sid: sid for sid in segs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_key = defaultdict(list)
    for sid, s in segs.items():
        by_key[s["key"]].append(sid)
    for sids in by_key.values():
        for a in range(len(sids)):
            for b in range(a + 1, len(sids)):
                sa, sb = segs[sids[a]], segs[sids[b]]
                if sa["era"] == sb["era"]:
                    continue
                lo, hi = (sa, sb) if sa["smin"] <= sb["smin"] else (sb, sa)
                if hi["smin"] <= lo["smax"] + 2:
                    union(sids[a], sids[b])

    comps = defaultdict(list)
    for sid in segs:
        comps[find(sid)].append(sid)

    # build entity records, ordered by first appearance for stable slug suffixes
    order = sorted(comps.values(),
                   key=lambda ss: (min(segs[x]["smin"] for x in ss),
                                   segs[ss[0]]["key"], segs[ss[0]]["display"]))
    seg_to_entity, entities = {}, {}
    used_slugs = defaultdict(int)
    slug_collisions = []
    for ss in order:
        members = [segs[x] for x in ss]
        key = members[0]["key"]
        ov_disp = next((mm["override_display"] for mm in members if mm["override_display"]), None)
        # canonical display: override, else the name with the most games
        disp = ov_disp or max(members, key=lambda mm: mm["games"])["display"]
        base = key
        used_slugs[base] += 1
        slug = base if used_slugs[base] == 1 else "%s-%d" % (base, used_slugs[base])
        if used_slugs[base] > 1:
            slug_collisions.append((slug, disp, [mm["seg_id"] for mm in members]))
        ent = {
            "slug": slug, "display": disp, "key": key,
            "seg_ids": [mm["seg_id"] for mm in members],
            "eras": sorted({mm["era"] for mm in members}),
            "seasons": sorted(set().union(*[mm["seasons"] for mm in members])),
            "teams": sorted(set().union(*[mm["teams"] for mm in members])),
        }
        entities[slug] = ent
        for mm in members:
            seg_to_entity[mm["seg_id"]] = {"slug": slug, "display": disp, "key": key}

    # ---- audit --------------------------------------------------------------
    merges = [ss for ss in order if len({segs[x]["era"] for x in ss}) > 1]
    unmerged = {k: v for k, v in by_key.items()
                if len({find(x) for x in v}) > 1}
    lines = ["Player identity audit (teams/players round)",
             "=" * 60, "",
             "Segments: %d | entities: %d | cross-era merges: %d | "
             "same-key splits kept: %d | slug collisions: %d"
             % (len(segs), len(entities), len(merges), len(unmerged), len(slug_collisions)),
             ""]
    lines.append("CROSS-ERA MERGES (segments joined into one player):")
    lines.append("-" * 60)
    for ss in sorted(merges, key=lambda s: segs[s[0]]["key"]):
        head = seg_to_entity[ss[0]]
        lines.append("%s  [%s]" % (head["display"], head["slug"]))
        for x in sorted(ss, key=lambda z: segs[z]["smin"]):
            s = segs[x]
            lines.append("    %-5s id=%-8s %s..%s  (%d games)  \"%s\""
                         % (s["era"], s["player_id"], s["seasons"][0], s["seasons"][-1],
                            s["games"], s["display"]))
    lines += ["", "SAME-KEY PAIRS LEFT UNMERGED (gap > 1 season or same-era):",
              "-" * 60]
    for k in sorted(unmerged):
        lines.append("key=%s" % k)
        for x in sorted(unmerged[k], key=lambda z: segs[z]["smin"]):
            s = segs[x]
            lines.append("    %-5s id=%-8s %s..%s  -> entity [%s]"
                         % (s["era"], s["player_id"], s["seasons"][0], s["seasons"][-1],
                            seg_to_entity[x]["slug"]))
    if slug_collisions:
        lines += ["", "SLUG COLLISIONS (distinct players sharing a base slug):", "-" * 60]
        for slug, disp, segids in slug_collisions:
            lines.append("    %s  \"%s\"  segs=%s" % (slug, disp, segids))
    with open(PLAYER_AUDIT_TXT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("players: %d segments -> %d entities (%d cross-era merges, "
          "%d same-key splits kept, %d slug collisions)"
          % (len(segs), len(entities), len(merges), len(unmerged), len(slug_collisions)))
    print("full audit -> %s" % os.path.relpath(PLAYER_AUDIT_TXT, REPO))
    print("\nSample cross-era merges:")
    for ss in sorted(merges, key=lambda s: -sum(segs[x]["games"] for x in s))[:8]:
        h = seg_to_entity[ss[0]]
        rng = "/".join("%s:%s..%s" % (segs[x]["era"], segs[x]["seasons"][0], segs[x]["seasons"][-1])
                       for x in sorted(ss, key=lambda z: segs[z]["smin"]))
        print("  %-26s %s" % (h["display"], rng))
    print("\nSample same-key pairs kept SEPARATE (the gap rule at work):")
    shown = 0
    for k in sorted(unmerged, key=lambda k: -len(unmerged[k])):
        segids = unmerged[k]
        if len({find(x) for x in segids}) < 2:
            continue
        rng = " | ".join("%s %s..%s [%s]" % (segs[x]["era"], segs[x]["seasons"][0],
                         segs[x]["seasons"][-1], seg_to_entity[x]["slug"])
                         for x in sorted(segids, key=lambda z: segs[z]["smin"]))
        print("  %-22s %s" % (k, rng))
        shown += 1
        if shown >= 8:
            break
    return seg_to_entity, entities


def slugify_unique(display, ref_key, used):
    slug = ref_key
    if slug in used and used[slug] != ref_key:
        slug = "%s-%s" % (ref_key, re.sub(r"[^a-z0-9]+", "", ref_key)[:6])
    used[slug] = ref_key
    return slug


def aggregate(off, gm, pl, tg, game_tot, display, raw_ids, eras, seg_to_entity, league_baselines):
    hr("SECTION 6  Per-referee aggregation")

    games_meta = gm.copy()
    games_meta["kind"] = games_meta.apply(season_type_label, axis=1)
    games_meta["abs_margin"] = (games_meta["home_pts"] - games_meta["away_pts"]).abs()
    games_meta["total_pts"] = games_meta["home_pts"] + games_meta["away_pts"]
    # Per-team (not combined) box-score figures for /matchup/
    # (docs/MATCHUP_SPEC.md) -- tg is one row per (game_id, team_abbr); merge
    # the home side's and away side's own FTA/PF onto the game row so a
    # team's "own" vs. "opponent's" FTA/PF can be told apart, unlike
    # game_tot's box_fta/box_pf (both teams combined, used for the whistle
    # profile's "combined FTA" stat elsewhere).
    tg_box = tg[["game_id", "team_abbr", "team_fta", "team_pf"]]
    games_meta = games_meta.merge(
        tg_box.rename(columns={"team_abbr": "home_team_abbr", "team_fta": "home_fta", "team_pf": "home_pf"}),
        on=["game_id", "home_team_abbr"], how="left"
    ).merge(
        tg_box.rename(columns={"team_abbr": "away_team_abbr", "team_fta": "away_fta", "team_pf": "away_pf"}),
        on=["game_id", "away_team_abbr"], how="left"
    )
    games_meta["home_canon"] = games_meta["home_team_abbr"].apply(
        lambda t: nba_tricodes.canonical_franchise(t) if isinstance(t, str) and t else None)
    games_meta["away_canon"] = games_meta["away_team_abbr"].apply(
        lambda t: nba_tricodes.canonical_franchise(t) if isinstance(t, str) and t else None)
    gmeta = games_meta.set_index("game_id")

    got = game_tot.set_index("game_id")
    baselines = build_player_baselines(pl, games_meta)

    # ref -> list of game_ids (one row per (ref, game) after trimming)
    ref_games = off.groupby("ref_key")["game_id"].apply(list).to_dict()

    # pre-index player logs by game for top performances / swings
    pl_by_game = {g: d for g, d in pl.groupby("game_id")}

    referees_index = []
    all_swings = []          # master (ref, player-segment) swing rows for player pages
    all_team_records = []    # master (ref, team) rows for team pages
    # Clean the output dir first so referees dropped by an override merge don't
    # linger as stale/orphaned files from a previous build.
    ref_dir = os.path.join(DATA, "referees")
    os.makedirs(ref_dir, exist_ok=True)
    for old in glob.glob(os.path.join(ref_dir, "*.json")):
        os.remove(old)

    # Tier C per-referee game logs (docs/TIER_C_SPEC.md section 3): a separate
    # output dir, kept out of data/referees/{id}.json so the main per-referee
    # page's JSON doesn't balloon for the busiest officials (1,700+ games).
    game_log_dir = os.path.join(DATA, "referee_games")
    os.makedirs(game_log_dir, exist_ok=True)
    for old in glob.glob(os.path.join(game_log_dir, "*.json")):
        os.remove(old)

    # /matchup/ (docs/MATCHUP_SPEC.md): team x referee lookup, one file per
    # referee (same reasoning as referee_games above -- kept out of the main
    # per-referee JSON so it doesn't balloon for the busiest officials).
    matchup_dir = os.path.join(DATA, "matchups")
    os.makedirs(matchup_dir, exist_ok=True)
    for old in glob.glob(os.path.join(matchup_dir, "*.json")):
        os.remove(old)

    # Slugs are assigned in ONE pass, upfront, keyed only on ref_key -- not
    # interleaved with the main per-referee loop below. The game-log crew
    # links need every OTHER referee's slug while building THIS referee's
    # entry, which the old interleaved assignment couldn't guarantee (a
    # later-alphabetical ref_key wouldn't have a slug yet).
    used_slugs = {}
    slug_of = {rk: slugify_unique(display[rk], rk, used_slugs) for rk in sorted(ref_games)}

    # game_id -> sorted list of ref_keys who officiated it (post-trim, so this
    # is exactly the real crew -- 1, 2, or 3 names depending on era coverage).
    # Built once here rather than via build_game_crew() (which needs the final
    # referees_index/slugs this function itself produces -- a chicken-and-egg
    # problem solved the same way as the slug pre-pass above).
    game_to_crew = off.groupby("game_id")["ref_key"].apply(lambda s: sorted(set(s))).to_dict()

    for ref_key in sorted(ref_games):
        gids = ref_games[ref_key]
        gsub = gmeta.reindex(gids)
        gsub = gsub[gsub["season"].notna()]
        if gsub.empty:
            continue

        slug = slug_of[ref_key]
        seasons = sorted(gsub["season"].unique())
        kinds = gsub["kind"]

        n_total = len(gsub)
        n_rs = int((kinds == "RS").sum())
        n_po = int((kinds == "PO").sum())
        n_pi = int((kinds == "PI").sum())
        finals_games = int(((gsub["po_round"] == 4)).sum())
        game7s = int(((gsub["po_game_num"] == 7)).sum())
        active = CURRENT_SEASON in seasons

        per_season = {}
        for s, g2 in gsub.groupby("season"):
            k = g2["kind"]
            per_season[s] = {
                "rs": int((k == "RS").sum()),
                "po": int((k == "PO").sum()),
                "pi": int((k == "PI").sum()),
                "total": int(len(g2)),
            }

        # ---- team records ------------------------------------------------
        # Grouped by CANONICAL franchise (nba_tricodes.canonical_franchise),
        # not raw team_abbr, per the franchise-consolidation layer: a
        # historical/relocated tricode's games roll into its modern
        # successor's row (VAN->MEM, NJN->BKN, NOH/NOK->NOP). This only
        # affects team-LEVEL aggregation; the raw per-game team_abbr is
        # untouched everywhere else (notable games, top performances, etc.).
        team_rows = defaultdict(lambda: {"games": 0, "wins": 0, "losses": 0,
                                         "home_games": 0, "home_wins": 0, "margin_sum": 0.0})
        for gid, g in gsub.iterrows():
            for side in ("home", "away"):
                team = g["%s_team_abbr" % side]
                if not isinstance(team, str) or not team:
                    continue
                team = nba_tricodes.canonical_franchise(team)
                won = (g["home_win"] == 1) if side == "home" else (g["home_win"] == 0)
                margin = (g["home_pts"] - g["away_pts"]) if side == "home" \
                    else (g["away_pts"] - g["home_pts"])
                tr = team_rows[team]
                tr["games"] += 1
                tr["wins"] += 1 if won else 0
                tr["losses"] += 0 if won else 1
                if side == "home":
                    tr["home_games"] += 1
                    tr["home_wins"] += 1 if won else 0
                if pd.notna(margin):
                    tr["margin_sum"] += float(margin)
        team_records = []
        for team, tr in team_rows.items():
            rec = {
                "team_abbr": team,
                "games": tr["games"],
                "wins": tr["wins"],
                "losses": tr["losses"],
                "win_pct": clean_num(tr["wins"] / tr["games"]) if tr["games"] else None,
                "home_games": tr["home_games"],
                "home_wins": tr["home_wins"],
                "avg_margin_for_team": clean_num(tr["margin_sum"] / tr["games"]) if tr["games"] else None,
            }
            team_records.append(rec)
            # master row for the team pages (same numbers, tagged with this ref)
            m = dict(rec)
            m.update({"ref_key": ref_key, "ref_name": display[ref_key], "ref_slug": slug,
                      "away_games": tr["games"] - tr["home_games"],
                      "away_wins": tr["wins"] - tr["home_wins"]})
            all_team_records.append(m)
        team_records.sort(key=lambda r: -r["games"])

        # ---- /matchup/ team x referee lookup (docs/MATCHUP_SPEC.md) --------
        # "Melt" each game this ref worked into two rows, one per side, each
        # already resolved to THAT team's own win/pts_for/pts_against/fta_for/
        # fta_against/pf_for/pf_against/is_home -- turns a home-or-away game
        # table into a per-team perspective table a single groupby can
        # aggregate, instead of re-deriving "which side is this team" inside
        # every aggregation. PI is excluded (no league baseline exists for it
        # elsewhere on the site either, and it's not RS or PO).
        rs_po = gsub[gsub["kind"].isin(["RS", "PO"])]
        home_persp = pd.DataFrame({
            "team": rs_po["home_canon"], "kind": rs_po["kind"], "season": rs_po["season"],
            "is_home": True, "win": rs_po["home_win"] == 1,
            "pts_for": rs_po["home_pts"], "pts_against": rs_po["away_pts"],
            "fta_for": rs_po["home_fta"], "fta_against": rs_po["away_fta"],
            "pf_for": rs_po["home_pf"], "pf_against": rs_po["away_pf"],
        })
        away_persp = pd.DataFrame({
            "team": rs_po["away_canon"], "kind": rs_po["kind"], "season": rs_po["season"],
            "is_home": False, "win": rs_po["home_win"] == 0,
            "pts_for": rs_po["away_pts"], "pts_against": rs_po["home_pts"],
            "fta_for": rs_po["away_fta"], "fta_against": rs_po["home_fta"],
            "pf_for": rs_po["away_pf"], "pf_against": rs_po["home_pf"],
        })
        melted = pd.concat([home_persp, away_persp], ignore_index=True)
        melted = melted[melted["team"].notna()]

        matchup_teams = {}
        for team, tgrp in melted.groupby("team"):
            career = {}
            for kind, kgrp in tgrp.groupby("kind"):
                career[kind.lower()] = _agg_matchup(kgrp)
            season_rows = []
            for season, sgrp in tgrp.groupby("season"):
                row = {"season": season}
                for kind, skgrp in sgrp.groupby("kind"):
                    row[kind.lower()] = _agg_matchup(skgrp)
                season_rows.append(row)
            season_rows.sort(key=lambda r: r["season"], reverse=True)
            matchup_teams[team] = {"team_abbr": team, "career": career, "seasons": season_rows}

        matchup_doc = {"official_id": ref_key, "name": display[ref_key], "slug": slug,
                       "teams": matchup_teams}
        assert_no_nan(matchup_doc, "matchup[%s]" % ref_key)
        with open(os.path.join(matchup_dir, "%s.json" % ref_key), "w", encoding="utf-8") as fh:
            json.dump(matchup_doc, fh, ensure_ascii=False, indent=2)

        # ---- whistle profile (RS and PO separately), with League Context
        # era-adjusted expected baseline + differential (docs/
        # LEAGUE_CONTEXT_SPEC.md section 2). expected[key] is the games-
        # weighted average of the LEAGUE value for that stat, across exactly
        # the seasons this referee worked that kind, weighted by this
        # referee's own games in that season -- so a ref who worked mostly
        # 1990s seasons gets an expected baseline pulled toward the (lower-
        # scoring) 1990s league average, not the career-spanning average.
        # differential = actual - expected. Rank/percentile (assigned in
        # build_whistle_leaderboards below) are computed on differential, not
        # the raw value -- the entire point of this round: a raw ranking is
        # dominated by era, not by the official.
        whistle = {}
        for kind in ("RS", "PO"):
            ksub = gsub[gsub["kind"] == kind]
            entry = _whistle_stat_values(ksub, got)
            kind_key = kind.lower()

            weights_by_stat = defaultdict(float)
            sums = defaultdict(float)
            for season in ksub["season"].unique():
                w = per_season.get(season, {}).get(kind_key, 0)
                if w <= 0:
                    continue
                lb = league_baselines.get(season, {}).get(kind_key)
                if not lb:
                    continue
                for stat_key, *_rest in WHISTLE_STATS:
                    v = lb.get(stat_key)
                    if v is not None:
                        sums[stat_key] += v * w
                        weights_by_stat[stat_key] += w
            expected = {stat_key: (clean_num(sums[stat_key] / weights_by_stat[stat_key])
                                   if weights_by_stat[stat_key] else None)
                       for stat_key, *_rest in WHISTLE_STATS}
            differential = {}
            for stat_key, *_rest in WHISTLE_STATS:
                actual, exp = entry.get(stat_key), expected.get(stat_key)
                differential[stat_key] = clean_num(actual - exp) if (actual is not None
                                                                      and exp is not None) else None

            entry["expected"] = expected
            entry["differential"] = differential
            whistle[kind_key] = entry

        # ---- League Context section 3: per-season splits ---------------------
        # One row per season this referee worked, RS and PO broken out
        # separately (matching the whistle profile's own RS/PO split -- the
        # two are different scoring/pace regimes and combining them would be
        # misleading). This is the season selector: a full table, not a
        # dropdown that hides data. Play-In games are counted (games_pi) so
        # the row reconciles with games_total, but carry no stats/diff --
        # league_baselines deliberately has no PI population to compare
        # against (build_league_baselines / whistle profiles are RS/PO only).
        season_splits = []
        for season, ssub in gsub.groupby("season"):
            row = {"season": season, "games_rs": 0, "games_po": 0, "games_pi": 0, "stats": {}}
            row["games_pi"] = int((ssub["kind"] == "PI").sum())
            for kind in ("RS", "PO"):
                kind_key = kind.lower()
                kssub = ssub[ssub["kind"] == kind]
                svals = _whistle_stat_values(kssub, got)
                row["games_%s" % kind_key] = svals["n"]
                if svals["n"] == 0:
                    continue
                lb = league_baselines.get(season, {}).get(kind_key)
                stat_row = {}
                for stat_key, *_rest in WHISTLE_STATS:
                    actual = svals.get(stat_key)
                    lgval = lb.get(stat_key) if lb else None
                    diff = clean_num(actual - lgval) if (actual is not None
                                                         and lgval is not None) else None
                    stat_row[stat_key] = {"value": actual, "league": lgval, "diff": diff}
                row["stats"][kind_key] = stat_row
            season_splits.append(row)
        season_splits.sort(key=lambda r: r["season"], reverse=True)

        # ---- top performances + player swings -------------------------------
        # Swings accumulate at the CANONICAL ENTITY level (seg_to_entity slug):
        # a player's games with a referee combine across eras into one row and
        # one >=15 threshold. Baselines stay keyed on (player_id, season), which
        # is era-safe (seasons are single-era) and correctly compares each game
        # to that player's own same-era same-season average. The (era, player_id)
        # -> entity mapping keeps the 120 cross-era numeric-id collisions apart
        # (e.g. "JR Smith" and "Andrew Bogut" never merge).
        top_perf = []
        sw = defaultdict(lambda: {"name": None, "slug": None, "pids": defaultdict(int),
                                  "seasons": set(), "pts": [], "fta": [], "pf": [],
                                  "reb": [], "ast": [], "base_pts": [], "base_fta": [],
                                  "base_pf": [], "base_reb": [], "base_ast": []})
        for gid, g in gsub.iterrows():
            rows = pl_by_game.get(gid)
            if rows is None:
                continue
            home, away = g["home_team_abbr"], g["away_team_abbr"]
            season, kind = g["season"], g["kind"]
            era = "espn" if is_espn_scheme(gid) else "nba"
            for _, p in rows.iterrows():
                team = p["team_abbr"]
                opp = away if team == home else home
                seg_id = "%s:%s" % (era, p["player_id"])
                ent = seg_to_entity.get(seg_id)
                top_perf.append((float(p["pts"]), p["player_name"], team, opp,
                                 g["game_date"], gid, seg_id))
                if kind == "PI":
                    continue  # play-in has no baseline bucket
                base = baselines.get((p["player_id"], season, kind))
                if base is None or ent is None:
                    continue
                if kind == "PO" and base[3] < PO_BASELINE_MIN:
                    continue
                acc = sw[ent["slug"]]
                acc["name"] = ent["display"]
                acc["slug"] = ent["slug"]
                acc["pids"][p["player_id"]] += 1
                acc["seasons"].add(season)
                acc["pts"].append(float(p["pts"]))
                acc["fta"].append(float(p["fta"]))
                acc["pf"].append(float(p["pf"]))
                acc["reb"].append(float(p["reb"]))
                acc["ast"].append(float(p["ast"]))
                acc["base_pts"].append(base[0])
                acc["base_fta"].append(base[1])
                acc["base_pf"].append(base[2])
                acc["base_reb"].append(base[4])
                acc["base_ast"].append(base[5])

        top_perf.sort(key=lambda r: -r[0])
        top_performances = [{
            "player_name": r[1], "pts": int(r[0]), "team_abbr": r[2], "opp_abbr": r[3],
            "game_date": r[4], "game_id": r[5],
            "player_slug": (seg_to_entity.get(r[6]) or {}).get("slug"),
        } for r in top_perf[:TOP_PERF_N]]

        def _mean(a):
            return sum(a) / len(a)

        # Full swing rows for every (ref, player-ENTITY) with n>=15 combined
        # games. The ref page renders the top-50 subset (pts/fta/pf); the master
        # list feeds the player pages so both views share one computation and are
        # numerically identical. player_id is the entity's most-played segment id
        # (representative only; the slug is the canonical key).
        full_swings = []
        for ent_slug, acc in sw.items():
            n = len(acc["pts"])
            if n < SWING_MIN_GAMES:
                continue
            rep_pid = max(acc["pids"].items(), key=lambda kv: kv[1])[0]
            pts_with, pts_base = _mean(acc["pts"]), _mean(acc["base_pts"])
            reb_with, reb_base = _mean(acc["reb"]), _mean(acc["base_reb"])
            ast_with, ast_base = _mean(acc["ast"]), _mean(acc["base_ast"])
            full_swings.append({
                "player_id": rep_pid, "name": acc["name"], "n_games": n,
                "slug": ent_slug,
                "pts_with_ref": clean_num(pts_with),
                "pts_baseline": clean_num(pts_base),
                "pts_swing": clean_num(pts_with - pts_base),
                "reb_with_ref": clean_num(reb_with), "reb_baseline": clean_num(reb_base),
                "reb_swing": clean_num(reb_with - reb_base),
                "ast_with_ref": clean_num(ast_with), "ast_baseline": clean_num(ast_base),
                "ast_swing": clean_num(ast_with - ast_base),
                "fta_swing": clean_num(_mean(acc["fta"]) - _mean(acc["base_fta"])),
                "pf_swing": clean_num(_mean(acc["pf"]) - _mean(acc["base_pf"])),
                "seasons": sorted(acc["seasons"]),
            })
        # master record for player pages (all rows, tagged with this ref)
        for r in full_swings:
            rec = dict(r)
            rec["ref_key"] = ref_key
            rec["ref_name"] = display[ref_key]
            rec["ref_slug"] = slug
            all_swings.append(rec)

        # ref-page player_swings: the same entity-level rows, top 50 by |pts
        # swing|, projected to the fields the ref page renders.
        player_swings = []
        for r in full_swings:
            player_swings.append({
                "player_id": r["player_id"], "name": r["name"], "n_games": r["n_games"],
                "slug": r["slug"],
                "pts_with_ref": r["pts_with_ref"], "pts_baseline": r["pts_baseline"],
                "pts_swing": r["pts_swing"],
                "fta_swing": r["fta_swing"], "pf_swing": r["pf_swing"],
            })
        player_swings.sort(key=lambda r: -abs(r["pts_swing"] or 0))
        player_swings = player_swings[:SWING_TOP_N]
        # Selection above is by |swing| (unchanged); DISPLAY order is signed
        # value descending -- biggest positive first, biggest negative last.
        player_swings.sort(key=lambda r: -(r["pts_swing"] or 0))

        # ---- notable games (Finals + Game 7s; NBA-scheme only) --------------
        notable = []
        for gid, g in gsub.iterrows():
            is_finals = (g["po_round"] == 4)
            is_g7 = (g["po_game_num"] == 7)
            if not (is_finals or is_g7):
                continue
            rnd = int(g["po_round"]) if pd.notna(g["po_round"]) else None
            gnum = int(g["po_game_num"]) if pd.notna(g["po_game_num"]) else None
            notable.append({
                "game_id": gid, "date": g["game_date"], "season": g["season"],
                "matchup": "%s@%s" % (g["away_team_abbr"], g["home_team_abbr"]),
                "result": "%s %d, %s %d" % (
                    g["home_team_abbr"], int(g["home_pts"]) if pd.notna(g["home_pts"]) else 0,
                    g["away_team_abbr"], int(g["away_pts"]) if pd.notna(g["away_pts"]) else 0),
                "round": "Finals" if rnd == 4 else ("Round %d" % rnd if rnd else None),
                "game_num": gnum,
            })
        notable.sort(key=lambda r: r["date"], reverse=True)

        # ---- Tier C: full per-game log, grouped by season (docs/TIER_C_SPEC.md
        # section 3) -- every game this referee worked, not just Finals/G7s.
        # Written to its own file (game_log_dir), not embedded in ref_doc.
        log_by_season = defaultdict(list)
        for gid, g in gsub.iterrows():
            kind = g["kind"]
            rnd = int(g["po_round"]) if pd.notna(g["po_round"]) else None
            if kind == "RS":
                round_label = "Regular Season"
            elif kind == "PI":
                round_label = "Play-In"
            elif rnd == 4:
                round_label = "Finals"
            elif rnd:
                round_label = "Round %d" % rnd
            else:
                round_label = "Playoffs"
            co_officials = [{"name": display[k], "slug": slug_of[k]}
                            for k in game_to_crew.get(gid, []) if k != ref_key]
            log_by_season[g["season"]].append({
                "game_id": gid, "date": g["game_date"], "kind": kind,
                "home_team_abbr": g["home_team_abbr"], "away_team_abbr": g["away_team_abbr"],
                # Canonical franchise for each side (docs/MATCHUP_SPEC.md) --
                # so /matchup/'s client-side game-log filter matches the exact
                # same team grouping the summary above it was built from (a
                # historical tricode like VAN must filter into a MEM matchup).
                "home_canon": g["home_canon"], "away_canon": g["away_canon"],
                "home_pts": int(g["home_pts"]) if pd.notna(g["home_pts"]) else None,
                "away_pts": int(g["away_pts"]) if pd.notna(g["away_pts"]) else None,
                "round_label": round_label,
                "co_officials": co_officials,
            })
        by_season_log = [
            {"season": s, "games": sorted(log_by_season[s], key=lambda r: r["date"], reverse=True)}
            for s in sorted(log_by_season, reverse=True)
        ]
        game_log_doc = {
            "official_id": ref_key, "name": display[ref_key], "slug": slug,
            "games_total": n_total, "by_season": by_season_log,
        }
        assert_no_nan(game_log_doc, "game_log[%s]" % ref_key)
        with open(os.path.join(game_log_dir, "%s.json" % ref_key), "w", encoding="utf-8") as fh:
            json.dump(game_log_doc, fh, ensure_ascii=False, indent=2)

        # ---- officiating quality score (DASHBOARD_SPEC section 2) -----------
        # Regular-season games count at QUALITY_RS_POINTS each; playoff games
        # with a known round (label_rounds) are weighted by how deep into the
        # playoffs that round was. Games with no round label at all (a
        # genuinely unrecoverable game) and Play-In games simply don't
        # contribute beyond their base RS weight -- they have none, since
        # only kind=="RS" games get QUALITY_RS_POINTS.
        quality_total = int(n_rs * QUALITY_RS_POINTS + sum(
            QUALITY_PO_POINTS.get(int(r), 0) for r in gsub["po_round"].dropna()))
        seasons_active = len(per_season)
        quality_per_season = clean_num(quality_total / seasons_active) if seasons_active else None

        summary = {
            "official_id": ref_key, "name": display[ref_key], "slug": slug,
            "raw_ids": sorted(raw_ids[ref_key]), "eras": sorted(eras[ref_key]),
            "first_season": seasons[0], "last_season": seasons[-1],
            "games_total": n_total, "games_rs": n_rs, "games_po": n_po, "games_pi": n_pi,
            "finals_games": finals_games, "game7s": game7s, "active": active,
            "quality_total": quality_total, "seasons_active": seasons_active,
            "quality_per_season": quality_per_season,
            "per_season": per_season,
        }

        ref_doc = {
            "summary": summary,
            "team_records": team_records,
            "whistle_profile": whistle,
            "season_splits": season_splits,
            "top_performances": top_performances,
            "player_swings": player_swings,
            "notable_games": notable,
        }
        assert_no_nan(ref_doc, "ref[%s]" % ref_key)

        with open(os.path.join(DATA, "referees", "%s.json" % ref_key), "w",
                  encoding="utf-8") as fh:
            json.dump(ref_doc, fh, ensure_ascii=False, indent=2)

        referees_index.append({
            "official_id": ref_key, "name": display[ref_key], "slug": slug,
            "first_season": seasons[0], "last_season": seasons[-1],
            "games_total": n_total, "games_rs": n_rs, "games_po": n_po,
            "finals_games": finals_games, "game7s": game7s, "active": active,
            "quality_total": quality_total, "seasons_active": seasons_active,
            "quality_per_season": quality_per_season,
        })

        # keep the per-ref detail around for QA/leaderboards without re-reading
        ref_doc["_index"] = referees_index[-1]

    print("wrote %d per-referee JSON files" % len(referees_index))
    assert_no_nan(referees_index, "referees_index")
    with open(os.path.join(DATA, "referees.json"), "w", encoding="utf-8") as fh:
        json.dump(referees_index, fh, ensure_ascii=False, indent=2)

    print("collected %d (ref, player-segment) swing rows and %d (ref, team) rows"
          % (len(all_swings), len(all_team_records)))
    return referees_index, all_swings, all_team_records


# ----------------------------------------------------------------------------
# team & player pages (TEAMS_PLAYERS_SPEC §1)
# ----------------------------------------------------------------------------
def build_game_crew(off_ref, ref_lookup):
    """game_id -> [{name, slug}] for the (trimmed) officiating crew."""
    crew = {}
    for gid, grp in off_ref.groupby("game_id"):
        seen, out = set(), []
        for rk in grp["ref_key"]:
            if rk in ref_lookup and rk not in seen:
                seen.add(rk)
                nm, sl = ref_lookup[rk]
                out.append({"name": nm, "slug": sl})
        crew[gid] = out
    return crew


def build_crewmates(off_ref, referees_index):
    """For each canonical referee, count games officiated together with every
    other referee (crew-of-3 only, alternates already excluded by the first-3
    rule) and inject a top-5 top_partners array into that ref's JSON."""
    hr("SECTION 8  Crewmates")
    from itertools import combinations
    name_of = {r["official_id"]: r["name"] for r in referees_index}
    slug_of = {r["official_id"]: r["slug"] for r in referees_index}
    pair = defaultdict(int)
    for _gid, grp in off_ref.groupby("game_id"):
        crew = sorted(set(grp["ref_key"]))       # distinct keys in the trimmed crew
        for a, b in combinations(crew, 2):
            pair[(a, b)] += 1
    partners = defaultdict(list)
    for (a, b), c in pair.items():
        partners[a].append((b, c))
        partners[b].append((a, c))
    for r in referees_index:
        rk = r["official_id"]
        ranked = sorted(partners.get(rk, []),
                        key=lambda x: (-x[1], name_of.get(x[0], x[0])))[:5]
        top = [{"name": name_of[o], "slug": slug_of[o], "games": c}
               for o, c in ranked if o in name_of]
        path = os.path.join(DATA, "referees", "%s.json" % rk)
        doc = json.load(open(path, encoding="utf-8"))
        doc["top_partners"] = top
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    print("added top_partners to %d referee JSONs (%d distinct ref pairs counted)"
          % (len(referees_index), len(pair)))


def build_whistle_leaderboards(referees_index):
    """For each whistle-profile stat (RS and PO separately): gather qualifying
    refs -- LEADERBOARD_MIN_GAMES for RS (same gate the site's other rate
    leaderboards use), PO_LEADERBOARD_MIN_GAMES for PO (playoff games are
    scarcer per career, so reusing the RS threshold left almost the entire
    playoff-experienced pool gray/unranked) -- applied to whichever n-column
    that stat is computed from -- rank them, and:
      (a) write data/whistle_leaderboards.json, one sorted list per (stat,
          kind), for the dedicated /leaderboard/{slug}/ pages;
      (b) inject each qualifying ref's percentile rank back into their own
          whistle_profile[kind][key + "_pctile"] (100 = highest value in the
          qualifying field, 0 = lowest -- not "good"/"bad", just where they
          fall). Non-qualifying refs get None so the renderer never KeyErrors.

    League Context (docs/LEAGUE_CONTEXT_SPEC.md section 2): rank/percentile
    are computed on the era-adjusted DIFFERENTIAL, not the raw value -- a raw
    ranking is dominated by which seasons a referee happened to work, not by
    the official. The raw value ships in every row too (section 6 keeps it as
    a legitimate, separately-labeled factual column), just not as the sort
    key. A referee with no computable differential for a stat (in practice
    only possible if every season they worked that stat is missing a league
    baseline, which shouldn't happen given build_league_baselines covers
    every season with any games) is excluded rather than ranked on a value
    that isn't era-adjusted.
    """
    hr("SECTION 8  Whistle-profile leaderboards + percentiles")
    docs = {r["official_id"]: json.load(
        open(os.path.join(DATA, "referees", "%s.json" % r["official_id"]), encoding="utf-8"))
        for r in referees_index}
    min_games_for = {"rs": LEADERBOARD_MIN_GAMES, "po": PO_LEADERBOARD_MIN_GAMES}

    leaderboards = {}
    for key, ncol, label, slug in WHISTLE_STATS:
        leaderboards[key] = {"label": label, "slug": slug}
        for kind in ("rs", "po"):
            min_games = min_games_for[kind]
            rows = []
            skipped_no_diff = 0
            for r in referees_index:
                off_id = r["official_id"]
                entry = docs[off_id]["whistle_profile"][kind]
                val, n = entry.get(key), entry.get(ncol)
                diff = (entry.get("differential") or {}).get(key)
                if val is None or n is None or n < min_games:
                    continue
                if diff is None:
                    skipped_no_diff += 1
                    continue
                rows.append({"official_id": off_id, "name": r["name"], "slug": r["slug"],
                             "value": val, "diff": diff, "n": n})
            rows.sort(key=lambda x: x["diff"], reverse=True)
            total = len(rows)
            for rank, row in enumerate(rows, 1):
                pctile = clean_num((total - rank) / (total - 1) * 100) if total > 1 else 100.0
                wp = docs[row["official_id"]]["whistle_profile"][kind]
                wp[key + "_pctile"] = pctile
                wp[key + "_rank"] = rank
                row["rank"], row["pctile"] = rank, pctile
            leaderboards[key][kind] = [
                {"rank": x["rank"], "name": x["name"], "slug": x["slug"],
                 "value": x["value"], "diff": x["diff"], "n": x["n"], "pctile": x["pctile"]}
                for x in rows]
            # Pool size travels on every referee's doc (even non-qualifying
            # ones) so the compact card ("12th lowest of 118") can render
            # "not enough games to rank" copy with the real denominator.
            for r in referees_index:
                docs[r["official_id"]]["whistle_profile"][kind][key + "_qualifying"] = total
            if skipped_no_diff:
                print("    %-22s %-2s: %d qualifying-by-n referee(s) skipped "
                      "(no era-adjusted differential available)" % (key, kind, skipped_no_diff))
        print("  %-24s rs qualifying=%-4d (>=%d)   po qualifying=%-4d (>=%d)"
              % (key, len(leaderboards[key]["rs"]), LEADERBOARD_MIN_GAMES,
                 len(leaderboards[key]["po"]), PO_LEADERBOARD_MIN_GAMES))

    # Every ref's whistle_profile gets a (possibly None) pctile/rank field for
    # every stat, even when they don't qualify, so render_pages.py can read it
    # unconditionally.
    for off_id, doc in docs.items():
        for kind in ("rs", "po"):
            for key, *_rest in WHISTLE_STATS:
                doc["whistle_profile"][kind].setdefault(key + "_pctile", None)
                doc["whistle_profile"][kind].setdefault(key + "_rank", None)
        path = os.path.join(DATA, "referees", "%s.json" % off_id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)

    leaderboards["_meta"] = {"min_games": {"rs": LEADERBOARD_MIN_GAMES,
                                           "po": PO_LEADERBOARD_MIN_GAMES}}
    assert_no_nan(leaderboards, "whistle_leaderboards")
    with open(os.path.join(DATA, "whistle_leaderboards.json"), "w", encoding="utf-8") as fh:
        json.dump(leaderboards, fh, ensure_ascii=False, indent=2)
    print("wrote data/whistle_leaderboards.json")
    return leaderboards


def _clean_dir(path):
    os.makedirs(path, exist_ok=True)
    for old in glob.glob(os.path.join(path, "*.json")):
        os.remove(old)


def _recent_form_count_window(d, size):
    """Rolling game-count window over one referee's ordered game series `d`.
    Returns (per-stat dict of {value, n} arrays aligned to d's rows using
    min_periods=1 -- so a short career still yields a "however many games
    are available" figure -- plus a parallel "exact" array using
    min_periods=size, NaN until a genuine size-game window exists, which is
    what feeds the league-wide pool), and the games-in-window array."""
    games = np.minimum(np.arange(1, len(d) + 1), size)
    out = {}
    for key, col, needs_box in RECENT_FORM_STATS:
        s = d[col]
        if needs_box:
            cnt = s.notna().rolling(size, min_periods=1).sum()
            summ = s.fillna(0.0).rolling(size, min_periods=1).sum()
            value = (summ / cnt).where(cnt >= RECENT_FORM_MIN_BOX)
            cnt_exact = s.notna().rolling(size, min_periods=size).sum()
            summ_exact = s.fillna(0.0).rolling(size, min_periods=size).sum()
            exact = (summ_exact / cnt_exact).where(cnt_exact >= RECENT_FORM_MIN_BOX)
            n = cnt
        else:
            value = s.rolling(size, min_periods=1).mean()
            exact = s.rolling(size, min_periods=size).mean()
            n = s.rolling(size, min_periods=1).count()
        out[key] = {"value": value.to_numpy(), "n": n.to_numpy(), "exact": exact.to_numpy()}
    return out, games


def _recent_form_calendar_window(d, days):
    """Trailing calendar-day window (pandas time-based rolling, so irregular
    game spacing is handled natively). No separate "exact" array -- pooling
    eligibility for this window type is decided per-position by how many
    games actually fell in that slice (see RECENT_FORM_MIN_BOX below)."""
    idxd = d.set_index("game_date_dt")
    win = "%dD" % days
    games = idxd["total_pts"].rolling(win).count().to_numpy()
    out = {}
    for key, col, needs_box in RECENT_FORM_STATS:
        s = idxd[col]
        if needs_box:
            cnt = s.notna().rolling(win).sum()
            summ = s.fillna(0.0).rolling(win).sum()
            value = (summ / cnt).where(cnt >= RECENT_FORM_MIN_BOX)
            n = cnt
        else:
            value = s.rolling(win).mean()
            n = s.rolling(win).count()
        out[key] = {"value": value.to_numpy(), "n": n.to_numpy()}
    return out, games


def _recent_form_calendar_window_asof(d, days, asof):
    """The referee's CURRENT calendar window, computed exactly once, anchored
    to `asof` (the actual build date) rather than to that referee's own last
    game. This is what makes the window go genuinely empty off-season:
    _recent_form_calendar_window's rolling("Nd") series is anchored to each
    ROW's own date, so its last element answers "how many games fell in the
    30 days before this referee's own most recent game" -- a number that
    stays whatever it was even if that most recent game was months ago. Only
    this as-of-today slice answers the question the feature actually needs to
    ask: how many games has this referee worked in the last 30 real days."""
    cutoff = pd.Timestamp(asof) - pd.Timedelta(days=days)
    sub = d[(d["game_date_dt"] > cutoff) & (d["game_date_dt"] <= pd.Timestamp(asof))]
    stats = {}
    for key, col, needs_box in RECENT_FORM_STATS:
        s = sub[col].dropna()
        n = len(s)
        min_n = RECENT_FORM_MIN_BOX if needs_box else 1
        stats[key] = {"value": float(s.mean()) if n >= min_n else None, "n": n}
    return stats, len(sub)


RECENT_FORM_WINDOW_DEFS = [
    ("n5", 5, "count", "Last 5 games"),
    ("n10", 10, "count", "Last 10 games"),
    ("n25", 25, "count", "Last 25 games"),
    ("cal30", RECENT_FORM_CAL_DAYS, "calendar", "Last 30 days"),
]


def build_recent_form(referees_index, off_ref, gm, tg, game_tot):
    """Recent form: rolling windows over each referee's own chronological
    RS+PO game log (docs/RECENT_FORM_SPEC.md), shown against the league-wide
    distribution of same-size windows so a reader can tell whether a recent
    deviation is notable or routine -- see the RECENT_FORM_* constants above
    for the sample-size and pooling policy this implements.

    Two passes, same shape as build_whistle_leaderboards: pass 1 computes
    every referee's rolling series and feeds the league-wide pool; pass 2
    (after every referee's pooled contribution is in) classifies each
    referee's CURRENT window against that pool and writes the final files.
    """
    hr("SECTION 9  Recent form (rolling windows + league variance)")
    games_meta = gm.copy()
    games_meta["kind"] = games_meta.apply(season_type_label, axis=1)
    games_meta["abs_margin"] = (games_meta["home_pts"] - games_meta["away_pts"]).abs()
    games_meta["total_pts"] = games_meta["home_pts"] + games_meta["away_pts"]
    games_meta["home_win_f"] = (games_meta["home_win"] == 1).astype(float)
    gmeta = games_meta.set_index("game_id")
    got = game_tot.set_index("game_id")

    ref_games = off_ref.groupby("ref_key")["game_id"].apply(list).to_dict()
    today = datetime.date.today()

    # ---- build each referee's ordered per-game series ----------------------
    series = {}
    for r in referees_index:
        off_id = r["official_id"]
        gids = ref_games.get(off_id, [])
        gsub = gmeta.reindex(gids)
        gsub = gsub[gsub["kind"].isin(["RS", "PO"])]
        if gsub.empty:
            continue
        box = got.reindex(gsub.index)
        has_box = box["n_teams"] == 2
        d = pd.DataFrame({
            "game_date_dt": pd.to_datetime(gsub["game_date"]),
            "total_pts": gsub["total_pts"],
            "home_win_f": gsub["home_win_f"],
            "abs_margin": gsub["abs_margin"],
            "box_fta": box["box_fta"].where(has_box),
            "box_pf": box["box_pf"].where(has_box),
            "is_ot_f": box["is_ot"].astype(float).where(has_box),
        }, index=gsub.index).sort_values("game_date_dt").reset_index(drop=True)
        series[off_id] = d

    # ---- career baselines (RS+PO combined, chronological -- this feature's
    # own population, independent of the whistle_profile's RS/PO split) -----
    baselines = {}
    for off_id, d in series.items():
        base = {}
        for key, col, needs_box in RECENT_FORM_STATS:
            s = d[col].dropna()
            min_n = RECENT_FORM_MIN_BOX if needs_box else 1
            base[key] = clean_num(s.mean()) if len(s) >= min_n else None
        baselines[off_id] = base

    # ---- pass 1: rolling windows + pooled deviations -----------------------
    pooled = defaultdict(list)   # (window_key, stat_key) -> [deviation, ...]
    raw = {}                     # off_id -> {window_key: {"stats":..., "games":..., "kind":...}}
    for off_id, d in series.items():
        career_games = len(d)
        base = baselines[off_id]
        raw[off_id] = {}
        for wkey, size_or_days, kind, label in RECENT_FORM_WINDOW_DEFS:
            if kind == "count":
                stats, games_arr = _recent_form_count_window(d, size_or_days)
                pool_eligible = career_games >= RECENT_FORM_POOL_MULT * size_or_days
            else:
                stats, games_arr = _recent_form_calendar_window(d, size_or_days)
                pool_eligible = career_games >= RECENT_FORM_POOL_MIN_CAL_CAREER
            raw[off_id][wkey] = {"stats": stats, "games": games_arr, "kind": kind,
                                 "size": size_or_days, "label": label}
            if not pool_eligible:
                continue
            for key, col, needs_box in RECENT_FORM_STATS:
                b = base.get(key)
                if b is None:
                    continue
                vals = stats[key]["exact"] if kind == "count" else stats[key]["value"]
                for pos in range(len(vals)):
                    v = vals[pos]
                    if v is None or (isinstance(v, float) and np.isnan(v)):
                        continue
                    if kind == "calendar" and games_arr[pos] < RECENT_FORM_MIN_BOX:
                        continue
                    pooled[(wkey, key)].append(float(v) - b)

    pool_stats = {}
    for pk, devs in pooled.items():
        arr = np.array(sorted(devs))
        pool_stats[pk] = {
            "n": len(arr),
            "sorted": arr,
            "lo": float(np.percentile(arr, RECENT_FORM_NORMAL_LO)),
            "hi": float(np.percentile(arr, RECENT_FORM_NORMAL_HI)),
        }
    print("  pooled window samples per (window, stat) -- e.g. n5/avg_total_points: %d, "
          "n25/avg_total_points: %d, cal30/avg_total_points: %d"
          % (pool_stats.get(("n5", "avg_total_points"), {}).get("n", 0),
             pool_stats.get(("n25", "avg_total_points"), {}).get("n", 0),
             pool_stats.get(("cal30", "avg_total_points"), {}).get("n", 0)))

    # ---- pass 2: classify each referee's CURRENT window, write files -------
    recent_form_dir = os.path.join(DATA, "recent_form")
    _clean_dir(recent_form_dir)
    dashboard_candidates = []   # every (ref, window, stat) classified "outside", for the widget
    name_by_id = {r["official_id"]: r["name"] for r in referees_index}
    slug_by_id = {r["official_id"]: r["slug"] for r in referees_index}

    for off_id, d in series.items():
        base = baselines[off_id]
        last_game_date = d["game_date_dt"].iloc[-1]
        days_since_last = (pd.Timestamp(today) - last_game_date).days
        windows_out = {}
        for wkey, size_or_days, kind, label in RECENT_FORM_WINDOW_DEFS:
            if kind == "calendar":
                # Anchored to `today`, not to this referee's own last game --
                # see _recent_form_calendar_window_asof's docstring.
                cur_stats, games_now = _recent_form_calendar_window_asof(d, size_or_days, today)
            else:
                info = raw[off_id][wkey]
                games_now = int(info["games"][-1]) if not np.isnan(info["games"][-1]) else 0
                cur_stats = {key: {"value": info["stats"][key]["value"][-1],
                                   "n": info["stats"][key]["n"][-1]}
                            for key, col, needs_box in RECENT_FORM_STATS}
            stat_out = {}
            for key, col, needs_box in RECENT_FORM_STATS:
                v = cur_stats[key]["value"]
                n = cur_stats[key]["n"]
                v = None if (v is None or (isinstance(v, float) and np.isnan(v))) else clean_num(v)
                n = 0 if (n is None or (isinstance(n, float) and np.isnan(n))) else int(n)
                b = base.get(key)
                diff = clean_num(v - b) if (v is not None and b is not None) else None
                status, pctile_rank = None, None
                pk = (wkey, key)
                # Only classify a genuine full-size window (count: exactly
                # `size` games; calendar: at least RECENT_FORM_MIN_BOX games
                # actually in the slice) against a pool with enough samples
                # to mean anything.
                full_enough = (games_now >= size_or_days) if kind == "count" else (games_now >= RECENT_FORM_MIN_BOX)
                if diff is not None and full_enough and pool_stats.get(pk, {}).get("n", 0) >= 50:
                    ps = pool_stats[pk]
                    status = "outside" if (diff < ps["lo"] or diff > ps["hi"]) else "within"
                    pctile_rank = clean_num(
                        100.0 * np.searchsorted(ps["sorted"], diff, side="right") / ps["n"])
                stat_out[key] = {"value": v, "baseline": b, "diff": diff, "n": n, "status": status,
                                 "pctile_rank": pctile_rank}
                if status == "outside" and pctile_rank is not None:
                    dashboard_candidates.append({
                        "official_id": off_id, "window": wkey, "window_label": label,
                        "stat": key, "extremity": abs(pctile_rank - 50),
                        "value": v, "baseline": b, "diff": diff, "n": n,
                        "pctile_rank": pctile_rank,
                    })
            windows_out[wkey] = {"label": label, "kind": kind, "size": size_or_days,
                                 "games": games_now, "stats": stat_out}
        doc = {
            "official_id": off_id, "name": name_by_id[off_id], "slug": slug_by_id[off_id],
            "as_of": today.isoformat(), "days_since_last_game": days_since_last,
            "baseline": base, "windows": windows_out,
        }
        assert_no_nan(doc, "recent_form[%s]" % off_id)
        with open(os.path.join(recent_form_dir, "%s.json" % off_id), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)

    # ---- dashboard widget: the most extreme (ref, window, stat), one per
    # active referee, gated to officials who've actually worked recently so
    # an off-season/retired outlier from years ago can't surface here -------
    dashboard_candidates.sort(key=lambda c: -c["extremity"])
    seen_refs = set()
    spotlight_rows = []
    active_ids = {off_id for off_id, d in series.items()
                 if (pd.Timestamp(today) - d["game_date_dt"].iloc[-1]).days <= RECENT_FORM_CAL_DAYS}
    for c in dashboard_candidates:
        if c["official_id"] not in active_ids or c["official_id"] in seen_refs:
            continue
        seen_refs.add(c["official_id"])
        spotlight_rows.append({
            "official_id": c["official_id"], "name": name_by_id[c["official_id"]],
            "slug": slug_by_id[c["official_id"]], "window": c["window"],
            "window_label": c["window_label"], "stat": c["stat"], "value": c["value"],
            "baseline": c["baseline"], "diff": c["diff"], "n": c["n"],
            "pctile_rank": c["pctile_rank"],
        })
        if len(spotlight_rows) >= RECENT_FORM_DASHBOARD_TOP_N:
            break

    recent_form_dashboard = {
        "as_of": today.isoformat(),
        "in_season": bool(active_ids),   # league-wide: has ANYONE worked a game in the last 30 days
        "spotlight": spotlight_rows,
    }
    assert_no_nan(recent_form_dashboard, "recent_form_dashboard")
    with open(os.path.join(DATA, "recent_form_dashboard.json"), "w", encoding="utf-8") as fh:
        json.dump(recent_form_dashboard, fh, ensure_ascii=False, indent=2)

    print("wrote %d data/recent_form/*.json files" % len(series))
    print("  league in-season (>=1 ref worked within %d days): %s (%d active referees)"
          % (RECENT_FORM_CAL_DAYS, recent_form_dashboard["in_season"], len(active_ids)))
    print("  dashboard spotlight: %d entries (from %d outside-normal-variance candidates "
          "among active referees)" % (len(spotlight_rows),
                                      sum(1 for c in dashboard_candidates if c["official_id"] in active_ids)))
    return recent_form_dashboard


def build_team_pages(all_team_records, gm):
    hr("SECTION 8  Team pages")
    team_dir = os.path.join(DATA, "teams")
    _clean_dir(team_dir)

    # franchise-consolidation audit: confirm each historical tricode's games
    # are entirely absent as a standalone key (they roll into their canonical
    # successor) and print how many games moved.
    print("Franchise consolidation (historical tricode -> canonical franchise):")
    for hist, canon in sorted(nba_tricodes.FRANCHISE_CANONICAL.items()):
        n = int(((gm["home_team_abbr"] == hist) | (gm["away_team_abbr"] == hist)).sum())
        print("  %s -> %s : %d games rolled up" % (hist, canon, n))
    print("SEA / OKC kept as separate canonical entities (no merge, per the "
          "2008 relocation settlement)")

    by_team = defaultdict(list)
    for r in all_team_records:
        by_team[r["team_abbr"]].append(r)

    # per-team seasons + dataset game totals from the games table, grouped by
    # canonical franchise (VAN's 2000-01 season rolls into MEM's, etc.) --
    # same consolidation as the team_records aggregation above.
    seasons_for, total_for = {}, {}
    long = pd.concat([
        gm[["game_id", "season"]].assign(t=gm["home_team_abbr"].map(nba_tricodes.canonical_franchise)),
        gm[["game_id", "season"]].assign(t=gm["away_team_abbr"].map(nba_tricodes.canonical_franchise)),
    ])
    for tri, grp in long.groupby("t"):
        seasons_for[tri] = sorted(grp["season"].unique())
        total_for[tri] = int(grp["game_id"].nunique())

    index = []
    for tri in sorted(by_team):
        if tri not in ALLOWED_TRICODES:
            continue
        recs = [r for r in by_team[tri] if r["games"] >= TEAM_REF_MIN_GAMES]
        recs.sort(key=lambda r: -r["games"])
        ref_records = [{
            "ref_name": r["ref_name"], "ref_slug": r["ref_slug"], "games": r["games"],
            "wins": r["wins"], "losses": r["losses"], "win_pct": r["win_pct"],
            "home_games": r["home_games"], "home_wins": r["home_wins"],
            "away_games": r["away_games"], "away_wins": r["away_wins"],
            "avg_margin_for_team": r["avg_margin_for_team"],
        } for r in recs]
        seasons = seasons_for.get(tri, [])
        doc = {
            "summary": {
                "tricode": tri, "name": nba_tricodes.display_name(tri), "slug": tri.lower(),
                "first_season": seasons[0] if seasons else None,
                "last_season": seasons[-1] if seasons else None,
                "games_total": total_for.get(tri, 0),
                "historical": tri in nba_tricodes.HISTORICAL_TRICODES,
            },
            "ref_records": ref_records,
        }
        assert_no_nan(doc, "team[%s]" % tri)
        with open(os.path.join(team_dir, "%s.json" % tri.lower()), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        index.append({"tricode": tri, "name": doc["summary"]["name"], "slug": tri.lower()})

    with open(os.path.join(DATA, "teams.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    print("wrote %d team pages" % len(index))
    return index


def build_player_pages(all_swings, entities, seg_to_entity, pl, gm, game_crew):
    hr("SECTION 8  Player pages")
    player_dir = os.path.join(DATA, "players")
    _clean_dir(player_dir)

    # ref_splits grouped by entity slug (each all_swings row is one (ref, seg))
    by_slug = defaultdict(list)
    for r in all_swings:
        if r.get("slug"):
            by_slug[r["slug"]].append(r)
    qualifying = set(by_slug)

    # per-game rows for every qualifying player's segments (for top_games + summary)
    m = pl.merge(gm[["game_id", "season", "home_team_abbr", "away_team_abbr",
                     "game_date", "era"]], on="game_id", how="inner")
    m["seg_id"] = m["era"] + ":" + m["player_id"]
    m["slug"] = m["seg_id"].map(lambda s: (seg_to_entity.get(s) or {}).get("slug"))
    m = m[m["slug"].isin(qualifying)].copy()
    m["opp"] = m["away_team_abbr"].where(m["team_abbr"] == m["home_team_abbr"],
                                         m["home_team_abbr"])

    games_by_slug = {slug: grp for slug, grp in m.groupby("slug")}

    index = []
    for slug in sorted(qualifying):
        ent = entities[slug]
        grp = games_by_slug.get(slug)
        seasons = sorted(grp["season"].unique()) if grp is not None else ent["seasons"]
        teams = sorted(set(grp["team_abbr"])) if grp is not None else ent["teams"]
        games_total = int(grp["game_id"].nunique()) if grp is not None else 0

        splits = sorted(by_slug[slug], key=lambda r: -r["n_games"])
        ref_splits = [{
            "ref_name": r["ref_name"], "ref_slug": r["ref_slug"], "player_id": r["player_id"],
            "n_games": r["n_games"],
            "pts_with_ref": r["pts_with_ref"], "pts_baseline": r["pts_baseline"],
            "pts_swing": r["pts_swing"],
            "reb_with_ref": r["reb_with_ref"], "reb_baseline": r["reb_baseline"],
            "reb_swing": r["reb_swing"],
            "ast_with_ref": r["ast_with_ref"], "ast_baseline": r["ast_baseline"],
            "ast_swing": r["ast_swing"], "seasons": r["seasons"],
        } for r in splits]

        top_games = []
        if grp is not None:
            for _, row in grp.sort_values("pts", ascending=False).head(PLAYER_TOP_GAMES).iterrows():
                top_games.append({
                    "pts": int(row["pts"]), "reb": int(row["reb"]), "ast": int(row["ast"]),
                    "team_abbr": row["team_abbr"], "opp_abbr": row["opp"],
                    "game_date": row["game_date"], "game_id": row["game_id"],
                    "crew": game_crew.get(row["game_id"], []),
                })

        doc = {
            "summary": {
                "name": ent["display"], "slug": slug,
                "first_season": seasons[0] if seasons else None,
                "last_season": seasons[-1] if seasons else None,
                "teams": teams, "games_total": games_total, "eras": ent["eras"],
            },
            "ref_splits": ref_splits,
            "top_games": top_games,
        }
        assert_no_nan(doc, "player[%s]" % slug)
        with open(os.path.join(player_dir, "%s.json" % slug), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        index.append({"slug": slug, "name": ent["display"]})

    with open(os.path.join(DATA, "players.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    print("wrote %d player pages" % len(index))
    return index


def qa_teams_players(referees_index, team_index, player_index):
    hr("SECTION 8  QA gate (teams & players)")
    import random
    random.seed(0)
    failures = []

    def load(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    ref_docs = {r["official_id"]: load(os.path.join(DATA, "referees", "%s.json" % r["official_id"]))
                for r in referees_index}

    # (hard) every player page has >=1 ref split
    empty = [p["slug"] for p in player_index
             if not load(os.path.join(DATA, "players", "%s.json" % p["slug"]))["ref_splits"]]
    print("[hard] player pages with zero ref splits: %d" % len(empty))
    if empty:
        failures.append("empty player pages: %s" % empty[:5])

    # (hard) spot-check 20 random ref-JSON player_swings rows reconcile identically
    pairs = []
    for r in referees_index:
        for row in ref_docs[r["official_id"]]["player_swings"]:
            if row.get("slug"):
                pairs.append((r["official_id"], row))
    random.shuffle(pairs)
    checked = mismatch = 0
    for off_id, row in pairs[:20]:
        pdoc = load(os.path.join(DATA, "players", "%s.json" % row["slug"]))
        # entity-level: exactly one ref_split per (player, referee)
        match = next((s for s in pdoc["ref_splits"]
                      if s["ref_slug"] == ref_docs[off_id]["summary"]["slug"]), None)
        checked += 1
        if match is None or match["n_games"] != row["n_games"] \
                or match["pts_swing"] != row["pts_swing"] \
                or match["pts_with_ref"] != row["pts_with_ref"] \
                or match["pts_baseline"] != row["pts_baseline"]:
            mismatch += 1
            failures.append("player-split mismatch: ref=%s player=%s" % (off_id, row["slug"]))
    print("[hard] player-split spot-check: %d checked, %d mismatched" % (checked, mismatch))

    # (hard) spot-check 20 random team ref_records reconcile with the ref JSON
    tpairs = []
    for t in team_index:
        tdoc = load(os.path.join(DATA, "teams", "%s.json" % t["slug"]))
        for rr in tdoc["ref_records"]:
            tpairs.append((t["tricode"], rr))
    random.shuffle(tpairs)
    tchecked = tmis = 0
    slug_to_off = {ref_docs[r["official_id"]]["summary"]["slug"]: r["official_id"]
                   for r in referees_index}
    for tri, rr in tpairs[:20]:
        off_id = slug_to_off.get(rr["ref_slug"])
        tchecked += 1
        trow = next((x for x in ref_docs[off_id]["team_records"] if x["team_abbr"] == tri), None) \
            if off_id else None
        if trow is None or trow["games"] != rr["games"] or trow["wins"] != rr["wins"] \
                or trow["avg_margin_for_team"] != rr["avg_margin_for_team"]:
            tmis += 1
            failures.append("team-record mismatch: team=%s ref=%s" % (tri, rr["ref_slug"]))
    print("[hard] team-record spot-check: %d checked, %d mismatched" % (tchecked, tmis))

    if failures:
        hr("TEAMS/PLAYERS QA GATE: FAILED")
        for f in failures[:10]:
            print("  FAIL: %s" % f)
        raise SystemExit(1)
    print("\n[teams/players QA checks all passed]")


# ----------------------------------------------------------------------------
# leaderboards
# ----------------------------------------------------------------------------
def build_leaderboards(referees_index):
    hr("Leaderboards")

    def load_detail(off_id):
        with open(os.path.join(DATA, "referees", "%s.json" % off_id),
                  encoding="utf-8") as fh:
            return json.load(fh)

    details = {r["official_id"]: load_detail(r["official_id"]) for r in referees_index}

    def top(rows, key, n=25, reverse=True, filt=None):
        rows = [r for r in rows if filt is None or filt(r)]
        rows = sorted(rows, key=key, reverse=reverse)
        return rows[:n]

    def entry(r, extra):
        base = {"official_id": r["official_id"], "name": r["name"], "slug": r["slug"]}
        base.update(extra)
        return base

    idx = referees_index

    most_games = [entry(r, {"games_total": r["games_total"]})
                  for r in top(idx, lambda r: r["games_total"])]
    most_games_active = [entry(r, {"games_total": r["games_total"]})
                         for r in top(idx, lambda r: r["games_total"], filt=lambda r: r["active"])]
    most_po = [entry(r, {"games_po": r["games_po"]})
               for r in top(idx, lambda r: r["games_po"])]
    most_finals = [entry(r, {"finals_games": r["finals_games"]})
                   for r in top(idx, lambda r: r["finals_games"])]
    most_g7 = [entry(r, {"game7s": r["game7s"]})
               for r in top(idx, lambda r: r["game7s"])]

    def current_games(r):
        ps = details[r["official_id"]]["summary"]["per_season"].get(CURRENT_SEASON)
        return ps["total"] if ps else 0
    most_current = [entry(r, {"games_current": current_games(r)})
                    for r in top(idx, current_games, filt=lambda r: r["active"])]

    # home win% and avg total FTA need min-n gates and come from whistle profiles.
    # Like the six dedicated whistle leaderboards (League Context section 2),
    # this index-page widget ranks on the era-adjusted differential, not the
    # raw value; referees with no computable differential are excluded. Raw
    # values are still carried for display and for the dashboard's separate
    # literal-record extremes (_raw_extremes below).
    hw = []
    fta = []
    skipped_hw = skipped_fta = 0
    for r in idx:
        det = details[r["official_id"]]
        wp = det["whistle_profile"]
        n_all = wp["rs"]["n"] + wp["po"]["n"]
        if n_all >= LEADERBOARD_MIN_GAMES:
            # home win% over RS+PO combined; differential is the games-weighted
            # average of each kind's own differential (equivalent to combined
            # actual minus combined expected).
            hw_num = 0.0
            diff_num, diff_den = 0.0, 0
            for k in ("rs", "po"):
                e = wp[k]
                if e["home_win_pct"] is not None:
                    hw_num += e["home_win_pct"] * e["n"]
                d = (e.get("differential") or {}).get("home_win_pct")
                if d is not None:
                    diff_num += d * e["n"]
                    diff_den += e["n"]
            if diff_den:
                hw.append(entry(r, {"home_win_pct": clean_num(hw_num / n_all),
                                     "diff": clean_num(diff_num / diff_den), "n": n_all}))
            else:
                skipped_hw += 1
        rs = wp["rs"]
        if rs["n_boxscore"] >= LEADERBOARD_MIN_GAMES and rs["avg_total_fta"] is not None:
            diff = (rs.get("differential") or {}).get("avg_total_fta")
            if diff is not None:
                fta.append(entry(r, {"avg_total_fta": rs["avg_total_fta"], "diff": diff,
                                      "n": rs["n_boxscore"]}))
            else:
                skipped_fta += 1
    if skipped_hw:
        print("  [note] home-win%% widget: %d referees skipped (no differential)" % skipped_hw)
    if skipped_fta:
        print("  [note] FTA widget: %d referees skipped (no differential)" % skipped_fta)

    # Officiating quality score (DASHBOARD_SPEC section 2). Career-total
    # ranking has no seasons_active gate; the per-season ranking applies
    # QUALITY_MIN_SEASONS so a single lucky rookie-season Finals assignment
    # can't top the list on n=1 -- the same small-sample guard the site
    # already applies everywhere else (e.g. LEADERBOARD_MIN_GAMES).
    most_quality_total = [entry(r, {"quality_total": r["quality_total"]})
                          for r in top(idx, lambda r: r["quality_total"])]
    most_quality_per_season = [
        entry(r, {"quality_per_season": r["quality_per_season"], "n": r["seasons_active"]})
        for r in top(idx, lambda r: r["quality_per_season"],
                     filt=lambda r: r["seasons_active"] >= QUALITY_MIN_SEASONS)]

    leaderboards = {
        "most_career_games": most_games,
        "most_career_games_active": most_games_active,
        "most_playoff_games": most_po,
        "most_finals_games": most_finals,
        "most_game7s": most_g7,
        "most_games_current_season": most_current,
        "highest_home_win_pct": sorted(hw, key=lambda r: -r["diff"])[:25],
        "lowest_home_win_pct": sorted(hw, key=lambda r: r["diff"])[:25],
        "highest_avg_total_fta_rs": sorted(fta, key=lambda r: -r["diff"])[:25],
        "lowest_avg_total_fta_rs": sorted(fta, key=lambda r: r["diff"])[:25],
        "most_quality_total": most_quality_total,
        "most_quality_per_season": most_quality_per_season,
        "_meta": {"min_games_for_rate_leaderboards": LEADERBOARD_MIN_GAMES,
                  "min_seasons_for_quality_per_season": QUALITY_MIN_SEASONS,
                  "current_season": CURRENT_SEASON},
        # Literal raw-value extremes (unranked by differential) for the
        # dashboard's factual "records" strip -- that widget claims a literal
        # "highest ever recorded" value, which must stay raw, not diff-ranked.
        "_raw_extremes": {
            "highest_home_win_pct": max(hw, key=lambda r: r["home_win_pct"]) if hw else None,
            "lowest_home_win_pct": min(hw, key=lambda r: r["home_win_pct"]) if hw else None,
            "highest_avg_total_fta_rs": max(fta, key=lambda r: r["avg_total_fta"]) if fta else None,
        },
    }
    assert_no_nan(leaderboards, "leaderboards")
    with open(os.path.join(DATA, "leaderboards.json"), "w", encoding="utf-8") as fh:
        json.dump(leaderboards, fh, ensure_ascii=False, indent=2)
    print("wrote data/leaderboards.json")
    return leaderboards, details


# ----------------------------------------------------------------------------
# Tier C: informational density (docs/TIER_C_SPEC.md)
# ----------------------------------------------------------------------------
def build_crews(off_ref, gm, game_tot, ref_lookup):
    """Every unique trio of canonical referees who worked a game together
    (crew-of-3 only -- alternates are already excluded by the first-3-by-
    row-order rule upstream, so a game's distinct ref_keys ARE the real crew
    when there are exactly 3). Returns the top CREW_TOP_N trios by games
    together; the index widget takes the first 5 of this same list."""
    hr("SECTION 9  Crew chemistry (Tier C)")
    gmeta = gm.set_index("game_id")
    got = game_tot.set_index("game_id")

    trio_games = defaultdict(list)
    for gid, grp in off_ref.groupby("game_id"):
        crew = tuple(sorted(set(grp["ref_key"])))
        if len(crew) == 3:
            trio_games[crew].append(gid)

    rows = []
    for crew, gids in trio_games.items():
        refs = [{"name": ref_lookup[k][0], "slug": ref_lookup[k][1]} for k in crew if k in ref_lookup]
        if len(refs) != 3:
            continue
        sub = gmeta.reindex(gids)
        sub = sub[sub["season"].notna()]
        if sub.empty:
            continue
        seasons = sorted(sub["season"].unique())
        avg_pts = clean_num((sub["home_pts"] + sub["away_pts"]).mean())
        box = got.reindex(sub.index)
        box = box[box["n_teams"] == 2]
        avg_fta = clean_num(box["box_fta"].mean()) if len(box) else None
        rows.append({
            "refs": refs, "games": len(sub),
            "first_season": seasons[0], "last_season": seasons[-1],
            "avg_total_points": avg_pts, "pts_n": len(sub),
            "avg_total_fta": avg_fta, "fta_n": len(box),
        })
    rows.sort(key=lambda r: -r["games"])
    top = rows[:CREW_TOP_N]
    print("crew-of-3 trios found: %d distinct ; top trio: %s games together"
          % (len(rows), top[0]["games"] if top else 0))
    return top


def build_team_officials(team_index):
    """For each canonical franchise, the official who has worked the most of
    that team's games -- frequency only (deliberately not a win-rate extreme;
    see docs/TIER_C_SPEC.md for why). Reads back the team JSON build_team_pages
    just wrote, whose ref_records are already sorted by -games and gated at
    TEAM_REF_MIN_GAMES, so ref_records[0] (if any) IS this by construction."""
    hr("SECTION 9  Team officials (Tier C)")
    rows = []
    for t in team_index:
        doc = json.load(open(os.path.join(DATA, "teams", "%s.json" % t["slug"]), encoding="utf-8"))
        recs = doc["ref_records"]
        if not recs:
            continue
        top = recs[0]
        rows.append({
            "tricode": t["tricode"], "team_name": t["name"], "team_slug": t["slug"],
            "ref_name": top["ref_name"], "ref_slug": top["ref_slug"],
            "games": top["games"], "n": top["games"],
        })
    rows.sort(key=lambda r: r["team_name"])
    print("team_officials: %d/%d teams have a qualifying most-frequent official "
          "(min %d games)" % (len(rows), len(team_index), TEAM_REF_MIN_GAMES))
    return rows


def build_debuts_farewells(referees_index):
    """Per season: referees whose first game in the dataset falls in that
    season, and whose last game falls in that season. CURRENT_SEASON never
    contributes a farewell entry -- those referees are presumably still
    active, so labeling them a "farewell" would be a real (not descriptive)
    claim this site doesn't have grounds to make. Sorted most-recent-season
    first, matching every other "history" list on the site."""
    hr("SECTION 9  Debuts & farewells (Tier C)")
    by_season = defaultdict(lambda: {"debuts": [], "farewells": []})
    for r in referees_index:
        by_season[r["first_season"]]["debuts"].append({"name": r["name"], "slug": r["slug"]})
        if r["last_season"] != CURRENT_SEASON:
            by_season[r["last_season"]]["farewells"].append({"name": r["name"], "slug": r["slug"]})
    for s in by_season.values():
        s["debuts"].sort(key=lambda x: x["name"])
        s["farewells"].sort(key=lambda x: x["name"])
    out = [{"season": s, "debuts": by_season[s]["debuts"], "farewells": by_season[s]["farewells"]}
           for s in sorted(by_season, reverse=True)]
    n_debuts = sum(len(s["debuts"]) for s in out)
    n_farewells = sum(len(s["farewells"]) for s in out)
    print("debuts_farewells: %d season(s) ; %d total debuts ; %d total farewells "
          "(farewells always omitted for %s, the current season)"
          % (len(out), n_debuts, n_farewells, CURRENT_SEASON))
    return out


def build_era_leaders(referees_index, details):
    """By decade (DECADES): leaders in total games, playoff games, and Finals
    games. Total/playoff games are summed from each ref's per_season (already
    computed in aggregate()); Finals games are counted from each ref's
    notable_games (round=='Finals' is already exactly the po_round==4 games --
    see aggregate()'s notable-games loop -- and now carries a 'season' field
    added specifically so this can bucket them by decade without re-deriving
    a season from a bare date)."""
    hr("SECTION 9  Era leaders (Tier C)")
    name_of = {r["official_id"]: r["name"] for r in referees_index}
    slug_of = {r["official_id"]: r["slug"] for r in referees_index}
    per_decade = {label: defaultdict(lambda: {"total": 0, "po": 0, "finals": 0})
                 for label, *_ in DECADES}
    # "All-time" is not one of the DECADES buckets -- it's every season in the
    # database, accumulated alongside (not instead of) the per-decade tallies.
    all_time_acc = defaultdict(lambda: {"total": 0, "po": 0, "finals": 0})

    for r in referees_index:
        off_id = r["official_id"]
        doc = details[off_id]
        for season, counts in doc["summary"]["per_season"].items():
            all_time_acc[off_id]["total"] += counts["total"]
            all_time_acc[off_id]["po"] += counts["po"]
            label = season_decade(season)
            if label is None:
                continue
            acc = per_decade[label][off_id]
            acc["total"] += counts["total"]
            acc["po"] += counts["po"]
        for g in doc["notable_games"]:
            if g.get("round") != "Finals":
                continue
            all_time_acc[off_id]["finals"] += 1
            label = season_decade(g["season"])
            if label is None:
                continue
            per_decade[label][off_id]["finals"] += 1

    def top_list_for(accs, field):
        rows = [{"official_id": oid, "name": name_of[oid], "slug": slug_of[oid], "value": a[field]}
                for oid, a in accs.items() if a[field] > 0]
        rows.sort(key=lambda x: -x["value"])
        return rows[:ERA_LEADERS_TOP_N]

    def print_era(label, partial):
        e = era_leaders[label]
        print("  %-8s (%s%s): total leader=%s(%d)  playoff leader=%s(%d)  finals leader=%s(%d)"
              % (label, e["season_range"], " partial" if partial else "",
                 e["total_games"][0]["name"] if e["total_games"] else "—",
                 e["total_games"][0]["value"] if e["total_games"] else 0,
                 e["playoff_games"][0]["name"] if e["playoff_games"] else "—",
                 e["playoff_games"][0]["value"] if e["playoff_games"] else 0,
                 e["finals_games"][0]["name"] if e["finals_games"] else "—",
                 e["finals_games"][0]["value"] if e["finals_games"] else 0))

    era_leaders = {}
    for label, lo, hi, partial in DECADES:
        accs = per_decade[label]
        era_leaders[label] = {
            "label": label, "partial": partial,
            "season_range": "%d-%02d through %d-%02d" % (lo, (lo + 1) % 100, hi, (hi + 1) % 100),
            "total_games": top_list_for(accs, "total"),
            "playoff_games": top_list_for(accs, "po"),
            "finals_games": top_list_for(accs, "finals"),
        }
        print_era(label, partial)
    era_leaders["All-time"] = {
        "label": "All-time", "partial": False,
        "season_range": "%d-%02d through %s" % (
            SEASON_FLOOR_YEAR, (SEASON_FLOOR_YEAR + 1) % 100, CURRENT_SEASON),
        "total_games": top_list_for(all_time_acc, "total"),
        "playoff_games": top_list_for(all_time_acc, "po"),
        "finals_games": top_list_for(all_time_acc, "finals"),
    }
    print_era("All-time", False)
    return era_leaders


def build_swings_all(all_swings):
    """Every (player, referee) pair already meeting SWING_MIN_GAMES (n>=15 --
    the same gate used everywhere else on the site), sorted by signed points
    swing. Kept in its own file, capped at the top/bottom SWINGS_ALL_CAP, since
    the full all_swings list (tens of thousands of rows) is far too large to
    ship whole (docs/TIER_C_SPEC.md)."""
    hr("Swings, all qualifying pairs (Tier C)")
    rows = sorted(all_swings, key=lambda r: -(r["pts_swing"] or 0))

    def proj(r):
        return {
            "player_name": r["name"], "player_slug": r["slug"],
            "ref_name": r["ref_name"], "ref_slug": r["ref_slug"],
            "n_games": r["n_games"],
            "pts_with_ref": r["pts_with_ref"], "pts_baseline": r["pts_baseline"],
            "pts_swing": r["pts_swing"],
        }
    top = [proj(r) for r in rows[:SWINGS_ALL_CAP]]
    tail = rows[-SWINGS_ALL_CAP:] if len(rows) > SWINGS_ALL_CAP else []
    bottom = [proj(r) for r in reversed(tail)]   # most-negative first
    out = {"top": top, "bottom": bottom, "total_pairs": len(rows)}
    assert_no_nan(out, "swings_all")
    with open(os.path.join(DATA, "swings_all.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("wrote data/swings_all.json (top=%d, bottom=%d, of %d total qualifying pairs)"
          % (len(top), len(bottom), len(rows)))
    return out


def qa_game_logs(referees_index):
    """Tier C QA: every referee's game-log row count must equal games_total,
    and every co-official slug in every game log must resolve to a real
    referee page."""
    hr("SECTION 9  Game-log QA (Tier C)")
    known_slugs = {r["slug"] for r in referees_index}
    failures = []
    for r in referees_index:
        path = os.path.join(DATA, "referee_games", "%s.json" % r["official_id"])
        doc = json.load(open(path, encoding="utf-8"))
        logged = sum(len(s["games"]) for s in doc["by_season"])
        if logged != r["games_total"]:
            failures.append("game-log count mismatch: %s logged=%d games_total=%d"
                            % (r["official_id"], logged, r["games_total"]))
        for s in doc["by_season"]:
            for g in s["games"]:
                for co in g["co_officials"]:
                    if co["slug"] not in known_slugs:
                        failures.append("game-log co-official slug not in referees_index: "
                                        "%s (ref=%s game=%s)" % (co["slug"], r["official_id"], g["game_id"]))
    print("[hard] game-log row count matches games_total for all %d referees: %s"
          % (len(referees_index), not any("mismatch" in f for f in failures)))
    print("[hard] every co-official slug across all game logs resolves to a real referee: %s"
          % (not any("co-official slug" in f for f in failures)))
    if failures:
        hr("GAME-LOG QA: FAILED")
        for f in failures[:10]:
            print("  FAIL: %s" % f)
        raise SystemExit(1)
    print("\n[game-log QA checks all passed]")


def qa_league_context(referees_index):
    """League Context QA (docs/LEAGUE_CONTEXT_SPEC.md): every season split's
    game counts must reconcile with that referee's games_total, and no NaN
    anywhere in season_splits (assert_no_nan already covers this at write
    time in aggregate(); re-verified here from the written files as an
    independent check, same pattern as qa_game_logs)."""
    hr("SECTION 9  League Context QA")
    failures = []
    for r in referees_index:
        path = os.path.join(DATA, "referees", "%s.json" % r["official_id"])
        doc = json.load(open(path, encoding="utf-8"))
        splits = doc["season_splits"]
        summed = sum(row["games_rs"] + row["games_po"] + row["games_pi"] for row in splits)
        if summed != r["games_total"]:
            failures.append("season_splits game-count mismatch: %s summed=%d games_total=%d"
                            % (r["official_id"], summed, r["games_total"]))
        assert_no_nan(splits, "season_splits[%s]" % r["official_id"])
    print("[hard] season_splits game counts reconcile with games_total for all %d referees: %s"
          % (len(referees_index), not failures))
    if failures:
        hr("LEAGUE CONTEXT QA: FAILED")
        for f in failures[:10]:
            print("  FAIL: %s" % f)
        raise SystemExit(1)
    print("\n[League Context QA checks all passed]")


def league_context_spot_check(referees_index):
    """League Context QA (docs/LEAGUE_CONTEXT_SPEC.md): the whole point of
    this round is that a 1990s-heavy referee gets swept toward the bottom of
    a RAW scoring leaderboard as a pure artifact of era (1990s combined
    points were far lower league-wide), not moved there by anything about how
    they officiated. This must be shown working, not just asserted: find the
    most 1990s-heavy qualifying referee and report their combined-points (RS)
    rank before (raw value) vs. after (era-adjusted differential)."""
    hr("SECTION 9  League Context spot-check: raw vs. differential ranking")
    docs = {r["official_id"]: json.load(
        open(os.path.join(DATA, "referees", "%s.json" % r["official_id"]), encoding="utf-8"))
        for r in referees_index}

    key, ncol = "avg_total_points", "n"
    rows = {}
    for r in referees_index:
        off_id = r["official_id"]
        entry = docs[off_id]["whistle_profile"]["rs"]
        val, n = entry.get(key), entry.get(ncol)
        diff = (entry.get("differential") or {}).get(key)
        if val is None or n is None or n < LEADERBOARD_MIN_GAMES or diff is None:
            continue
        rows[off_id] = {"name": r["name"], "value": val, "diff": diff}

    total = len(rows)
    raw_rank = {off_id: i for i, off_id in
               enumerate(sorted(rows, key=lambda k: rows[k]["value"], reverse=True), 1)}
    diff_rank = {off_id: i for i, off_id in
                enumerate(sorted(rows, key=lambda k: rows[k]["diff"], reverse=True), 1)}

    NINETIES = {"1993-94", "1994-95", "1995-96", "1996-97", "1997-98", "1998-99", "1999-00"}
    heaviest, heaviest_n = None, -1
    for off_id in rows:
        per_season = docs[off_id]["summary"]["per_season"]
        n90s = sum(v.get("rs", 0) for s, v in per_season.items() if s in NINETIES)
        if n90s > heaviest_n:
            heaviest, heaviest_n = off_id, n90s

    print("  Stat: combined points (RS), ranked descending (rank 1 = highest value), "
         "%d qualifying referees (>=%d RS games)" % (total, LEADERBOARD_MIN_GAMES))
    if heaviest and heaviest_n > 0:
        row = rows[heaviest]
        shift = raw_rank[heaviest] - diff_rank[heaviest]
        print("  %s: %d 1990s regular-season games (the most in the qualifying pool)"
             % (row["name"], heaviest_n))
        print("    BEFORE -- ranked by raw value:                #%d of %d  (%.1f combined pts/gm)"
             % (raw_rank[heaviest], total, row["value"]))
        print("    AFTER  -- ranked by era-adjusted differential: #%d of %d  (%+.1f vs. their own-era baseline)"
             % (diff_rank[heaviest], total, row["diff"]))
        print("    shift: %+d rank places" % shift)
    else:
        print("  no qualifying 1990s-heavy referee found in the pool for this spot-check")


def qa_matchups(referees_index, team_index):
    """/matchup/ QA (docs/MATCHUP_SPEC.md): no NaN (re-verified from disk,
    same pattern as qa_league_context), every team_abbr key resolves to a
    real team page, and every referee's summed matchup games (career RS + PO
    across every team) reconcile with games_rs + games_po (PI is deliberately
    excluded from /matchup/, same as League Context)."""
    hr("SECTION 9  Matchup QA")
    known_team_slugs = {t["slug"] for t in team_index}
    failures = []
    for r in referees_index:
        path = os.path.join(DATA, "matchups", "%s.json" % r["official_id"])
        doc = json.load(open(path, encoding="utf-8"))
        assert_no_nan(doc, "matchup[%s]" % r["official_id"])
        summed = 0
        for team_abbr, t in doc["teams"].items():
            if team_abbr.lower() not in known_team_slugs:
                failures.append("matchup team_abbr resolves to no team page: %s (ref=%s)"
                                % (team_abbr, r["official_id"]))
            for kind in ("rs", "po"):
                summed += t["career"].get(kind, {}).get("games", 0)
        # Each game contributes one row to the home team's total and one to
        # the away team's -- so the correct reconciliation is against TWICE
        # games_rs+games_po, same doubling qa_gate already checks for the
        # career team_records (see "team_records games == 2 * games_total").
        expected = 2 * (r["games_rs"] + r["games_po"])
        if summed != expected:
            failures.append("matchup game-count mismatch: %s summed=%d expected(2x rs+po)=%d"
                            % (r["official_id"], summed, expected))
    print("[hard] matchup game counts (career, RS+PO, doubled for home+away) reconcile "
          "with games_rs+games_po for all %d referees: %s"
          % (len(referees_index), not any("mismatch" in f for f in failures)))
    print("[hard] every matchup team_abbr resolves to a real team page: %s"
          % (not any("resolves to no team page" in f for f in failures)))
    if failures:
        hr("MATCHUP QA: FAILED")
        for f in failures[:10]:
            print("  FAIL: %s" % f)
        raise SystemExit(1)
    print("\n[matchup QA checks all passed]")


def qa_recent_form(referees_index):
    """Recent form QA (docs/RECENT_FORM_SPEC.md): no NaN (re-verified from
    disk), every count-window's game total equals min(career RS+PO games,
    window size) -- catching an off-by-one in the rolling logic would show
    up here immediately -- and every status is one of the three legal
    values. Also prints how many referees carry at least one "outside
    normal variance" flag vs. all-"within", so a build that accidentally
    flags everyone (or nobody) is visible without opening a single file."""
    hr("SECTION 9  Recent form QA")
    by_id = {r["official_id"]: r for r in referees_index}
    failures = []
    n_any_outside = n_all_within_or_none = 0
    for r in referees_index:
        path = os.path.join(DATA, "recent_form", "%s.json" % r["official_id"])
        doc = json.load(open(path, encoding="utf-8"))
        assert_no_nan(doc, "recent_form[%s]" % r["official_id"])
        career = r["games_rs"] + r["games_po"]
        any_outside = False
        for wkey, w in doc["windows"].items():
            if w["kind"] == "count":
                expected_games = min(career, w["size"])
                if w["games"] != expected_games:
                    failures.append("recent_form games mismatch: %s/%s games=%d expected=%d"
                                    % (r["official_id"], wkey, w["games"], expected_games))
            for key, s in w["stats"].items():
                if s["status"] not in (None, "within", "outside"):
                    failures.append("recent_form illegal status: %s/%s/%s = %r"
                                    % (r["official_id"], wkey, key, s["status"]))
                if s["status"] == "outside":
                    any_outside = True
        if any_outside:
            n_any_outside += 1
        else:
            n_all_within_or_none += 1
    print("[hard] recent_form count-window game totals reconcile with min(career, window size) "
          "for all %d referees: %s" % (len(referees_index), not any("games mismatch" in f for f in failures)))
    print("[hard] every recent_form status is a legal value: %s"
          % (not any("illegal status" in f for f in failures)))
    print("  referees with >=1 window/stat flagged outside normal variance: %d" % n_any_outside)
    print("  referees with none flagged (all within, or not enough data to classify): %d"
          % n_all_within_or_none)
    if failures:
        hr("RECENT FORM QA: FAILED")
        for f in failures[:10]:
            print("  FAIL: %s" % f)
        raise SystemExit(1)
    print("\n[recent form QA checks all passed]")


def recent_form_spot_check(referees_index):
    """Recent form QA: demonstrate the variance framing actually distinguishes
    routine from notable, the same way league_context_spot_check demonstrates
    the era-adjustment round -- print one referee whose n10/avg_total_points
    window is flagged "outside" normal variance and one flagged "within", so
    the before/after isn't just asserted by the code, it's visible in the
    build log."""
    hr("SECTION 9  Recent form spot-check: outside vs. within normal variance")
    outside_ex, within_ex = None, None
    for r in referees_index:
        path = os.path.join(DATA, "recent_form", "%s.json" % r["official_id"])
        doc = json.load(open(path, encoding="utf-8"))
        s = doc["windows"].get("n10", {}).get("stats", {}).get("avg_total_points", {})
        if s.get("status") == "outside" and outside_ex is None:
            outside_ex = (r["name"], s)
        elif s.get("status") == "within" and within_ex is None:
            within_ex = (r["name"], s)
        if outside_ex and within_ex:
            break
    if outside_ex:
        name, s = outside_ex
        print("  OUTSIDE normal range -- %-20s last 10 games combined points: %.1f "
              "vs. career baseline %.1f (%+.1f, pctile %.0f)"
              % (name, s["value"], s["baseline"], s["diff"], s["pctile_rank"]))
    else:
        print("  no referee currently flagged 'outside' on n10/avg_total_points for this spot-check")
    if within_ex:
        name, s = within_ex
        print("  WITHIN  normal range -- %-20s last 10 games combined points: %.1f "
              "vs. career baseline %.1f (%+.1f, pctile %.0f) -- routine, not a trend"
              % (name, s["value"], s["baseline"], s["diff"], s["pctile_rank"]))
    else:
        print("  no referee currently flagged 'within' on n10/avg_total_points for this spot-check")


# ----------------------------------------------------------------------------
# frontpage dashboard (DASHBOARD_SPEC section 1)
# ----------------------------------------------------------------------------
# 3-5 fixed, factual curiosity entries for the history strip -- genuinely true
# facts about this project's own dataset/methodology (verified during earlier
# build rounds), not fabricated trivia.
DASHBOARD_CURIOSITIES = [
    "This database spans two different data-collection eras -- Kaggle's nbadb "
    "warehouse and ESPN's public API -- reconciled into one continuous record "
    "back to the 1993-94 season.",
    "Four historical franchises' games are folded into their modern "
    "successor's team page: the Vancouver Grizzlies into Memphis, the New "
    "Jersey Nets into Brooklyn, and both New Orleans Hornets eras into the "
    "Pelicans.",
    "The Seattle SuperSonics keep their own separate page -- per the 2008 "
    "relocation settlement, the Thunder don't claim that franchise's history.",
    "Five playoff series across two seasons (2000-01 and 1995-96) are "
    "missing games entirely from the historical record, including a Finals "
    "with only one game on file.",
    "Alternate officials count too: when a game lists more than three names, "
    "only the first three (as originally recorded) are treated as having "
    "actually worked it.",
]


def build_dashboard(referees_index, details, gm, pl, game_crew, off_ref, leaderboards,
                    seg_to_entity, crews, team_officials, debuts_farewells, era_leaders):
    hr("SECTION 9  Frontpage dashboard")

    # ---- spotlight --------------------------------------------------------
    # One entry per ref: a precomputed "signature line" is the SPOTLIGHT_STAT_
    # KEYS stat where this ref's RS percentile (already computed by
    # build_whistle_leaderboards, same n>=LEADERBOARD_MIN_GAMES gate) sits
    # farthest from the field median -- i.e. the same "how unusual" measure
    # already validated and shown on the ref page, not a second parallel
    # statistic. Refs below the gate fall back to a tenure line (seasons_active
    # + career games) instead.
    spotlight = []
    for r in referees_index:
        det = details[r["official_id"]]
        rs = det["whistle_profile"]["rs"]
        best_key, best_dist = None, -1
        if rs["n"] >= LEADERBOARD_MIN_GAMES:
            for key in SPOTLIGHT_STAT_KEYS:
                p = rs.get(key + "_pctile")
                if p is None:
                    continue
                d = abs(p - 50)
                if d > best_dist:
                    best_key, best_dist = key, d
        signature = None
        if best_key:
            signature = {"key": best_key, "value": rs[best_key],
                        "pctile": rs[best_key + "_pctile"], "n": rs["n"]}
        spotlight.append({
            "slug": r["slug"], "name": r["name"], "games_total": r["games_total"],
            "first_season": r["first_season"], "last_season": r["last_season"],
            "seasons_active": r["seasons_active"], "active": r["active"],
            "signature": signature,
        })
    spotlight.sort(key=lambda x: x["slug"])  # stable rotation order

    # ---- records ------------------------------------------------------------
    # Reuse already-computed, already-gated leaderboard #1 entries wherever
    # possible rather than recomputing the same rankings a second way.
    def rec(label, value_display, name, slug, n):
        return {"label": label, "value": value_display, "ref_name": name,
                "ref_slug": slug, "n": n}

    records = []
    raw_extremes = leaderboards["_raw_extremes"]
    if raw_extremes["highest_home_win_pct"]:
        e = raw_extremes["highest_home_win_pct"]
        records.append(rec("Highest home team win rate", pct_str(e["home_win_pct"]),
                           e["name"], e["slug"], e["n"]))
    if raw_extremes["lowest_home_win_pct"]:
        e = raw_extremes["lowest_home_win_pct"]
        records.append(rec("Lowest home team win rate", pct_str(e["home_win_pct"]),
                           e["name"], e["slug"], e["n"]))
    if raw_extremes["highest_avg_total_fta_rs"]:
        e = raw_extremes["highest_avg_total_fta_rs"]
        records.append(rec("Busiest whistle (combined FTA/game, RS)", dec_str(e["avg_total_fta"]),
                           e["name"], e["slug"], e["n"]))
    if leaderboards["most_career_games"]:
        e = leaderboards["most_career_games"][0]
        records.append(rec("Most career games", "%s games" % int_str(e["games_total"]),
                           e["name"], e["slug"], e["games_total"]))
    if leaderboards["most_playoff_games"]:
        e = leaderboards["most_playoff_games"][0]
        records.append(rec("Most playoff games", "%s games" % int_str(e["games_po"]),
                           e["name"], e["slug"], e["games_po"]))
    if leaderboards["most_career_games_active"]:
        e = leaderboards["most_career_games_active"][0]
        records.append(rec("Most games, active official", "%s games" % int_str(e["games_total"]),
                           e["name"], e["slug"], e["games_total"]))

    # most OT games (RS+PO combined) -- not already in leaderboards.json
    ot_best = None
    for r in referees_index:
        det = details[r["official_id"]]
        wp = det["whistle_profile"]
        ot = (wp["rs"].get("ot_games") or 0) + (wp["po"].get("ot_games") or 0)
        if ot_best is None or ot > ot_best[0]:
            ot_best = (ot, r)
    if ot_best and ot_best[0] > 0:
        ot, r = ot_best
        records.append(rec("Most overtime games", "%s OT games" % int_str(ot),
                           r["name"], r["slug"], r["games_total"]))

    # longest tenure span -- not already in leaderboards.json
    span_best = None
    for r in referees_index:
        span_years = int(str(r["last_season"])[:4]) + 1 - int(str(r["first_season"])[:4])
        if span_best is None or span_years > span_best[0]:
            span_best = (span_years, r)
    if span_best:
        yrs, r = span_best
        records.append(rec("Longest tenure", "%d seasons (%s–%s)" % (yrs, r["first_season"], r["last_season"]),
                           r["name"], r["slug"], yrs))

    # ---- history ------------------------------------------------------------
    # Global (not per-ref) join of player logs with game context, reused for
    # both the top-10 scoring strip and the date_index below.
    pl_g = pl.merge(
        gm[["game_id", "game_date", "home_team_abbr", "away_team_abbr", "era"]],
        on="game_id", how="inner")
    pl_g["opp_abbr"] = pl_g["away_team_abbr"].where(
        pl_g["team_abbr"] == pl_g["home_team_abbr"], pl_g["home_team_abbr"])
    pl_g["seg_id"] = pl_g["era"] + ":" + pl_g["player_id"]
    pl_g["slug"] = pl_g["seg_id"].map(lambda s: (seg_to_entity.get(s) or {}).get("slug"))
    pl_g["name"] = pl_g["seg_id"].map(lambda s: (seg_to_entity.get(s) or {}).get("display"))

    top10 = pl_g.sort_values("pts", ascending=False).head(10)
    top_scoring_games = [{
        "player_name": row["name"] or row["player_name"], "player_slug": row["slug"],
        "pts": int(row["pts"]), "team_abbr": row["team_abbr"], "opp_abbr": row["opp_abbr"],
        "game_date": row["game_date"], "game_id": row["game_id"],
        "crew": game_crew.get(row["game_id"], []),
    } for _, row in top10.iterrows()]

    # most frequent crew trio ever (crews are already alternate-trimmed, so a
    # game's distinct ref_keys ARE the trio when a game has exactly 3).
    name_of = {r["official_id"]: r["name"] for r in referees_index}
    slug_of = {r["official_id"]: r["slug"] for r in referees_index}
    trio_counter = defaultdict(int)
    for _gid, grp in off_ref.groupby("game_id"):
        crew = tuple(sorted(set(grp["ref_key"])))
        if len(crew) == 3:
            trio_counter[crew] += 1
    top_trio = None
    if trio_counter:
        best_crew, best_n = max(trio_counter.items(), key=lambda kv: kv[1])
        top_trio = {
            "refs": [{"name": name_of[k], "slug": slug_of[k]} for k in best_crew if k in name_of],
            "games": best_n,
        }

    history = {
        "top_scoring_games": top_scoring_games,
        "top_crew_trio": top_trio,
        "curiosities": DASHBOARD_CURIOSITIES,
    }

    # ---- latest_game_day --------------------------------------------------
    # The most recent calendar date in the WHOLE dataset with any game on it
    # (RS, PO, or PI -- this is a "who actually worked most recently" snapshot,
    # not a stat, so no season-type filtering) plus every game that date and
    # its officiating crew. Used by the index page's Tonight's Officials
    # module as a non-empty fallback when no live "tonight" assignments exist
    # yet (the pipeline that would produce those doesn't exist yet, and only
    # ever covers in-season days once it does) -- keeps that module, the
    # page's promoted anchor, from ever being a hole.
    gm_dated = gm.dropna(subset=["game_date"])
    latest_date = gm_dated["game_date"].max()
    latest_rows = gm_dated[gm_dated["game_date"] == latest_date].sort_values("game_id")
    latest_game_day = {
        "date": latest_date,
        "games": [{
            "game_id": row["game_id"],
            "home_team_abbr": row["home_team_abbr"], "away_team_abbr": row["away_team_abbr"],
            "home_pts": clean_num(row["home_pts"]), "away_pts": clean_num(row["away_pts"]),
            "crew": game_crew.get(row["game_id"], []),
        } for _, row in latest_rows.iterrows()],
    }

    # ---- date_index -----------------------------------------------------
    # Highest-scoring individual performance on each calendar date (MM-DD)
    # across all seasons. Every one of the 366 possible calendar dates gets an
    # entry: dates with no games in the dataset fall back to the nearest PRIOR
    # date that has one (wrapping from Jan 1 back to Dec 31), precomputed here
    # so the client JS only ever does a dict lookup.
    pl_g["month_day"] = pl_g["game_date"].str.slice(5, 10)
    best_idx = pl_g.groupby("month_day")["pts"].idxmax()
    best_by_md = {}
    for md, idx_ in best_idx.items():
        row = pl_g.loc[idx_]
        best_by_md[md] = {
            "month_day": md, "date": row["game_date"],
            "player_name": row["name"] or row["player_name"], "player_slug": row["slug"],
            "pts": int(row["pts"]), "team_abbr": row["team_abbr"], "opp_abbr": row["opp_abbr"],
            "game_id": row["game_id"], "crew": game_crew.get(row["game_id"], []),
        }

    all_mds = [d.strftime("%m-%d") for d in pd.date_range("2000-01-01", "2000-12-31")]
    first_with_data = next((md for md in all_mds if md in best_by_md), None)
    date_index = {}
    if first_with_data:
        start = all_mds.index(first_with_data)
        rotated = all_mds[start:] + all_mds[:start]
        carry = None
        for md in rotated:
            if md in best_by_md:
                carry = best_by_md[md]
            date_index[md] = carry
    print("date_index: %d/%d calendar dates have a real game; rest fall back to the "
          "nearest prior date" % (len(best_by_md), len(all_mds)))

    dashboard = {
        "spotlight": spotlight,
        "records": records,
        "history": history,
        "date_index": date_index,
        "latest_game_day": latest_game_day,
        # Tier C (docs/TIER_C_SPEC.md)
        "crews": crews,
        "team_officials": team_officials,
        "debuts_farewells": debuts_farewells,
        "era_leaders": era_leaders,
    }
    assert_no_nan(dashboard, "dashboard")
    with open(os.path.join(DATA, "dashboard.json"), "w", encoding="utf-8") as fh:
        json.dump(dashboard, fh, ensure_ascii=False, indent=2)
    print("wrote data/dashboard.json (%d spotlight, %d records, %d top-scoring, "
          "trio=%s, %d curiosities, %d crews, %d team_officials, %d debuts_farewells "
          "seasons, %d era_leaders decades, latest_game_day=%s with %d games)"
          % (len(spotlight), len(records), len(top_scoring_games),
             "yes" if top_trio else "no", len(DASHBOARD_CURIOSITIES),
             len(crews), len(team_officials), len(debuts_farewells), len(era_leaders),
             latest_game_day["date"], len(latest_game_day["games"])))
    return dashboard


def qa_dashboard(dashboard, referees_index, team_index):
    """DASHBOARD_SPEC section 5: no NaN (already asserted in build_dashboard),
    every slug resolves to a real referee, every records/history entry carries
    n or count. Also covers the Tier C structures (docs/TIER_C_SPEC.md section
    4): every slug in crews/team_officials/debuts_farewells/era_leaders must
    resolve to a real referee (or, for team_officials, a real team) page."""
    hr("SECTION 9  Dashboard QA")
    failures = []
    known_slugs = {r["slug"] for r in referees_index}
    known_team_slugs = {t["slug"] for t in team_index}

    bad_spot = [s["slug"] for s in dashboard["spotlight"] if s["slug"] not in known_slugs]
    if bad_spot:
        failures.append("spotlight slugs not in referees_index: %s" % bad_spot[:5])

    bad_rec = [r["label"] for r in dashboard["records"]
              if r["ref_slug"] not in known_slugs or not r.get("n")]
    if bad_rec:
        failures.append("records missing a valid slug or n: %s" % bad_rec[:5])

    for g in dashboard["history"]["top_scoring_games"]:
        if not g.get("pts"):
            failures.append("top_scoring_games entry missing pts: %s" % g.get("game_id"))
        for c in g.get("crew") or []:
            if c["slug"] not in known_slugs:
                failures.append("top_scoring_games crew slug not in referees_index: %s" % c["slug"])

    trio = dashboard["history"]["top_crew_trio"]
    if trio:
        for rf in trio["refs"]:
            if rf["slug"] not in known_slugs:
                failures.append("top_crew_trio slug not in referees_index: %s" % rf["slug"])
        if not trio.get("games"):
            failures.append("top_crew_trio missing a games count")

    print("[hard] spotlight entries: %d, all slugs resolve: %s"
          % (len(dashboard["spotlight"]), not bad_spot))
    print("[hard] records entries: %d, all carry ref_slug + n: %s"
          % (len(dashboard["records"]), not bad_rec))
    print("[hard] top_scoring_games: %d, top_crew_trio present: %s"
          % (len(dashboard["history"]["top_scoring_games"]), bool(trio)))
    print("[hard] date_index: %d calendar-date entries" % len(dashboard["date_index"]))

    ld = dashboard["latest_game_day"]
    bad_ld_crew = [c["slug"] for g in ld["games"] for c in g.get("crew") or []
                   if c["slug"] not in known_slugs]
    if not ld.get("date") or not ld.get("games"):
        failures.append("latest_game_day missing a date or has zero games")
    if bad_ld_crew:
        failures.append("latest_game_day crew slugs not in referees_index: %s" % bad_ld_crew[:5])
    print("[hard] latest_game_day: %s, %d games, all crew slugs resolve: %s"
          % (ld.get("date"), len(ld.get("games") or []), not bad_ld_crew))

    # ---- Tier C ------------------------------------------------------------
    bad_crew = []
    for c in dashboard["crews"]:
        for rf in c["refs"]:
            if rf["slug"] not in known_slugs:
                bad_crew.append(rf["slug"])
        if not c.get("games"):
            failures.append("crews entry missing a games count: %s" % c["refs"])
    if bad_crew:
        failures.append("crews slugs not in referees_index: %s" % bad_crew[:5])
    print("[hard] crews: %d trios, all ref slugs resolve: %s" % (len(dashboard["crews"]), not bad_crew))

    bad_to_ref = [t["ref_slug"] for t in dashboard["team_officials"] if t["ref_slug"] not in known_slugs]
    bad_to_team = [t["team_slug"] for t in dashboard["team_officials"] if t["team_slug"] not in known_team_slugs]
    if bad_to_ref:
        failures.append("team_officials ref slugs not in referees_index: %s" % bad_to_ref[:5])
    if bad_to_team:
        failures.append("team_officials team slugs not in teams.json: %s" % bad_to_team[:5])
    print("[hard] team_officials: %d teams, all ref+team slugs resolve: %s"
          % (len(dashboard["team_officials"]), not (bad_to_ref or bad_to_team)))

    bad_df = []
    for s in dashboard["debuts_farewells"]:
        for r in s["debuts"] + s["farewells"]:
            if r["slug"] not in known_slugs:
                bad_df.append(r["slug"])
    current_farewells = next((s["farewells"] for s in dashboard["debuts_farewells"]
                              if s["season"] == CURRENT_SEASON), [])
    if bad_df:
        failures.append("debuts_farewells slugs not in referees_index: %s" % bad_df[:5])
    if current_farewells:
        failures.append("farewells present for the current season %s (should always be "
                        "empty): %s" % (CURRENT_SEASON, current_farewells))
    print("[hard] debuts_farewells: %d seasons, all slugs resolve: %s, %s farewells for "
          "current season: %s" % (len(dashboard["debuts_farewells"]), not bad_df,
                                  CURRENT_SEASON, len(current_farewells)))

    bad_era = []
    for label, era in dashboard["era_leaders"].items():
        for cat in ("total_games", "playoff_games", "finals_games"):
            for row in era[cat]:
                if row["slug"] not in known_slugs:
                    bad_era.append((label, cat, row["slug"]))
    if bad_era:
        failures.append("era_leaders slugs not in referees_index: %s" % bad_era[:5])
    print("[hard] era_leaders: %d decades, all slugs resolve: %s"
          % (len(dashboard["era_leaders"]), not bad_era))

    if failures:
        hr("DASHBOARD QA: FAILED")
        for f in failures[:10]:
            print("  FAIL: %s" % f)
        raise SystemExit(1)
    print("\n[dashboard QA checks all passed]")


# ----------------------------------------------------------------------------
# QA gate (BUILD_SPEC section 7 + PHASE1 section 4.3)
# ----------------------------------------------------------------------------
def qa_gate(off_raw, gm, off_trimmed, referees_index, details):
    hr("SECTION 7  QA gate")
    failures = []

    # (hard) every officials game_id joins to exactly one game
    game_ids = set(gm["game_id"])
    orphan = set(off_raw["game_id"]) - game_ids
    print("[hard] officials game_ids with no matching game: %d" % len(orphan))
    if orphan:
        failures.append("orphan officials game_ids: %s" % list(orphan)[:5])
    counts = gm["game_id"].value_counts()
    if (counts > 1).any():
        failures.append("duplicate game_id rows in games table")

    # (hard) each season is a single id scheme -- regression guard against
    # re-introducing a cross-scheme duplicate (e.g. the 2012-13 fragment)
    mixed = gm.groupby("season")["era"].nunique()
    bad_seasons = mixed[mixed > 1].index.tolist()
    print("[hard] seasons spanning >1 id scheme (must be 0): %d %s"
          % (len(bad_seasons), bad_seasons))
    if bad_seasons:
        failures.append("mixed-scheme seasons: %s" % bad_seasons)

    # (hard) every team_abbr resolves through the tricode allow-list
    abbrs = set(gm["home_team_abbr"]) | set(gm["away_team_abbr"])
    bad_abbr = {a for a in abbrs if a not in ALLOWED_TRICODES}
    print("[hard] team_abbr values outside the tricode allow-list: %d %s"
          % (len(bad_abbr), sorted(bad_abbr)))
    if bad_abbr:
        failures.append("unrecognized tricodes: %s" % sorted(bad_abbr))

    # (hard) sum of a ref's team_records games == 2 * games_total
    mism = []
    for r in referees_index:
        det = details[r["official_id"]]
        s = sum(t["games"] for t in det["team_records"])
        if s != 2 * r["games_total"]:
            mism.append((r["official_id"], s, 2 * r["games_total"]))
    print("[hard] referees whose team_records games != 2x games_total: %d" % len(mism))
    if mism:
        failures.append("team_records/games_total mismatch: %s" % mism[:5])

    # (soft) 3-official coverage per season (nbadb era is ~87-92% by design)
    per_game = off_trimmed.groupby("game_id")["official_id"].size()
    g3 = gm.copy()
    g3["n_off"] = g3["game_id"].map(per_game).fillna(0)
    cov = g3.groupby(["season", "era"]).apply(
        lambda d: (d["n_off"] >= 3).mean(), include_groups=False)
    print("[soft] 3-official coverage per season (nbadb ~87-92%% expected, ESPN higher):")
    low = []
    for (season, era), pct in cov.items():
        flag = "  <-- below 95%" if pct < 0.95 else ""
        if pct < 0.95:
            low.append((season, era, round(pct * 100, 1)))
        print("        %-8s %-4s  %.1f%%%s" % (season, era, pct * 100, flag))
    print("[soft] %d season/era splits below 95%% 3-official coverage "
          "(expected for nbadb era; not a build failure)" % len(low))

    if failures:
        hr("QA GATE: FAILED")
        for f in failures:
            print("  FAIL: %s" % f)
        raise SystemExit(1)
    print("\n[hard QA checks all passed]")


def spot_checks(referees_index, details):
    hr("SECTION 7  Spot-checks (eyeball vs NBAstuffer before publishing)")
    by_name = {r["name"]: r for r in referees_index}

    def show(name):
        # tolerant name lookup
        hit = by_name.get(name)
        if hit is None:
            cands = [r for r in referees_index if name.lower() in r["name"].lower()]
            hit = cands[0] if cands else None
        if hit is None:
            print("  %-20s NOT FOUND" % name)
            return
        det = details[hit["official_id"]]["summary"]
        print("  %-20s total=%-5d RS=%-5d PO=%-4d finals=%-3d g7=%-3d  seasons %s-%s  eras=%s active=%s"
              % (hit["name"], det["games_total"], det["games_rs"], det["games_po"],
                 det["finals_games"], det["game7s"], det["first_season"],
                 det["last_season"], "+".join(det["eras"]), det["active"]))

    print("Veteran refs (compare career games to NBAstuffer public tables):")
    for n in ["Scott Foster", "Tony Brothers", "James Capers"]:
        show(n)
    print("\nCross-era ref (raw data spans ESPN 2000-03 AND nbadb post-2003):")
    for n in ["Bennett Salvatore", "Dan Crawford", "Joe Crawford"]:
        show(n)


def dashboard_spot_checks(referees_index, details, dashboard):
    """DASHBOARD_SPEC section 5: hand-checkable quality-score numbers for 3
    known playoff-heavy refs, plus a spotlight-rotation sanity line. Printed
    for human review, same pattern as spot_checks() above -- no external
    ground truth to assert against automatically."""
    hr("SECTION 9  Dashboard spot-checks")
    by_name = {r["name"]: r for r in referees_index}

    print("Quality score (RS=1/R1=2/R2=4/R3=8/Finals=16 pt per game worked) "
          "vs. already-verified Finals/Game-7 counts:")
    for n in ["Scott Foster", "Tony Brothers", "James Capers"]:
        r = by_name.get(n)
        if r is None:
            print("  %-20s NOT FOUND" % n)
            continue
        print("  %-20s quality_total=%-5d quality_per_season=%-6s seasons_active=%-3d "
              "(cross-check: finals=%-3d g7=%-3d games_po=%-4d)"
              % (n, r["quality_total"], r["quality_per_season"], r["seasons_active"],
                 r["finals_games"], r["game7s"], r["games_po"]))

    spotlight = dashboard["spotlight"]
    if spotlight:
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        pick_today = spotlight[today.timetuple().tm_yday % len(spotlight)]
        pick_tomorrow = spotlight[tomorrow.timetuple().tm_yday % len(spotlight)]
        print("\nSpotlight rotation sanity check (day-of-year %% %d spotlight entries):"
              % len(spotlight))
        print("  today    (%s): %s" % (today.isoformat(), pick_today["name"]))
        print("  tomorrow (%s): %s" % (tomorrow.isoformat(), pick_tomorrow["name"]))


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    hr("SECTION 2  Load & era-tag")
    gm = load_games()
    valid_ids = set(gm["game_id"])
    off_raw = load_officials(valid_ids)
    pl = load_player_logs(valid_ids)

    off_trimmed = exclude_alternates(off_raw)
    off_ref, display, raw_ids, eras = reconcile_referees(off_trimmed)
    gm = label_rounds(gm)

    tg, game_tot = build_team_game(pl)

    # League Context (docs/LEAGUE_CONTEXT_SPEC.md) -- computed before
    # aggregate() so every referee's era-adjusted expected baseline can be
    # built against it.
    league_baselines = build_league_baselines(gm, game_tot)

    seg_to_entity, entities = reconcile_players(pl, gm, load_player_overrides())
    referees_index, all_swings, all_team_records = aggregate(
        off_ref, gm, pl, tg, game_tot, display, raw_ids, eras, seg_to_entity, league_baselines)

    build_crewmates(off_ref, referees_index)
    build_whistle_leaderboards(referees_index)
    build_recent_form(referees_index, off_ref, gm, tg, game_tot)
    ref_lookup = {r["official_id"]: (r["name"], r["slug"]) for r in referees_index}
    game_crew = build_game_crew(off_ref, ref_lookup)
    team_index = build_team_pages(all_team_records, gm)
    player_index = build_player_pages(all_swings, entities, seg_to_entity, pl, gm, game_crew)

    leaderboards, details = build_leaderboards(referees_index)

    # Tier C (docs/TIER_C_SPEC.md)
    crews = build_crews(off_ref, gm, game_tot, ref_lookup)
    team_officials = build_team_officials(team_index)
    debuts_farewells = build_debuts_farewells(referees_index)
    era_leaders = build_era_leaders(referees_index, details)
    build_swings_all(all_swings)

    dashboard = build_dashboard(referees_index, details, gm, pl, game_crew, off_ref,
                                leaderboards, seg_to_entity, crews, team_officials,
                                debuts_farewells, era_leaders)
    qa_gate(off_raw, gm, off_trimmed, referees_index, details)
    qa_teams_players(referees_index, team_index, player_index)
    qa_dashboard(dashboard, referees_index, team_index)
    qa_game_logs(referees_index)
    qa_league_context(referees_index)
    league_context_spot_check(referees_index)
    qa_matchups(referees_index, team_index)
    qa_recent_form(referees_index)
    recent_form_spot_check(referees_index)
    spot_checks(referees_index, details)
    dashboard_spot_checks(referees_index, details, dashboard)

    hr("BUILD COMPLETE")
    print("referees: %d | teams: %d | players: %d"
          % (len(referees_index), len(team_index), len(player_index)))


if __name__ == "__main__":
    main()
