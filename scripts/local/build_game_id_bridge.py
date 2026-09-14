"""
build_game_id_bridge.py  --  LOCAL script. Builds the NBA<->ESPN game-id bridge.

  python scripts\\local\\build_game_id_bridge.py

THE PROBLEM. This repo holds two extracts keyed by two different game-id
schemes for the same games:

  source-data/officials.csv.gz   2023-24 onward came from ESPN, so its games
                                 are keyed 401584690 (9-digit ESPN id).
  stats.nba.com play-by-play     keyed 0022300001 (10-char NBA id, whose digits
                                 encode season type and season start year).

Nothing joins them, so a play-by-play row in those seasons cannot reach its own
crew sheet. That cost 3,215 calls in the last extraction run -- every J.Goble
call in 2023-24 and 2024-25, dropped because the crew that would have said
which Goble it was could not be looked up.

THE BRIDGE. cdn.nba.com play-by-play (shufinskiy's cdnnba_* archives) is keyed
by NBA id AND carries timeActual, a real timestamp, plus each team's tricode.
source-data/games.csv.gz carries ESPN id, date and both tricodes. So a game is
identified by its date and the pair of teams in it, and that identifies it in
both schemes. Where the cdn archives fall short -- cdnnba_po_2025 was
snapshotted mid-playoffs -- shotdetail_* fills in: same NBA id, a plain
GAME_DATE, and HTM/VTM.

  match key: {tricode, tricode} (unordered) + date

Unordered because cdn's rows say which team committed an action, not which team
was at home; the pair plus the date is already unique -- the same two teams
never play twice on one date.

Both sides turn out to date a game from the same UTC instant (cdn's timeActual,
and ESPN's feed behind games.csv.gz), so all 3,916 games matched at offset 0.
The one-day tolerance is kept as a safety net for a source that later dates by
local tip-off instead, and the offset is recorded per row rather than assumed.
Note that a UTC date runs a day ahead of the local one for a late tip -- a
Portland evening game dates as the next morning. That is how games.csv.gz
already stores it site-wide; the bridge matches the convention rather than
changing it.

VERIFICATION, not assumption. Every bridged pair is checked against the final
score: cdn's last scoreHome/scoreAway versus games.csv.gz's home_pts/away_pts.
Agreement confirms the pairing AND settles which side was home, from a field
the match key never touched. A wrong pairing shows up as two unrelated
scorelines, so the check is graded by size: a gap of a point or two is the two
feeds disagreeing about a late free throw, while a large one means the games
are not the same game. Both are reported and flagged in the output.

Output (committed; it is small and other tools depend on it):
  source-data/game_id_bridge.csv.gz
  source-data/_game_id_bridge_report.txt

Reused by: extract_referee_calls.py (crew lookup for 2023-24 onward), and the
planned L2M integration, which faces the same two schemes.

NETWORK. The shufinskiy index and the cdnnba archives it names. Nothing else.
"""

import argparse
import collections
import csv
import datetime
import gzip
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import nba_data_source as nds  # noqa: E402
from nba_tricodes import to_nba_tricode as normalize_tricode  # noqa: E402

REPO_ROOT = nds.REPO_ROOT
SOURCE_DIR = nds.SOURCE_DIR
GAMES_CSV = os.path.join(SOURCE_DIR, "games.csv.gz")
OUT_BRIDGE = nds.BRIDGE_CSV
OUT_REPORT = os.path.join(SOURCE_DIR, "_game_id_bridge_report.txt")

# cdnnba carries a real timestamp AND the running score, which is why it is the
# primary source: the score gives a check the match key never touched. Playoffs
# are separate archives.
DEFAULT_KEYS = ["cdnnba_2023", "cdnnba_po_2023",
                "cdnnba_2024", "cdnnba_po_2024",
                "cdnnba_2025", "cdnnba_po_2025"]

# FALLBACK. cdnnba_po_2025 was snapshotted mid-playoffs and stops after the
# first round and part of the second, leaving 25 games of 2025-26 with no way
# across. shotdetail carries GAME_DATE, HTM and VTM against the same NBA game
# id, so it bridges those games -- with no score to check against, but with
# home and away named outright, which the unordered pair match does not use.
# Used only for games the primary source does not have.
FALLBACK_KEYS = ["shotdetail_2023", "shotdetail_po_2023",
                 "shotdetail_2024", "shotdetail_po_2024",
                 "shotdetail_2025", "shotdetail_po_2025"]

# One day either way. A UTC timestamp for a 7:30pm ET tip is the next calendar
# day in UTC, so the offset is expected, not a defect. Wider than this would
# start matching a different meeting of the same two teams.
MAX_DAY_OFFSET = 1

# Total absolute points of disagreement still counted as the same game. Two
# feeds can differ by a free throw at the buzzer; they cannot differ by a
# basket and still be the same game as any other pairing on that date.
SCORE_NEAR_GAP = 2

_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


def section(title):
    emit("")
    emit("=" * 78)
    emit(title)
    emit("=" * 78)


def pad_gid(v):
    """NBA ids lose their leading zeros to anything that reads them as a
    number. 22300001 and 0022300001 are the same game."""
    s = str(v or "").strip()
    if s and s.isdigit() and len(s) < 10:
        s = s.zfill(10)
    return s


def parse_date(ts):
    """The date part of an ISO-8601 timestamp, tz marker and all."""
    s = (ts or "").strip()
    if not s:
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, IndexError):
        return None


def to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# side A: cdn.nba.com play-by-play, keyed by NBA id
# --------------------------------------------------------------------------- #
def scan_cdn(keys, index, no_download=False, limit=None):
    """NBA game id -> {date, teams, final score}.

    One pass per archive. Per game we keep the EARLIEST timestamp (tip-off,
    not the last free throw, which can cross midnight UTC), every distinct
    tricode seen, and the score carried by the highest-ordered action.
    """
    games = {}
    for key in keys:
        path = nds.ensure_dataset(key, index, emit=emit, no_download=no_download)
        if not path:
            continue
        seen = set()
        rows = 0
        for row in nds.iter_csv_rows(path, limit=limit):
            rows += 1
            gid = pad_gid(row.get("gameId"))
            if not gid:
                continue
            g = games.setdefault(gid, {"date": None, "teams": set(), "order": -1,
                                       "home_pts": None, "away_pts": None,
                                       "source": key})
            seen.add(gid)
            d = parse_date(row.get("timeActual"))
            if d and (g["date"] is None or d < g["date"]):
                g["date"] = d
            tri = normalize_tricode((row.get("teamTricode") or "").strip().upper())
            if tri:
                g["teams"].add(tri)
            order = to_int(row.get("orderNumber"))
            hs, as_ = to_int(row.get("scoreHome")), to_int(row.get("scoreAway"))
            if order is not None and hs is not None and as_ is not None and order > g["order"]:
                g["order"], g["home_pts"], g["away_pts"] = order, hs, as_
        emit("  %-18s %9d row(s), %5d game(s)" % (key, rows, len(seen)))
    return games


def scan_shotdetail(keys, index, have, no_download=False, limit=None):
    """NBA game id -> {date, teams, home, away} for games `have` does not cover.

    stats.nba.com's shot chart carries GAME_DATE as a plain YYYYMMDD local
    date, plus HTM and VTM. No score, so a bridged row from here is flagged
    score_check=unknown -- but home and away are named, which the pair match
    never looks at, so the pairing still gets an independent check.
    """
    games = {}
    for key in keys:
        path = nds.ensure_dataset(key, index, emit=emit, no_download=no_download)
        if not path:
            continue
        rows = added = 0
        for row in nds.iter_csv_rows(path, limit=limit):
            rows += 1
            gid = pad_gid(row.get("GAME_ID"))
            if not gid or gid in have or gid in games:
                continue
            raw = (row.get("GAME_DATE") or "").strip()
            if len(raw) != 8 or not raw.isdigit():
                continue
            home = normalize_tricode((row.get("HTM") or "").strip().upper())
            away = normalize_tricode((row.get("VTM") or "").strip().upper())
            if not home or not away:
                continue
            games[gid] = {
                "date": datetime.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8])),
                "teams": {home, away}, "home": home, "away": away,
                "order": -1, "home_pts": None, "away_pts": None, "source": key,
            }
            added += 1
        emit("  %-18s %9d row(s), %5d game(s) not already covered" % (key, rows, added))
    return games


# --------------------------------------------------------------------------- #
# side B: this repo's games extract, keyed by ESPN id
# --------------------------------------------------------------------------- #
def load_espn_games(seasons):
    """(date, frozenset(pair)) -> list of game records, for the seasons asked."""
    by_key = collections.defaultdict(list)
    total = 0
    with gzip.open(GAMES_CSV, "rt", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            if seasons and rec.get("season") not in seasons:
                continue
            d = parse_date(rec.get("game_date"))
            home = normalize_tricode((rec.get("home_team_abbr") or "").strip().upper())
            away = normalize_tricode((rec.get("away_team_abbr") or "").strip().upper())
            if not d or not home or not away:
                continue
            total += 1
            by_key[(d, frozenset((home, away)))].append({
                "espn_game_id": (rec.get("game_id") or "").strip(),
                "season": rec.get("season") or "",
                "date": d, "home": home, "away": away,
                "home_pts": to_int(rec.get("home_pts")),
                "away_pts": to_int(rec.get("away_pts")),
            })
    return by_key, total


def season_of_gid(gid):
    """2023-24 from 0022300001. The scheme puts the season start year at
    digits 3:5 and the season type at digit 2."""
    if len(gid) != 10 or not gid.isdigit():
        return None
    yy = int(gid[3:5])
    start = 2000 + yy if yy < 90 else 1900 + yy
    return "%d-%02d" % (start, (start + 1) % 100)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keys", nargs="*", default=DEFAULT_KEYS,
                    help="shufinskiy dataset keys to read (default: cdnnba 2023-2025)")
    ap.add_argument("--limit", type=int, help="stop after N rows per archive (smoke test)")
    ap.add_argument("--no-download", action="store_true",
                    help="use only what is already cached")
    ap.add_argument("--out", default=OUT_BRIDGE)
    ap.add_argument("--out-report", default=OUT_REPORT)
    args = ap.parse_args()

    emit("game-id bridge (build_game_id_bridge.py)")
    emit("=" * 78)
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))

    index = nds.load_index(emit=emit, no_download=args.no_download)
    emit("dataset index: %d entries" % len(index))

    section("SCANNING cdn.nba.com PLAY-BY-PLAY (NBA ids)")
    cdn = scan_cdn(args.keys, index, args.no_download, args.limit)
    if not cdn:
        emit("")
        emit("No cdn archive could be read. Nothing to bridge; no file written.")
        _write_report(args.out_report)
        return 1
    emit("")
    emit("%d NBA-keyed game(s) from the primary source" % len(cdn))

    section("FILLING GAPS FROM shotdetail")
    extra = scan_shotdetail(FALLBACK_KEYS, index, set(cdn), args.no_download, args.limit)
    if extra:
        emit("")
        emit("%d game(s) the primary source does not carry, bridged from shotdetail"
             % len(extra))
        cdn.update(extra)
    else:
        emit("")
        emit("nothing to fill -- the primary source covers every game.")

    seasons = {s for s in (season_of_gid(g) for g in cdn) if s}
    espn_by_key, espn_total = load_espn_games(seasons)
    emit("%d ESPN-keyed game(s) in %s from games.csv.gz"
         % (espn_total, ", ".join(sorted(seasons))))

    section("MATCHING")
    rows = []
    used_espn = {}
    fail = collections.Counter()
    failures = []
    offsets = collections.Counter()
    score_ok = score_bad = score_unknown = score_near = 0
    home_ok = home_bad = 0
    near_detail = []

    for gid in sorted(cdn):
        g = cdn[gid]
        season = season_of_gid(gid)
        if g["date"] is None or len(g["teams"]) != 2:
            fail["no date or not exactly two tricodes"] += 1
            failures.append((gid, season, "no date or not exactly two tricodes",
                             str(sorted(g["teams"]))))
            continue
        pair = frozenset(g["teams"])
        # Nearest date first: the exact date is the common case, one day out is
        # the UTC rollover, and anything further is not this game.
        cand = None
        for off in range(0, MAX_DAY_OFFSET + 1):
            for delta in ((0,) if off == 0 else (-off, off)):
                bucket = espn_by_key.get((g["date"] + datetime.timedelta(days=delta), pair))
                if bucket:
                    free = [c for c in bucket if c["espn_game_id"] not in used_espn]
                    if free:
                        cand, chosen_off = free[0], delta
                        break
            if cand:
                break
        if not cand:
            fail["no ESPN game with this pair within +/-%d day" % MAX_DAY_OFFSET] += 1
            failures.append((gid, season,
                             "no ESPN game with this pair within +/-%d day" % MAX_DAY_OFFSET,
                             "%s %s" % (g["date"], "/".join(sorted(pair)))))
            continue

        used_espn[cand["espn_game_id"]] = gid
        offsets[chosen_off] += 1

        # Independent check. The score never entered the match key, so
        # agreement is evidence the pairing is right -- and it settles which
        # tricode was the home side.
        if (g["home_pts"] is None or cand["home_pts"] is None):
            verdict = "unknown"
            score_unknown += 1
        elif (g["home_pts"], g["away_pts"]) == (cand["home_pts"], cand["away_pts"]):
            verdict = "match"
            score_ok += 1
        else:
            gap = (abs(g["home_pts"] - cand["home_pts"])
                   + abs(g["away_pts"] - cand["away_pts"]))
            detail = "cdn %s-%s vs espn %s-%s (gap %d)" % (
                g["home_pts"], g["away_pts"], cand["home_pts"], cand["away_pts"], gap)
            if gap <= SCORE_NEAR_GAP:
                # Two feeds, one game, a point in it. The pairing stands.
                verdict = "near"
                score_near += 1
                near_detail.append((gid, season, detail))
            else:
                verdict = "MISMATCH"
                score_bad += 1
                failures.append((gid, season, "final score is a different game", detail))

        # When the source named home and away itself (shotdetail does; cdn
        # does not), check that against games.csv.gz. The pair match is
        # unordered, so this is information it never used.
        if g.get("home") and g.get("away"):
            if (g["home"], g["away"]) == (cand["home"], cand["away"]):
                home_check = "match"
                home_ok += 1
            else:
                home_check = "FLIPPED"
                home_bad += 1
                failures.append((gid, season, "home/away disagree",
                                 "source %s@%s vs espn %s@%s"
                                 % (g["away"], g["home"], cand["away"], cand["home"])))
        else:
            home_check = "n/a"

        rows.append({
            "nba_game_id": gid, "espn_game_id": cand["espn_game_id"],
            "season": season or cand["season"],
            "game_date": cand["date"].isoformat(),
            "home_abbr": cand["home"], "away_abbr": cand["away"],
            "date_offset_days": chosen_off, "score_check": verdict,
            "home_check": home_check, "source": g["source"],
        })

    emit("bridged            : %d" % len(rows))
    emit("unbridged          : %d" % sum(fail.values()))
    emit("")
    emit("date offset (cdn timeActual date vs games.csv.gz date)")
    for off in sorted(offsets):
        emit("  %+d day : %d game(s)" % (off, offsets[off]))
    emit("")
    emit("final-score check (independent of the match key)")
    emit("  agrees exactly        : %d" % score_ok)
    emit("  within %d point(s)     : %d" % (SCORE_NEAR_GAP, score_near))
    emit("  a different game      : %d" % score_bad)
    emit("  unknown               : %d" % score_unknown)
    emit("")
    emit("home/away check (only where the source names them: shotdetail does)")
    emit("  agrees    : %d" % home_ok)
    emit("  flipped   : %d" % home_bad)
    for gid, season, detail in near_detail:
        emit("    %s %s %s" % (gid, season or "?", detail))
    if score_near:
        emit("")
        emit("A gap of a point or two is the two feeds disagreeing about the last")
        emit("basket, not a mispairing: the teams and the date still agree, and a")
        emit("wrong pairing would produce two unrelated scorelines. Those rows are")
        emit("kept, flagged score_check=near.")
    if score_bad:
        emit("")
        emit("*** A large disagreement means the pairing is WRONG. Those rows are")
        emit("written flagged score_check=MISMATCH so a consumer can exclude them;")
        emit("investigate before relying on them.")

    section("UNBRIDGED GAMES")
    if not failures:
        emit("None. Every NBA-keyed game found its ESPN counterpart.")
        emit("")
        emit("The reverse direction is not one-to-one and is not meant to be:")
        emit("games.csv.gz carries every SCHEDULED game, cdn only every PLAYED one,")
        emit("so an in-progress season leaves ESPN ids with nothing to bridge to")
        emit("until those games are played.")
    else:
        emit("%-12s %-9s %-46s %s" % ("nba_game_id", "season", "reason", "detail"))
        emit("-" * 78)
        for gid, season, reason, detail in failures[:60]:
            emit("%-12s %-9s %-46s %s" % (gid, season or "?", reason, detail))
        if len(failures) > 60:
            emit("... and %d more" % (len(failures) - 60))
        emit("")
        for reason, n in fail.most_common():
            emit("  %-56s %d" % (reason, n))

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["nba_game_id", "espn_game_id", "season",
                                           "game_date", "home_abbr", "away_abbr",
                                           "date_offset_days", "score_check",
                                           "home_check", "source"])
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["nba_game_id"]):
            w.writerow(r)
    section("OUTPUT")
    emit("-> %s (%d row(s))" % (os.path.relpath(args.out, REPO_ROOT), len(rows)))
    _write_report(args.out_report)
    return 0


def _write_report(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_lines) + "\n")
    print("\n-> wrote %s" % os.path.relpath(path, REPO_ROOT))


if __name__ == "__main__":
    sys.exit(main())
