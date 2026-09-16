"""
build_official_id_map.py  --  LOCAL script. NBA person id -> our referee slug.

  python scripts\\local\\build_official_id_map.py

WHY. Two NBA feeds identify an official by a numeric person id rather than by
a name:

  cdn.nba.com play-by-play      officialId on every foul action
  official.nba.com assignments  official1_code / official2_code / official3_code

A person id is unambiguous in a way a name is not. "J.Goble" is Jacyn or John
depending on the game and needs a crew sheet to settle; 1627962 is Jacyn Goble
and nothing else. This map is what lets those feeds skip name resolution
entirely.

TWO SOURCES, KEPT APART IN THE OUTPUT.

  extract       source-data/officials.csv.gz already carries NBA person ids for
                the seasons it drew from the NBA (66,051 rows, 142 distinct
                ids, each with exactly one name). Mapped through the built
                referee docs, so the identity overrides are already applied and
                a merged pair resolves to its canonical slug.

  crew_inferred The seasons from 2023-24 came from ESPN, so an official whose
                whole career sits in them has NO NBA person id anywhere in our
                data. Those ids are recovered from the play-by-play itself: for
                a game where the crew is known and every other id on it is
                already mapped, the leftover official is who the unknown id
                belongs to. Unanimity across every game is required -- one
                dissenting game and the id is reported rather than mapped.

MALFORMED IDS. The feed occasionally emits a corrupted id: 11629177 is a stray
leading digit on 1629177, and both resolve to the same official by crew. They
are mapped, flagged in the note column, and never silently normalised -- the
raw string is what a consumer will actually see in the feed.

Output (committed; small, and other tools depend on it):
  source-data/official_id_map.csv.gz
  source-data/_official_id_map_report.txt

DEPENDS ON a build having run: reads data/referees/*.json for the canonical
slugs and the identity overrides already folded in.

NETWORK. The shufinskiy index and the cdnnba archives it names.
"""

import argparse
import collections
import csv
import glob
import gzip
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import nba_data_source as nds  # noqa: E402

REPO_ROOT = nds.REPO_ROOT
SOURCE_DIR = nds.SOURCE_DIR
DATA_DIR = os.path.join(REPO_ROOT, "data")
OFFICIALS_CSV = os.path.join(SOURCE_DIR, "officials.csv.gz")
OUT_MAP = os.path.join(SOURCE_DIR, "official_id_map.csv.gz")
OUT_REPORT = os.path.join(SOURCE_DIR, "_official_id_map_report.txt")

# cdnnba is the only shufinskiy format carrying officialId.
CDN_KEYS = ["cdnnba_2023", "cdnnba_po_2023", "cdnnba_2024", "cdnnba_po_2024",
            "cdnnba_2025", "cdnnba_po_2025"]

# A well-formed NBA person id is all digits and at most this long. Anything
# longer is a feed corruption -- mapped if the evidence is unanimous, flagged
# either way.
MAX_SANE_ID_LEN = 7

_lines = []


def emit(msg=""):
    print(msg)
    _lines.append(msg)


def section(t):
    emit("")
    emit("=" * 78)
    emit(t)
    emit("=" * 78)


def pad_gid(v):
    s = str(v or "").strip()
    return s.zfill(10) if s.isdigit() and len(s) < 10 else s


def load_referee_ids():
    """(nba person id -> slug, slug -> display name) from the built docs."""
    id2slug, names = {}, {}
    for p in sorted(glob.glob(os.path.join(DATA_DIR, "referees", "*.json"))):
        s = json.load(open(p, encoding="utf-8"))["summary"]
        names[s["official_id"]] = s["name"]
        for rid in s.get("raw_ids", []):
            if not str(rid).startswith("espn:"):
                id2slug[str(rid)] = s["official_id"]
    return id2slug, names


def load_crews(id2slug, names):
    """canonical game id -> set of slugs on that game's crew."""
    nba2espn, espn2nba = nds.load_game_id_bridge()
    name2slug = {n: s for s, n in names.items()}
    crew = collections.defaultdict(set)
    with gzip.open(OFFICIALS_CSV, "rt", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            gid = pad_gid(r["game_id"])
            gid = espn2nba.get(gid, espn2nba.get(r["game_id"].strip(), gid))
            slug = id2slug.get(r["official_id"].strip()) or \
                name2slug.get((r.get("official_name") or "").strip())
            if slug:
                crew[gid].add(slug)
    return crew


def scan_cdn(args):
    """(id -> games it appears in, game -> ids seen on it)."""
    idx = nds.load_index(emit=emit, no_download=args.no_download)
    games_of = collections.defaultdict(set)
    ids_in_game = collections.defaultdict(set)
    for key in CDN_KEYS:
        path = nds.ensure_dataset(key, idx, emit=emit, no_download=args.no_download)
        if not path:
            continue
        n = 0
        for row in nds.iter_csv_rows(path):
            oid = (row.get("officialId") or "").strip()
            if not oid or oid in ("0", "None"):
                continue
            gid = pad_gid(row.get("gameId"))
            games_of[oid].add(gid)
            ids_in_game[gid].add(oid)
            n += 1
        emit("  %-18s %8d action(s) carrying an officialId" % (key, n))
    return games_of, ids_in_game


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--out", default=OUT_MAP)
    ap.add_argument("--out-report", default=OUT_REPORT)
    args = ap.parse_args()

    emit("NBA person id -> referee slug (build_official_id_map.py)")
    emit("=" * 78)

    id2slug, names = load_referee_ids()
    emit("ids carried by source-data/officials.csv.gz, via the built referee "
         "docs: %d" % len(id2slug))

    rows = []
    for pid, slug in sorted(id2slug.items()):
        rows.append({"nba_person_id": pid, "official_id": slug,
                     "name": names.get(slug, ""), "source": "extract",
                     "games_evidence": "", "note": ""})

    section("SCANNING cdn.nba.com PLAY-BY-PLAY")
    games_of, ids_in_game = scan_cdn(args)
    emit("")
    emit("distinct officialId values in the play-by-play: %d" % len(games_of))
    unknown = [i for i in games_of if i not in id2slug]
    emit("of those, not already mapped: %d" % len(unknown))

    section("RECOVERING UNMAPPED IDS FROM THE CREW")
    crew = load_crews(id2slug, names)
    emit("Method: for each game an unknown id appears in, take the crew on file")
    emit("and remove every official already identified by a mapped id on that")
    emit("same game. One official left over, in every game, is the answer.")
    emit("")
    emit("%-12s %6s  %-24s %s" % ("officialId", "games", "resolves to", "evidence"))
    emit("-" * 78)
    resolved = unresolved = 0
    for oid in sorted(unknown, key=lambda x: -len(games_of[x])):
        cand = collections.Counter()
        usable = 0
        for gid in games_of[oid]:
            known = {id2slug[i] for i in ids_in_game[gid] if i in id2slug}
            left = crew.get(gid, set()) - known
            if len(left) == 1:
                cand[next(iter(left))] += 1
                usable += 1
        note = ""
        if len(oid) > MAX_SANE_ID_LEN:
            note = "malformed id, kept verbatim as the feed emits it"
        if len(cand) == 1 and usable:
            slug = next(iter(cand))
            rows.append({"nba_person_id": oid, "official_id": slug,
                         "name": names.get(slug, ""), "source": "crew_inferred",
                         "games_evidence": usable, "note": note})
            resolved += 1
            emit("%-12s %6d  %-24s unanimous%s" % (
                oid, len(games_of[oid]), slug, " [" + note + "]" if note else ""))
        else:
            unresolved += 1
            emit("%-12s %6d  %-24s %s" % (
                oid, len(games_of[oid]), "NOT MAPPED",
                dict(cand) if cand else "no usable crew on file"))
    emit("")
    emit("recovered: %d | left unmapped: %d" % (resolved, unresolved))
    if unresolved:
        emit("An unmapped id is reported, never guessed. Its calls fall back to")
        emit("name-form resolution, exactly as before this map existed.")

    section("OUTPUT")
    os.makedirs(SOURCE_DIR, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["nba_person_id", "official_id", "name",
                                           "source", "games_evidence", "note"])
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["source"], r["official_id"])):
            w.writerow(r)
    by_src = collections.Counter(r["source"] for r in rows)
    emit("-> %s" % os.path.relpath(args.out, REPO_ROOT))
    emit("   %d row(s): %s" % (len(rows), dict(by_src)))
    emit("   %d distinct referee(s) covered" % len({r["official_id"] for r in rows}))

    with open(args.out_report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_lines) + "\n")
    print("\n-> wrote %s" % os.path.relpath(args.out_report, REPO_ROOT))


if __name__ == "__main__":
    main()
