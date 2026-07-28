"""
probe_1990s_shape.py  --  LOCAL, READ-ONLY probe (run by Jorge on Windows).

Why this exists
----------------
fetch_espn_seasons.py's --discover-floor mode probed one November 15th per
year, walking backward from 1992 to 1985, and found events on two of those
eight dates: 1988-11-15 and 1991-11-15. Before concluding anything about
where ESPN's real archive floor sits, we need to know what those two events
actually ARE -- real completed NBA games with legitimate box scores, or
something anomalous (an exhibition, a non-NBA game, a malformed/partial
payload) that happens to sit in ESPN's index despite the surrounding years
being empty.

Separately, --discover-floor only checked one date (Nov 15) per year, and
only 8 years. This script spot-checks three spread-out seasons already known
to have real NBA games (1985-86, 1987-88, 1990-91) across FOUR dates each
(November, January, March, and a plausible playoff-window date), to see
whether event coverage / officials data is consistent across a season or
spotty even within seasons ESPN clearly carries.

The officials-shape check in both parts goes DELIBERATELY BEYOND the narrow
`discover_officials_candidates()` used elsewhere (which only matches a key
literally named "officials" holding a list of dicts). Tonight's earlier
West/East Conference Finals gameNote parsing bug in fetch_espn_round_labels.py
was exactly this class of failure: a narrow, path-specific check silently
missing real data that existed under a shape the check didn't anticipate.
Before concluding "officials data doesn't exist" for these old payloads, this
script also (a) scans every dict key anywhere in the payload for
official/referee/umpire/crew-shaped substrings, regardless of the value's
shape, and (b) scans every STRING value anywhere in the payload for those
same keywords, in case crew info is embedded in freeform text (e.g. a game
note) rather than a structured field.

Writes NOTHING to games.csv.gz / officials.csv.gz / player_logs -- read-only.
Output is console + one (overwritten each run) file:
    source-data/_probe_1990s_shape.txt

Reuses (does not duplicate) the ESPN plumbing from fetch_espn_seasons.py:
    get_json, SCOREBOARD_URL, SUMMARY_URL, discover_officials_candidates,
    pick_officials_list, official_fields, nba_abbr, ALLOWED_TRICODES

  python scripts\\local\\probe_1990s_shape.py
"""

import os
import sys
import json
import time
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_1990s_shape.txt")

# Reuse the ESPN plumbing already written and tested in fetch_espn_seasons.py.
# (Importing that module is side-effect-safe: its work is behind __main__.)
sys.path.insert(0, SCRIPT_DIR)
from fetch_espn_seasons import (  # noqa: E402
    get_json,
    SCOREBOARD_URL,
    SUMMARY_URL,
    discover_officials_candidates,
    pick_officials_list,
    official_fields,
    nba_abbr,
    ALLOWED_TRICODES,
)

DELAY_SECONDS = 1.0

# --------------------------------------------------------------------------- #
# Part 1: the two anomalous floor-probe dates -- full payload inspection.
# --------------------------------------------------------------------------- #
FLOOR_DATES = ["1988-11-15", "1991-11-15"]

# --------------------------------------------------------------------------- #
# Part 2: three spread-out seasons, four dates each (Nov / Jan / Mar /
# a plausible playoff window). Dates are generous guesses, not confirmed --
# that is exactly what this probe is for. Playoff windows in this era ran
# April-June; mid-May sits inside every one of these three postseasons
# without needing to know exact bracket dates in advance.
# --------------------------------------------------------------------------- #
SPREAD_SEASONS = [
    {"label": "1985-86", "november": "1985-11-15", "january": "1986-01-15",
     "march": "1986-03-15", "playoff_window": "1986-05-15"},
    {"label": "1987-88", "november": "1987-11-15", "january": "1988-01-15",
     "march": "1988-03-15", "playoff_window": "1988-05-15"},
    {"label": "1990-91", "november": "1990-11-15", "january": "1991-01-15",
     "march": "1991-03-15", "playoff_window": "1991-05-15"},
]

# Collected output lines (printed live AND written to the summary file).
_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


# --------------------------------------------------------------------------- #
# Broad officials-shape search -- deliberately wider than
# discover_officials_candidates(), to rule out the "narrow check missed it"
# class of bug before concluding officials data doesn't exist.
# --------------------------------------------------------------------------- #
OFFICIAL_KEY_SUBSTRINGS = ("official", "referee", "ref", "umpire", "crew")
OFFICIAL_TEXT_KEYWORDS = ("referee", "official", "umpire", "crew chief", "crew")


def find_broad_key_hits(obj, path="root"):
    """Recursively walk the ENTIRE payload for any dict key whose name
    contains an official/referee/umpire/crew-shaped substring, regardless of
    what the value looks like (list, dict, string, number). Returns
    [(path, key, value_type, preview)]."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "{}.{}".format(path, k)
            if any(s in str(k).lower() for s in OFFICIAL_KEY_SUBSTRINGS):
                preview = json.dumps(v, ensure_ascii=False)
                if len(preview) > 300:
                    preview = preview[:300] + "...(preview truncated; full payload dumped above)"
                hits.append((p, k, type(v).__name__, preview))
            hits.extend(find_broad_key_hits(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_broad_key_hits(v, "{}[{}]".format(path, i)))
    return hits


def find_keyword_in_strings(obj, path="root"):
    """Recursively scan every STRING value anywhere in the payload (not just
    keys) for officials-related keywords, in case crew info is embedded in
    freeform text rather than a structured field. Returns
    [(path, keyword, snippet)]."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits.extend(find_keyword_in_strings(v, "{}.{}".format(path, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_keyword_in_strings(v, "{}[{}]".format(path, i)))
    elif isinstance(obj, str):
        low = obj.lower()
        for kw in OFFICIAL_TEXT_KEYWORDS:
            if kw in low:
                snippet = obj if len(obj) <= 300 else obj[:300] + "...(truncated)"
                hits.append((path, kw, snippet))
                break
    return hits


def report_officials_shape(summary):
    """Run the narrow check (reused, unmodified) plus both broad checks, and
    print everything found. Returns True if ANY of the three found something."""
    narrow = discover_officials_candidates(summary)
    if narrow:
        emit("  narrow check (discover_officials_candidates, key=='officials'):")
        for path, lst in narrow:
            emit("    FOUND at {} ({} entries)".format(path, len(lst)))
            for entry in lst:
                oid, nm, jr = official_fields(entry)
                emit("      parsed -> id={!r} name={!r} jersey={!r}".format(oid, nm, jr))
    else:
        emit("  narrow check (discover_officials_candidates, key=='officials'): NOT FOUND")

    broad_keys = find_broad_key_hits(summary)
    if broad_keys:
        emit("  broad key-name scan (any key containing official/referee/umpire/crew):")
        for path, key, kind, preview in broad_keys:
            emit("    {}  (key={!r}, type={})".format(path, key, kind))
            emit("      {}".format(preview))
    else:
        emit("  broad key-name scan: no key anywhere in the payload matches "
             "official/referee/umpire/crew.")

    broad_text = find_keyword_in_strings(summary)
    if broad_text:
        emit("  broad string-value scan (any text value mentioning those words):")
        for path, kw, snippet in broad_text:
            emit("    {}  (matched {!r}): {!r}".format(path, kw, snippet))
    else:
        emit("  broad string-value scan: no string value anywhere in the payload "
             "mentions official/referee/umpire/crew.")

    return bool(narrow or broad_keys or broad_text)


# --------------------------------------------------------------------------- #
# Part 1 helpers
# --------------------------------------------------------------------------- #
def is_completed(event):
    status = (((event.get("status") or {}).get("type")) or {})
    return bool(status.get("completed") or status.get("state") == "post")


def event_matchup(event):
    try:
        comps = event["competitions"][0]["competitors"]
    except (KeyError, IndexError, TypeError):
        return event.get("shortName") or event.get("name") or ""
    home = away = ""
    for c in comps:
        abbr = nba_abbr((c.get("team") or {}).get("abbreviation", ""))
        if c.get("homeAway") == "home":
            home = abbr
        elif c.get("homeAway") == "away":
            away = abbr
    if home or away:
        return "{} @ {}".format(away or "?", home or "?")
    return event.get("shortName") or event.get("name") or ""


def assess_game_shape(event, summary):
    """Print a real-game-vs-anomalous assessment. Returns True if it looks
    like a real, completed NBA game with a legitimate box score."""
    comp = (event.get("competitions") or [{}])[0]
    home = away = None
    for c in comp.get("competitors", []):
        if c.get("homeAway") == "home":
            home = c
        elif c.get("homeAway") == "away":
            away = c
    home_abbr = nba_abbr((home or {}).get("team", {}).get("abbreviation", "")) if home else ""
    away_abbr = nba_abbr((away or {}).get("team", {}).get("abbreviation", "")) if away else ""
    home_score = (home or {}).get("score") if home else None
    away_score = (away or {}).get("score") if away else None
    valid_teams = bool(home_abbr and away_abbr
                       and home_abbr in ALLOWED_TRICODES and away_abbr in ALLOWED_TRICODES)

    box = summary.get("boxscore") or {}
    box_teams = box.get("teams") or []
    box_players = box.get("players") or []
    n_athletes = sum(len(g.get("athletes") or [])
                     for tb in box_players for g in (tb.get("statistics") or []))

    completed = is_completed(event)
    season_type = (event.get("season") or {}).get("type")

    emit("  event id: {}".format(event.get("id")))
    emit("  matchup: {}".format(event_matchup(event)))
    emit("  season.type: {}   status.completed: {}".format(season_type, completed))
    emit("  score: away={} home={}".format(away_score, home_score))
    emit("  both team abbreviations are real NBA tricodes (current or historical): {}"
         .format(valid_teams))
    emit("  boxscore.teams entries: {}".format(len(box_teams)))
    emit("  boxscore.players athlete rows: {}".format(n_athletes))

    anomalies = []
    if not completed:
        anomalies.append("event.status is not marked completed")
    if not valid_teams:
        anomalies.append("one or both team abbreviations are not a real NBA tricode "
                          "(possible exhibition / All-Star / international game)")
    if home_score in (None, "") or away_score in (None, ""):
        anomalies.append("missing a final score")
    if n_athletes == 0:
        anomalies.append("no player box-score rows found")

    if anomalies:
        emit("  VERDICT: ANOMALOUS -- {}".format("; ".join(anomalies)))
        return False
    emit("  VERDICT: looks like a real, completed NBA game with a legitimate box score.")
    return True


def probe_floor_date(dstr):
    emit("\n{}".format("=" * 70))
    emit("FLOOR DATE {}".format(dstr))
    emit("=" * 70)
    d = datetime.date.fromisoformat(dstr)
    ymd = d.strftime("%Y%m%d")
    try:
        sb = get_json(SCOREBOARD_URL, {"dates": ymd})
    except RuntimeError as e:
        emit("  scoreboard FAILED: {}".format(e))
        return
    time.sleep(DELAY_SECONDS)

    events = sb.get("events", []) or []
    emit("  events on this date: {}".format(len(events)))
    if not events:
        emit("  (nothing to inspect -- --discover-floor found events here, but this "
             "run's scoreboard call returned none. ESPN's index may be inconsistent "
             "across requests; note this discrepancy.)")
        return

    for i, ev in enumerate(events):
        emit("\n  --- event {}/{}: id={!r}  {}  season.type={}  completed={} ---".format(
            i + 1, len(events), ev.get("id"), event_matchup(ev),
            (ev.get("season") or {}).get("type"), is_completed(ev)))
        eid = str(ev.get("id", "")).strip()
        if not eid:
            emit("    (no event id -- skipping summary fetch)")
            continue
        try:
            summ = get_json(SUMMARY_URL, {"event": eid})
        except RuntimeError as e:
            emit("    summary FAILED: {}".format(e))
            time.sleep(DELAY_SECONDS)
            continue
        time.sleep(DELAY_SECONDS)

        emit("\n  FULL RAW SUMMARY PAYLOAD (event {}):".format(eid))
        emit(json.dumps(summ, indent=2, ensure_ascii=False, sort_keys=True))

        emit("\n  --- shape assessment for event {} ---".format(eid))
        assess_game_shape(ev, summ)

        emit("\n  --- officials-shape search for event {} ---".format(eid))
        report_officials_shape(summ)


# --------------------------------------------------------------------------- #
# Part 2 helper
# --------------------------------------------------------------------------- #
def probe_spread_date(label, when, dstr):
    emit("\n  [{}] {} ({})".format(label, when, dstr))
    d = datetime.date.fromisoformat(dstr)
    ymd = d.strftime("%Y%m%d")
    try:
        sb = get_json(SCOREBOARD_URL, {"dates": ymd})
    except RuntimeError as e:
        emit("    scoreboard FAILED: {}".format(e))
        return
    time.sleep(DELAY_SECONDS)

    events = sb.get("events", []) or []
    emit("    events: {}".format(len(events)))
    if not events:
        return

    completed = [ev for ev in events if is_completed(ev)]
    target = completed[0] if completed else events[0]
    eid = str(target.get("id", "")).strip()
    emit("    probing officials via event {!r}  {}".format(eid, event_matchup(target)))
    if not eid:
        return
    try:
        summ = get_json(SUMMARY_URL, {"event": eid})
    except RuntimeError as e:
        emit("    summary FAILED: {}".format(e))
        time.sleep(DELAY_SECONDS)
        return
    time.sleep(DELAY_SECONDS)

    narrow_path, narrow_lst = pick_officials_list(summ)
    broad_keys = find_broad_key_hits(summ)
    broad_text = find_keyword_in_strings(summ)
    emit("    narrow officials parse: {}{}".format(
        bool(narrow_lst),
        "  (at {}, {} entries)".format(narrow_path, len(narrow_lst)) if narrow_lst else ""))
    emit("    broad key-name hits: {}   broad string-keyword hits: {}".format(
        len(broad_keys), len(broad_text)))
    if broad_keys and not narrow_lst:
        emit("    !! broad scan found something the narrow check missed -- see keys:")
        for path, key, kind, _preview in broad_keys:
            emit("       {}  (key={!r}, type={})".format(path, key, kind))


def main():
    emit("1990s ESPN shape probe (probe_1990s_shape.py)")
    emit("=" * 70)
    emit("Read-only. Writes nothing to games.csv.gz / officials.csv.gz / player_logs.")
    emit("Run at (local clock): {}".format(datetime.datetime.now().isoformat(timespec="seconds")))

    emit("\n" + "#" * 70)
    emit("# PART 1: full inspection of the two events --discover-floor found")
    emit("# (dates checked: {})".format(", ".join(FLOOR_DATES)))
    emit("#" * 70)
    for dstr in FLOOR_DATES:
        probe_floor_date(dstr)

    emit("\n\n" + "#" * 70)
    emit("# PART 2: spread-check three seasons across 4 dates each")
    emit("# (November / January / March / a plausible playoff window)")
    emit("#" * 70)
    for season in SPREAD_SEASONS:
        emit("\n{}".format("-" * 70))
        emit("SEASON {}".format(season["label"]))
        emit("-" * 70)
        probe_spread_date("november", season["label"], season["november"])
        probe_spread_date("january", season["label"], season["january"])
        probe_spread_date("march", season["label"], season["march"])
        probe_spread_date("playoff_window", season["label"], season["playoff_window"])

    emit("\n" + "=" * 70)
    emit("Done. This probe only reports what it finds -- no recommendation about")
    emit("SEASONS or the archive floor is made here; that is a follow-up decision")
    emit("once the shape above is reviewed.")

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    print("\n-> wrote {}".format(os.path.relpath(OUT_PATH, REPO_ROOT)))


if __name__ == "__main__":
    main()
