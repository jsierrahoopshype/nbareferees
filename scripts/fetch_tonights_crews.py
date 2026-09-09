"""
fetch_tonights_crews.py  --  builds data/tonights-crews.json, the live feed
behind the home page's "Tonight's Officials" module.

Source (found by the September source probes, see
source-data/_probe_rest_namespaces.txt):

    https://official.nba.com/wp-json/api/v1/get-game-officials?date=YYYY-MM-DD

It returns clean JSON -- nba.Table.rows[], one row per game, each carrying the
game id/date/code, home and away team ids and abbreviations, and official1..4
(name, code, jersey number). Unlike stats.nba.com and ESPN, it answers from a
GitHub Actions runner, which is what lets this run as a scheduled job rather
than on a laptop.

OUTPUT SHAPE -- assets/app.js documents and consumes:

    {date, games:[{away, home, tipoff_et, crew:[{name, slug}], crew_note}]}

That contract is met exactly. This script adds extra keys the frontend
ignores (generated_at, source, game_id, official_code, jersey, unmatched,
official_code_index); nothing documented was renamed or dropped. Two notes on
the fit:

  - The payload carries no tip-off time, so tipoff_et is "" unless a
    time-shaped field turns up in the response (one is looked for). The
    frontend renders the slot empty rather than wrong.
  - official4 is null in the regular season and is the ALTERNATE when present.
    Site-wide convention counts a crew as the first three officials
    (alternates excluded), so crew[] holds officials 1-3 and the alternate
    goes to crew_note as "Alternate: <name>" rather than being silently
    dropped.

OFF-SEASON / NO GAMES: a valid file is still written, with games: []. app.js
then keeps the server-rendered fallback (the most recent real game day, from
data/dashboard.json) -- it does NOT blank the module. That is the existing
designed behavior and this script does not change it.

REFEREE MATCHING: each official's name is matched to a canonical slug from
data/referees.json using build.py's normalization rules plus
data/referee_identity_overrides.csv. Anything unmatched is REPORTED, never
guessed: the name still renders, just without a link, and it is listed in the
output's "unmatched" array. The endpoint's official_code values are NBA person
ids we have never mapped; they are recorded per official and in
official_code_index for a future canonical-id round, and are deliberately not
used for matching yet.

  python scripts/fetch_tonights_crews.py                      # today, US Eastern
  python scripts/fetch_tonights_crews.py --date 2026-04-10
  python scripts/fetch_tonights_crews.py --from-file raw.json # offline, no network
  python scripts/fetch_tonights_crews.py --save-raw raw.json

Writes exactly one file: data/tonights-crews.json (--out to change). On a
failed fetch it writes NOTHING and exits non-zero, so a network blip leaves
the previous day's file in place rather than replacing it with an empty one.
Stdlib only -- no third-party imports, so CI needs no install step.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
DATA_DIR = os.path.join(REPO_ROOT, "data")
REFEREES_JSON = os.path.join(DATA_DIR, "referees.json")
OVERRIDES_CSV = os.path.join(DATA_DIR, "referee_identity_overrides.csv")
DEFAULT_OUT = os.path.join(DATA_DIR, "tonights-crews.json")

ENDPOINT = "https://official.nba.com/wp-json/api/v1/get-game-officials"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT = 30
RETRIES = 3

# Mirrors build.py's NAME_SUFFIXES / norm_ref_key. Duplicated rather than
# imported so this stays stdlib-only (build.py pulls in pandas, which CI would
# then have to install); keep the two in step if build.py's rules change.
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_ref_key(name):
    """Canonical referee key -- same rules as build.py's norm_ref_key:
    lowercase, drop accents, strip periods/apostrophes, collapse whitespace
    and hyphens, drop Jr/Sr/II/III. Middle initials are deliberately KEPT."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = s.lower().replace(".", " ").replace("'", "")
    s = re.sub(r"[^a-z0-9\s-]", " ", s).replace("-", " ")
    toks = [t for t in s.split() if t and t not in NAME_SUFFIXES]
    return "-".join(toks)


def collapse_initials(key):
    """Drop single-letter tokens (build.py uses this to surface audit pairs)."""
    return "-".join(t for t in key.split("-") if len(t) > 1)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def us_eastern_today():
    """Today's date on the NBA's scheduling clock (US Eastern).

    zoneinfo needs the tzdata package on Windows, where it is often missing,
    so fall back to the US DST rule (second Sunday in March to first Sunday in
    November) rather than silently using UTC and rolling the date over early.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 - no tzdata; compute the offset ourselves
        utc = datetime.datetime.now(datetime.timezone.utc)
        year = utc.year

        def nth_sunday(month, nth):
            d = datetime.date(year, month, 1)
            d += datetime.timedelta(days=(6 - d.weekday()) % 7)   # first Sunday
            return d + datetime.timedelta(days=7 * (nth - 1))

        dst_start = datetime.datetime.combine(nth_sunday(3, 2), datetime.time(7))   # 2am ET = 07:00 UTC
        dst_end = datetime.datetime.combine(nth_sunday(11, 1), datetime.time(6))    # 2am ET = 06:00 UTC
        naive_utc = utc.replace(tzinfo=None)
        offset = -4 if dst_start <= naive_utc < dst_end else -5
        return (naive_utc + datetime.timedelta(hours=offset)).date()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def decode_body(raw, content_encoding):
    enc = (content_encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001
        return raw
    return raw


def fetch_json(url, verbose=True):
    """GET url and parse JSON. Returns (payload, raw_text). Raises on failure --
    the caller must not write an output file when this fails."""
    delay = 2.0
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                hdrs = dict(resp.headers)
                status = resp.getcode()
            body = decode_body(raw, hdrs.get("Content-Encoding", hdrs.get("content-encoding", "")))
            text = body.decode("utf-8", "replace")
            if status != 200:
                raise RuntimeError("HTTP %s from %s" % (status, url))
            return json.loads(text), text
        except Exception as exc:  # noqa: BLE001
            last = exc
            if verbose:
                print("  attempt %d/%d failed: %s: %s" % (attempt, RETRIES, type(exc).__name__, exc))
            if attempt < RETRIES:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError("fetch failed after %d attempts: %s" % (RETRIES, last))


# --------------------------------------------------------------------------- #
# Payload walking -- tolerant, because the exact key casing is not guaranteed
# --------------------------------------------------------------------------- #
def find_rows(payload):
    """Locate the game rows.

    The documented path is nba.Table.rows, but the endpoint is undocumented
    and its casing could change, so fall back to the first list-of-dicts under
    any key named 'rows' (then any list of dicts that looks like game rows).
    """
    if isinstance(payload, dict):
        node = payload
        for key in ("nba", "Table", "rows"):
            nxt = get_ci(node, key)
            if nxt is None:
                node = None
                break
            node = nxt
        if isinstance(node, list):
            return node, "nba.Table.rows"

    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() == "rows" and isinstance(v, list) and v and isinstance(v[0], dict):
                    found.append((v, path + "." + k))
                walk(v, path + "." + k)
        elif isinstance(node, list) and node and isinstance(node[0], dict):
            keys = {k.lower() for k in node[0]}
            if keys & {"game_id", "gameid"} and any("official" in k for k in keys):
                found.append((node, path + "[]"))

    walk(payload, "")
    if found:
        return found[0]
    return None, None


def get_ci(d, *names):
    """Case/underscore-insensitive dict lookup: get_ci(row, 'game_id','gameId')."""
    if not isinstance(d, dict):
        return None
    flat = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in d.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in flat:
            return flat[key]
    return None


def official_from_row(row, index):
    """Pull officialN out of a row, tolerating nested or flat layouts.

    Nested:  {"official1": {"name": ..., "code": ..., "jersey_number": ...}}
    Flat:    {"official1": "Scott Foster", "official1_code": 1234,
              "official1_jersey_num": 48}
    Returns {"name","code","jersey"} or None when the slot is empty.
    """
    base = get_ci(row, "official%d" % index, "official_%d" % index)
    name = code = jersey = None
    if isinstance(base, dict):
        name = get_ci(base, "name", "official_name", "full_name", "display_name")
        code = get_ci(base, "code", "official_code", "person_id", "id")
        jersey = get_ci(base, "jersey_number", "jersey_num", "jersey", "number")
    elif isinstance(base, str):
        name = base
    if not name:
        name = get_ci(row, "official%d_name" % index, "official_%d_name" % index)
    if code is None:
        code = get_ci(row, "official%d_code" % index, "official_%d_code" % index,
                      "official%d_id" % index)
    if jersey is None:
        jersey = get_ci(row, "official%d_jersey_num" % index, "official%d_jersey" % index,
                        "official_%d_jersey_num" % index, "official%d_number" % index)
    name = (str(name).strip() if name is not None else "")
    if not name or name.lower() in ("none", "null", "n/a", "-"):
        return None
    return {"name": name,
            "code": str(code).strip() if code not in (None, "") else None,
            "jersey": str(jersey).strip() if jersey not in (None, "") else None}


TEAM_FROM_CODE = re.compile(r"^\d{8}/([A-Z]{3})([A-Z]{3})$")


def teams_from_row(row):
    """(away_abbr, home_abbr). Prefers explicit fields; falls back to game_code,
    whose NBA convention is YYYYMMDD/AWAYHOME."""
    away = get_ci(row, "away_team_abbreviation", "away_team_abbrev", "away_team_abbr",
                  "away_team_tricode", "visitor_team_abbreviation", "away_abbr", "away_team")
    home = get_ci(row, "home_team_abbreviation", "home_team_abbrev", "home_team_abbr",
                  "home_team_tricode", "home_abbr", "home_team")
    code = get_ci(row, "game_code", "gamecode")
    if (not away or not home) and code:
        m = TEAM_FROM_CODE.match(str(code).strip().upper())
        if m:
            away = away or m.group(1)
            home = home or m.group(2)
    return (str(away).strip().upper() if away else ""), (str(home).strip().upper() if home else "")


TIME_FIELDS = ("game_time_et", "game_time", "gametime", "tipoff", "tip_off",
               "tipoff_time", "start_time", "game_status_text", "status_text",
               "arena_local_time")


def tipoff_from_row(row):
    """The payload documented to us has no tip-off time; look anyway, since an
    undocumented endpoint may carry one, and return "" rather than inventing."""
    for field in TIME_FIELDS:
        val = get_ci(row, field)
        if val and str(val).strip() and str(val).strip().lower() not in ("none", "null"):
            return str(val).strip()
    return ""


# --------------------------------------------------------------------------- #
# Referee matching
# --------------------------------------------------------------------------- #
def load_referee_index():
    """key -> {'slug','name'} for canonical referees, plus the override map."""
    with open(REFEREES_JSON, "r", encoding="utf-8") as fh:
        refs = json.load(fh)
    by_key, by_collapsed = {}, {}
    for r in refs:
        slug, name = r.get("slug"), r.get("name")
        if not slug or not name:
            continue
        entry = {"slug": slug, "name": name, "active": bool(r.get("active"))}
        by_key.setdefault(norm_ref_key(name), entry)
        by_key.setdefault(slug, entry)
        by_collapsed.setdefault(collapse_initials(norm_ref_key(name)), []).append(entry)

    overrides = {}
    if os.path.exists(OVERRIDES_CSV):
        with open(OVERRIDES_CSV, "r", encoding="utf-8") as fh:
            rows = [ln for ln in fh if not ln.lstrip().startswith("#")]
        for row in csv.DictReader(rows):
            raw = (row.get("raw_name_or_id") or "").strip()
            canon = (row.get("canonical_ref_key") or "").strip()
            if raw and canon:
                overrides[norm_ref_key(raw)] = canon
    return by_key, by_collapsed, overrides


def suggest_candidates(name, by_key, limit=3):
    """Canonical referees sharing this name's last token. REPORTED ONLY -- a
    surname match is not enough to link on, but it saves a manual lookup when
    deciding whether an override row is warranted."""
    toks = norm_ref_key(name).split("-")
    if not toks:
        return []
    surname = toks[-1]
    out = []
    for entry in by_key.values():
        if entry["slug"] in out:
            continue
        if norm_ref_key(entry["name"]).split("-")[-1] == surname:
            out.append(entry["slug"])
        if len(out) >= limit:
            break
    return out


def match_official(name, by_key, by_collapsed, overrides):
    """Return (slug_or_None, match_type). Never guesses silently: an
    initials-collapse match is reported as 'collapsed' and only accepted when
    exactly one canonical referee matches."""
    key = norm_ref_key(name)
    if key in overrides:
        canon = overrides[key]
        if canon in by_key:
            return by_key[canon]["slug"], "override"
        return canon, "override-key"
    if key in by_key:
        return by_key[key]["slug"], "exact"
    candidates = by_collapsed.get(collapse_initials(key), [])
    uniq = {c["slug"]: c for c in candidates}
    if len(uniq) == 1:
        return list(uniq.values())[0]["slug"], "collapsed"
    return None, "unmatched"


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_payload(rows, date_iso, source_url, quiet=False):
    by_key, by_collapsed, overrides = load_referee_index()
    games, unmatched, code_index = [], [], {}
    fuzzy = []

    for row in rows or []:
        away, home = teams_from_row(row)
        game_id = get_ci(row, "game_id", "gameid")
        game_code = get_ci(row, "game_code", "gamecode")
        officials = [o for o in (official_from_row(row, i) for i in (1, 2, 3, 4)) if o]

        crew = []
        for off in officials[:3]:
            slug, how = match_official(off["name"], by_key, by_collapsed, overrides)
            member = {"name": off["name"], "slug": slug}
            if off["code"]:
                member["official_code"] = off["code"]
            if off["jersey"]:
                member["jersey"] = off["jersey"]
            crew.append(member)
            if how == "unmatched":
                unmatched.append({"name": off["name"], "official_code": off["code"],
                                  "normalized_key": norm_ref_key(off["name"]),
                                  "game_id": str(game_id) if game_id else None,
                                  "candidates": suggest_candidates(off["name"], by_key)})
            elif how == "collapsed":
                fuzzy.append((off["name"], slug))
            if off["code"]:
                code_index.setdefault(off["code"], {"name": off["name"], "slug": slug})

        # official4 is the alternate when present (playoffs); site convention
        # counts the crew as the first three, so name the alternate in the note
        # instead of dropping it.
        note = ""
        if len(officials) > 3:
            alt = officials[3]
            alt_slug, alt_how = match_official(alt["name"], by_key, by_collapsed, overrides)
            note = "Alternate: %s" % alt["name"]
            if alt_how == "unmatched":
                unmatched.append({"name": alt["name"], "official_code": alt["code"],
                                  "normalized_key": norm_ref_key(alt["name"]),
                                  "game_id": str(game_id) if game_id else None,
                                  "candidates": suggest_candidates(alt["name"], by_key)})
            if alt["code"]:
                code_index.setdefault(alt["code"], {"name": alt["name"], "slug": alt_slug})

        game = {"away": away, "home": home, "tipoff_et": tipoff_from_row(row),
                "crew": crew}
        if note:
            game["crew_note"] = note
        if game_id:
            game["game_id"] = str(game_id)
        if game_code:
            game["game_code"] = str(game_code)
        games.append(game)

    games.sort(key=lambda g: (g.get("game_id") or "", g["away"], g["home"]))

    payload = {
        "date": date_iso,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": source_url,
        "game_count": len(games),
        "games": games,
        # For the future canonical-id round: NBA person ids seen today, with
        # the slug we matched by NAME (null where we could not).
        "official_code_index": code_index,
        "unmatched": unmatched,
    }
    if not games:
        payload["note"] = ("No games on this date (off-season or a dark night). "
                           "app.js keeps the server-rendered fallback.")
    if not quiet:
        report(payload, fuzzy)
    return payload


def report(payload, fuzzy):
    print("")
    print("date          : %s" % payload["date"])
    print("games         : %d" % payload["game_count"])
    total = sum(len(g["crew"]) for g in payload["games"])
    linked = sum(1 for g in payload["games"] for c in g["crew"] if c.get("slug"))
    print("crew slots    : %d (%d linked to a referee page)" % (total, linked))
    alts = [g["crew_note"] for g in payload["games"] if g.get("crew_note")]
    if alts:
        print("alternates    : %d game(s) with a 4th official -> crew_note" % len(alts))
    if fuzzy:
        print("")
        print("MATCHED VIA INITIALS-COLLAPSE (unique match, but eyeball these):")
        for name, slug in fuzzy:
            print("  %-28s -> %s" % (name, slug))
    if payload["unmatched"]:
        print("")
        print("UNMATCHED -- rendered without a link, NOT guessed:")
        for u in payload["unmatched"]:
            print("  %-28s official_code=%-8s key=%s"
                  % (u["name"], u["official_code"] or "-", u["normalized_key"]))
            if u.get("candidates"):
                print("  %-28s   same surname on file: %s (suggestion only, not applied)"
                      % ("", ", ".join(u["candidates"])))
        print("  Fix by adding a row to data/referee_identity_overrides.csv, or")
        print("  leave for the canonical-id round (codes are in official_code_index).")
    else:
        print("unmatched     : none")
    missing_tip = [g for g in payload["games"] if not g.get("tipoff_et")]
    if missing_tip:
        print("")
        print("note: %d/%d games have no tip-off time in the payload; the frontend's"
              % (len(missing_tip), payload["game_count"]))
        print("      crew-tip slot renders empty for those.")


def write_output(payload, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def main():
    ap = argparse.ArgumentParser(description="Build data/tonights-crews.json from official.nba.com.")
    ap.add_argument("--date", help="game date YYYY-MM-DD (default: today, US Eastern)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output path (default data/tonights-crews.json)")
    ap.add_argument("--endpoint", default=ENDPOINT, help="override the endpoint URL")
    ap.add_argument("--from-file", help="read the raw endpoint JSON from a file instead of fetching")
    ap.add_argument("--save-raw", help="also save the raw endpoint response to this path")
    ap.add_argument("--quiet", action="store_true", help="suppress the matching report")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass

    date_iso = args.date or us_eastern_today().isoformat()
    try:
        datetime.date.fromisoformat(date_iso)
    except ValueError:
        print("ERROR: --date must be YYYY-MM-DD, got %r" % date_iso)
        sys.exit(2)

    url = "%s?date=%s" % (args.endpoint, urllib.parse.quote(date_iso))
    print("Tonight's crews for %s" % date_iso)

    if args.from_file:
        print("Reading raw payload from %s (offline)" % args.from_file)
        with open(args.from_file, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
        payload_in = json.loads(raw_text)
        source = "file:%s" % os.path.basename(args.from_file)
    else:
        print("GET %s" % url)
        try:
            payload_in, raw_text = fetch_json(url)
        except Exception as exc:  # noqa: BLE001
            # Deliberately write nothing: a blip must not replace a good file.
            print("")
            print("FETCH FAILED: %s" % exc)
            print("No file written; %s left untouched." % os.path.relpath(args.out, REPO_ROOT))
            sys.exit(1)
        source = url

    if args.save_raw:
        with open(args.save_raw, "w", encoding="utf-8") as fh:
            fh.write(raw_text)
        print("raw response -> %s" % args.save_raw)

    rows, path = find_rows(payload_in)
    if rows is None:
        print("")
        print("ERROR: could not find game rows in the response.")
        print("Top-level keys: %s"
              % (list(payload_in)[:20] if isinstance(payload_in, dict) else type(payload_in).__name__))
        print("Expected nba.Table.rows[]. Re-run with --save-raw and inspect.")
        sys.exit(1)
    print("rows found at : %s (%d row(s))" % (path, len(rows)))

    payload = build_payload(rows, date_iso, source, quiet=args.quiet)
    write_output(payload, args.out)
    print("")
    print("-> wrote %s (%d game(s))" % (os.path.relpath(args.out, REPO_ROOT), payload["game_count"]))


if __name__ == "__main__":
    main()
