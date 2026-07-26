"""
recover_2000_01_playoffs.py  --  LOCAL recovery fetch (run by Jorge on Windows).

The completeness audit in fetch_espn_round_labels.py flags 2000-01 as the only
ESPN season with missing playoff games: four late-round series where neither
team reached the clinch number (CHA/MIL semis 3-2, LAL/SAS West Finals 3-0,
MIL/PHI East Finals 2-2, LAL/PHI Finals 1-0 -- the Lakers' 15-1 run). A missing
game silently corrupts game_num for its series, so before we settle for nulling
those game numbers we make a real attempt to recover the games.

The bulk fetch (fetch_espn_seasons.py) already walked 2000-01 and still missed
these, so this script does ONE thing that walk doesn't: when a queried date
returns zero events -- June 8, 2001 has done this on two independent runs months
apart, which could be a real archive gap OR a date-boundary artifact in ESPN's
indexing -- it also probes date-1 and date+1, and (because parse_game_row keys
game_date off the payload, not the query) stores any recovered game under its
OWN actual date.

It reuses the fetch_espn_seasons.py machinery unchanged (get_json, parse_game_row,
parse_officials_rows, parse_player_rows, merge_rows, merge_player_logs) and the
audit primitives from fetch_espn_round_labels.py (series_format, game_winner,
is_espn_scheme). Merges additively into games/officials/player_logs exactly like
the bulk fetch, then re-runs the completeness audit on 2000-01 and reports what
was actually recovered versus what is still genuinely missing.

  python scripts\\local\\recover_2000_01_playoffs.py

Additive and idempotent (merge is keyed on game_id); safe to re-run.
"""

import os
import sys
import json
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))

# Reuse the ESPN fetch/parse/merge machinery and the audit primitives. Importing
# either module is side-effect-safe (their work is behind __main__).
sys.path.insert(0, SCRIPT_DIR)
from fetch_espn_seasons import (  # noqa: E402
    get_json,
    USER_AGENT,
    SCOREBOARD_URL,
    SUMMARY_URL,
    DELAY_SECONDS,
    GAMES_PATH,
    GAMES_COLUMNS,
    OFFICIALS_PATH,
    OFFICIALS_COLUMNS,
    PLAYER_LOGS_DIR,  # noqa: F401  (kept for parity / debugging)
    parse_game_row,
    parse_officials_rows,
    parse_player_rows,
    merge_rows,
    merge_player_logs,
)
from fetch_espn_round_labels import (  # noqa: E402
    is_espn_scheme,
    series_format,
    game_winner,
)

SEASON = "2000-01"
# 2000-01 postseason ran 2001-04-21 (first round) .. 2001-06-15 (Finals G5).
# Clip every search window to these outer bounds so we never scan into the
# regular season or past the Finals.
BOUND_LO = datetime.date(2001, 4, 19)
BOUND_HI = datetime.date(2001, 6, 20)
# A best-of-7 spans ~2 weeks; pad each incomplete series' present-game span by
# this many days on both sides so a missing game BEFORE the first (or AFTER the
# last) game we have still falls inside the walk.
WINDOW_PAD = 12
ONE_DAY = datetime.timedelta(days=1)


# --------------------------------------------------------------------------- #
# analysis (mirrors the fetch_espn_round_labels.py audit, scoped to one season)
# --------------------------------------------------------------------------- #
def analyze_series(games, season):
    """Return one dict per ESPN-scheme playoff series in `season`:
    teams, round, format label, clinch number, games present, per-team wins,
    series score, complete flag, present dates, present game_ids."""
    po = games[(games["season"] == season) & (games["season_type"] == "Playoffs")].copy()
    po = po[po["game_id"].map(is_espn_scheme)]
    if po.empty:
        return []
    po["_date"] = pd.to_datetime(po["game_date"])
    po = po.sort_values("_date")
    po["_series"] = po.apply(
        lambda r: frozenset({r["home_team_abbr"], r["away_team_abbr"]}), axis=1)

    # round = per-team series-sequence order (robust to missing games within a
    # series, since the ordering is by each series' earliest present date).
    ser_first = po.groupby("_series")["_date"].min()
    team_series = {}
    for ser, first in ser_first.items():
        for t in ser:
            team_series.setdefault(t, []).append((first, ser))
    series_round = {}
    for _t, lst in team_series.items():
        for i, (_f, ser) in enumerate(sorted(lst), 1):
            series_round.setdefault(ser, set()).add(i)
    series_round = {s: min(rs) for s, rs in series_round.items()}

    start_year = int(str(season)[:4])
    out = []
    for ser, ssd in po.groupby("_series"):
        rnd = series_round[ser]
        clinch, _cap, label = series_format(rnd, start_year)
        wins = ssd.apply(game_winner, axis=1).value_counts().to_dict()
        top = max(wins.values()) if wins else 0
        out.append({
            "teams": ser,
            "round": rnd,
            "label": label,
            "clinch": clinch,
            "n": len(ssd),
            "wins": wins,
            "score": "-".join(str(wins.get(t, 0)) for t in sorted(ser)),
            "complete": top >= clinch,
            "dates": sorted(d.date() for d in ssd["_date"]),
            "game_ids": set(ssd["game_id"]),
        })
    return out


def teams_label(ser):
    return "/".join(sorted(ser))


# --------------------------------------------------------------------------- #
# scoreboard walk with a zero-event date-1/date+1 fallback
# --------------------------------------------------------------------------- #
def raw_scoreboard(date, retries=4):
    """Diagnostic scoreboard fetch. Builds the request EXACTLY as
    fetch_espn_seasons.get_json does -- same URL (SCOREBOARD_URL + '?dates=YYYYMMDD'
    via urlencode), same User-Agent + Accept headers, same 60s timeout, same 4x
    exponential backoff (2/4/8s) -- but instead of get_json's behavior of hiding
    everything behind a raised RuntimeError, it returns (events, diag):

      events : the parsed events list, or [] when non-populated
      diag   : None on a clean populated HTTP 200; otherwise a human-readable
               string carrying the HTTP status code and the first 200 chars of
               the RAW response body (or the exception).

    This is the whole point of the recovery re-run: a swallowed 403 / redirect /
    HTML bot-block page / error-JSON / genuinely-empty off-day all previously
    collapsed to the same silent 'zero events'. Now each is distinguishable, so
    a uniform failure across known-good dates (e.g. 2001-05-23, which is in our
    extract) is visibly NOT an archive gap."""
    ymd = date.strftime("%Y%m%d")
    url = SCOREBOARD_URL + "?" + urllib.parse.urlencode({"dates": ymd})
    delay = 2.0
    diag = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.getcode()
                raw = resp.read()
            body = raw.decode("utf-8", "replace")
            try:
                data = json.loads(body)
            except ValueError:
                return [], "HTTP %s but body is not JSON: %r" % (status, body[:1200])
            events = data.get("events", []) or []
            if events:
                return events, None
            # A clean 200 with no events is either a genuine off-day or an error
            # payload dressed as success -- show the body (1200 chars: 200 cut off
            # right before the events key) so we can tell which.
            return [], "HTTP %s, no 'events': %r" % (status, body[:1200])
        except urllib.error.HTTPError as e:
            try:
                ebody = e.read().decode("utf-8", "replace")[:1200]
            except Exception:  # noqa: BLE001
                ebody = "<unreadable>"
            diag = "HTTPError %s %s: %r" % (e.code, e.reason, ebody)
        except Exception as e:  # noqa: BLE001
            diag = "%s: %s" % (type(e).__name__, e)
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    return [], diag


def scoreboard_events(date, cache):
    """Memoized scoreboard fetch for a single date -> list of events. Logs the
    diagnostic (HTTP status + raw body snippet) for every non-populated date so
    nothing is silently treated as empty."""
    key = date.isoformat()
    if key in cache:
        return cache[key]
    events, diag = raw_scoreboard(date)
    if diag:
        print("     scoreboard %s -> %s" % (key, diag))
    time.sleep(DELAY_SECONDS)
    cache[key] = events
    return events


def find_candidates(incomplete, present_dates, cache):
    """Walk each incomplete series' padded date window; return
    {game_id: (event, row)} for completed playoff games that match one of the
    incomplete series' team pairs and are NOT already in the extract.

    Only GENUINE GAP dates are walked: any date that already carries a present
    playoff game (present_dates) is skipped, because the original fetch demonstrably
    reached it and processed every event on it -- a missing game can only live on
    a date that returned nothing. This stops the walk from re-querying known-good
    dates (e.g. MIL/PHI's 05-23/25/26/28) and muddying the diagnosis.

    On any gap date that still returns zero events, also probe date-1 and date+1
    (the boundary-artifact fallback) and scan those events too. The +-1 probes are
    NOT gap-filtered -- a boundary-shifted game may sit under a present neighbor."""
    pairs = {s["teams"]: s for s in incomplete}
    have = set().union(*[s["game_ids"] for s in incomplete]) if incomplete else set()

    # union of padded, clipped windows, MINUS dates already confirmed present
    window_dates = set()
    for s in incomplete:
        lo = max(min(s["dates"]) - datetime.timedelta(WINDOW_PAD), BOUND_LO)
        hi = min(max(s["dates"]) + datetime.timedelta(WINDOW_PAD), BOUND_HI)
        d = lo
        while d <= hi:
            window_dates.add(d)
            d += ONE_DAY
    walk_dates = {d for d in window_dates if d not in present_dates}
    print("  walking %d genuine-gap date(s); skipped %d already-present date(s)"
          % (len(walk_dates), len(window_dates) - len(walk_dates)))

    # POSITIVE CONTROLS: probe dates we KNOW carry a present game. If the fetch
    # mechanism is healthy these return events; if they return a diagnostic
    # (403 / HTML block / non-JSON) then the uniform 'zero events' across gap
    # dates is a fetch failure, not an archive gap. These are the only present-date
    # queries -- the recovery walk itself stays gap-only.
    #   - the earliest present date of the incomplete series, and
    #   - 2001-06-15 specifically (Finals G5, in our extract from the original bulk
    #     fetch): a fresh success narrows the problem to this ~26-day window and
    #     clears everything else already fetched; a fresh failure means something
    #     changed archive-side since the original run.
    control_dates = [min(min(s["dates"]) for s in incomplete), datetime.date(2001, 6, 15)]
    for cdate in control_dates:
        cev, cdiag = raw_scoreboard(cdate)
        time.sleep(DELAY_SECONDS)
        print("  CONTROL probe of known-present %s -> %s"
              % (cdate.isoformat(),
                 cdiag if cdiag else "%d events (fetch mechanism OK)" % len(cev)))

    candidates = {}  # game_id -> (event, row)
    for d in sorted(walk_dates):
        evs = scoreboard_events(d, cache)
        scan = list(evs)
        if not evs:
            # +-1 boundary probe, gap-filtered: a missing game can't hide under a
            # present neighbor (the original fetch would have captured it there).
            probes = [p for p in (d - ONE_DAY, d + ONE_DAY) if p not in present_dates]
            print("  %s: scoreboard returned 0 events -> probing %s"
                  % (d.isoformat(),
                     " and ".join(p.isoformat() for p in probes) if probes
                     else "(both neighbors already present -- nothing to probe)"))
            for p in probes:
                scan.extend(scoreboard_events(p, cache))

        for ev in scan:
            row, suffix = parse_game_row(ev, SEASON)
            if row is None or suffix != "PO":
                continue
            ser = frozenset({row["home_team_abbr"], row["away_team_abbr"]})
            if ser not in pairs:
                continue
            gid = row["game_id"]
            if gid in have or gid in candidates:
                continue
            status = (((ev.get("status") or {}).get("type")) or {})
            if not (status.get("completed") or status.get("state") == "post"):
                continue
            candidates[gid] = (ev, row)
            print("     + candidate %s  %s @ %s  %s  (%s, R%d %s)"
                  % (gid, row["away_team_abbr"], row["home_team_abbr"],
                     row["game_date"], row["season_type"], pairs[ser]["round"],
                     teams_label(ser)))
    return candidates


# --------------------------------------------------------------------------- #
# fetch summaries for candidates and merge additively
# --------------------------------------------------------------------------- #
def recover(candidates):
    """Fetch each candidate's summary, parse game/officials/player rows, and
    merge additively into the extracts (same as fetch_espn_seasons.py). Returns
    the set of recovered game_ids actually written."""
    if not candidates:
        print("  no recoverable candidates found.")
        return set()

    game_rows, official_rows = [], []
    player_rows = []
    recovered = set()
    for gid, (ev, row) in sorted(candidates.items()):
        try:
            summ = get_json(SUMMARY_URL, {"event": gid})
        except Exception as e:  # noqa: BLE001
            print("     summary %s FAILED: %s (skipping)" % (gid, e))
            time.sleep(DELAY_SECONDS)
            continue
        time.sleep(DELAY_SECONDS)
        game_rows.append(row)
        orows = parse_officials_rows(summ, gid)
        prows = parse_player_rows(summ, gid)
        official_rows.extend(orows)
        player_rows.extend(prows)
        recovered.add(gid)
        print("     recovered %s  %s  officials=%d players=%d"
              % (gid, row["game_date"], len(orows), len(prows)))

    if game_rows:
        merge_rows(GAMES_PATH, GAMES_COLUMNS, game_rows, ["game_id"])
    if official_rows:
        merge_rows(OFFICIALS_PATH, OFFICIALS_COLUMNS, official_rows, ["game_id", "official_id"])
    if player_rows:
        merge_player_logs({(SEASON, "PO"): player_rows})
    return recovered


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def report(before, after, recovered):
    print("\n" + "=" * 70)
    print("2000-01 recovery result")
    print("=" * 70)
    before_by = {s["teams"]: s for s in before}
    after_by = {s["teams"]: s for s in after}

    still_missing = []
    for ser, b in before_by.items():
        a = after_by.get(ser, b)
        gained = a["game_ids"] - b["game_ids"]
        state = "COMPLETE now" if a["complete"] else "STILL INCOMPLETE"
        print("  R%d %s (%s): %d -> %d games, score %s -> %s  [%s]"
              % (b["round"], teams_label(ser), b["label"], b["n"], a["n"],
                 b["score"], a["score"], state))
        for gid in sorted(gained):
            print("       + recovered %s" % gid)
        if not a["complete"]:
            still_missing.append(a)

    print("\n  games recovered: %d" % len(recovered))
    if still_missing:
        print("  series STILL genuinely missing games after a real attempt "
              "(these get game_num nulled, round kept -- option 1):")
        for a in still_missing:
            print("     R%d %s: %d games present, score %s, need %d to clinch"
                  % (a["round"], teams_label(a["teams"]), a["n"], a["score"], a["clinch"]))
    else:
        print("  all previously-incomplete 2000-01 series are now COMPLETE. "
              "No game_num nulling needed.")


def main():
    print("2000-01 playoff recovery (recover_2000_01_playoffs.py)")
    print("=" * 70)

    games = pd.read_csv(GAMES_PATH, dtype=str, keep_default_na=False)
    before = analyze_series(games, SEASON)
    incomplete = [s for s in before if not s["complete"]]
    print("Incomplete 2000-01 series to target (%d):" % len(incomplete))
    for s in incomplete:
        print("  R%d %s (%s): %d games, score %s, dates %s"
              % (s["round"], teams_label(s["teams"]), s["label"], s["n"], s["score"],
                 ", ".join(d.isoformat() for d in s["dates"])))
    if not incomplete:
        print("  nothing to recover; 2000-01 is already complete.")
        return

    # Dates already carrying a present 2000-01 playoff game -- the walk skips
    # these (the original fetch reached them and processed every event), so we
    # only probe genuine gaps.
    po_all = games[(games["season"] == SEASON) & (games["season_type"] == "Playoffs")]
    present_dates = set(pd.to_datetime(po_all["game_date"]).dt.date)

    print("\nWalking genuine-gap dates (with zero-event date+-1 fallback):")
    cache = {}
    candidates = find_candidates(incomplete, present_dates, cache)

    print("\nFetching summaries for %d candidate game(s) and merging additively:"
          % len(candidates))
    recovered = recover(candidates)

    games2 = pd.read_csv(GAMES_PATH, dtype=str, keep_default_na=False)
    after = analyze_series(games2, SEASON)
    report(before, after, recovered)


if __name__ == "__main__":
    main()
