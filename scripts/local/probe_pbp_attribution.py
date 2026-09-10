"""
probe_pbp_attribution.py  --  LOCAL, READ-ONLY probe of per-referee foul
attribution in the nbadb play-by-play table.

  python scripts\\local\\probe_pbp_attribution.py "C:\\Users\\Jorge Sierra\\Downloads\\archive\\nba.sqlite"

The play-by-play table has no official column, but the calling referee is
embedded in the description text as a trailing parenthetical:

    "Brown P.FOUL (P1.T1) (R.Garretson)"
    "Covington S.FOUL (P1.T2) (S.Foster)"

THIS IS A PROBE, NOT A BUILD. It answers what is actually in there before
anything is designed on top of it, and writes nothing to any extract.

What it reports:
  1. Per-season attribution coverage -- for every season in the file, the share
     of foul events carrying a referee name, so the real start of coverage and
     its completeness are visible rather than assumed (the NBA is said to have
     begun publishing referee names with the 2015 playoffs; this measures it).
  2. The full event vocabulary that carries attribution, grouped by the NBA's
     OWN event codes (eventmsgtype, eventmsgactiontype) rather than by guessing
     at the text -- so technicals, ejections and violations show up on their
     own terms, with sample descriptions.
  3. Every distinct referee name-form ("R.Garretson"), with the seasons and
     volume each appears in, checked against our 166 canonical referees:
     forms that match exactly one, forms that COLLIDE (J.Capers could be James
     Capers Sr. or Jr.), and forms that match nothing.
  4. The paired-turnover question: "Simmons Offensive Foul Turnover (P2.T4)" is
     the turnover half of an offensive foul and carries no referee by design.
     Pass 2 pairs those rows against the foul row at the same game/period/clock
     and reports how many are paired -- so the parser can exclude them instead
     of scoring them as missing attribution.
  5. Samples for every pattern found.

Nothing about the parenthetical is assumed. Every parenthetical group in every
description is shape-classified (letters -> A/a, digits -> #) and the shape
census is reported, so a form nobody predicted shows up as its own row rather
than being silently dropped by a regex written in advance.

Follows extract_from_nbadb.py's conventions: the table is located by column
signature, never by name, and game_id is treated as a zero-padded 10-char
string throughout.

Options:
  --limit N          stop after N play-by-play rows (quick look)
  --seasons 2014-15,2015-16     restrict to these seasons
  --skip-pairing     skip pass 2 (the offensive-foul/turnover pairing)
  --samples N        samples kept per pattern (default 3)

Stdlib only (sqlite3, re) -- no pandas, so the scan streams instead of loading
13.6M rows into memory. Report goes to stdout and appends to
source-data/_probe_pbp_attribution.txt.

Runtime: pass 1 measured at ~130k rows/sec on a synthetic table, so roughly 2
minutes for 13.6M rows. Pass 2 adds a filtered, ORDERed query; SQLite may have
to sort a few million rows without an index, so if it drags, --skip-pairing
gets everything else.
"""

import argparse
import collections
import datetime
import json
import os
import re
import sqlite3
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
REFEREES_JSON = os.path.join(REPO_ROOT, "data", "referees.json")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_pbp_attribution.txt")

DEFAULT_DB = r"C:\Users\Jorge Sierra\Downloads\archive\nba.sqlite"

SEASON_TYPE_MAP = {"1": "Pre", "2": "RS", "3": "AS", "4": "PO", "5": "PI"}

# A referee token: one or more initials, then a surname. Deliberately loose on
# the surname (apostrophes, hyphens, internal spaces for "Van Duyne") and
# deliberately strict on the initials, so "P1.T1" cannot match -- a digit
# immediately after the first letter disqualifies it.
REF_TOKEN_RE = re.compile(r"^[A-Z](?:\.[A-Z])*\.\s?[A-Za-z][A-Za-z'`\u2019\-]*(?:[ \-][A-Za-z'`\u2019\-]+)*\.?$")
PAREN_RE = re.compile(r"\(([^()]*)\)")

PROGRESS_EVERY = 1000000

_lines = []


def emit(msg=""):
    text = str(msg)
    safe = text.encode("ascii", "backslashreplace").decode("ascii")
    print(safe)
    _lines.append(safe)


def section(title):
    emit("")
    emit("=" * 78)
    emit(title)
    emit("=" * 78)


def norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


_SHAPE_CACHE = {}


def shape_of(text):
    """Memoized wrapper -- the parenthetical vocabulary repeats enormously
    across 13.6M rows ("P1.T1" alone appears millions of times), and the
    per-character loop below is the hot path of the whole scan."""
    cached = _SHAPE_CACHE.get(text)
    if cached is not None:
        return cached
    shape = _shape_of_uncached(text)
    if len(_SHAPE_CACHE) < 200000:
        _SHAPE_CACHE[text] = shape
    return shape


def _shape_of_uncached(text):
    """Compact shape signature: letters -> A/a, digits -> #, runs collapsed.

    "R.Garretson" -> "A.Aa+" ; "P1.T1" -> "A#.A#" ; "24 SEC" -> "#+ A+"
    Lets an unexpected parenthetical form appear as its own row in the census
    instead of being silently swallowed by a regex written in advance.
    """
    out = []
    for ch in text:
        if ch.isdigit():
            c = "#"
        elif ch.isupper():
            c = "A"
        elif ch.islower():
            c = "a"
        else:
            c = ch
        if out and out[-1][0] == c and c in "Aa#":
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return "".join(c if n == 1 else c + "+" for c, n in out)


def season_start_year_from_gid(gid):
    """Digits 4-5 of game_id give the season start year (pivot at 46)."""
    if not gid or len(gid) < 5:
        return None
    try:
        yy = int(gid[3:5])
    except ValueError:
        return None
    return 1900 + yy if yy >= 46 else 2000 + yy


def season_str(year):
    return None if year is None else "{}-{:02d}".format(year, (year + 1) % 100)


def season_type_from_gid(gid):
    if not gid or len(gid) < 3:
        return "??"
    return SEASON_TYPE_MAP.get(gid[2], "??")


def pad_gid(value):
    """game_id as a zero-padded 10-char string -- the project's #1 join risk."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(10) if s else ""


# --------------------------------------------------------------------------- #
# Schema introspection -- locate by column signature, never by name
# --------------------------------------------------------------------------- #
def introspect(conn):
    info = {}
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    for (table,) in cur.fetchall():
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)]
        except sqlite3.DatabaseError:
            continue
        if cols:
            info[table] = cols
    return info


def locate_pbp(info):
    """The play-by-play table: has home/visitor description columns and a game id."""
    best, best_score = None, 0
    for table, cols in info.items():
        n = {norm(c) for c in cols}
        score = 0
        score += 3 * sum(1 for c in n if "description" in c)
        score += 2 * any("eventmsgtype" in c for c in n)
        score += 1 * any("eventnum" in c for c in n)
        score += 1 * any(c in ("gameid", "gameid10") for c in n)
        if score > best_score:
            best, best_score = table, score
    return best


def locate_games(info):
    """A per-game table carrying a date and/or season, for cross-checking the
    season decoded from game_id. Optional -- the decode stands on its own."""
    best, best_score = None, 0
    for table, cols in info.items():
        n = {norm(c) for c in cols}
        if any("playerid" in c for c in n):
            continue
        if not any(c.startswith("gameid") for c in n):
            continue
        score = 0
        score += any("gamedate" in c for c in n)
        score += any(c == "season" or "seasonid" in c or "seasonyear" in c for c in n)
        score += any("hometeamid" in c or "teamidhome" in c for c in n)
        if score > best_score:
            best, best_score = table, score
    return best


def pick(cols, *patterns):
    """First column whose normalized name matches a pattern (exact, then substring)."""
    ncols = [(c, norm(c)) for c in cols]
    for pat in patterns:
        for c, nc in ncols:
            if nc == pat:
                return c
    for pat in patterns:
        for c, nc in ncols:
            if pat in nc:
                return c
    return None


# --------------------------------------------------------------------------- #
# Description analysis
# --------------------------------------------------------------------------- #
def analyze_description(desc):
    """(ref_tokens, groups) for one description.

    groups is every parenthetical in order; ref_tokens are those matching the
    referee shape, with their positions, so "is the referee always last?" is a
    measured fact rather than an assumption.
    """
    if not desc:
        return [], []
    groups = PAREN_RE.findall(desc)
    refs = [(i, g.strip()) for i, g in enumerate(groups) if REF_TOKEN_RE.match(g.strip())]
    return refs, groups


class Counters(object):
    def __init__(self, samples):
        self.rows = 0
        self.rows_with_desc = 0
        self.rows_both_desc = 0
        self.rows_with_ref = 0
        self.rows_multi_ref = 0
        self.ref_not_last = 0
        # per season: [foul rows, foul rows with ref, all rows, all rows with ref]
        self.season = collections.defaultdict(lambda: [0, 0, 0, 0])
        self.season_type = collections.defaultdict(lambda: [0, 0])
        # (eventmsgtype, eventmsgactiontype) -> [rows, rows_with_ref]
        self.events = collections.defaultdict(lambda: [0, 0])
        self.event_samples = collections.defaultdict(list)
        self.event_ref_samples = collections.defaultdict(list)
        self.paren_shapes = collections.Counter()
        self.paren_shape_samples = collections.defaultdict(list)
        self.last_shapes = collections.Counter()
        # name-form -> {"n": count, "seasons": Counter}
        self.forms = collections.defaultdict(lambda: {"n": 0, "seasons": collections.Counter()})
        self.foul_no_ref_samples = []
        self.turnover_rows = 0
        self.samples = samples

    def keep(self, bucket, key, value):
        lst = bucket[key]
        if len(lst) < self.samples and value not in lst:
            lst.append(value)


def scan(conn, pbp, cols, args, ctr):
    """Pass 1: stream the table, counting everything."""
    c_gid = pick(cols, "gameid")
    c_type = pick(cols, "eventmsgtype")
    c_action = pick(cols, "eventmsgactiontype")
    c_home = pick(cols, "homedescription")
    c_vis = pick(cols, "visitordescription")
    c_neutral = pick(cols, "neutraldescription")
    c_num = pick(cols, "eventnum")

    selected = [c for c in (c_gid, c_type, c_action, c_num, c_home, c_vis, c_neutral) if c]
    emit("columns used: %s" % ", ".join(selected))
    missing = [n for n, c in (("game_id", c_gid), ("eventmsgtype", c_type),
                              ("homedescription", c_home), ("visitordescription", c_vis))
               if not c]
    if missing:
        emit("WARNING: expected column(s) not found: %s -- results below are partial."
             % ", ".join(missing))

    sql = 'SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in selected), pbp)
    if args.limit:
        sql += " LIMIT %d" % args.limit
    emit("query: %s" % sql)
    emit("")

    want_seasons = set(args.seasons.split(",")) if args.seasons else None
    idx = {c: i for i, c in enumerate(selected)}

    for row in conn.execute(sql):
        ctr.rows += 1
        if ctr.rows % PROGRESS_EVERY == 0:
            print("    ...%d rows scanned" % ctr.rows)

        gid = pad_gid(row[idx[c_gid]]) if c_gid else ""
        season = season_str(season_start_year_from_gid(gid)) or "unknown"
        if want_seasons and season not in want_seasons:
            continue
        stype = season_type_from_gid(gid)

        home = row[idx[c_home]] if c_home else None
        vis = row[idx[c_vis]] if c_vis else None
        neu = row[idx[c_neutral]] if c_neutral else None
        descs = [d for d in (home, vis, neu) if d]
        if home and vis:
            ctr.rows_both_desc += 1
        if not descs:
            ctr.season[season][2] += 1
            continue
        ctr.rows_with_desc += 1

        # One row is one event: scan every description it carries, but count
        # the row once, whichever side the text sits on.
        all_refs, all_groups = [], []
        for d in descs:
            refs, groups = analyze_description(d)
            all_refs.extend(refs)
            all_groups.extend(groups)
            if groups:
                last = groups[-1].strip()
                ctr.last_shapes[shape_of(last)] += 1
            for g in groups:
                g = g.strip()
                sh = shape_of(g)
                ctr.paren_shapes[sh] += 1
                ctr.keep(ctr.paren_shape_samples, sh, g)

        primary = descs[0]
        has_ref = bool(all_refs)
        is_foul = "foul" in primary.lower()
        if "turnover" in primary.lower():
            ctr.turnover_rows += 1

        s = ctr.season[season]
        s[2] += 1
        if has_ref:
            s[3] += 1
        if is_foul:
            s[0] += 1
            if has_ref:
                s[1] += 1
        st = ctr.season_type[(season, stype)]
        st[0] += 1
        if has_ref:
            st[1] += 1

        if has_ref:
            ctr.rows_with_ref += 1
            if len(all_refs) > 1:
                ctr.rows_multi_ref += 1
            for pos, token in all_refs:
                # Is the referee always the LAST parenthetical? Measured, not assumed.
                if all_groups and token != all_groups[-1].strip():
                    ctr.ref_not_last += 1
                    break
            for _, token in all_refs:
                f = ctr.forms[token]
                f["n"] += 1
                f["seasons"][season] += 1
        elif is_foul and len(ctr.foul_no_ref_samples) < 25:
            ctr.foul_no_ref_samples.append((season, primary))

        ev = (row[idx[c_type]] if c_type else None,
              row[idx[c_action]] if c_action else None)
        e = ctr.events[ev]
        e[0] += 1
        ctr.keep(ctr.event_samples, ev, primary[:150])
        if has_ref:
            e[1] += 1
            ctr.keep(ctr.event_ref_samples, ev, primary[:150])


# --------------------------------------------------------------------------- #
# Pass 2 -- the paired-turnover question
# --------------------------------------------------------------------------- #
def pairing_pass(conn, pbp, cols, args):
    """Are 'Offensive Foul Turnover' rows paired with a foul row that carries
    the referee? Pulls only foul/turnover rows, ordered, and pairs them on
    (game_id, period, clock)."""
    section("4. PAIRED TURNOVER ROWS (the double-count trap)")
    c_gid = pick(cols, "gameid")
    c_period = pick(cols, "period")
    c_clock = pick(cols, "pctimestring")
    c_num = pick(cols, "eventnum")
    c_home = pick(cols, "homedescription")
    c_vis = pick(cols, "visitordescription")
    if not (c_gid and c_period and c_clock):
        emit("Skipped: need game_id, period and pctimestring columns; not all present.")
        return

    selected = [c for c in (c_gid, c_period, c_clock, c_num, c_home, c_vis) if c]
    where = " OR ".join(
        'IFNULL("%s",\'\') LIKE %s' % (c, p)
        for c in (c_home, c_vis) if c
        for p in ("'%Foul%'", "'%FOUL%'", "'%Turnover%'"))
    sql = 'SELECT %s FROM "%s" WHERE %s ORDER BY "%s", "%s"' % (
        ", ".join('"%s"' % c for c in selected), pbp, where, c_gid, c_num or c_period)
    emit("Pulling only foul/turnover rows, ordered by game and event number, and")
    emit("pairing them on (game_id, period, clock).")
    emit("")

    idx = {c: i for i, c in enumerate(selected)}
    bucket = collections.defaultdict(lambda: {"foul_ref": 0, "foul_noref": 0,
                                              "foul_turnover": 0, "samples": []})
    stats = collections.Counter()
    examples = []
    current_key = None
    group = None

    def flush(key, g):
        if not g or not g["foul_turnover"]:
            return
        stats["foul_turnover_rows"] += g["foul_turnover"]
        if g["foul_ref"]:
            stats["paired_with_attributed_foul"] += g["foul_turnover"]
            if len(examples) < 6:
                examples.append((key, list(g["samples"])))
        elif g["foul_noref"]:
            stats["paired_with_unattributed_foul"] += g["foul_turnover"]
        else:
            stats["orphan_no_foul_row"] += g["foul_turnover"]
            if len(examples) < 8:
                examples.append((key, list(g["samples"])))

    for row in conn.execute(sql):
        gid = pad_gid(row[idx[c_gid]])
        key = (gid, row[idx[c_period]], row[idx[c_clock]])
        if key != current_key:
            flush(current_key, group)
            current_key, group = key, {"foul_ref": 0, "foul_noref": 0,
                                       "foul_turnover": 0, "samples": []}
        for c in (c_home, c_vis):
            if not c:
                continue
            d = row[idx[c]]
            if not d:
                continue
            low = d.lower()
            refs, _ = analyze_description(d)
            if "turnover" in low and "foul" in low:
                group["foul_turnover"] += 1
                if len(group["samples"]) < 4:
                    group["samples"].append(d[:110])
            elif "foul" in low:
                if refs:
                    group["foul_ref"] += 1
                    if len(group["samples"]) < 4:
                        group["samples"].append(d[:110])
                else:
                    group["foul_noref"] += 1
    flush(current_key, group)

    total = stats["foul_turnover_rows"]
    emit("rows whose text has both 'foul' and 'turnover' : %d" % total)
    if not total:
        emit("None found -- nothing to exclude.")
        return
    for label, key in (("paired with an ATTRIBUTED foul at the same clock", "paired_with_attributed_foul"),
                       ("paired with an unattributed foul (pre-2015, presumably)", "paired_with_unattributed_foul"),
                       ("no foul row at that clock at all (orphan)", "orphan_no_foul_row")):
        n = stats[key]
        emit("  %-52s %8d  (%5.1f%%)" % (label, n, 100.0 * n / total))
    emit("")
    emit("Reading: a paired row is the turnover HALF of an offensive foul. It")
    emit("carries no referee by design, so a parser must exclude these rather")
    emit("than count them as missing attribution -- and must not count the foul")
    emit("twice either.")
    emit("")
    emit("Examples (one clock, both halves):")
    for key, samples in examples:
        emit("  game %s  period %s  %s" % key)
        for smp in samples:
            emit("      %s" % smp)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def report_seasons(ctr):
    section("1. PER-SEASON ATTRIBUTION COVERAGE")
    emit("'Foul rows' = rows whose description contains 'foul' (case-insensitive),")
    emit("which includes the paired turnover rows counted in section 4 -- so the")
    emit("rate below is a FLOOR until those are excluded.")
    emit("")
    emit("%-10s %12s %12s %8s   %12s %12s %8s"
         % ("season", "foul rows", "with ref", "rate", "all rows", "with ref", "rate"))
    emit("-" * 78)
    first_covered = next((sn for sn in sorted(ctr.season) if ctr.season[sn][3] > 0), None)
    for season in sorted(ctr.season):
        fouls, fouls_ref, allrows, all_ref = ctr.season[season]
        frate = (100.0 * fouls_ref / fouls) if fouls else 0.0
        arate = (100.0 * all_ref / allrows) if allrows else 0.0
        flag = "  <-- first season with any attribution" if season == first_covered else ""
        emit("%-10s %12d %12d %7.1f%%   %12d %12d %7.1f%%%s"
             % (season, fouls, fouls_ref, frate, allrows, all_ref, arate, flag))

    emit("")
    emit("By season type (attribution rate over ALL rows), where it is non-zero:")
    emit("%-10s %-5s %12s %12s %8s" % ("season", "type", "rows", "with ref", "rate"))
    emit("-" * 78)
    for (season, stype) in sorted(ctr.season_type):
        rows, refs = ctr.season_type[(season, stype)]
        if not refs:
            continue
        emit("%-10s %-5s %12d %12d %7.1f%%"
             % (season, stype, rows, refs, 100.0 * refs / rows))
    emit("")
    emit("This is the check on 'coverage began with the 2015 playoffs': if that")
    emit("holds, 2014-15 PO is the first row above and 2014-15 RS is absent.")


def report_parens(ctr):
    section("2a. PARENTHETICAL VOCABULARY (every group, by shape)")
    emit("Shapes: A=uppercase run, a=lowercase run, #=digit run, + = 2 or more.")
    emit("So 'R.Garretson' is A.Aa+ and 'P1.T1' is A#.A#. Nothing is assumed to")
    emit("be a referee; the census shows what is actually in the text.")
    emit("")
    emit("%-22s %12s   %s" % ("shape", "count", "samples"))
    emit("-" * 78)
    for shape, n in ctr.paren_shapes.most_common(40):
        samples = ", ".join(ctr.paren_shape_samples[shape][:3])
        ref_like = " *** referee-shaped" if REF_TOKEN_RE.match(
            (ctr.paren_shape_samples[shape] or [""])[0]) else ""
        emit("%-22s %12d   %s%s" % (shape, n, samples[:44], ref_like))
    if len(ctr.paren_shapes) > 40:
        emit("... and %d more distinct shapes" % (len(ctr.paren_shapes) - 40))

    emit("")
    emit("Position check: is the referee always the LAST parenthetical?")
    emit("  rows with a referee token          : %d" % ctr.rows_with_ref)
    emit("  rows with MORE THAN ONE ref token  : %d" % ctr.rows_multi_ref)
    emit("  rows where a ref token is not last : %d" % ctr.ref_not_last)
    if not ctr.ref_not_last and not ctr.rows_multi_ref:
        emit("  -> 'last parenthetical, if it is referee-shaped' is a safe rule here.")
    else:
        emit("  -> NOT safe to take the last group blindly; see the counts above.")


def report_events(ctr, args):
    section("2b. EVENT VOCABULARY CARRYING ATTRIBUTION")
    emit("Grouped by the NBA's own codes (eventmsgtype, eventmsgactiontype), so")
    emit("this is the real scope -- fouls, technicals, ejections, violations --")
    emit("without guessing from the text.")
    emit("")
    rows = [(ev, v) for ev, v in ctr.events.items() if v[1] > 0]
    rows.sort(key=lambda kv: -kv[1][1])
    emit("%-8s %-8s %12s %12s %8s" % ("msgtype", "action", "rows", "with ref", "rate"))
    emit("-" * 78)
    for ev, (n, refs) in rows:
        emit("%-8s %-8s %12d %12d %7.1f%%" % (ev[0], ev[1], n, refs, 100.0 * refs / n))
        for smp in ctr.event_ref_samples[ev][:args.samples]:
            emit("        %s" % smp)
    if not rows:
        emit("No event type carried a referee token.")

    emit("")
    emit("Event types with NO attribution at all (top 15 by volume) -- these are")
    emit("what a parser must not treat as gaps:")
    emit("%-8s %-8s %12s   %s" % ("msgtype", "action", "rows", "sample"))
    emit("-" * 78)
    none_rows = [(ev, v) for ev, v in ctr.events.items() if v[1] == 0]
    none_rows.sort(key=lambda kv: -kv[1][0])
    for ev, (n, _) in none_rows[:15]:
        smp = (ctr.event_samples[ev] or [""])[0]
        emit("%-8s %-8s %12d   %s" % (ev[0], ev[1], n, smp[:52]))


def load_canonical():
    """canonical initial.surname forms -> [(slug, name, first, last season)]."""
    if not os.path.exists(REFEREES_JSON):
        return {}
    with open(REFEREES_JSON, "r", encoding="utf-8") as fh:
        refs = json.load(fh)
    out = collections.defaultdict(list)
    for r in refs:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        parts = name.split()
        if len(parts) < 2:
            continue
        surname = " ".join(parts[1:])
        entry = (r.get("slug"), name, r.get("first_season"), r.get("last_season"))
        # Prefixes to try. A first name spelled out ("Scott") contributes one
        # initial; a first name that IS initials ("J.T.", "JB") also contributes
        # its full dotted form, or "J.T. Orr" would never match "J.T.Orr".
        bare = parts[0].replace(".", "")
        prefixes = {parts[0][0]}
        if bare.isupper() and 1 < len(bare) <= 3:
            prefixes.add(".".join(list(bare)))
        for prefix in prefixes:
            for variant in {surname, surname.replace(" ", ""), surname.replace("-", "")}:
                key = (prefix + "." + variant).lower().replace(" ", "")
                if entry not in out[key]:
                    out[key].append(entry)
    return out


def report_forms(ctr):
    section("3. DISTINCT REFEREE NAME-FORMS, AND WHETHER THEY MAP")
    canonical = load_canonical()
    distinct_refs = len({e[0] for entries in canonical.values() for e in entries})
    emit("%d distinct name-form(s) observed." % len(ctr.forms))
    emit("Matched against the %d canonical referees in data/referees.json by"
         % distinct_refs)
    emit("first-initial + surname. NOTHING is auto-resolved here -- collisions are")
    emit("reported for a human to settle, which is the point of this section.")
    emit("")
    unique, collide, unknown = [], [], []
    for form, meta in ctr.forms.items():
        key = form.lower().replace(" ", "").replace("..", ".")
        cands = canonical.get(key, [])
        if len(cands) == 1:
            unique.append((form, meta, cands))
        elif len(cands) > 1:
            collide.append((form, meta, cands))
        else:
            unknown.append((form, meta, cands))

    def seasons_of(meta):
        ss = sorted(meta["seasons"])
        return "%s..%s" % (ss[0], ss[-1]) if ss else "-"

    def span_seasons(first, last):
        try:
            return int(str(last)[:4]) - int(str(first)[:4]) + 1
        except (TypeError, ValueError):
            return None

    emit("-- %d form(s) matching exactly ONE canonical referee --" % len(unique))
    long_spans = []
    for form, meta, cands in sorted(unique, key=lambda t: -t[1]["n"]):
        slug, name, first, last = cands[0]
        span = span_seasons(first, last)
        # Career span is worth surfacing but does NOT by itself mean two people
        # merged: 30-year officiating careers are real (Dick Bavetta worked 39).
        # It is a shortlist to eyeball, not a finding.
        warn = ""
        if span and span >= 28:
            warn = "  <-- %d-season span" % span
            long_spans.append((form, slug, name, first, last, span))
        emit("  %-22s %8d calls  %-18s -> %s%s"
             % (form, meta["n"], seasons_of(meta), slug, warn))
    if long_spans:
        emit("")
        emit("  Longest spans among the matched referees. Careers this long are")
        emit("  genuinely common in officiating (Dick Bavetta worked 39 seasons), so")
        emit("  this is NOT a claim that any of them is wrong. It is the shortlist")
        emit("  where a merged Sr./Jr. identity would hide -- and if one IS merged,")
        emit("  attribution cannot be split correctly however the name-form is")
        emit("  mapped. Worth checking against NBRA's roster, no more than that:")
        for form, slug, name, first, last, span in long_spans:
            emit("    %-22s %-24s %s..%s (%d seasons)" % (form, name, first, last, span))

    emit("")
    emit("-- %d form(s) COLLIDING with more than one canonical referee --" % len(collide))
    if not collide:
        emit("  none")
    for form, meta, cands in sorted(collide, key=lambda t: -t[1]["n"]):
        emit("  *** %-18s %8d calls  seen %s" % (form, meta["n"], seasons_of(meta)))
        for slug, name, first, last in cands:
            emit("        candidate: %-22s %-26s %s..%s" % (slug, name, first, last))
        emit("        -> the observed seasons above vs. each candidate's own span is")
        emit("           the tiebreak; where they overlap, this needs a manual call.")

    emit("")
    emit("-- %d form(s) matching NOTHING in referees.json --" % len(unknown))
    for form, meta, _ in sorted(unknown, key=lambda t: -t[1]["n"]):
        emit("  %-22s %8d calls  %s" % (form, meta["n"], seasons_of(meta)))
    if unknown:
        emit("  These are either officials missing from our canonical list, a")
        emit("  surname spelling that differs, or a parenthetical that merely looks")
        emit("  referee-shaped. Worth eyeballing before any mapping is built.")


def report_samples(ctr, args):
    section("5. SAMPLES")
    emit("-- foul rows WITHOUT a referee token (the gaps, or the paired halves) --")
    for season, desc in ctr.foul_no_ref_samples[:args.samples * 5]:
        emit("  [%s] %s" % (season, desc[:110]))
    if not ctr.foul_no_ref_samples:
        emit("  none")


def main():
    ap = argparse.ArgumentParser(description="Probe referee attribution in nbadb play-by-play.")
    ap.add_argument("db", nargs="?", default=DEFAULT_DB, help="path to nba.sqlite")
    ap.add_argument("--limit", type=int, help="stop after N play-by-play rows")
    ap.add_argument("--seasons", help="comma-separated season labels, e.g. 2014-15,2015-16")
    ap.add_argument("--samples", type=int, default=3, help="samples per pattern (default 3)")
    ap.add_argument("--skip-pairing", action="store_true", help="skip pass 2")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass

    if not os.path.exists(args.db):
        print("ERROR: no SQLite file at %s" % args.db)
        print("Pass the path as the first argument.")
        sys.exit(1)

    emit("nbadb play-by-play referee-attribution probe")
    emit("=" * 78)
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("DB: %s (%.1f GB)" % (args.db, os.path.getsize(args.db) / 1073741824.0))
    emit("READ-ONLY: opened with SQLite's read-only URI; writes nothing to any extract.")

    uri = "file:%s?mode=ro" % args.db.replace("?", "%3f").replace("#", "%23")
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(args.db)
        emit("(read-only URI refused; opened normally -- still only SELECTs below)")
    conn.text_factory = lambda b: b.decode("utf-8", "replace")

    info = introspect(conn)
    pbp = locate_pbp(info)
    section("0. SCHEMA")
    emit("%d table(s)/view(s) in the file." % len(info))
    if not pbp:
        emit("ERROR: no table looks like play-by-play (no description columns found).")
        emit("Tables: %s" % ", ".join(sorted(info)[:40]))
        sys.exit(1)
    cols = info[pbp]
    emit("play-by-play table located by column signature: %s" % pbp)
    emit("  %d columns: %s" % (len(cols), ", ".join(cols)))
    try:
        total = conn.execute('SELECT COUNT(*) FROM "%s"' % pbp).fetchone()[0]
        emit("  %d rows" % total)
    except sqlite3.DatabaseError as exc:
        emit("  row count unavailable: %s" % exc)

    games = locate_games(info)
    if games:
        emit("per-game table (for cross-checking the season decode): %s" % games)
        gcols = info[games]
        c_gid, c_date = pick(gcols, "gameid"), pick(gcols, "gamedate")
        if c_gid and c_date:
            emit("  cross-check on 5 games (game_id decode vs. stored date):")
            sql = ('SELECT "%s","%s" FROM "%s" WHERE "%s" IS NOT NULL '
                   'ORDER BY "%s" DESC LIMIT 5' % (c_gid, c_date, games, c_date, c_date))
            for gid, date in conn.execute(sql):
                gid = pad_gid(gid)
                emit("    %s -> decoded %s %-3s | stored date %s"
                     % (gid, season_str(season_start_year_from_gid(gid)),
                        season_type_from_gid(gid), str(date)[:10]))
    else:
        emit("no per-game table found; seasons come from the game_id decode alone.")

    section("PASS 1: scanning play-by-play")
    ctr = Counters(args.samples)
    scan(conn, pbp, cols, args, ctr)
    emit("rows scanned              : %d" % ctr.rows)
    emit("rows with any description : %d" % ctr.rows_with_desc)
    emit("rows with home AND visitor description: %d" % ctr.rows_both_desc)
    emit("rows with a referee token : %d" % ctr.rows_with_ref)

    report_seasons(ctr)
    report_parens(ctr)
    report_events(ctr, args)
    report_forms(ctr)
    if not args.skip_pairing:
        pairing_pass(conn, pbp, cols, args)
    report_samples(ctr, args)

    section("WHAT THIS DOES NOT SETTLE")
    emit("* Which of two same-initial officials a colliding form refers to. The")
    emit("  seasons in section 3 narrow it; overlapping careers need a human call.")
    emit("* Whether a foul row without a referee is a real gap or the turnover")
    emit("  half of a pair -- section 4 measures the pairing, and the section 1")
    emit("  rate should be recomputed with those excluded before it is trusted.")
    emit("* Whether attribution is complete WITHIN a covered season, or only for")
    emit("  certain event types -- compare sections 1 and 2b before assuming.")
    emit("* Nothing here is written to any extract. Design comes next, once the")
    emit("  numbers above are read.")

    conn.close()
    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n\n" + "\n".join(_lines) + "\n")
    print("\n-> appended findings to %s" % os.path.relpath(OUT_PATH, REPO_ROOT))


if __name__ == "__main__":
    main()
