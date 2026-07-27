"""
probe_nba_assignments.py  --  LOCAL, READ-ONLY reachability probe (run by
Jorge on Windows).

DASHBOARD_SPEC section 4: before September's "Tonight's Crews" pipeline gets
built, this answers two questions about official.nba.com's daily referee
assignments:
  1. Is the page/API reachable at all, and from where (this script records
     Jorge's local-machine result; the cloud session recorded its own result
     separately -- see the top of source-data/_probe_assignments.txt)?
  2. What shape is the response in (HTML page? JSON API? something else?),
     and are crew names for the most recent games parseable out of it?

July is off-season -- there may be nothing real to parse yet. That's fine;
this round only needs reachability + shape. Nothing is assumed about the URL
or the JSON shape going in -- that's exactly what this script discovers.

Writes NOTHING to games.csv.gz / officials.csv.gz / any other extract. Its
only output is an ADDITIVE append to source-data/_probe_assignments.txt (the
cloud environment's section, written by the cloud session, is preserved --
this script appends a "LOCAL ENVIRONMENT" section below it rather than
overwriting the file), so the two environments' reachability results sit
side by side for the September GitHub-Actions-vs-local-scheduled-task call.

  python scripts\\local\\probe_nba_assignments.py

Candidate URLs (untested assumptions, not confirmed endpoints -- that's the
point of this probe): official.nba.com's public referee-assignments page,
plus the legacy data.nba.com JSON endpoint some hobbyist NBA tools have used
for "today" schedule/game data. Both are tried; neither is assumed correct.
"""

import os
import sys
import json
import time
import datetime
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_assignments.txt")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Candidates to test -- NOT assumed correct. The referee-assignments page is
# official.nba.com's public officiating page; the data.nba.com endpoint is a
# legacy, unofficial-but-long-public JSON feed some hobbyist NBA tools have
# used for daily schedule/game data. Both are worth trying since we don't
# know in advance which (if either) actually carries assignment data, or in
# what shape.
CANDIDATES = [
    ("referee-assignments page", "https://official.nba.com/referee-assignments/"),
    ("official.nba.com root", "https://official.nba.com/"),
    ("data.nba.com today.json (legacy)", "https://data.nba.com/data/10s/prod/v1/today.json"),
]

CREW_KEYWORDS = ("referee", "official", "crew", "assignment")

_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


def fetch_raw(url, retries=3):
    """GET url, returning (status, content_type, body_text, error). Never
    raises -- every failure mode (connection refused, timeout, non-200,
    non-decodable body) is captured and reported, not hidden."""
    delay = 2.0
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                status = resp.getcode()
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read()
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError:
                body = raw.decode("utf-8", "replace")
            return status, ctype, body, None
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            return e.code, e.headers.get("Content-Type", "") if e.headers else "", body, None
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    return None, None, None, last_err


def probe_one(label, url):
    emit("\n--- %s ---" % label)
    emit("URL: %s" % url)
    status, ctype, body, err = fetch_raw(url)
    if err is not None:
        emit("UNREACHABLE: %s: %s" % (type(err).__name__, err))
        return {"label": label, "url": url, "reachable": False}

    emit("HTTP status: %s" % status)
    emit("Content-Type: %s" % ctype)
    emit("Body length: %d bytes" % len(body or ""))

    is_json = "json" in (ctype or "").lower()
    parsed = None
    if is_json or (body and body.strip()[:1] in "{["):
        try:
            parsed = json.loads(body)
            emit("Body parses as JSON. Top-level type: %s" % type(parsed).__name__)
            if isinstance(parsed, dict):
                emit("Top-level keys: %s" % list(parsed.keys())[:20])
            elif isinstance(parsed, list):
                emit("List length: %d; first element type: %s"
                     % (len(parsed), type(parsed[0]).__name__ if parsed else "n/a"))
        except ValueError as e:
            emit("Content-Type/leading char suggested JSON but json.loads FAILED: %s" % e)
    else:
        emit("Body does not look like JSON -- treating as HTML/text.")

    hits = [kw for kw in CREW_KEYWORDS if kw in (body or "").lower()]
    emit("Keyword scan (%s): found %s" % (", ".join(CREW_KEYWORDS), hits or "none"))
    emit("First 300 chars of body:")
    emit("  %r" % (body or "")[:300])

    return {"label": label, "url": url, "reachable": True, "status": status,
            "is_json": bool(parsed is not None), "keyword_hits": hits}


def main():
    emit("NBA assignments reachability + shape probe (probe_nba_assignments.py)")
    emit("=" * 70)
    emit("Run from: Jorge's local Windows machine")
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("Read-only. Writes nothing to games/officials/player_logs extracts.")

    results = [probe_one(label, url) for label, url in CANDIDATES]

    emit("\n" + "=" * 70)
    emit("LOCAL ENVIRONMENT SUMMARY")
    reachable = [r for r in results if r.get("reachable")]
    emit("reachable: %d/%d candidates" % (len(reachable), len(results)))
    for r in results:
        if r.get("reachable"):
            emit("  OK    %-32s HTTP %s  json=%s  keyword_hits=%s"
                 % (r["label"], r["status"], r["is_json"], r["keyword_hits"]))
        else:
            emit("  FAIL  %-32s unreachable" % r["label"])
    if any(r.get("keyword_hits") for r in reachable):
        emit("At least one candidate's body mentions referee/official/crew/assignment "
             "keywords -- worth a closer look for September's real parser.")
    else:
        emit("No candidate showed assignment-shaped content -- expected in July "
             "(off-season); revisit this probe closer to opening night.")

    os.makedirs(SOURCE_DIR, exist_ok=True)
    # APPEND (not overwrite): the cloud session's section, if present, stays;
    # this adds the local-environment section below it.
    with open(OUT_PATH, "a", encoding="utf-8") as f:
        f.write("\n\n" + "\n".join(_lines) + "\n")
    print("\n-> appended local-environment findings to %s"
          % os.path.relpath(OUT_PATH, REPO_ROOT))


if __name__ == "__main__":
    main()
