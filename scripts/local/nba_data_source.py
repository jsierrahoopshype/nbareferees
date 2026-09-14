"""
nba_data_source.py  --  shared access to shufinskiy/nba_data (Apache-2.0), and
to the NBA<->ESPN game-id bridge built from it.

Factored out so the extraction, the bridge builder and the 2025-26 probe all
resolve dataset URLs the same way. Nothing here parses play-by-play; callers
decide what a row means.

  index  -- https://raw.githubusercontent.com/shufinskiy/nba_data/main/list_data.txt
            read at run time. URLs are never hardcoded: the repo adds a file
            per season, and a lookup picks those up without an edit here.
  cache  -- source-data/_nba_data_cache/ (gitignored; tens of MB of tar.xz,
            reproducible from the index).

NETWORK. Only the index and the archives named by it.
"""

import csv
import gzip
import io
import os
import tarfile
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
DATASET_CACHE = os.path.join(SOURCE_DIR, "_nba_data_cache")
BRIDGE_CSV = os.path.join(SOURCE_DIR, "game_id_bridge.csv.gz")

INDEX_URL = "https://raw.githubusercontent.com/shufinskiy/nba_data/main/list_data.txt"
USER_AGENT = "nbareferees/1.0 (+https://github.com/jsierrahoopshype/nbareferees)"
TIMEOUT = 60

# Archives are big enough that a truncated download is a real failure mode. A
# real one is megabytes; anything under this is an error page, not data.
MIN_ARCHIVE_BYTES = 100000


def _log(emit, msg):
    (emit or print)(msg)


def load_index(emit=None, no_download=False):
    """dataset key -> URL. Falls back to the cached copy, and says so."""
    os.makedirs(DATASET_CACHE, exist_ok=True)
    cached = os.path.join(DATASET_CACHE, "list_data.txt")
    text = None
    if no_download and os.path.exists(cached):
        text = open(cached, encoding="utf-8").read()
    else:
        try:
            req = urllib.request.Request(INDEX_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                text = resp.read().decode("utf-8", "replace")
            with open(cached, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as exc:  # noqa: BLE001
            if os.path.exists(cached):
                _log(emit, "  index fetch failed (%s); using cached copy" % exc)
                text = open(cached, encoding="utf-8").read()
            else:
                _log(emit, "  INDEX UNREACHABLE (%s) and no cached copy" % exc)
                return {}
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def ensure_dataset(key, index, emit=None, no_download=False):
    """Local path to one archive, downloading it once. None if unobtainable,
    having said why."""
    os.makedirs(DATASET_CACHE, exist_ok=True)
    path = os.path.join(DATASET_CACHE, key + ".tar.xz")
    if os.path.exists(path) and os.path.getsize(path) > MIN_ARCHIVE_BYTES:
        return path
    if no_download:
        _log(emit, "  %s not cached and --no-download given; skipped" % key)
        return None
    url = index.get(key)
    if not url:
        _log(emit, "  %s is not in the index; skipped (the repo may not publish it yet)" % key)
        return None
    # The index points at github.com/.../raw/...; some networks serve only
    # raw.githubusercontent.com. Same object either way, so fall back rather
    # than fail.
    candidates = [url]
    alt = url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/raw/", "/")
    if alt != url:
        candidates.append(alt)
    for candidate in candidates:
        try:
            _log(emit, "  downloading %s" % candidate)
            req = urllib.request.Request(candidate, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT * 10) as resp:
                data = resp.read()
            if len(data) < MIN_ARCHIVE_BYTES:
                _log(emit, "    only %d bytes -- not an archive, trying next" % len(data))
                continue
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            _log(emit, "    %.1f MB cached" % (len(data) / 1048576.0))
            return path
        except Exception as exc:  # noqa: BLE001
            _log(emit, "    failed: %s: %s" % (type(exc).__name__, exc))
    _log(emit, "  %s could not be downloaded; skipped" % key)
    return None


def iter_csv_rows(path, limit=None):
    """Raw dict rows from the .csv member(s) of one tar.xz, streamed. The
    uncompressed CSVs run to ~95MB, so nothing is held in memory."""
    n = 0
    with tarfile.open(path, "r:xz") as tf:
        for member in tf:
            if not member.isfile() or not member.name.lower().endswith(".csv"):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            reader = csv.DictReader(
                io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline=""))
            for row in reader:
                yield row
                n += 1
                if limit and n >= limit:
                    return


# --------------------------------------------------------------------------- #
# The NBA <-> ESPN game-id bridge
# --------------------------------------------------------------------------- #
def load_game_id_bridge(path=BRIDGE_CSV):
    """nba_game_id -> espn_game_id, both directions.

    Returns (nba_to_espn, espn_to_nba). Empty dicts if the bridge has not been
    built; every caller must work without it rather than assume it is there.

    Why this exists: source-data/officials.csv.gz keys 2023-24 onward by ESPN
    game id (401584690) because those seasons were built from ESPN, while
    stats.nba.com play-by-play keys everything by NBA id (0022300001). Any
    join between a play-by-play row and a crew sheet in those seasons has to
    cross that, and so will the planned L2M work, which is ESPN-dated but
    NBA-keyed at the report end.
    """
    nba_to_espn, espn_to_nba = {}, {}
    if not os.path.exists(path):
        return nba_to_espn, espn_to_nba
    with gzip.open(path, "rt", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            nba = (rec.get("nba_game_id") or "").strip()
            espn = (rec.get("espn_game_id") or "").strip()
            if nba and espn:
                nba_to_espn[nba] = espn
                espn_to_nba[espn] = nba
    return nba_to_espn, espn_to_nba
