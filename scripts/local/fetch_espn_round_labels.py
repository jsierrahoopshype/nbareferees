"""
fetch_espn_round_labels.py  --  LOCAL script (run by Jorge on Windows).

Produces playoff ROUND and GAME-NUMBER labels for every ESPN-scheme playoff
game in source-data/games.csv.gz, closing the BUILD_SPEC section 5 gap where
ESPN ids (unlike the 10-digit '00...' NBA ids) do not encode round/game in the
id itself.

Two label sources, combined:

  (1) STRUCTURAL DERIVATION  -- pure pandas, NO network, runs anywhere.
      Within each season's Playoffs games, a "series" is the unordered team
      pair. game_num = the game's chronological rank inside its series. round =
      each team's series-sequence order that postseason (a team's 1st series is
      round 1, its 2nd is round 2, ...). Both teams in a series share a round.
      A standard bracket is 15 series = 8 (R1) + 4 (R2) + 2 (R3) + 1 (R4/Finals).

  (2) ESPN gameNote  -- authoritative, needs the ESPN API (blocked from the
      build cloud; that's why this lives in scripts/local/). For the seasons
      ESPN carries a usable note (2012-13 and 2023-24 onward), each game's
      summary is fetched and header.gameNote (fallback: the scoreboard
      competitions[0].notes[0].headline) is parsed to round + game number,
      cross-validated against the derivation, and -- on any mismatch -- WINS,
      with the disagreement printed.

Output (the only extract this writes):
    source-data/round_labels.csv.gz   columns: game_id,round,game_num,source
      source = "gamenote"  (label came from an ESPN note this run/earlier)
             = "derived"   (structural only -- no note fetched for that season)

Resume-safe: fetched notes are cached in
    source-data/_round_labels_gamenote_cache.json
so re-running only fetches game_ids not already cached. Delete that file to
force a clean re-fetch.

Reuses the ESPN plumbing from fetch_espn_seasons.py (get_json, SUMMARY_URL,
SCOREBOARD_URL, is_nba_game_id) -- importing it is side-effect-safe.

  python scripts\\local\\fetch_espn_round_labels.py               # full run
  python scripts\\local\\fetch_espn_round_labels.py --derive-only # structural only, no network
"""

import os
import re
import sys
import json
import time
import argparse

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
OUT_PATH = os.path.join(SOURCE_DIR, "round_labels.csv.gz")
CACHE_PATH = os.path.join(SOURCE_DIR, "_round_labels_gamenote_cache.json")

# Reuse the ESPN plumbing (importing is side-effect-safe -- work is behind __main__).
sys.path.insert(0, SCRIPT_DIR)
from fetch_espn_seasons import (  # noqa: E402
    get_json,
    SUMMARY_URL,
    is_nba_game_id,
)

DELAY_SECONDS = 1.0
OUT_COLUMNS = ["game_id", "round", "game_num", "source"]

# Seasons where ESPN carries a usable playoff gameNote worth fetching. The early
# 2000s (2000-01..2002-03) predate the note, so those stay structural-only.
GAMENOTE_SEASONS = {"2012-13", "2023-24", "2024-25", "2025-26"}


# --------------------------------------------------------------------------- #
# (1) structural derivation  --  pure pandas, no network
# --------------------------------------------------------------------------- #
def is_espn_scheme(game_id):
    """NBA scheme is 10 digits starting '00'; everything else is ESPN."""
    g = str(game_id)
    return not (len(g) == 10 and g.startswith("00"))


def series_format(rnd, season_start_year):
    """(clinch_wins, max_games, label) for a series in `rnd` that postseason.
    First rounds were best-of-5 (3 wins to advance, <=5 games) only in the
    2000-01 and 2001-02 postseasons; the NBA moved the first round to best-of-7
    starting with the 2002-03 playoffs. Every other series is best-of-7."""
    if rnd == 1 and season_start_year <= 2001:
        return 3, 5, "best-of-5"
    return 4, 7, "best-of-7"


def game_winner(row):
    """Winning tricode for a game row (home_win '1' -> home won, else away)."""
    return row["home_team_abbr"] if str(row["home_win"]) == "1" else row["away_team_abbr"]


def derive_structural(games):
    """Return a DataFrame [game_id, season, round, game_num] for every
    ESPN-scheme playoff game, derived purely from series structure. Also prints
    a per-season validation report + a completeness audit (below), and returns
    (df, ok) where ok is False if any season fails a check.

    Completeness audit: for every derived series we count each team's wins from
    the games present and compare against the format's clinch number (4 for
    best-of-7, 3 for best-of-5 pre-2003 first rounds). A series where NEITHER
    team reached the clinch number is INCOMPLETE -- the signature of missing
    games, not a real short series -- and is flagged. (A missing game also
    silently corrupts game_num for that series, since the chronological rank of
    the games we do have no longer matches the true game numbers.) Series with
    more games than the format allows are flagged OVER-LONG."""
    po = games[games["season_type"] == "Playoffs"].copy()
    po = po[po["game_id"].map(is_espn_scheme)].copy()
    po["_date"] = pd.to_datetime(po["game_date"])

    rows = []
    all_ok = True
    flagged_total = 0
    print("Structural derivation + completeness audit (per season):")
    for season, sd in po.groupby("season"):
        start_year = int(str(season)[:4])
        sd = sd.sort_values("_date")
        sd["_series"] = sd.apply(
            lambda r: frozenset({r["home_team_abbr"], r["away_team_abbr"]}), axis=1)

        # game_num = chronological rank within the series
        sd["_gnum"] = sd.groupby("_series")["_date"].rank(method="first").astype(int)

        # round = per-team series-sequence order; both teams should agree.
        ser_first = sd.groupby("_series")["_date"].min()
        team_series = {}
        for ser, first in ser_first.items():
            for t in ser:
                team_series.setdefault(t, []).append((first, ser))
        series_rounds = {}  # series -> set of round numbers claimed by its two teams
        for t, lst in team_series.items():
            for rnd_idx, (_first, ser) in enumerate(sorted(lst), 1):
                series_rounds.setdefault(ser, set()).add(rnd_idx)

        disagree = {ser: rs for ser, rs in series_rounds.items() if len(rs) != 1}
        series_round = {ser: min(rs) for ser, rs in series_rounds.items()}
        sd["_round"] = sd["_series"].map(series_round)
        sd["_winner"] = sd.apply(game_winner, axis=1)

        # ---- bracket validation ------------------------------------------- #
        n_series = sd["_series"].nunique()
        r1 = sum(1 for r in series_round.values() if r == 1)
        r4 = sum(1 for r in series_round.values() if r == 4)

        # ---- completeness audit (win-based) ------------------------------- #
        flags = []  # (round, teams, n_games, "w1-w2", clinch, kind)
        over_long = []
        for ser, ssd in sd.groupby("_series", sort=False):
            rnd = series_round[ser]
            clinch, cap, label = series_format(rnd, start_year)
            wins = ssd["_winner"].value_counts().to_dict()
            wvals = sorted(wins.values(), reverse=True)
            top = wvals[0] if wvals else 0
            n_games = len(ssd)
            teams = "/".join(sorted(ser))
            win_str = "-".join(str(wins.get(t, 0)) for t in sorted(ser))
            if top < clinch:
                flags.append((rnd, teams, n_games, win_str, clinch, label, "INCOMPLETE"))
            if n_games > cap:
                over_long.append((rnd, teams, n_games, cap, label))

        ok = (r4 == 1) and (r1 == 8) and (len(disagree) == 0) \
            and not flags and not over_long
        all_ok = all_ok and ok
        flagged_total += len(flags) + len(over_long)
        print("  %s: %d games  series=%d  R1=%d  R4=%d  round-disagreements=%d  "
              "flagged=%d  ->  %s"
              % (season, len(sd), n_series, r1, r4, len(disagree),
                 len(flags) + len(over_long), "OK" if ok else "FLAGS"))
        for ser, rs in disagree.items():
            print("     ROUND DISAGREEMENT %s teams claim rounds %s" % (set(ser), sorted(rs)))
        for rnd, teams, n_games, win_str, clinch, label, kind in sorted(flags):
            print("     %s  R%d %s (%s): %d games present, series score %s, "
                  "no team reached %d wins -> games MISSING"
                  % (kind, rnd, teams, label, n_games, win_str, clinch))
        for rnd, teams, n_games, cap, label in sorted(over_long):
            print("     OVER-LONG  R%d %s (%s): %d games present (> %d)"
                  % (rnd, teams, label, n_games, cap))

        for _, r in sd.iterrows():
            rows.append({"game_id": r["game_id"], "season": season,
                         "round": int(r["_round"]), "game_num": int(r["_gnum"])})

    df = pd.DataFrame(rows, columns=["game_id", "season", "round", "game_num"])
    print("  derived %d ESPN-scheme playoff labels; %d flagged series across all "
          "seasons; overall %s"
          % (len(df), flagged_total, "PASSED" if all_ok else "has FLAGS (see above)"))
    return df, all_ok


# --------------------------------------------------------------------------- #
# (2) ESPN gameNote fetch + parse
# --------------------------------------------------------------------------- #
def parse_gamenote(text):
    """Parse an ESPN playoff note like 'Western Conference First Round - Game 3'
    or 'NBA Finals - Game 7' into (round, game_num). Returns (None, None) on no
    match. Order matters: the specific round words are tested before the generic
    'final' so 'NBA Finals' resolves to round 4 only after the others are ruled out."""
    if not text:
        return None, None
    t = str(text).lower()
    m = re.search(r"game\s+(\d+)", t)
    game_num = int(m.group(1)) if m else None
    if "first round" in t or "1st round" in t:
        rnd = 1
    elif "semifinal" in t:
        rnd = 2
    elif "conference final" in t:
        rnd = 3
    elif "final" in t:  # 'NBA Finals'
        rnd = 4
    else:
        rnd = None
    return rnd, game_num


def extract_note_text(summary):
    """Pull the raw note string from a summary payload: header.gameNote first,
    then the scoreboard competitions[0].notes[0].headline fallback."""
    header = summary.get("header") or {}
    note = header.get("gameNote")
    if note:
        return note
    # fallback path
    comps = header.get("competitions") or []
    if comps:
        notes = comps[0].get("notes") or []
        if notes:
            hl = notes[0].get("headline")
            if hl:
                return hl
    return None


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_cache(cache):
    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, sort_keys=True)


def fetch_gamenotes(derived, cache):
    """For playoff games in GAMENOTE_SEASONS, fetch+parse the ESPN note (using and
    updating `cache`). Returns dict game_id -> {'round','game_num','note'} for
    every game that produced a parseable note."""
    targets = derived[derived["season"].isin(GAMENOTE_SEASONS)]["game_id"].tolist()
    targets = [g for g in targets if not is_nba_game_id(g)]
    todo = [g for g in targets if g not in cache]
    print("\ngameNote fetch: %d games in %s ; %d cached ; %d to fetch"
          % (len(targets), sorted(GAMENOTE_SEASONS), len(targets) - len(todo), len(todo)))

    for n, gid in enumerate(todo, 1):
        try:
            summ = get_json(SUMMARY_URL, {"event": gid})
        except Exception as e:  # noqa: BLE001
            print("  [%d/%d] %s  FETCH FAILED: %s" % (n, len(todo), gid, e))
            time.sleep(DELAY_SECONDS)
            continue
        time.sleep(DELAY_SECONDS)
        note = extract_note_text(summ)
        rnd, gnum = parse_gamenote(note)
        cache[gid] = {"note": note, "round": rnd, "game_num": gnum}
        if n % 20 == 0 or n == len(todo):
            save_cache(cache)
        if rnd is None or gnum is None:
            print("  [%d/%d] %s  UNPARSED note=%r" % (n, len(todo), gid, note))
    save_cache(cache)

    parsed = {}
    for gid in targets:
        c = cache.get(gid)
        if c and c.get("round") is not None and c.get("game_num") is not None:
            parsed[gid] = c
    return parsed


# --------------------------------------------------------------------------- #
# combine + report
# --------------------------------------------------------------------------- #
def combine(derived, notes):
    """Merge structural + gameNote labels. gameNote wins on disagreement. Prints
    agreement stats and every mismatch. Returns the output DataFrame."""
    der = {r.game_id: (r.round, r.game_num) for r in derived.itertuples(index=False)}

    agree = 0
    mismatches = []
    out = []
    for gid in derived["game_id"]:
        d_round, d_gnum = der[gid]
        note = notes.get(gid)
        if note:
            g_round, g_gnum = note["round"], note["game_num"]
            if (g_round, g_gnum) == (d_round, d_gnum):
                agree += 1
            else:
                mismatches.append((gid, (d_round, d_gnum), (g_round, g_gnum), note.get("note")))
            out.append({"game_id": gid, "round": g_round, "game_num": g_gnum,
                        "source": "gamenote"})
        else:
            out.append({"game_id": gid, "round": d_round, "game_num": d_gnum,
                        "source": "derived"})

    n_notes = len(notes)
    print("\nCross-validation (gameNote vs derived):")
    print("  games with a parsed note: %d" % n_notes)
    print("  agreements: %d ; mismatches: %d" % (agree, len(mismatches)))
    if mismatches:
        print("  gameNote WINS on these (game_id: derived -> gamenote  [note]):")
        for gid, d, g, raw in mismatches:
            print("    %s: round/game %s -> %s   %r" % (gid, d, g, raw))

    df = pd.DataFrame(out, columns=OUT_COLUMNS)
    src_counts = df["source"].value_counts().to_dict()
    print("  source breakdown: %s" % src_counts)
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--derive-only", action="store_true",
                    help="run the structural derivation only; no network, no gameNote fetch")
    args = ap.parse_args()

    print("ESPN playoff round/game-number labeler (fetch_espn_round_labels.py)")
    print("=" * 70)

    games = pd.read_csv(GAMES_PATH, dtype=str)
    derived, ok = derive_structural(games)
    if not ok:
        print("\nWARNING: structural validation FAILED for one or more seasons "
              "(see above). Labels still written, but review before trusting.")

    if args.derive_only:
        notes = {}
        print("\n--derive-only: skipping ESPN gameNote fetch.")
    else:
        cache = load_cache()
        notes = fetch_gamenotes(derived, cache)

    df = combine(derived, notes)

    os.makedirs(SOURCE_DIR, exist_ok=True)
    df[OUT_COLUMNS].to_csv(OUT_PATH, index=False, encoding="utf-8-sig", compression="gzip")
    print("\n-> wrote %s (%d rows)" % (os.path.relpath(OUT_PATH, REPO_ROOT), len(df)))


if __name__ == "__main__":
    main()
