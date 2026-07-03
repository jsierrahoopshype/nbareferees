"""
backfill_officials.py  --  LOCAL script (run by Jorge on Windows, in cmd).
CONTINGENCY ONLY.

Run this ONLY if source-data/_freshness.txt flags games missing officials in
recent seasons (coverage < 95% for a 2000-01+ season, or a recent season that
looks incomplete). For every game_id present in games.csv.gz but absent from
officials.csv.gz (season >= 2000-01), this calls nba_api BoxScoreSummaryV3
(V2 is degraded), extracts the officials, and APPENDS them to officials.csv.gz.

  python scripts\\local\\backfill_officials.py

NETWORK NOTE: stats.nba.com blocks datacenter/cloud IPs -> Jorge's machine only.

  pip install nba_api pandas

Behavior:
  * 1.5s delay between calls (~40 games/min; a full missing month < 1 hour).
  * Resume-safe: appended games become "present" in officials.csv.gz, so a
    re-run naturally skips them. Progress is printed and flushed to disk in
    batches so an interrupted run loses at most one batch.
  * game_id kept as a 10-digit string throughout.
  * Output encoding utf-8-sig, gzip compression.
"""

import os
import re
import sys
import time

import pandas as pd

try:
    from nba_api.stats.endpoints import BoxScoreSummaryV3
except ImportError:
    print("ERROR: BoxScoreSummaryV3 not available. Upgrade nba_api:")
    print("       pip install --upgrade nba_api pandas")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
GAMES_PATH = os.path.join(SOURCE_DIR, "games.csv.gz")
OFFICIALS_PATH = os.path.join(SOURCE_DIR, "officials.csv.gz")

DELAY_SECONDS = 1.5
FLUSH_EVERY = 25  # write to disk every N newly-fetched games

OFFICIALS_COLUMNS = ["game_id", "official_id", "official_name", "jersey_num"]

NBA_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}


def season_start_year_from_gid(gid):
    if not gid or len(gid) < 5:
        return None
    try:
        yy = int(gid[3:5])
    except ValueError:
        return None
    return 1900 + yy if yy >= 46 else 2000 + yy


def load_str_csv(path, **kwargs):
    """Read a gzipped CSV forcing game_id (and other ids) to string dtype."""
    return pd.read_csv(
        path,
        compression="gzip",
        dtype={
            "game_id": str,
            "official_id": str,
            "jersey_num": str,
        },
        **kwargs,
    )


def clean_id_str(v, width=None):
    if v is None:
        return ""
    s = str(v).strip()
    s = re.sub(r"\.0$", "", s)
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    if width and s:
        s = s.zfill(width)
    return s


def extract_officials(game_id):
    """
    Call BoxScoreSummaryV3 for one game_id and return a list of dict rows:
    {game_id, official_id, official_name, jersey_num}. Empty list if none.
    """
    resp = BoxScoreSummaryV3(game_id=game_id, headers=NBA_HEADERS, timeout=60)

    officials = []
    # Preferred path: the V3 nested dict.
    try:
        d = resp.get_dict()
        summary = d.get("boxScoreSummary") or d.get("BoxScoreSummary") or {}
        raw = summary.get("officials") or summary.get("Officials") or []
        for o in raw:
            oid = o.get("personId") or o.get("PERSON_ID") or o.get("official_id")
            name = o.get("name")
            if not name:
                fn = (o.get("firstName") or "").strip()
                ln = (o.get("familyName") or o.get("lastName") or "").strip()
                name = (fn + " " + ln).strip()
            jersey = o.get("jerseyNum") or o.get("JERSEY_NUM") or o.get("jersey_num") or ""
            if oid:
                officials.append({
                    "game_id": clean_id_str(game_id, width=10),
                    "official_id": clean_id_str(oid),
                    "official_name": (name or "").strip(),
                    "jersey_num": clean_id_str(jersey),
                })
    except Exception:  # noqa: BLE001 - fall through to data-frame scan
        officials = []

    # Fallback: scan the endpoint's data frames for an officials-shaped table.
    if not officials:
        try:
            for df in resp.get_data_frames():
                cols = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in df.columns}
                oid_c = next((cols[k] for k in cols if "officialid" in k or "personid" in k), None)
                nm_c = next((cols[k] for k in cols if k in ("name", "officialname")), None)
                fn_c = next((cols[k] for k in cols if "firstname" in k), None)
                ln_c = next((cols[k] for k in cols if "lastname" in k or "familyname" in k), None)
                jr_c = next((cols[k] for k in cols if "jersey" in k), None)
                if oid_c and (nm_c or (fn_c and ln_c)):
                    for _, row in df.iterrows():
                        if nm_c:
                            name = str(row[nm_c]).strip()
                        else:
                            name = "{} {}".format(
                                str(row[fn_c]).strip(), str(row[ln_c]).strip()).strip()
                        officials.append({
                            "game_id": clean_id_str(game_id, width=10),
                            "official_id": clean_id_str(row[oid_c]),
                            "official_name": name,
                            "jersey_num": clean_id_str(row[jr_c]) if jr_c else "",
                        })
                    break
        except Exception:  # noqa: BLE001
            pass

    return officials


def append_and_write(existing, new_rows):
    """Concatenate new rows, dedupe, and rewrite officials.csv.gz atomically-ish."""
    if new_rows:
        add = pd.DataFrame(new_rows, columns=OFFICIALS_COLUMNS)
        combined = pd.concat([existing, add], ignore_index=True)
    else:
        combined = existing
    combined = combined.drop_duplicates(["game_id", "official_id"])
    combined.to_csv(OFFICIALS_PATH, index=False, encoding="utf-8-sig", compression="gzip")
    return combined


def main():
    if not os.path.exists(GAMES_PATH):
        print("ERROR: {} not found. Run extract_from_nbadb.py first.".format(GAMES_PATH))
        sys.exit(1)

    games = load_str_csv(GAMES_PATH)
    games["game_id"] = games["game_id"].map(lambda v: clean_id_str(v, width=10))

    if os.path.exists(OFFICIALS_PATH):
        officials = load_str_csv(OFFICIALS_PATH)
        officials["game_id"] = officials["game_id"].map(lambda v: clean_id_str(v, width=10))
        officials["official_id"] = officials["official_id"].map(clean_id_str)
    else:
        officials = pd.DataFrame(columns=OFFICIALS_COLUMNS)

    have = set(officials["game_id"].unique())

    # Games from 2000-01+ that have no officials rows.
    games["_start_year"] = games["game_id"].map(season_start_year_from_gid)
    eligible = games[
        games["_start_year"].notna() & (games["_start_year"] >= 2000)
    ]
    missing = [gid for gid in eligible["game_id"].unique() if gid and gid not in have]
    missing.sort()

    print("Games (2000-01+): {}".format(len(eligible)))
    print("Already have officials for: {}".format(len(have & set(eligible['game_id']))))
    print("Missing officials for: {} games".format(len(missing)))
    if not missing:
        print("Nothing to backfill. Done.")
        return

    est_min = len(missing) * DELAY_SECONDS / 60.0
    print("Estimated time: ~{:.1f} min at {}s/call.\n".format(est_min, DELAY_SECONDS))

    pending = []
    fetched = failed = 0
    for i, gid in enumerate(missing, 1):
        try:
            rows = extract_officials(gid)
            if rows:
                pending.extend(rows)
                fetched += 1
                status = "{} officials".format(len(rows))
            else:
                failed += 1
                status = "no officials returned"
        except Exception as e:  # noqa: BLE001
            failed += 1
            status = "ERROR: {}".format(e)
        print("  [{}/{}] {}  {}".format(i, len(missing), gid, status), flush=True)

        if len(pending) >= FLUSH_EVERY:
            officials = append_and_write(officials, pending)
            pending = []
            print("    (flushed to officials.csv.gz)")

        time.sleep(DELAY_SECONDS)

    officials = append_and_write(officials, pending)
    print("\nDone. fetched={} failed/empty={} total_officials_rows={}".format(
        fetched, failed, len(officials)))
    if failed:
        print("Some games returned no officials. Re-run to retry (present games are skipped).")


if __name__ == "__main__":
    main()
