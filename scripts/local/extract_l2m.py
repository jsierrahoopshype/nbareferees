"""
extract_l2m.py  --  LOCAL script. Slims the NBA's Last Two Minute reports.

  python scripts\\local\\extract_l2m.py

WHAT L2M IS. For games that are close in the final two minutes, the NBA
publishes a review of every call and every non-call in that window, grading
each one. atlhawksfanatic/L2M (MIT) collects those reports into one tidy CSV,
2015 onward. This script reduces its 53MB / 90,302 rows to the two small
extracts this site needs, and nothing else enters the repo.

WHAT IT CANNOT TELL YOU, which governs everything downstream. Each assessed
play carries the CREW that worked the game -- OFFICIAL_1..4 -- and no field
saying which of the three made or missed that particular call. So a play graded
incorrect says one of three officials erred and never which. The comment text
does not name officials either; a surname in it belongs to a player.

Consequently this extract deliberately offers NO path to a per-referee error
count. It records reports per GAME and crews per GAME. Dividing a crew-level
error by one official's games would manufacture a number the source cannot
support, and unlike a wrong rate elsewhere on this site it would read as an
accusation against a named person.

Outputs (both committed; small):
  source-data/l2m_games.csv.gz   one row per game with a report: how many
                                 plays were assessed and how they were graded
  source-data/l2m_crew.csv.gz    one row per (game, official) -- the crew on
                                 each reported game, by NBA person id
  source-data/_l2m_report.txt    coverage + QA

GAME IDS. L2M keys by NBA game id. Our games from 2023-24 are ESPN-keyed, so
ids are canonicalised through source-data/game_id_bridge.csv.gz exactly as the
rest of the pipeline does.

NETWORK. One file from raw.githubusercontent.com, cached under source-data/.
"""

import argparse
import collections
import csv
import gzip
import os
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import nba_data_source as nds  # noqa: E402

REPO_ROOT = nds.REPO_ROOT
SOURCE_DIR = nds.SOURCE_DIR
CACHE = os.path.join(SOURCE_DIR, "_l2m_cache")
L2M_URL = ("https://raw.githubusercontent.com/atlhawksfanatic/L2M/master/"
           "1-tidy/L2M/L2M_stats_nba.csv")
OUT_GAMES = os.path.join(SOURCE_DIR, "l2m_games.csv.gz")
OUT_CREW = os.path.join(SOURCE_DIR, "l2m_crew.csv.gz")
OUT_REPORT = os.path.join(SOURCE_DIR, "_l2m_report.txt")

# The league's four grades. CC/CNC are the call (or the decision not to call)
# judged correct; IC/INC judged incorrect. INC -- an incorrect NON-call, a foul
# the crew missed -- is by far the most common error and the reason this data
# is interesting rather than inflammatory.
DECISIONS = ["CC", "CNC", "IC", "INC", "NA"]

_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


def section(t):
    emit("")
    emit("=" * 78)
    emit(t)
    emit("=" * 78)


def fetch_l2m(no_download=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "L2M_stats_nba.csv")
    if os.path.exists(path) and os.path.getsize(path) > 1000000:
        return path
    if no_download:
        emit("not cached and --no-download given; nothing to do")
        return None
    emit("downloading %s" % L2M_URL)
    req = urllib.request.Request(L2M_URL, headers={"User-Agent": nds.USER_AGENT})
    with urllib.request.urlopen(req, timeout=600) as resp, open(path + ".part", "wb") as fh:
        fh.write(resp.read())
    os.replace(path + ".part", path)
    emit("  %.1f MB cached" % (os.path.getsize(path) / 1048576.0))
    return path


def season_label(yr):
    """L2M's '2015' is our '2014-15': it labels a season by the year it ends."""
    try:
        end = int(str(yr).strip())
    except (TypeError, ValueError):
        return ""
    return "%d-%02d" % (end - 1, end % 100)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()

    emit("Last Two Minute reports (extract_l2m.py)")
    emit("=" * 78)
    path = fetch_l2m(args.no_download)
    if not path:
        return 1

    nba2espn, espn2nba = nds.load_game_id_bridge()

    def canon(g):
        g = str(g or "").strip()
        if g.isdigit() and len(g) < 10:
            g = g.zfill(10)
        return espn2nba.get(g, g)

    csv.field_size_limit(10 ** 7)
    games = {}
    crews = collections.defaultdict(dict)
    rows_read = skipped = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            rows_read += 1
            gid = (r.get("GAME_ID") or "").strip()
            if not gid or gid == "NA":
                skipped += 1
                continue
            gid = canon(gid)
            g = games.setdefault(gid, {
                "game_id": gid, "season": season_label(r.get("season")),
                "playoff": "1" if str(r.get("playoff")).strip().upper() == "TRUE" else "0",
                "assessed": 0, "CC": 0, "CNC": 0, "IC": 0, "INC": 0, "NA": 0})
            g["assessed"] += 1
            d = (r.get("decision") or "").strip().upper()
            g[d if d in DECISIONS else "NA"] += 1
            for n in ("1", "2", "3", "4"):
                oid = (r.get("OFFICIAL_ID_" + n) or "").strip()
                nm = (r.get("OFFICIAL_" + n) or "").strip()
                if oid and oid != "NA":
                    crews[gid][oid] = nm if nm != "NA" else ""

    emit("rows read: %d | rows with no game id: %d" % (rows_read, skipped))
    emit("games with a report: %d" % len(games))

    section("COVERAGE AGAINST OUR GAMES")
    ours = {}
    with gzip.open(os.path.join(SOURCE_DIR, "games.csv.gz"), "rt",
                   encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            ours[canon(r["game_id"])] = r["season"]
    total_by_season = collections.Counter(ours.values())
    have = collections.Counter()
    orphan = 0
    for gid in games:
        if gid in ours:
            have[ours[gid]] += 1
        else:
            orphan += 1
    emit("our games: %d" % len(ours))
    emit("L2M games matching one of ours: %d" % sum(have.values()))
    emit("L2M games we cannot place     : %d" % orphan)
    emit("")
    emit("%-9s %8s %8s %8s" % ("season", "ourGames", "withL2M", "share"))
    emit("-" * 78)
    for s in sorted(have):
        emit("%-9s %8d %8d %7.1f%%"
             % (s, total_by_season[s], have[s], 100.0 * have[s] / total_by_season[s]))
    emit("")
    emit("A report exists only for games that were CLOSE in the last two")
    emit("minutes, which is roughly a third of them. An official's report count")
    emit("therefore tracks how many close games they drew more than anything")
    emit("else, and no figure built from it can be read as a per-game rate.")

    section("HOW THE LEAGUE GRADED THE PLAYS")
    tot = collections.Counter()
    for g in games.values():
        for d in DECISIONS:
            tot[d] += g[d]
    n_all = sum(tot.values())
    emit("%-6s %9s %8s  %s" % ("grade", "plays", "share", "meaning"))
    emit("-" * 78)
    for d, meaning in (("CC", "correct call"), ("CNC", "correct non-call"),
                       ("IC", "INCORRECT call -- a whistle that should not have blown"),
                       ("INC", "INCORRECT non-call -- a foul that went unwhistled"),
                       ("NA", "not assessed")):
        emit("%-6s %9d %7.1f%%  %s" % (d, tot[d], 100.0 * tot[d] / n_all, meaning))
    wrong = tot["IC"] + tot["INC"]
    emit("")
    emit("plays judged incorrect: %d of %d (%.1f%%)" % (wrong, n_all, 100.0 * wrong / n_all))
    emit("of those, %.0f%% are MISSED calls rather than wrong whistles (%d INC vs %d IC)."
         % (100.0 * tot["INC"] / wrong, tot["INC"], tot["IC"]))
    emit("That asymmetry is the finding here. It is a statement about what the")
    emit("league's own reviewers flag, not about any individual official.")

    section("ATTRIBUTION -- WHAT THIS DATA DOES NOT SUPPORT")
    sizes = collections.Counter(len(c) for c in crews.values())
    emit("officials named per reported game: %s"
         % ", ".join("%d officials on %d game(s)" % (k, v) for k, v in sorted(sizes.items())))
    emit("")
    emit("Every assessed play carries the whole crew and no per-call official.")
    emit("There is no column, and no text, identifying which of the three made")
    emit("or missed the play being graded. Any per-referee error count built")
    emit("from this would be invented, so the extract offers no route to one.")

    section("OUTPUT")
    os.makedirs(SOURCE_DIR, exist_ok=True)
    with gzip.open(OUT_GAMES, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["game_id", "season", "playoff", "assessed"] + DECISIONS)
        w.writeheader()
        for gid in sorted(games):
            w.writerow(games[gid])
    with gzip.open(OUT_CREW, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["game_id", "nba_person_id", "official_name"])
        for gid in sorted(crews):
            for oid, nm in sorted(crews[gid].items()):
                w.writerow([gid, oid, nm])
    emit("-> %s (%d row(s))" % (os.path.relpath(OUT_GAMES, REPO_ROOT), len(games)))
    emit("-> %s (%d row(s))" % (os.path.relpath(OUT_CREW, REPO_ROOT),
                                sum(len(c) for c in crews.values())))
    with open(OUT_REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_lines) + "\n")
    print("\n-> wrote %s" % os.path.relpath(OUT_REPORT, REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
