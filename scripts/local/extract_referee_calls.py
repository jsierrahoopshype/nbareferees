"""
extract_referee_calls.py  --  LOCAL script (run by Jorge on Windows, in cmd).

Reads the raw nbadb SQLite and writes a slim, gzipped extract of INDIVIDUAL
REFEREE CALL ATTRIBUTION: one row per foul/technical/violation event that the
league's own play-by-play attributes to a named official.

  python scripts\\local\\extract_referee_calls.py "C:\\Users\\Jorge Sierra\\Downloads\\archive\\nba.sqlite"

WHAT THIS IS, AND ITS LIMITS (both are carried into the extract's own header
and repeated on every page that shows these numbers):

  * The NBA began printing the calling official's name in play-by-play with the
    2015 playoffs. nbadb's play-by-play ends in June 2023. So attribution
    covers 2014-15 PLAYOFFS through 2022-23 and nothing else -- roughly a
    third of the era this site covers (1993-94 onward).
  * Within that window the probe measured 91-93% of foul events carrying a
    name. The rest are unattributed and simply absent here. A count from this
    extract is therefore a FLOOR, never a total.
  * These are calls RECORDED AGAINST an official in the league's feed. Nothing
    here measures whether a call was correct.

Outputs (both committed; the raw DB never enters the repo):
  source-data/referee_calls.csv.gz       one row per attributed call
  source-data/referee_calls_coverage.csv.gz  one row per covered game: how many
                                         foul events it had, how many carried a
                                         name -- the denominator for rates, and
                                         the evidence for the coverage figure
  source-data/_referee_calls_report.txt  QA report (appended)

PAIRED TURNOVER HALVES (probe section 4). An offensive foul emits TWO rows: the
foul ("Embiid OFF.Foul (P2.T3) (J.T.Orr)") and its turnover half ("Embiid
Offensive Foul Turnover (P2.T4)"), which carries no official by design. The
turnover half is dropped from BOTH the attributed rows and the coverage
denominator -- counting it as an unattributed foul would deflate the coverage
rate, and counting the pair twice would inflate the call count. A bounded
sample of games is additionally verified row-by-row on (game_id, period, clock)
and the result is reported.

NAME-FORM RESOLUTION. Play-by-play gives an initial-plus-surname form
("R.Garretson"). Resolution, in order:
  1. data/referee_callform_overrides.csv, if the form is listed there.
  2. Per-game crew lookup for forms that genuinely collide in the attribution
     era. J.Goble is Jacyn or John depending on the game, so the crew for that
     game (source-data/officials.csv.gz) decides. A game where the crew does
     not settle it is REPORTED and its calls are dropped, never guessed.
  3. Exact match against data/referees.json on first-initial + surname.
Anything left over is reported by form, with counts, and dropped.

NO NETWORK. Reads the local SQLite plus two committed extracts. Writes nothing
outside source-data/.
"""

import argparse
import collections
import csv
import datetime
import gzip
import json
import os
import re
import sqlite3
import sys
import unicodedata

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
DATA_DIR = os.path.join(REPO_ROOT, "data")
REFEREES_JSON = os.path.join(DATA_DIR, "referees.json")
OVERRIDES_CSV = os.path.join(DATA_DIR, "referee_callform_overrides.csv")
# build.py's own identity overrides already map former/variant names to canonical
# keys (a referee who changed surname mid-career, a spelling the source got
# wrong). Play-by-play prints whatever the league printed AT THE TIME, so those
# same variants turn up here as name-forms -- "D.Mosher" for Dannica Baroody.
# Feeding this file into the form index means a name change is handled once,
# where it is already recorded, instead of needing a second hand-written entry.
IDENTITY_OVERRIDES_CSV = os.path.join(DATA_DIR, "referee_identity_overrides.csv")
OFFICIALS_CSV = os.path.join(SOURCE_DIR, "officials.csv.gz")
GAMES_CSV = os.path.join(SOURCE_DIR, "games.csv.gz")
OUT_CALLS = os.path.join(SOURCE_DIR, "referee_calls.csv.gz")
OUT_COVERAGE = os.path.join(SOURCE_DIR, "referee_calls_coverage.csv.gz")
OUT_REPORT = os.path.join(SOURCE_DIR, "_referee_calls_report.txt")

DEFAULT_DB = r"C:\Users\Jorge Sierra\Downloads\archive\nba.sqlite"

# Attribution starts with the 2014-15 playoffs; earlier seasons carry none.
MIN_SEASON_START_YEAR = 2014
SEASON_TYPE_MAP = {"1": "Pre", "2": "RS", "3": "AS", "4": "PO", "5": "PI"}
KEEP_SEASON_TYPES = {"RS", "PO", "PI"}

PAIR_SAMPLE_GAMES = 300
PROGRESS_EVERY = 1000000

REF_TOKEN_RE = re.compile(
    r"^[A-Z](?:\.[A-Z])*\.\s?[A-Za-z][A-Za-z'`\u2019\-]*(?:[ \-][A-Za-z'`\u2019\-]+)*\.?$")
PAREN_RE = re.compile(r"\(([^()]*)\)")

# Call type from the description text. Ordered: the first match wins, so the
# specific forms sit above the general ones ("C.P.FOUL" before "P.FOUL").
#
# Text is the classifier because it is stable and legible. The NBA's own
# (eventmsgtype, eventmsgactiontype) pair is carried on every row as well, so a
# finer split -- personal-block vs. personal-take, which share the "P.FOUL"
# text -- stays possible later without re-reading the DB. The QA report
# cross-tabs the two so any disagreement is visible.
CALL_TYPES = [
    ("flagrant_2", re.compile(r"flagrant.*type\s*2|flagrant\.type\s*2", re.I)),
    ("flagrant_1", re.compile(r"flagrant.*type\s*1|flagrant\.type\s*1|flagrant", re.I)),
    ("away_from_play", re.compile(r"away\.?from\.?play", re.I)),
    ("clear_path", re.compile(r"\bc\.p\.foul|clear\s*path", re.I)),
    ("double_technical", re.compile(r"double\s*tech", re.I)),
    ("hanging_technical", re.compile(r"hanging.*tech|tech.*hanging", re.I)),
    ("defensive_3_seconds", re.compile(r"def\.?\s*3\s*sec|defensive\s*3\s*sec", re.I)),
    ("delay_of_game", re.compile(r"delay\s*of\s*game|\bdelay\b", re.I)),
    ("technical", re.compile(r"\bt\.foul|technical", re.I)),
    ("ejection", re.compile(r"ejection", re.I)),
    ("punch", re.compile(r"punch", re.I)),
    ("inbound", re.compile(r"\bin\.foul|inbound", re.I)),
    ("loose_ball", re.compile(r"\bl\.?b\.foul|loose\s*ball", re.I)),
    ("offensive", re.compile(r"off\.foul|offensive\s*foul|charge", re.I)),
    ("shooting", re.compile(r"\bs\.foul", re.I)),
    ("personal", re.compile(r"\bp\.foul|personal", re.I)),
    ("violation", re.compile(r"violation", re.I)),
]

CSV_FIELDS = ["game_id", "season", "season_type", "period", "clock", "call_type",
              "call_phrase", "eventmsgtype", "eventmsgactiontype", "ref_form",
              "official_id", "player_name", "player_id"]
COVERAGE_FIELDS = ["game_id", "season", "season_type", "foul_events",
                   "attributed", "unattributed", "paired_turnovers_dropped"]

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


def norm_ref_key(name):
    """Same rules as build.py's norm_ref_key (kept stdlib-only here)."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = s.lower().replace(".", " ").replace("'", "")
    s = re.sub(r"[^a-z0-9\s-]", " ", s).replace("-", " ")
    toks = [t for t in s.split() if t and t not in {"jr", "sr", "ii", "iii", "iv", "v"}]
    return "-".join(toks)


def pad_gid(value):
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(10) if s else ""


def season_start_year_from_gid(gid):
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
    return SEASON_TYPE_MAP.get(gid[2], "??") if gid and len(gid) > 2 else "??"


def call_type_of(text):
    for name, rx in CALL_TYPES:
        if rx.search(text):
            return name
    return "other"


def call_phrase_of(desc):
    """The event wording with the parentheticals and the leading player name
    stripped, for the QA report's vocabulary listing."""
    stripped = PAREN_RE.sub("", desc).strip()
    return re.sub(r"\s+", " ", stripped)[:60]


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
def _add_forms(by_form, name, ref):
    """Index every initial+surname form this display name could print as."""
    parts = name.split()
    if len(parts) < 2:
        return
    surname = " ".join(parts[1:])
    bare = parts[0].replace(".", "")
    prefixes = {parts[0][0]}
    if bare.isupper() and 1 < len(bare) <= 3:
        prefixes.add(".".join(list(bare)))
    for prefix in prefixes:
        for variant in {surname, surname.replace(" ", ""), surname.replace("-", "")}:
            key = (prefix + "." + variant).lower().replace(" ", "")
            if ref not in by_form[key]:
                by_form[key].append(ref)


def load_referees():
    with open(REFEREES_JSON, "r", encoding="utf-8") as fh:
        refs = json.load(fh)
    by_form = collections.defaultdict(list)
    by_key = {}
    for r in refs:
        name, slug = r.get("name"), r.get("slug")
        if not name or not slug:
            continue
        by_key[norm_ref_key(name)] = r
        _add_forms(by_form, name, r)

    # Former/variant names from build.py's identity overrides, mapped onto the
    # canonical referee they resolve to.
    variants = 0
    if os.path.exists(IDENTITY_OVERRIDES_CSV):
        with open(IDENTITY_OVERRIDES_CSV, "r", encoding="utf-8-sig", newline="") as fh:
            rows = [ln for ln in fh if not ln.lstrip().startswith("#")]
        by_ref_key = {norm_ref_key(r["name"]): r for r in refs if r.get("name")}
        for row in csv.DictReader(rows):
            raw = (row.get("raw_name_or_id") or "").strip()
            canon = (row.get("canonical_ref_key") or "").strip()
            target = by_ref_key.get(canon) or next(
                (r for r in refs if r.get("slug") == canon), None)
            if raw and target:
                before = sum(len(v) for v in by_form.values())
                _add_forms(by_form, raw, target)
                variants += sum(len(v) for v in by_form.values()) - before
    return refs, by_form, by_key, variants


def load_overrides():
    """form (lowercased, spaces stripped) -> slug."""
    out = {}
    if not os.path.exists(OVERRIDES_CSV):
        return out
    with open(OVERRIDES_CSV, "r", encoding="utf-8-sig", newline="") as fh:
        rows = [ln for ln in fh if not ln.lstrip().startswith("#")]
    for r in csv.DictReader(rows):
        form = (r.get("name_form") or "").strip()
        slug = (r.get("official_id") or "").strip()
        if form and slug:
            out[form.lower().replace(" ", "")] = slug
    return out


def load_game_crews():
    """game_id -> {norm_ref_key(name): official_name} from the committed extract."""
    crews = collections.defaultdict(dict)
    if not os.path.exists(OFFICIALS_CSV):
        return crews
    with gzip.open(OFFICIALS_CSV, "rt", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            gid = pad_gid(r.get("game_id"))
            name = (r.get("official_name") or "").strip()
            if gid and name:
                crews[gid][norm_ref_key(name)] = name
    return crews


class Resolver(object):
    """name-form -> canonical slug, with the collision cases handled explicitly."""

    def __init__(self, by_form, overrides, crews):
        self.by_form = by_form
        self.overrides = overrides
        self.crews = crews
        self.unmatched = collections.Counter()
        self.ambiguous_games = collections.Counter()
        self.by_crew_resolved = collections.Counter()
        self.override_used = collections.Counter()

    def resolve(self, form, game_id):
        key = form.lower().replace(" ", "")
        if key in self.overrides:
            self.override_used[form] += 1
            return self.overrides[key], "override"

        candidates = self.by_form.get(key, [])
        if len(candidates) == 1:
            return candidates[0]["slug"], "exact"

        if len(candidates) > 1:
            # A genuine collision (J.Goble: Jacyn and John overlap in the
            # attribution era). The crew that worked THIS game settles it --
            # and when it does not, the calls are dropped, not guessed.
            crew = self.crews.get(game_id, {})
            hits = [c for c in candidates if norm_ref_key(c["name"]) in crew]
            if len(hits) == 1:
                self.by_crew_resolved[form] += 1
                return hits[0]["slug"], "crew"
            self.ambiguous_games[(form, game_id, len(hits))] += 1
            return None, "ambiguous"

        self.unmatched[form] += 1
        return None, "unmatched"


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
def introspect(conn):
    info = {}
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall():
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)]
        except sqlite3.DatabaseError:
            continue
        if cols:
            info[table] = cols
    return info


def locate_pbp(info):
    best, best_score = None, 0
    for table, cols in info.items():
        n = {norm(c) for c in cols}
        score = 3 * sum(1 for c in n if "description" in c)
        score += 2 * any("eventmsgtype" in c for c in n)
        score += 1 * any("eventnum" in c for c in n)
        if score > best_score:
            best, best_score = table, score
    return best


def pick(cols, *patterns):
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


def main():
    ap = argparse.ArgumentParser(description="Extract per-referee call attribution from nbadb.")
    ap.add_argument("db", nargs="?", default=DEFAULT_DB, help="path to nba.sqlite")
    ap.add_argument("--limit", type=int, help="stop after N play-by-play rows (smoke test)")
    ap.add_argument("--out-calls", default=OUT_CALLS)
    ap.add_argument("--out-coverage", default=OUT_COVERAGE)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass

    if not os.path.exists(args.db):
        print("ERROR: no SQLite file at %s" % args.db)
        sys.exit(1)

    emit("referee call attribution extract (extract_referee_calls.py)")
    emit("=" * 78)
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("DB: %s" % args.db)

    refs, by_form, _, variant_forms = load_referees()
    overrides = load_overrides()
    crews = load_game_crews()
    emit("canonical referees: %d | callform overrides: %d | games with a known crew: %d"
         % (len(refs), len(overrides), len(crews)))
    emit("former/variant name-forms folded in from referee_identity_overrides.csv: %d"
         % variant_forms)
    resolver = Resolver(by_form, overrides, crews)

    uri = "file:%s?mode=ro" % args.db.replace("?", "%3f").replace("#", "%23")
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(args.db)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")

    info = introspect(conn)
    pbp = locate_pbp(info)
    if not pbp:
        emit("ERROR: no play-by-play table found.")
        sys.exit(1)
    cols = info[pbp]
    emit("play-by-play table (located by column signature): %s" % pbp)

    c_gid = pick(cols, "gameid")
    c_type = pick(cols, "eventmsgtype")
    c_action = pick(cols, "eventmsgactiontype")
    c_period = pick(cols, "period")
    c_clock = pick(cols, "pctimestring")
    c_num = pick(cols, "eventnum")
    c_home = pick(cols, "homedescription")
    c_vis = pick(cols, "visitordescription")
    c_neutral = pick(cols, "neutraldescription")
    c_p1name = pick(cols, "player1name")
    c_p1id = pick(cols, "player1id")
    selected = [c for c in (c_gid, c_type, c_action, c_period, c_clock, c_num,
                            c_home, c_vis, c_neutral, c_p1name, c_p1id) if c]
    idx = {c: i for i, c in enumerate(selected)}

    section("SCANNING")
    sql = 'SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in selected), pbp)
    if args.limit:
        sql += " LIMIT %d" % args.limit
    emit(sql)

    coverage = collections.defaultdict(lambda: [0, 0, 0, 0])  # foul, attributed, unattributed, dropped
    game_meta = {}
    by_type = collections.Counter()
    by_season = collections.Counter()
    crosstab = collections.Counter()
    phrases = collections.defaultdict(collections.Counter)
    other_samples = []
    pair_sample = {}
    rows_scanned = 0
    rows_written = 0
    dropped_unresolved = 0

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with gzip.open(args.out_calls, "wt", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for row in conn.execute(sql):
            rows_scanned += 1
            if rows_scanned % PROGRESS_EVERY == 0:
                print("    ...%d rows scanned, %d calls written" % (rows_scanned, rows_written))

            gid = pad_gid(row[idx[c_gid]]) if c_gid else ""
            year = season_start_year_from_gid(gid)
            if year is None or year < MIN_SEASON_START_YEAR:
                continue
            stype = season_type_from_gid(gid)
            if stype not in KEEP_SEASON_TYPES:
                continue
            season = season_str(year)

            descs = [d for d in (row[idx[c_home]] if c_home else None,
                                 row[idx[c_vis]] if c_vis else None,
                                 row[idx[c_neutral]] if c_neutral else None) if d]
            if not descs:
                continue
            desc = descs[0]
            low = desc.lower()
            is_foulish = "foul" in low

            cov = coverage[gid]
            game_meta[gid] = (season, stype)

            # The turnover half of an offensive foul: no official by design.
            # Excluded from the numerator AND the denominator -- see the header.
            if is_foulish and "turnover" in low:
                cov[3] += 1
                if len(pair_sample) < PAIR_SAMPLE_GAMES or gid in pair_sample:
                    pair_sample.setdefault(gid, []).append(
                        (row[idx[c_period]], row[idx[c_clock]], "turnover", desc[:80]))
                continue

            groups = PAREN_RE.findall(desc)
            ref_forms = [g.strip() for g in groups if REF_TOKEN_RE.match(g.strip())]

            if is_foulish:
                cov[0] += 1
                if len(pair_sample) < PAIR_SAMPLE_GAMES or gid in pair_sample:
                    pair_sample.setdefault(gid, []).append(
                        (row[idx[c_period]], row[idx[c_clock]],
                         "foul_ref" if ref_forms else "foul_noref", desc[:80]))

            if not ref_forms:
                if is_foulish:
                    cov[2] += 1
                continue

            form = ref_forms[-1]
            slug, how = resolver.resolve(form, gid)
            if is_foulish:
                if slug:
                    cov[1] += 1
                else:
                    cov[2] += 1
            if not slug:
                dropped_unresolved += 1
                continue

            ctype = call_type_of(desc)
            phrase = call_phrase_of(desc)
            emt = row[idx[c_action]] if c_action else None
            crosstab[(ctype, row[idx[c_type]] if c_type else None, emt)] += 1
            by_type[ctype] += 1
            by_season[(season, ctype)] += 1
            phrases[ctype][phrase] += 1
            if ctype == "other" and len(other_samples) < 25:
                other_samples.append(desc[:120])

            writer.writerow({
                "game_id": gid, "season": season, "season_type": stype,
                "period": row[idx[c_period]] if c_period else "",
                "clock": row[idx[c_clock]] if c_clock else "",
                "call_type": ctype, "call_phrase": phrase,
                "eventmsgtype": row[idx[c_type]] if c_type else "",
                "eventmsgactiontype": emt if emt is not None else "",
                "ref_form": form, "official_id": slug,
                "player_name": (row[idx[c_p1name]] if c_p1name else "") or "",
                "player_id": (row[idx[c_p1id]] if c_p1id else "") or "",
            })
            rows_written += 1

    with gzip.open(args.out_coverage, "wt", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COVERAGE_FIELDS)
        writer.writeheader()
        for gid in sorted(coverage):
            season, stype = game_meta[gid]
            foul, att, unatt, dropped = coverage[gid]
            writer.writerow({"game_id": gid, "season": season, "season_type": stype,
                             "foul_events": foul, "attributed": att,
                             "unattributed": unatt, "paired_turnovers_dropped": dropped})

    conn.close()
    report(args, rows_scanned, rows_written, dropped_unresolved, coverage, game_meta,
           by_type, by_season, crosstab, phrases, other_samples, pair_sample, resolver)


def report(args, rows_scanned, rows_written, dropped_unresolved, coverage, game_meta,
           by_type, by_season, crosstab, phrases, other_samples, pair_sample, resolver):
    section("OUTPUT")
    emit("rows scanned         : %d" % rows_scanned)
    emit("attributed calls kept: %d" % rows_written)
    emit("calls dropped (form could not be resolved): %d" % dropped_unresolved)
    emit("games covered        : %d" % len(coverage))
    emit("-> %s" % os.path.relpath(args.out_calls, REPO_ROOT))
    emit("-> %s" % os.path.relpath(args.out_coverage, REPO_ROOT))

    section("COVERAGE BY SEASON (the denominator, and the ~8% that is missing)")
    per_season = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
    for gid, (foul, att, unatt, dropped) in coverage.items():
        season, stype = game_meta[gid]
        s = per_season[season]
        s[0] += 1
        s[1] += foul
        s[2] += att
        s[3] += unatt
        s[4] += dropped
    emit("%-10s %8s %12s %12s %12s %8s %12s"
         % ("season", "games", "foul events", "attributed", "unattributed", "rate", "TO halves"))
    emit("-" * 78)
    for season in sorted(per_season):
        games, foul, att, unatt, dropped = per_season[season]
        rate = (100.0 * att / foul) if foul else 0.0
        emit("%-10s %8d %12d %12d %12d %7.1f%% %12d"
             % (season, games, foul, att, unatt, rate, dropped))
    emit("")
    emit("'TO halves' are the paired turnover rows, excluded from every column")
    emit("to their left. Counting them as unattributed fouls would understate the")
    emit("rate; counting them as calls would double-count the offensive foul.")

    section("CALL TYPES")
    total = sum(by_type.values()) or 1
    emit("%-22s %12s %8s   %s" % ("call_type", "calls", "share", "most common wording"))
    emit("-" * 78)
    for ctype, n in by_type.most_common():
        top = phrases[ctype].most_common(1)
        emit("%-22s %12d %7.1f%%   %s"
             % (ctype, n, 100.0 * n / total, top[0][0] if top else ""))
    if by_type.get("other"):
        emit("")
        emit("Descriptions that fell into 'other' -- if a real call type is hiding")
        emit("here, CALL_TYPES needs a rule for it:")
        for smp in other_samples:
            emit("    %s" % smp)

    section("CALL TYPE vs. THE NBA'S OWN EVENT CODES")
    emit("Text is the classifier; the code pair is carried on every row. If one")
    emit("call_type spans several action types (personal-block and personal-take")
    emit("both print 'P.FOUL'), that is visible here and can be split later")
    emit("without re-reading the DB.")
    emit("")
    emit("%-22s %10s %10s %12s" % ("call_type", "msgtype", "action", "calls"))
    emit("-" * 78)
    for (ctype, emt, eat), n in sorted(crosstab.items(), key=lambda kv: (-kv[1]))[:40]:
        emit("%-22s %10s %10s %12d" % (ctype, emt, eat, n))

    section("NAME-FORM RESOLUTION")
    emit("resolved by override      : %d call(s) across %d form(s)"
         % (sum(resolver.override_used.values()), len(resolver.override_used)))
    for form, n in resolver.override_used.most_common():
        emit("    %-20s %d" % (form, n))
    emit("resolved by per-game crew : %d call(s) across %d form(s)"
         % (sum(resolver.by_crew_resolved.values()), len(resolver.by_crew_resolved)))
    for form, n in resolver.by_crew_resolved.most_common():
        emit("    %-20s %d" % (form, n))

    emit("")
    if resolver.ambiguous_games:
        emit("*** AMBIGUOUS -- crew lookup did NOT settle these; calls DROPPED, not guessed:")
        emit("%-14s %-12s %10s %10s" % ("form", "game_id", "crew hits", "calls"))
        for (form, gid, hits), n in resolver.ambiguous_games.most_common(40):
            emit("%-14s %-12s %10d %10d" % (form, gid, hits, n))
        emit("(crew hits = how many candidates with that form appear in the game's")
        emit(" crew: 0 means the crew list has neither, 2 means it has both.)")
    else:
        emit("ambiguous games: none -- every colliding form was settled by its game's crew.")

    emit("")
    if resolver.unmatched:
        emit("*** UNMATCHED FORMS -- no canonical referee, calls DROPPED:")
        for form, n in resolver.unmatched.most_common():
            emit("    %-20s %8d call(s)" % (form, n))
        emit("Add a row to data/referee_callform_overrides.csv for any of these that")
        emit("is a real official under a spelling we do not carry.")
    else:
        emit("unmatched forms: none.")

    section("PAIRING CHECK (sample of %d games)" % len(pair_sample))
    paired = orphan = 0
    for gid, events in pair_sample.items():
        clocks = collections.defaultdict(set)
        for period, clock, kind, _ in events:
            clocks[(period, clock)].add(kind)
        for key, kinds in clocks.items():
            if "turnover" in kinds:
                if "foul_ref" in kinds or "foul_noref" in kinds:
                    paired += 1
                else:
                    orphan += 1
    tot = paired + orphan
    if tot:
        emit("turnover halves in the sample : %d" % tot)
        emit("  paired with a foul at the same (game, period, clock): %d (%.1f%%)"
             % (paired, 100.0 * paired / tot))
        emit("  no foul at that clock (orphan)                      : %d (%.1f%%)"
             % (orphan, 100.0 * orphan / tot))
        emit("")
        emit("A high pairing rate is the evidence that dropping these rows removes")
        emit("the duplicate half rather than losing real calls.")
    else:
        emit("no turnover halves in the sampled games.")

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_REPORT, "a", encoding="utf-8") as fh:
        fh.write("\n\n" + "\n".join(_lines) + "\n")
    print("\n-> appended QA report to %s" % os.path.relpath(OUT_REPORT, REPO_ROOT))


if __name__ == "__main__":
    main()
