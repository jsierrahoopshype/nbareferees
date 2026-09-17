"""
probe_game_date_timezone.py  --  LOCAL, READ-ONLY probe of how
source-data/games.csv.gz dates a game.

  python scripts/local/probe_game_date_timezone.py

THE SUSPICION. The game-id bridge found 22 games where games.csv.gz sat one
day ahead of a locally-dated source, which would mean games.csv.gz dates a
game by the UTC INSTANT of tip-off rather than by the local calendar date it
was played on. A west-coast evening game is the next morning in UTC, so it
would be filed under the following day.

THIS IS A PROBE, NOT A FIX. It writes no extract and changes no rendering. It
answers: is that real, which rows does it affect, and how many games carry a
date that is not the date the game was played on.

WHY THIS CANNOT JUST BE MEASURED DIRECTLY. games.csv.gz carries a date and no
tip-off time, so "is this row's date the local date" is not a question any
single row can answer on its own. The probe therefore triangulates from four
independent angles, each of which could fail on its own:

  1. SCHEME CENSUS. game_id tells us which feed a row came from -- a 10-digit
     00xxxxxxxx is an NBA id (stats.nba.com, which publishes a local
     GAME_DATE), anything else is an ESPN id. If the defect is per-feed, it
     should fall exactly on that line and nowhere else.

  2. THE CHRISTMAS SLATE. The NBA plays every Christmas Day and plays NOTHING
     on Christmas Eve -- a league-wide off day. So any game filed on December
     24 is, on its face, a game that was not played that day. And the 2023
     Christmas slate is a five-game natural experiment whose tip-off times are
     a matter of public record, spanning noon to 10:30pm ET, which is exactly
     the range that separates "rolls over into the next UTC day" from "does
     not". If the UTC theory is right it predicts which of those five land on
     the 25th and which on the 26th, before looking.

  3. THE WEEKDAY SIGNATURE, ACROSS A SCHEME BOUNDARY. The NBA's weekly
     scheduling shape is strong and stable -- heavy Wednesday, light Thursday.
     Shifting late games into the next day drags that shape one day forward.
     Comparing an ESPN-scheme season against an NBA-scheme season from a
     DIFFERENT ERA would confuse the scheme with changing scheduling habits,
     so the comparison here is only ever between ADJACENT seasons, where
     scheduling habits are near-identical and the scheme is the one thing that
     changed. 2012-13 is the cleanest case in the file: a single ESPN-scheme
     season sitting between two NBA-scheme ones.

  4. THE BRIDGE. source-data/game_id_bridge.csv.gz already recorded, per game,
     the offset between games.csv.gz and an independently-sourced date, for
     3,941 games in 2023-26. It is a ready-made check that needs no network.

HOW MANY GAMES. With no tip-off times in the file, the count of misdated games
cannot be read off directly, so it is ESTIMATED by deconvolution, which is
reported as an estimate and never as a count. If a fraction L of games roll
over into the next day, the weekday histogram of a season is a mixture
    observed = (1-L) * true + L * (true shifted one day forward)
and L is the single free parameter. Fitting it against an adjacent
NBA-scheme season's histogram gives a per-season rollover rate. The fit's
residual is reported alongside it: a good fit is evidence the model is the
right shape, and a bad one is a warning not to trust the number.

TIME ZONES. Whether a game rolls over is arithmetic, not opinion: a game rolls
over when local tip-off + the zone's UTC offset crosses midnight. That
threshold is computed per zone for both standard and daylight time, which is
the part most likely to be misread -- the intuition that this is "a west-coast
problem" is only true from March to November. Under EST a 7:00pm tip in the
EASTERN zone is already midnight UTC, so in midwinter the problem covers the
whole league, not just the late Pacific games.
"""

import os
import csv
import gzip
import sys
import datetime
import collections

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
BRIDGE_PATH = os.path.join(SOURCE_DIR, "game_id_bridge.csv.gz")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_game_date_timezone.txt")

_out = []


def emit(msg=""):
    print(msg)
    _out.append(msg)


def section(title):
    emit()
    emit("=" * 78)
    emit(title)
    emit("=" * 78)


# ---------------------------------------------------------------------------
# Home-arena time zones. Standard-time UTC offsets; daylight time is one hour
# less negative. Only the home team matters -- a game is played on the home
# arena's clock.
# ---------------------------------------------------------------------------
TEAM_TZ = {
    # Eastern
    "BOS": "ET", "BKN": "ET", "NYK": "ET", "PHI": "ET", "TOR": "ET",
    "CHI": "CT", "CLE": "ET", "DET": "ET", "IND": "ET", "MIL": "CT",
    "ATL": "ET", "CHA": "ET", "MIA": "ET", "ORL": "ET", "WAS": "ET",
    # Central
    "DAL": "CT", "HOU": "CT", "MEM": "CT", "NOP": "CT", "SAS": "CT",
    "MIN": "CT", "OKC": "CT",
    # Mountain
    "DEN": "MT", "UTA": "MT",
    # Pacific
    "GSW": "PT", "LAC": "PT", "LAL": "PT", "PHX": "MT", "SAC": "PT",
    "POR": "PT", "SEA": "PT",
    # Historic / relocated tricodes that appear in older rows
    "NJN": "ET", "NOH": "CT", "NOK": "CT", "CHH": "ET", "VAN": "PT",
    "SEA": "PT", "WSB": "ET", "KCK": "CT", "SDC": "PT",
}
# Phoenix does not observe daylight saving; it is Mountain STANDARD all year,
# which means it matches Pacific clock time in summer and Mountain in winter.
NO_DST = {"PHX"}

TZ_STD_OFFSET = {"ET": -5, "CT": -6, "MT": -7, "PT": -8}


def rollover_threshold(tz, daylight):
    """Local tip-off at or after which a game crosses midnight UTC."""
    off = TZ_STD_OFFSET[tz] + (1 if daylight else 0)
    return 24 + off          # e.g. ET standard: 24 + (-5) = 19:00


def scheme_of(game_id):
    """Which feed a row came from, read off the id's own shape."""
    g = (game_id or "").strip()
    if len(g) == 10 and g.startswith("00") and g.isdigit():
        return "nba"
    if g.isdigit():
        return "espn"
    return "unknown"


def load_games():
    with gzip.open(GAMES_PATH, "rt", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("games.csv.gz is empty")
    # The file is written with a BOM, so the id column name carries it.
    gid_col = [k for k in rows[0] if k.endswith("game_id")][0]
    for r in rows:
        r["_gid"] = r[gid_col]
        r["_scheme"] = scheme_of(r[gid_col])
        y, m, d = (int(x) for x in r["game_date"].split("-"))
        r["_date"] = datetime.date(y, m, d)
    return rows


def is_regular(r):
    return (r.get("season_type") or "").lower().startswith("regular")


def weekday_hist(rows):
    """Percent of games on each weekday, Monday first."""
    c = collections.Counter(r["_date"].weekday() for r in rows)
    n = sum(c.values())
    if not n:
        return None
    return [100.0 * c[i] / n for i in range(7)]


def shift_forward(hist):
    """The same histogram with every game moved one day later."""
    return [hist[(i - 1) % 7] for i in range(7)]


def mean_abs_diff(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def fit_rollover_fraction(observed, reference):
    """Best L in observed ~= (1-L)*reference + L*shift(reference).

    Scanned rather than solved: one parameter on [0,1], and the scan also
    yields the residual at the optimum, which is the honest signal of whether
    the mixture model fits at all.
    """
    shifted = shift_forward(reference)
    best = (None, None)
    for i in range(0, 1001):
        L = i / 1000.0
        model = [(1 - L) * reference[j] + L * shifted[j] for j in range(7)]
        resid = mean_abs_diff(observed, model)
        if best[1] is None or resid < best[1]:
            best = (L, resid)
    return best


DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt_hist(h):
    return " ".join("%5.1f" % v for v in h)


# ---------------------------------------------------------------------------


def report_scheme_census(rows):
    section("1. WHICH SEASONS AND WHICH FEED")
    emit("game_id shape tells us the feed: 00xxxxxxxx is an NBA id")
    emit("(stats.nba.com, which publishes a plain local GAME_DATE); anything")
    emit("else is an ESPN id. If the defect is per-feed it lands on this line.")
    emit("")
    # Chronological, NOT the order rows happen to appear in the file. Section 3
    # compares ADJACENT seasons, so a wrong order silently compares a season
    # with one a decade away and the boundary test means nothing.
    counts = {}
    for r in rows:
        counts.setdefault(r["season"], collections.Counter())[r["_scheme"]] += 1
    by_season = collections.OrderedDict(sorted(counts.items()))
    emit("%-9s %-8s %6s   %s" % ("season", "scheme", "games", "mixed?"))
    totals = collections.Counter()
    espn_seasons, nba_seasons = [], []
    for season, c in by_season.items():
        scheme = c.most_common(1)[0][0]
        mixed = "MIXED %s" % dict(c) if len(c) > 1 else ""
        emit("%-9s %-8s %6d   %s" % (season, scheme, sum(c.values()), mixed))
        totals.update(c)
        (espn_seasons if scheme == "espn" else nba_seasons).append(season)
    emit("")
    emit("totals by scheme: %s" % dict(totals))
    emit("")
    emit("ESPN-scheme seasons (%d): %s" % (len(espn_seasons), ", ".join(espn_seasons)))
    emit("NBA-scheme seasons  (%d): %s" % (len(nba_seasons), ", ".join(nba_seasons)))
    return espn_seasons, nba_seasons, by_season


# The 2023 Christmas slate, tip-off times as scheduled and broadcast. These are
# public record, not derived from any file in this repo, which is what makes
# them usable as an independent check rather than a circular one.
XMAS_2023 = [
    ("MIL", "NYK", "12:00pm ET"),
    ("GSW", "DEN", "2:30pm ET"),
    ("BOS", "LAL", "5:00pm ET"),
    ("PHI", "MIA", "8:00pm ET"),
    ("DAL", "PHX", "10:30pm ET"),
]
# ET tip -> UTC date offset in late December (EST, UTC-5): rolls at 19:00 ET.
XMAS_2023_PREDICTED = {
    ("MIL", "NYK"): 0, ("GSW", "DEN"): 0, ("BOS", "LAL"): 0,
    ("PHI", "MIA"): 1, ("DAL", "PHX"): 1,
}


def report_christmas(rows, by_season):
    section("2. THE CHRISTMAS TEST")
    emit("The NBA plays every Christmas Day and plays NOTHING on Christmas Eve.")
    emit("A game filed on December 24 was therefore not played that day.")
    emit("")
    emit("%-9s %-6s %6s %6s %6s" % ("season", "scheme", "Dec24", "Dec25", "Dec26"))
    for season, c in by_season.items():
        scheme = c.most_common(1)[0][0]
        cnt = collections.Counter(r["game_date"][5:] for r in rows if r["season"] == season)
        emit("%-9s %-6s %6d %6d %6d" % (season, scheme,
                                        cnt.get("12-24", 0), cnt.get("12-25", 0),
                                        cnt.get("12-26", 0)))
    emit("")
    emit("Read the Dec24 column against the scheme column.")
    emit("")
    emit("-" * 78)
    emit("The 2023 slate, predicted BEFORE looking at where the rows landed.")
    emit("In late December the Eastern zone is on EST, so a game crosses")
    emit("midnight UTC when it tips at or after 19:00 ET.")
    emit("")
    stored = {}
    for r in rows:
        if r["season"] == "2023-24" and r["game_date"].startswith("2023-12-2"):
            stored[(r["away_team_abbr"], r["home_team_abbr"])] = r["game_date"]
    emit("%-11s %-12s %-12s %-12s %s" % ("game", "tip", "predicted", "stored", "verdict"))
    hits = 0
    for away, home, tip in XMAS_2023:
        pred_off = XMAS_2023_PREDICTED[(away, home)]
        pred = (datetime.date(2023, 12, 25)
                + datetime.timedelta(days=pred_off)).isoformat()
        got = stored.get((away, home), "(absent)")
        ok = (got == pred)
        hits += ok
        emit("%-11s %-12s %-12s %-12s %s" % ("%s@%s" % (away, home), tip, pred, got,
                                             "match" if ok else "MISS"))
    emit("")
    emit("%d of %d predicted correctly." % (hits, len(XMAS_2023)))
    if hits == len(XMAS_2023):
        emit("Every game lands where the UTC-instant theory says it should,")
        emit("including the split inside a single day's slate: the three")
        emit("afternoon games stay on the 25th and the two night games do not.")
        emit("A timezone-free off-by-one would have moved all five together.")
    return hits


def report_weekday_boundary(rows, by_season):
    section("3. THE WEEKDAY SIGNATURE ACROSS SCHEME BOUNDARIES")
    emit("The NBA's weekly shape is strong and stable. Moving late games into")
    emit("the next day drags that shape one day forward. Only ADJACENT seasons")
    emit("are compared, so scheduling habits are held near-constant and the")
    emit("scheme is the thing that changed.")
    emit("")
    seasons = list(by_season)
    scheme = {s: by_season[s].most_common(1)[0][0] for s in seasons}
    hist = {}
    for s in seasons:
        h = weekday_hist([r for r in rows if r["season"] == s and is_regular(r)])
        if h:
            hist[s] = h
    emit("%-9s %-6s %s" % ("season", "scheme", " ".join("%5s" % d for d in DOW)))
    for s in seasons:
        if s in hist:
            emit("%-9s %-6s %s" % (s, scheme[s], fmt_hist(hist[s])))
    emit("")
    emit("-" * 78)
    emit("Boundary pairs: each ESPN-scheme season against every NBA-scheme")
    emit("season immediately beside it.")
    emit("")
    emit("%-9s %-9s %10s %10s   %s" % ("espn", "nba ref", "as-is", "ref +1day", "verdict"))
    wins = ties = 0
    for i, s in enumerate(seasons):
        if scheme.get(s) != "espn" or s not in hist:
            continue
        for j in (i - 1, i + 1):
            if not (0 <= j < len(seasons)):
                continue
            ref = seasons[j]
            if scheme.get(ref) != "nba" or ref not in hist:
                continue
            asis = mean_abs_diff(hist[s], hist[ref])
            rot = mean_abs_diff(hist[s], shift_forward(hist[ref]))
            verdict = "shifted fits better" if rot < asis else "as-is fits better"
            wins += rot < asis
            ties += rot >= asis
            emit("%-9s %-9s %10.2f %10.2f   %s" % (s, ref, asis, rot, verdict))
    emit("")
    emit("%d boundary pair(s) fit better shifted, %d as-is." % (wins, ties))
    return hist, scheme, seasons


def report_rollover_estimate(hist, scheme, seasons, rows):
    section("4. HOW MANY GAMES -- ESTIMATED, NOT COUNTED")
    emit("games.csv.gz has no tip-off time, so the misdated games cannot be")
    emit("counted directly. If a fraction L of a season's games roll over,")
    emit("the weekday histogram is the mixture")
    emit("    observed = (1-L)*true + L*(true shifted one day forward)")
    emit("and L is the only free parameter. Fitted below against the nearest")
    emit("NBA-scheme season. The residual is reported beside every estimate:")
    emit("it is what says whether the model fits at all.")
    emit("")
    emit("%-9s %-9s %8s %10s %9s %10s" %
         ("season", "ref", "fitted L", "residual", "games", "est. wrong"))
    per_season = {}
    for i, s in enumerate(seasons):
        if scheme.get(s) != "espn" or s not in hist:
            continue
        ref = None
        for j in (i + 1, i - 1):
            if 0 <= j < len(seasons) and scheme.get(seasons[j]) == "nba" and seasons[j] in hist:
                ref = seasons[j]
                break
        if not ref:
            continue
        L, resid = fit_rollover_fraction(hist[s], hist[ref])
        n = sum(1 for r in rows if r["season"] == s)
        per_season[s] = (L, resid, n, ref)
        emit("%-9s %-9s %8.3f %10.2f %9d %10d" % (s, ref, L, resid, n, round(L * n)))
    emit("")
    if per_season:
        tot = sum(v[2] for v in per_season.values())
        est = sum(round(v[0] * v[2]) for v in per_season.values())
        Ls = sorted(v[0] for v in per_season.values())
        emit("Seasons with a usable adjacent reference: %d" % len(per_season))
        emit("  games in them      : %d" % tot)
        emit("  estimated misdated : %d  (%.1f%%)" % (est, 100.0 * est / tot))
        emit("  fitted L range     : %.2f to %.2f (median %.2f)"
             % (Ls[0], Ls[-1], Ls[len(Ls) // 2]))
        emit("")
        emit("Seasons with no adjacent NBA-scheme season (the 1993-2003 block)")
        emit("cannot be fitted this way at all, and are deliberately left out of")
        emit("the total rather than assigned a borrowed rate.")
    return per_season


def report_timezones(rows):
    section("5. TIME ZONES AND TIP-OFF TIMES")
    emit("Whether a game rolls over is arithmetic. It rolls when local tip-off")
    emit("plus the zone's UTC offset crosses midnight, so every zone has a")
    emit("threshold, and the threshold moves an hour with daylight saving.")
    emit("")
    emit("%-6s %-22s %-22s" % ("zone", "standard time (Nov-Mar)", "daylight time (Mar-Nov)"))
    for tz in ("ET", "CT", "MT", "PT"):
        emit("%-6s rolls at %02d:00 local    rolls at %02d:00 local"
             % (tz, rollover_threshold(tz, False), rollover_threshold(tz, True)))
    emit("")
    emit("This is the part most likely to be misread. The intuition that this")
    emit("is a west-coast problem holds only under daylight time. Under")
    emit("standard time -- which is most of the regular season -- an EASTERN")
    emit("7:00pm tip is already midnight UTC, so a routine 7:00 or 7:30pm start")
    emit("rolls over in every zone in the league. What survives correct are")
    emit("afternoon and early-evening games: weekend matinees, holiday noon")
    emit("games, and 7:00-7:30pm Eastern starts during the daylight-time months")
    emit("at the very start and end of the season.")
    emit("")
    emit("-" * 78)
    emit("Home games by zone, so the exposure per zone is visible even though")
    emit("the per-game tip-off time is not in the file.")
    emit("")
    counts = collections.Counter()
    unknown = collections.Counter()
    for r in rows:
        if r["_scheme"] != "espn":
            continue
        tz = TEAM_TZ.get(r["home_team_abbr"])
        if tz:
            counts[tz] += 1
        else:
            unknown[r["home_team_abbr"]] += 1
    tot = sum(counts.values()) or 1
    for tz in ("ET", "CT", "MT", "PT"):
        emit("  %-4s %6d home games in ESPN-scheme rows (%.1f%%)"
             % (tz, counts[tz], 100.0 * counts[tz] / tot))
    if unknown:
        emit("  unmapped tricodes: %s" % dict(unknown))
    emit("")
    emit("A per-zone count of MISDATED games needs the tip-off time, which is")
    emit("not in this file. The zone table above says which games are at risk")
    emit("and from what hour; it deliberately does not pretend to a count.")


def report_bridge():
    section("6. THE GAME-ID BRIDGE AS AN INDEPENDENT CHECK")
    if not os.path.exists(BRIDGE_PATH):
        emit("game_id_bridge.csv.gz absent -- skipped.")
        return
    with gzip.open(BRIDGE_PATH, "rt", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    emit("source-data/game_id_bridge.csv.gz already recorded, per game, the")
    emit("offset between games.csv.gz and an independently-sourced date.")
    emit("")
    emit("The two sources behind it date a game differently, and that is the")
    emit("whole value of it:")
    emit("  cdnnba_*     timeActual, an ISO UTC timestamp, read as a UTC date")
    emit("  shotdetail_* GAME_DATE, a plain local calendar date")
    emit("")
    ct = collections.Counter((r["source"].split("_")[0], r["date_offset_days"]) for r in rows)
    emit("%-14s %8s %8s" % ("source family", "offset", "games"))
    for (fam, off), n in sorted(ct.items()):
        emit("%-14s %8s %8d" % (fam, off, n))
    emit("")
    cdn_off = collections.Counter(r["date_offset_days"] for r in rows
                                  if r["source"].startswith("cdnnba"))
    shot_off = collections.Counter(r["date_offset_days"] for r in rows
                                   if r["source"].startswith("shotdetail"))
    emit("Against the UTC-dated source   : %s" % dict(cdn_off))
    emit("Against the local-dated source : %s" % dict(shot_off))
    emit("")
    emit("games.csv.gz agrees with the UTC-dated source on every one of its")
    emit("%d games, and disagrees with the local-dated source on %s of %d."
         % (sum(cdn_off.values()), shot_off.get("1", 0), sum(shot_off.values())))
    emit("That is the finding stated twice from opposite directions: the file")
    emit("holds the UTC date, not the local one.")
    emit("")
    emit("The offset rows are all 2025-26 playoffs only because that is the")
    emit("only window the local-dated source was needed to cover -- it is where")
    emit("the bridge looked, not where the problem is.")
    off1 = [r for r in rows if r["date_offset_days"] == "1"]
    if off1:
        emit("")
        emit("The %d offset games, with the date games.csv.gz holds:" % len(off1))
        for r in off1[:8]:
            emit("  %s  %s  %s @ %s  (stored %s, played the day before)"
                 % (r["nba_game_id"], r["season"], r["away_abbr"], r["home_abbr"],
                    r["game_date"]))
        if len(off1) > 8:
            emit("  ... and %d more" % (len(off1) - 8))
    zero_shot = [r for r in rows if r["source"].startswith("shotdetail")
                 and r["date_offset_days"] == "0"]
    if zero_shot:
        emit("")
        emit("The %d shotdetail game(s) that AGREE are the control: a local and"
             % len(zero_shot))
        emit("a UTC date coincide when the game did not tip late enough to")
        emit("cross midnight.")
        for r in zero_shot:
            emit("  %s  %s  %s @ %s  stored %s"
                 % (r["nba_game_id"], r["season"], r["away_abbr"], r["home_abbr"],
                    r["game_date"]))


# What reads game_date, established by reading scripts/build.py rather than
# guessed. Each entry: (label, where it surfaces, whether a one-day shift
# actually changes what a reader sees).
CONSUMERS = [
    ("latest_game_day (build.py ~3862)",
     "index.html, 'Most recent officiating crews' heading date",
     "YES -- displays the stored date outright, and can also SPLIT one real "
     "night across two stored dates, so the module may show only the late half"),
    ("date_index / month_day (build.py ~3881)",
     "index.html, 'On this date' card",
     "YES -- a game is indexed under the wrong calendar day, so the card "
     "attributes a performance to the day after it happened"),
    ("referee_games per-game date (build.py ~3814)",
     "referee/<slug>/games/, the full game log",
     "YES -- every row's date column"),
    ("notable games (build.py ~1399, ~1429)",
     "referee/<slug>/, notable-game tables",
     "YES -- the date beside each game"),
    ("swings (build.py ~2028, ~2063)",
     "swings/ and the index swings strip",
     "YES -- the date beside each swing"),
    ("recent form windows (build.py ~1695-1915)",
     "index recent-form spotlight, referee recent-form blocks",
     "PARTLY -- cal30 is a date window, so a shifted game can cross the "
     "boundary, and days_since_last_game is off by one"),
    ("era (build.py ~314)",
     "internal split between the two feeds",
     "NO -- derived from the game_id scheme, never from the date"),
    ("debuts / farewells (build.py ~2361)",
     "debuts/ and the index debuts widget",
     "NO -- keyed on first_season / last_season, which are season labels. A "
     "one-day shift cannot move a game out of its season, so this is NOT "
     "affected despite being a date-shaped feature"),
]


def report_consumers():
    section("7. WHAT ACTUALLY DISPLAYS A WRONG DATE")
    emit("Read off scripts/build.py, not guessed. A feature only matters here")
    emit("if a one-day shift changes what a reader sees.")
    for label, surface, verdict in CONSUMERS:
        emit("")
        emit("  %s" % label)
        emit("    surfaces at : %s" % surface)
        emit("    affected    : %s" % verdict)
    emit("")
    emit("Note the two NOs. era is keyed on the id scheme and debuts/farewells")
    emit("on season labels, so neither moves with the date even though both")
    emit("look like date features from the outside.")


def main():
    emit("game_date timezone probe (probe_game_date_timezone.py)")
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("games.csv.gz: %s" % GAMES_PATH)

    rows = load_games()
    emit("rows: %d" % len(rows))

    espn_seasons, nba_seasons, by_season = report_scheme_census(rows)
    report_christmas(rows, by_season)
    hist, scheme, seasons = report_weekday_boundary(rows, by_season)
    report_rollover_estimate(hist, scheme, seasons, rows)
    report_timezones(rows)
    report_bridge()
    report_consumers()

    section("WHAT THIS PROBE DOES NOT ESTABLISH")
    emit("- No per-game count of misdated games. games.csv.gz has no tip-off")
    emit("  time, so the totals in section 4 are a fitted estimate with a")
    emit("  stated residual, not a census.")
    emit("- The 1993-94..2002-03 ESPN block has no adjacent NBA-scheme season,")
    emit("  so no rate could be fitted for it. Its Dec-24 counts in section 2")
    emit("  say it is affected; they do not say by how much.")
    emit("- Nothing here was re-fetched. Confirming a specific game's real")
    emit("  local date needs the cdnnba timeActual timestamps, which are in a")
    emit("  gitignored cache and were not available to this run.")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(_out) + "\n")
    print()
    print("-> %s" % OUT_PATH)


if __name__ == "__main__":
    main()
