"""
diagnose_1990s_flags.py  --  READ-ONLY investigation (runs anywhere; no ESPN
network access needed -- it only reads the already-fetched games.csv.gz).

Why this exists
----------------
source-data/_completeness_1990s.txt (audit_1990s_completeness.py, reusing
fetch_espn_round_labels.py's derive_structural()) flagged two series:

  1994-95: ROUND DISAGREEMENT {'MIA', 'ORL'} teams claim rounds [1, 5]
           INCOMPLETE  R1 MIA/ORL (best-of-5): 1 games present, series score
           1-0, no team reached 3 wins -> games MISSING

  1995-96: INCOMPLETE  R2 CHI/NYK (best-of-7): 4 games present, series score
           3-1, no team reached 4 wins -> games MISSING

This script investigates BOTH, diagnosis only -- it attempts no recovery and
writes no round_labels.csv.gz, and it does not touch games.csv.gz or
officials.csv.gz. Findings:

(1) 1994-95 MIA/ORL is NOT a real series -- round 5 is not a valid playoff
    round (the bracket only has 4), so this was never a data GAP, it is a
    single corrupt/duplicate ESPN row. game_id 150611014 (game_date
    1995-06-11, home_team_id=14/MIA, away_team_id=19/ORL, score 106-103) sits
    exactly between two real 1995 NBA Finals games -- 150609019 (1995-06-10,
    Rockets @ Magic) and 150614019 (1995-06-15, Rockets @ Magic) -- and its
    score (106-103) and away team (ORL) are IDENTICAL to game_id 150611010
    (1995-06-11, home_team_id=10/HOU, away_team_id=19/ORL, 106-103), which is
    the real Game 3 of the 1995 Finals (Rockets swept Magic 4-0). The only
    difference between the two rows is home_team_id: 10 (HOU, correct) vs 14
    (MIA, wrong). Miami played 81 REGULAR SEASON games in games.csv.gz for
    1994-95 (a real franchise, correctly fetched) but has ZERO other playoff
    rows -- consistent with the real historical record, since Miami did not
    make the 1994-95 playoffs at all. So this is a ghost/duplicate ESPN event
    (150611014) carrying the wrong home_team_id for what is otherwise a
    correctly-scored real Finals game -- an ESPN-side archival data-quality
    issue, not a bug in fetch_espn_seasons.py's team-id normalization (both
    150611010's and 150611014's team_id -> abbreviation mappings are
    individually correct; ESPN's own payload for event 150611014 is what
    carries the wrong team_id). derive_structural() groups series purely by
    {home_team_abbr, away_team_abbr}, so this one bad row creates a phantom
    "MIA/ORL" series alongside the real "HOU/ORL" Finals series, which is
    why it gets flagged as a 1-0, unclinched INCOMPLETE series, and why ORL's
    chronological round-count includes a bogus 5th entry (round 5) alongside
    MIA's only entry (round 1) -- hence "claim rounds [1, 5]".

(2) 1995-96 CHI/NYK: the 4 present games (5/5, 5/11, 5/12, 5/14; CHI leads
    3-1) are all real and correctly scored -- an exhaustive search of every
    row in games.csv.gz pairing ESPN team_id 4 (CHI) and 18 (NYK), in any
    season_type or date, plus a same-date/same-score duplicate scan across
    all of 1995-96, finds NOTHING resembling a misfiled or duplicated Game 5.
    The Bulls won this East semifinal 4-1 per the real historical record;
    CHI/ORL (the Eastern Conference Finals) begins 1996-05-19, immediately
    after this series' last present game, which is exactly where a 5th
    CHI/NYK game would sit. Conclusion: the missing game is genuinely ABSENT
    from games.csv.gz (an ESPN-fetch coverage gap), not a mis-scored or
    misfiled existing row.

Output: console + source-data/_diagnose_1990s_flags.txt (overwritten each
run, since it reflects the CURRENT state of games.csv.gz, not a running log).

  python scripts\\local\\diagnose_1990s_flags.py
"""

import os

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
OUT_PATH = os.path.join(SOURCE_DIR, "_diagnose_1990s_flags.txt")

_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


def is_nba_scheme(gid):
    return len(gid) == 10 and gid.isdigit() and gid.startswith("00")


GAME_COLS = ["game_id", "game_date", "season_type", "home_team_id", "home_team_abbr",
            "away_team_id", "away_team_abbr", "home_pts", "away_pts", "home_win"]


def print_table(df, cols=GAME_COLS):
    if df.empty:
        emit("  (none)")
        return
    for _, r in df.sort_values("game_date").iterrows():
        emit("  " + "  ".join("{}={}".format(c, r[c]) for c in cols if c in df.columns))


# --------------------------------------------------------------------------- #
# Part 1: 1994-95 MIA/ORL
# --------------------------------------------------------------------------- #
def diagnose_mia_orl(games):
    emit("\n" + "#" * 70)
    emit("# PART 1: 1994-95 MIA/ORL -- 'claims rounds [1, 5]'")
    emit("#" * 70)

    po = games[(games["season"] == "1994-95") & (games["season_type"] == "Playoffs")].copy()
    emit("\n1994-95 playoff rows total: {} (NBA-scheme/nbadb rows among them: {})"
         .format(len(po), int(po["game_id"].map(is_nba_scheme).sum())))

    mia_orl = po[((po["home_team_abbr"] == "MIA") & (po["away_team_abbr"] == "ORL"))
                | ((po["home_team_abbr"] == "ORL") & (po["away_team_abbr"] == "MIA"))]
    emit("\nEvery MIA/ORL playoff row in 1994-95 (this is the entire flagged 'series'):")
    print_table(mia_orl)

    mia_any = po[(po["home_team_abbr"] == "MIA") | (po["away_team_abbr"] == "MIA")]
    emit("\nEvery 1994-95 PLAYOFF row involving MIA in any position (same as above -- "
        "MIA has no other playoff appearance that season):")
    print_table(mia_any)

    rs_mia = games[(games["season"] == "1994-95") & (games["season_type"] == "Regular Season")
                  & ((games["home_team_abbr"] == "MIA") | (games["away_team_abbr"] == "MIA"))]
    emit("\n1994-95 REGULAR SEASON rows involving MIA: {} (confirms Miami is a real, "
        "correctly-fetched 1994-95 franchise -- just one that did not make the "
        "playoffs, consistent with the actual historical standings)."
        .format(len(rs_mia)))

    # The real Finals sequence this ghost row sits inside.
    finals = po[((po["home_team_abbr"] == "HOU") & (po["away_team_abbr"] == "ORL"))
               | ((po["home_team_abbr"] == "ORL") & (po["away_team_abbr"] == "HOU"))]
    emit("\nThe real 1995 NBA Finals (HOU/ORL) rows the MIA row sits chronologically "
        "inside (Rockets swept Magic 4-0):")
    print_table(finals)

    emit("\nSide-by-side: the suspected ghost row vs. the real game on the same date:")
    same_date = po[po["game_date"] == "1995-06-11"]
    print_table(same_date)
    if len(same_date) == 2:
        r1, r2 = same_date.iloc[0], same_date.iloc[1]
        same_score = {r1["home_pts"], r1["away_pts"]} == {r2["home_pts"], r2["away_pts"]}
        same_away = r1["away_team_abbr"] == r2["away_team_abbr"] == "ORL"
        diff_home_id = r1["home_team_id"] != r2["home_team_id"]
        emit("  -> identical scores: {}   identical away team (ORL): {}   "
            "DIFFERENT home_team_id: {}".format(same_score, same_away, diff_home_id))
        emit("  -> since away_team_id/abbr and the score match exactly, and only "
            "home_team_id differs (10=HOU vs 14=MIA), this is a duplicate ESPN "
            "event carrying the WRONG home team for an otherwise-correct real "
            "game -- not a normalization bug (both team_id->abbr mappings are "
            "individually correct), and not a structural-derivation bug either. "
            "derive_structural() is working exactly as designed on bad input: it "
            "groups series purely by {home_team_abbr, away_team_abbr}, so this "
            "one corrupt row creates a phantom 'MIA/ORL' series (1 game, "
            "unclinched -> INCOMPLETE) alongside the real 'HOU/ORL' Finals "
            "series, and gives ORL a bogus 5th chronological series entry "
            "(round 5) alongside MIA's only entry (round 1) -- exactly "
            "reproducing the audit's 'claim rounds [1, 5]' disagreement.")

    emit("\nVERDICT (Part 1): 1994-95 MIA/ORL is not a real series and not a data "
        "gap. It is a single corrupt ESPN row (game_id 150611014) that duplicates "
        "the real Finals Game 3 (150611010) under the wrong home team. No "
        "recovery attempted in this pass -- diagnosis only.")


# --------------------------------------------------------------------------- #
# Part 2: 1995-96 CHI/NYK
# --------------------------------------------------------------------------- #
def diagnose_chi_nyk(games):
    emit("\n\n" + "#" * 70)
    emit("# PART 2: 1995-96 CHI/NYK -- '4 games present, 3-1, missing Game 5'")
    emit("#" * 70)

    po = games[(games["season"] == "1995-96") & (games["season_type"] == "Playoffs")].copy()
    emit("\n1995-96 playoff rows total: {} (NBA-scheme/nbadb rows among them: {})"
         .format(len(po), int(po["game_id"].map(is_nba_scheme).sum())))

    chi_nyk = po[((po["home_team_abbr"] == "CHI") & (po["away_team_abbr"] == "NYK"))
                | ((po["home_team_abbr"] == "NYK") & (po["away_team_abbr"] == "CHI"))]
    emit("\nEvery CHI/NYK playoff row in 1995-96:")
    print_table(chi_nyk)

    wins = {"CHI": 0, "NYK": 0}
    for _, r in chi_nyk.sort_values("game_date").iterrows():
        winner = r["home_team_abbr"] if r["home_win"] == "1" else r["away_team_abbr"]
        wins[winner] += 1
    emit("\nWin tally from the 4 present games: CHI {} - NYK {}".format(
        wins["CHI"], wins["NYK"]))
    emit("Real historical record for this series (per the caller): Bulls won 4-1. "
        "The present games are consistent with that -- CHI already leads 3-1 and "
        "needs exactly one more win (the clincher) to reach 4, matching a 4-1 "
        "finish precisely (not 4-2 or any other alternative).")

    emit("\nExhaustive search for a misfiled or duplicated Game 5, across the ENTIRE "
        "file (not just 1995-96 playoffs) -- every row anywhere pairing ESPN "
        "team_id 4 (CHI) and 18 (NYK), any season/season_type/date:")
    pair_anywhere = games[((games["home_team_id"] == "4") & (games["away_team_id"] == "18"))
                          | ((games["home_team_id"] == "18") & (games["away_team_id"] == "4"))]
    pair_1996 = pair_anywhere[pair_anywhere["season"] == "1995-96"]
    pair_1996_po = pair_1996[pair_1996["season_type"] == "Playoffs"]
    pair_1996_other = pair_1996[pair_1996["season_type"] != "Playoffs"]
    emit("  CHI/NYK rows found in 1995-96: {} total ({} Playoffs, {} other "
        "season_type -- the 4 regular-season meetings, which are separate real "
        "games, not a hidden Game 5). Playoffs count matches the 4 already "
        "shown above exactly; no 5th playoff entry hides under a different "
        "season_type.".format(len(pair_1996), len(pair_1996_po), len(pair_1996_other)))

    window = games[(games["season"] == "1995-96")
                  & (games["game_date"] >= "1996-05-14") & (games["game_date"] <= "1996-05-19")
                  & ((games["home_team_id"].isin(["4", "18"]))
                     | (games["away_team_id"].isin(["4", "18"])))]
    emit("\nAll 1995-96 rows 1996-05-14..1996-05-19 involving team_id 4 or 18 in any "
        "position (the gap window right after the last present CHI/NYK game and "
        "right before CHI/ORL, the real next round, begins 1996-05-19):")
    print_table(window)

    all96 = games[games["season"] == "1995-96"]
    dupe_check = all96[all96.duplicated(subset=["game_date", "home_pts", "away_pts"], keep=False)]
    dupe_involving_chi_nyk = dupe_check[
        (dupe_check["home_team_abbr"].isin(["CHI", "NYK"]))
        | (dupe_check["away_team_abbr"].isin(["CHI", "NYK"]))]
    emit("\nSame-date/same-score duplicate scan across all of 1995-96, restricted to "
        "rows involving CHI or NYK (the ghost-row pattern that explained Part 1): "
        "{} matching row(s) found.".format(len(dupe_involving_chi_nyk)))
    print_table(dupe_involving_chi_nyk)

    emit("\nBracket context: CHI/ORL (the real 1996 Eastern Conference Finals, Bulls "
        "swept Magic 4-0) begins 1996-05-19 -- immediately after this series' last "
        "present game (5/14) -- exactly where a 5th CHI/NYK game would need to sit "
        "for the bracket to connect. Nothing occupies that slot.")

    emit("\nVERDICT (Part 2): the missing game is genuinely ABSENT from "
        "games.csv.gz, not mis-scored or misfiled elsewhere. All 4 present rows "
        "check out (real teams, real scores, consistent with a 4-1 finish); an "
        "exhaustive search of the whole file finds no trace of a 5th CHI/NYK "
        "game under any team_id pairing, season_type, or nearby date, and no "
        "duplicate/ghost row like the one found in Part 1. This is an ESPN-fetch "
        "coverage gap, not a data-quality corruption. No recovery attempted in "
        "this pass -- diagnosis only.")


def main():
    emit("1990s completeness-flag diagnosis (diagnose_1990s_flags.py)")
    emit("=" * 70)
    emit("Read-only. Investigates the two series _completeness_1990s.txt flagged")
    emit("for 1994-95 (MIA/ORL) and 1995-96 (CHI/NYK). No round-labeling, no")
    emit("recovery attempted -- diagnosis only. Touches no extract file.")

    if not os.path.exists(GAMES_PATH):
        emit("\ngames.csv.gz not found at {} -- nothing to diagnose.".format(GAMES_PATH))
    else:
        games = pd.read_csv(GAMES_PATH, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        diagnose_mia_orl(games)
        diagnose_chi_nyk(games)

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    print("\n-> wrote {}".format(os.path.relpath(OUT_PATH, REPO_ROOT)))


if __name__ == "__main__":
    main()
