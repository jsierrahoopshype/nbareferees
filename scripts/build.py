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
import unicodedata
from collections import defaultdict

import pandas as pd

# Shared tricode normalization lives with the local scripts.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "local"))
import nba_tricodes  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "source-data")
DATA = os.path.join(REPO, "data")
OVERRIDE_CSV = os.path.join(DATA, "referee_identity_overrides.csv")

SEASON_FLOOR_YEAR = 2000          # 2000-01 is the first in-scope season
CURRENT_SEASON = "2025-26"        # "active" == worked this season
OT_MIN_THRESHOLD = 505            # total player-minutes/game; clean gap in data at 505
SWING_MIN_GAMES = 15              # min games under a ref to report a player swing
SWING_TOP_N = 50
PO_BASELINE_MIN = 5               # min playoff games in a season to trust a PO baseline
TOP_PERF_N = 25
LEADERBOARD_MIN_GAMES = 200
TEAM_REF_MIN_GAMES = 10           # min games of a team under a ref to list on team pages
PLAYER_TOP_GAMES = 10             # best scoring games shown on a player page

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
    print("games: %d rows (dropped %d pre-%d-01 rows)"
          % (len(gm), before - len(gm), SEASON_FLOOR_YEAR))
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
    espn_seasons = sorted(espn_po["season"].unique())
    print("ESPN-scheme playoff games: %d (round/Game-7 NOT derivable from id -- "
          "intentionally skipped)" % len(espn_po))
    print("KNOWN PHASE-1 GAP: Finals/Game-7 counts incomplete for ESPN seasons: %s"
          % espn_seasons)
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


def aggregate(off, gm, pl, tg, game_tot, display, raw_ids, eras, seg_to_entity):
    hr("SECTION 6  Per-referee aggregation")

    games_meta = gm.copy()
    games_meta["kind"] = games_meta.apply(season_type_label, axis=1)
    games_meta["abs_margin"] = (games_meta["home_pts"] - games_meta["away_pts"]).abs()
    games_meta["total_pts"] = games_meta["home_pts"] + games_meta["away_pts"]
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
    used_slugs = {}
    # Clean the output dir first so referees dropped by an override merge don't
    # linger as stale/orphaned files from a previous build.
    ref_dir = os.path.join(DATA, "referees")
    os.makedirs(ref_dir, exist_ok=True)
    for old in glob.glob(os.path.join(ref_dir, "*.json")):
        os.remove(old)

    for ref_key in sorted(ref_games):
        gids = ref_games[ref_key]
        gsub = gmeta.reindex(gids)
        gsub = gsub[gsub["season"].notna()]
        if gsub.empty:
            continue

        slug = slugify_unique(display[ref_key], ref_key, used_slugs)
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

        # ---- team records ----------------------------------------------------
        team_rows = defaultdict(lambda: {"games": 0, "wins": 0, "losses": 0,
                                         "home_games": 0, "home_wins": 0, "margin_sum": 0.0})
        for gid, g in gsub.iterrows():
            for side in ("home", "away"):
                team = g["%s_team_abbr" % side]
                if not isinstance(team, str) or not team:
                    continue
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

        # ---- whistle profile (RS and PO separately) -------------------------
        whistle = {}
        for kind in ("RS", "PO"):
            ksub = gsub[gsub["kind"] == kind]
            n = len(ksub)
            entry = {"n": n, "n_boxscore": 0, "avg_total_points": None,
                     "avg_total_fta": None, "avg_total_pf": None, "home_win_pct": None,
                     "avg_abs_margin": None, "ot_games": None, "ot_rate": None}
            if n:
                entry["avg_total_points"] = clean_num(ksub["total_pts"].mean())
                entry["home_win_pct"] = clean_num((ksub["home_win"] == 1).mean())
                entry["avg_abs_margin"] = clean_num(ksub["abs_margin"].mean())
                box = got.reindex(ksub.index)
                box = box[box["n_teams"] == 2]
                nb = len(box)
                entry["n_boxscore"] = nb
                if nb:
                    entry["avg_total_fta"] = clean_num(box["box_fta"].mean())
                    entry["avg_total_pf"] = clean_num(box["box_pf"].mean())
                    entry["ot_games"] = int(box["is_ot"].sum())
                    entry["ot_rate"] = clean_num(box["is_ot"].mean())
            whistle[kind.lower()] = entry

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
                "game_id": gid, "date": g["game_date"],
                "matchup": "%s@%s" % (g["away_team_abbr"], g["home_team_abbr"]),
                "result": "%s %d, %s %d" % (
                    g["home_team_abbr"], int(g["home_pts"]) if pd.notna(g["home_pts"]) else 0,
                    g["away_team_abbr"], int(g["away_pts"]) if pd.notna(g["away_pts"]) else 0),
                "round": "Finals" if rnd == 4 else ("Round %d" % rnd if rnd else None),
                "game_num": gnum,
            })
        notable.sort(key=lambda r: r["date"], reverse=True)

        summary = {
            "official_id": ref_key, "name": display[ref_key], "slug": slug,
            "raw_ids": sorted(raw_ids[ref_key]), "eras": sorted(eras[ref_key]),
            "first_season": seasons[0], "last_season": seasons[-1],
            "games_total": n_total, "games_rs": n_rs, "games_po": n_po, "games_pi": n_pi,
            "finals_games": finals_games, "game7s": game7s, "active": active,
            "per_season": per_season,
        }

        ref_doc = {
            "summary": summary,
            "team_records": team_records,
            "whistle_profile": whistle,
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


def _clean_dir(path):
    os.makedirs(path, exist_ok=True)
    for old in glob.glob(os.path.join(path, "*.json")):
        os.remove(old)


def build_team_pages(all_team_records, gm):
    hr("SECTION 8  Team pages")
    team_dir = os.path.join(DATA, "teams")
    _clean_dir(team_dir)

    by_team = defaultdict(list)
    for r in all_team_records:
        by_team[r["team_abbr"]].append(r)

    # per-team seasons + dataset game totals from the games table
    seasons_for, total_for = {}, {}
    long = pd.concat([
        gm[["game_id", "season"]].assign(t=gm["home_team_abbr"]),
        gm[["game_id", "season"]].assign(t=gm["away_team_abbr"]),
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

    # home win% and avg total FTA need min-n gates and come from whistle profiles
    hw = []
    fta = []
    for r in idx:
        det = details[r["official_id"]]
        wp = det["whistle_profile"]
        n_all = wp["rs"]["n"] + wp["po"]["n"]
        if n_all >= LEADERBOARD_MIN_GAMES:
            # home win% over RS+PO combined
            hw_num = 0.0
            for k in ("rs", "po"):
                e = wp[k]
                if e["home_win_pct"] is not None:
                    hw_num += e["home_win_pct"] * e["n"]
            hw.append(entry(r, {"home_win_pct": clean_num(hw_num / n_all), "n": n_all}))
        rs = wp["rs"]
        if rs["n_boxscore"] >= LEADERBOARD_MIN_GAMES and rs["avg_total_fta"] is not None:
            fta.append(entry(r, {"avg_total_fta": rs["avg_total_fta"], "n": rs["n_boxscore"]}))

    leaderboards = {
        "most_career_games": most_games,
        "most_career_games_active": most_games_active,
        "most_playoff_games": most_po,
        "most_finals_games": most_finals,
        "most_game7s": most_g7,
        "most_games_current_season": most_current,
        "highest_home_win_pct": sorted(hw, key=lambda r: -r["home_win_pct"])[:25],
        "lowest_home_win_pct": sorted(hw, key=lambda r: r["home_win_pct"])[:25],
        "highest_avg_total_fta_rs": sorted(fta, key=lambda r: -r["avg_total_fta"])[:25],
        "lowest_avg_total_fta_rs": sorted(fta, key=lambda r: r["avg_total_fta"])[:25],
        "_meta": {"min_games_for_rate_leaderboards": LEADERBOARD_MIN_GAMES,
                  "current_season": CURRENT_SEASON},
    }
    assert_no_nan(leaderboards, "leaderboards")
    with open(os.path.join(DATA, "leaderboards.json"), "w", encoding="utf-8") as fh:
        json.dump(leaderboards, fh, ensure_ascii=False, indent=2)
    print("wrote data/leaderboards.json")
    return leaderboards, details


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

    seg_to_entity, entities = reconcile_players(pl, gm, load_player_overrides())
    referees_index, all_swings, all_team_records = aggregate(
        off_ref, gm, pl, tg, game_tot, display, raw_ids, eras, seg_to_entity)

    build_crewmates(off_ref, referees_index)
    ref_lookup = {r["official_id"]: (r["name"], r["slug"]) for r in referees_index}
    game_crew = build_game_crew(off_ref, ref_lookup)
    team_index = build_team_pages(all_team_records, gm)
    player_index = build_player_pages(all_swings, entities, seg_to_entity, pl, gm, game_crew)

    leaderboards, details = build_leaderboards(referees_index)
    qa_gate(off_raw, gm, off_trimmed, referees_index, details)
    qa_teams_players(referees_index, team_index, player_index)
    spot_checks(referees_index, details)

    hr("BUILD COMPLETE")
    print("referees: %d | teams: %d | players: %d"
          % (len(referees_index), len(team_index), len(player_index)))


if __name__ == "__main__":
    main()
