"""
probe_seasontype_param.py  --  LOCAL, READ-ONLY, single-purpose probe (run by
Jorge on Windows).

One targeted test: does adding an explicit seasontype=3 (postseason) query
param to the ESPN scoreboard request change the result for 2001-06-08 -- the
real 2001 Finals Game 2 (76ers 89 @ Lakers 98), a date the recovery walk's
diagnostics reported as returning zero events?

Queries dates=20010608 TWICE:
  (a) as the existing scripts do (dates only)
  (b) with seasontype=3 added alongside it

For each, prints the full URL, HTTP status, and event count. If either variant
returns events, prints them (and flags a LAL/PHI match). If BOTH return zero
events, prints the (b) raw response body IN FULL (no truncation) so we can see
whether the season metadata reflects the param at all -- i.e. whether
seasontype was accepted but the date genuinely has nothing, or ignored
entirely.

Writes nothing; this script does not touch games.csv.gz, officials.csv.gz, or
any other extract. It does not modify or call recover_2000_01_playoffs.py.

  python scripts\\local\\probe_seasontype_param.py
"""

import os
import sys
import json
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from fetch_espn_seasons import SCOREBOARD_URL, USER_AGENT  # noqa: E402

TARGET_DATE = "20010608"
DELAY_SECONDS = 1.0


def raw_get(params):
    """One GET, no retry loop (this is a single targeted test, not the bulk
    walk). Returns (status, body_text, url). Raises nothing -- HTTPError is
    caught and its body is read the same way a success body would be."""
    url = SCOREBOARD_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace"), url
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = "<unreadable>"
        return e.code, body, url


def describe(label, params):
    status, body, url = raw_get(params)
    print("\n--- %s ---" % label)
    print("URL: %s" % url)
    print("HTTP status: %s" % status)
    try:
        data = json.loads(body)
    except ValueError:
        print("body is not JSON. Full body:\n%s" % body)
        return None, body
    events = data.get("events", []) or []
    print("events: %d" % len(events))
    if events:
        for ev in events:
            comps = ev.get("competitions", [{}])[0].get("competitors", [])
            abbrs = sorted(
                (c.get("team") or {}).get("abbreviation", "?") for c in comps)
            match = " <-- LAL/PHI MATCH" if set(abbrs) >= {"LAL", "PHI"} else ""
            print("  id=%s  teams=%s  date=%s%s"
                  % (ev.get("id"), abbrs, ev.get("date"), match))
    return events, body


def main():
    print("seasontype param probe (probe_seasontype_param.py)")
    print("=" * 70)
    print("Target date: 2001-06-08 (real 2001 Finals Game 2, PHI 89 @ LAL 98)")

    events_a, body_a = describe("(a) dates only (existing behavior)",
                                 {"dates": TARGET_DATE})
    time.sleep(DELAY_SECONDS)

    events_b, body_b = describe("(b) dates + seasontype=3",
                                 {"dates": TARGET_DATE, "seasontype": "3"})

    print("\n" + "=" * 70)
    if events_a or events_b:
        print("RESULT: seasontype=3 changed the outcome -- events were found. "
              "See the listing above for which variant(s) returned the game.")
    else:
        print("RESULT: both variants returned zero events. Full response body "
              "for variant (b) (seasontype=3), untruncated:\n")
        print(body_b)


if __name__ == "__main__":
    main()
