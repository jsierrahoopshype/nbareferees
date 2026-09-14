"""
probe_nba_data_2025.py  --  LOCAL probe. Writes nothing to the extracts.

shufinskiy/nba_data carries 2025-26 in two shapes that are NOT the
stats.nba.com play-by-play format extract_referee_calls.py already parses:

  cdnnba_2025      cdn.nba.com liveData actions
  nbastatsv3_2025  stats.nba.com's v3 play-by-play

Before either is parsed for referee attribution, this reports what they
actually contain. The question is narrow: can an official be tied to a foul,
and by what means -- a structured officialId field, or the trailing
"(E.Dalen)" parenthetical the existing parser reads?

  python scripts\\local\\probe_nba_data_2025.py

Reports, per file:
  1. columns, row count, and one full sample foul row
  2. whether an officialId-style column exists and how often it is populated
     ON FOUL ROWS specifically (a jump shot carries no official either way, so
     an overall rate would understate it)
  3. how often a foul description ends in a name parenthetical -- i.e. whether
     the existing parser would work unchanged
  4. distinct official ids found, and how many resolve against the committed
     source-data/officials.csv.gz

NETWORK. Downloads the two archives once into source-data/_nba_data_cache/
(gitignored) from the URLs published in list_data.txt. Nothing else.
"""

import argparse
import collections
import csv
import gzip
import io
import os
import re
import sys
import tarfile
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
DATASET_CACHE = os.path.join(SOURCE_DIR, "_nba_data_cache")
OFFICIALS_CSV = os.path.join(SOURCE_DIR, "officials.csv.gz")
OUT_REPORT = os.path.join(SOURCE_DIR, "_probe_nba_data_2025.txt")

INDEX_URL = "https://raw.githubusercontent.com/shufinskiy/nba_data/main/list_data.txt"
USER_AGENT = "nbareferees-probe/1.0 (+https://github.com/jsierrahoopshype/nbareferees)"
TIMEOUT = 60

KEYS = ["cdnnba_2025", "nbastatsv3_2025"]

# A foul row is found by text rather than by an event-type code, because the
# two files do not share a code vocabulary and this probe is meant to work
# without assuming one.
FOUL_RE = re.compile(r"\bfoul\b", re.I)
# The turnover half of an offensive foul. It is a separate row that names no
# official BY DESIGN, and the extractor already drops it from both the call
# count and the coverage denominator. Counting it as an unattributed foul here
# would understate attribution by ~7 points and make these files look worse
# than the seasons they are being compared against.
PAIR_RE = re.compile(r"\bturnover\b", re.I)
# The parenthetical the existing parser reads: initial(s) + surname, last in
# the string. "(P1.T1)" and "(Holmgren 1 FT)" must not match.
NAME_PAREN_RE = re.compile(
    r"\(([A-Z](?:\.[A-Z])*\.\s?[A-Za-z][A-Za-z'`’\-]*(?:[ \-][A-Za-z'`’\-]+)*)\)\s*$")
ID_COL_RE = re.compile(r"official", re.I)

_LINES = []


def emit(msg=""):
    print(msg)
    _LINES.append(msg)


def load_index():
    os.makedirs(DATASET_CACHE, exist_ok=True)
    cached = os.path.join(DATASET_CACHE, "list_data.txt")
    try:
        req = urllib.request.Request(INDEX_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
        with open(cached, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception as exc:  # noqa: BLE001
        if not os.path.exists(cached):
            emit("INDEX UNREACHABLE (%s) and nothing cached -- cannot probe." % exc)
            return {}
        emit("index fetch failed (%s); using cached copy" % exc)
        text = open(cached, encoding="utf-8").read()
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def ensure_dataset(key, index, no_download=False):
    os.makedirs(DATASET_CACHE, exist_ok=True)
    path = os.path.join(DATASET_CACHE, key + ".tar.xz")
    if os.path.exists(path) and os.path.getsize(path) > 100000:
        return path
    if no_download:
        emit("  %s not cached and --no-download given; skipped" % key)
        return None
    url = index.get(key)
    if not url:
        emit("  %s is not in the index; skipped" % key)
        return None
    # The index points at github.com/.../raw/...; some networks serve only
    # raw.githubusercontent.com. Same object, so fall back rather than fail.
    candidates = [url]
    alt = url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/raw/", "/")
    if alt != url:
        candidates.append(alt)
    for candidate in candidates:
        try:
            emit("  downloading %s" % candidate)
            req = urllib.request.Request(candidate, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT * 10) as resp:
                data = resp.read()
            if len(data) < 100000:
                emit("    only %d bytes -- not an archive, trying next" % len(data))
                continue
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            emit("    %.1f MB cached" % (len(data) / 1048576.0))
            return path
        except Exception as exc:  # noqa: BLE001
            emit("    failed: %s: %s" % (type(exc).__name__, exc))
    emit("  %s could not be downloaded" % key)
    return None


def iter_rows(path, limit=None):
    """Stream the CSV member(s) of one tar.xz as dicts."""
    n = 0
    with tarfile.open(path, "r:xz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.lower().endswith(".csv"):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
            for rec in reader:
                yield rec
                n += 1
                if limit and n >= limit:
                    return


def description_of(rec):
    """The human-readable text of a row, whichever column carries it.

    The two formats disagree on the column name, so take the longest of the
    candidates present rather than hardcoding one per format.
    """
    best = ""
    for key, val in rec.items():
        if not val or not key:
            continue
        kl = key.lower()
        if kl in ("description", "homedescription", "visitordescription",
                  "neutraldescription", "actiontype", "subtype") or "description" in kl:
            if len(val) > len(best):
                best = val
    return best


def load_official_ids():
    ids = {}
    if not os.path.exists(OFFICIALS_CSV):
        return ids
    with gzip.open(OFFICIALS_CSV, "rt", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            oid = (rec.get("official_id") or "").strip()
            if oid:
                ids[oid] = (rec.get("official_name") or "").strip()
    return ids


def probe(key, path, known_ids, sample_limit):
    emit("")
    emit("=" * 72)
    emit(key)
    emit("=" * 72)

    rows = 0
    foul_rows = 0
    pair_halves = 0
    columns = []
    id_cols = []
    id_populated = collections.Counter()
    paren_hits = 0
    distinct_ids = collections.Counter()
    sample_foul = None
    sample_descs = []

    for rec in iter_rows(path, limit=sample_limit):
        if not columns:
            columns = [c for c in rec.keys() if c]
            id_cols = [c for c in columns if ID_COL_RE.search(c)]
        rows += 1
        desc = description_of(rec)
        if not desc or not FOUL_RE.search(desc):
            continue
        blob = " ".join(str(rec.get(c) or "") for c in ("actionType", "subType")) + " " + desc
        if PAIR_RE.search(blob):
            pair_halves += 1
            continue
        foul_rows += 1
        if sample_foul is None:
            sample_foul = dict(rec)
        if len(sample_descs) < 5:
            sample_descs.append(desc)
        for col in id_cols:
            val = (rec.get(col) or "").strip()
            if val and val not in ("0", "None", "nan"):
                id_populated[col] += 1
                distinct_ids[val] += 1
        if NAME_PAREN_RE.search(desc.strip()):
            paren_hits += 1

    emit("")
    emit("1. SHAPE")
    emit("   %d column(s), %d row(s) read%s" % (
        len(columns), rows, " (capped by --limit)" if sample_limit else ""))
    emit("   columns: %s" % ", ".join(columns))
    emit("   foul rows: %d (a further %d paired turnover half/halves excluded," % (
        foul_rows, pair_halves))
    emit("     same rule the extractor applies, so this denominator is comparable)")
    if sample_foul:
        emit("")
        emit("   sample foul row, in full:")
        for k in columns:
            emit("     %-28s %s" % (k, sample_foul.get(k, "")))

    emit("")
    emit("2. STRUCTURED OFFICIAL ID")
    if not id_cols:
        emit("   NO official-ish column. Columns matching /official/i: none.")
    else:
        for col in id_cols:
            hits = id_populated[col]
            pct = (100.0 * hits / foul_rows) if foul_rows else 0.0
            emit("   %-20s populated on %d/%d foul rows (%.1f%%)" % (col, hits, foul_rows, pct))

    emit("")
    emit("3. NAME PARENTHETICAL (what the existing parser reads)")
    pct = (100.0 * paren_hits / foul_rows) if foul_rows else 0.0
    emit("   %d/%d foul descriptions end in a name parenthetical (%.1f%%)" % (
        paren_hits, foul_rows, pct))
    for d in sample_descs:
        emit("     %s" % d)
    if foul_rows and pct >= 95.0:
        emit("   -> the existing parser would read this file unchanged.")
    elif foul_rows and pct < 5.0:
        emit("   -> the existing parser would read NOTHING here; the description")
        emit("      format has changed and attribution must come from a field.")

    emit("")
    emit("4. DISTINCT IDS vs officials.csv.gz")
    if not distinct_ids:
        emit("   none found.")
    else:
        matched = [i for i in distinct_ids if i in known_ids]
        unmatched = [i for i in distinct_ids if i not in known_ids]
        emit("   %d distinct id(s); %d resolve against officials.csv.gz, %d do not" % (
            len(distinct_ids), len(matched), len(unmatched)))
        for i, _ in distinct_ids.most_common(5):
            emit("     %-12s %-24s %d call(s)" % (i, known_ids.get(i, "UNMATCHED"), distinct_ids[i]))
        if unmatched:
            emit("   unmatched: %s" % ", ".join(sorted(unmatched)[:20]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N rows per file (0 = whole file)")
    ap.add_argument("--no-download", action="store_true",
                    help="use only what is already cached")
    args = ap.parse_args()

    emit("probe_nba_data_2025 -- what the 2025-26 files actually contain")
    index = load_index()
    known_ids = load_official_ids()
    emit("officials.csv.gz: %d distinct official id(s) known" % len(known_ids))

    for key in KEYS:
        path = ensure_dataset(key, index, args.no_download)
        if not path:
            continue
        try:
            probe(key, path, known_ids, args.limit or None)
        except Exception as exc:  # noqa: BLE001
            emit("  FAILED to read %s: %s: %s" % (key, type(exc).__name__, exc))

    with open(OUT_REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LINES) + "\n")
    print("\n-> wrote %s" % os.path.relpath(OUT_REPORT, REPO_ROOT))


if __name__ == "__main__":
    main()
