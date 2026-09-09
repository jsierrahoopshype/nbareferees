"""
fetch_nbra_headshots.py  --  LOCAL script (run by Jorge; needs network the
cloud sandbox lacks -- egress to nbra.net is blocked there, same restriction
noted at the top of fetch_nbra_bios.py).

Downloads each official's headshot to assets/refs/<official_id>.jpg, using the
headshot_url column that fetch_nbra_bios.py extracts from its cached bio
pages. That column exists because the upload folder varies per official
(2016/05, 2018/11, 2021/03, ...) -- the URLs cannot be constructed, only read.

  python scripts\\local\\fetch_nbra_headshots.py
  python scripts\\local\\fetch_nbra_headshots.py --limit 5      # try a few first
  python scripts\\local\\fetch_nbra_headshots.py --dry-run      # list, fetch nothing
  python scripts\\local\\fetch_nbra_headshots.py --force        # re-download everything

Behavior:
  * RESUME-SAFE. A file that already exists is skipped without a request, so
    an interrupted run costs at most the one in-flight download. Each file is
    written to a .part alongside the target and renamed only after the whole
    body is on disk and has been checked -- an interrupt can never leave a
    truncated file that later runs would skip as "already done".
  * POLITE. 1.5s between downloads, charged only on an actual fetch, never on
    a skip. Three attempts with exponential backoff. Ordinary desktop browser
    headers plus a Referer of the official's own bio page, which is where a
    browser would be loading this image from.
  * HONEST ABOUT FORMAT. The target is .jpg, but the bytes are sniffed: a PNG
    or WebP is saved with its real extension rather than mislabeled, and the
    run reports it. A response that is not an image at all is refused and
    reported, never written.
  * Writes ONLY into assets/refs/. Touches no CSV, no extract, no data file.

Run scripts/local/fetch_nbra_bios.py first (it is the thing that fills in
headshot_url); this script only reads that CSV.
"""

import argparse
import csv
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
BIOS_CSV = os.path.join(REPO_ROOT, "source-data", "nbra_bios.csv")
OUT_DIR = os.path.join(REPO_ROOT, "assets", "refs")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DELAY_SECONDS = 1.5
TIMEOUT = 45
RETRIES = 3
MIN_BYTES = 1024          # anything smaller is a placeholder or an error page
MAX_BYTES = 12 * 1024 * 1024

# Extensions the renderer knows how to look for, in preference order.
KNOWN_EXTS = (".jpg", ".png", ".webp")

# Magic bytes -> real extension. Content-Type headers lie; file signatures do not.
SIGNATURES = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
]


def sniff_ext(data):
    """Real extension from the file signature, or None if it is not an image."""
    for sig, ext in SIGNATURES:
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def existing_file(slug):
    """Path of an already-downloaded headshot in any known format, else None."""
    for ext in KNOWN_EXTS:
        path = os.path.join(OUT_DIR, slug + ext)
        if os.path.exists(path) and os.path.getsize(path) >= MIN_BYTES:
            return path
    return None


def load_rows():
    """(official_id, headshot_url, bio_url) for matched rows that have a URL."""
    if not os.path.exists(BIOS_CSV):
        print("ERROR: %s not found." % os.path.relpath(BIOS_CSV, REPO_ROOT))
        print("Run scripts/local/fetch_nbra_bios.py first.")
        sys.exit(1)
    rows = []
    with open(BIOS_CSV, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if "headshot_url" not in (reader.fieldnames or []):
            print("ERROR: %s has no headshot_url column."
                  % os.path.relpath(BIOS_CSV, REPO_ROOT))
            print("Re-run scripts/local/fetch_nbra_bios.py -- it rebuilds the CSV in")
            print("full from the existing page cache, with no new network calls.")
            sys.exit(1)
        for r in reader:
            oid = (r.get("official_id") or "").strip()
            url = (r.get("headshot_url") or "").strip()
            if oid and url:
                rows.append((oid, url, (r.get("bio_url") or "").strip()))
    return rows


def download(url, referer):
    """Return (bytes, error). Never raises."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    delay = 2.0
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                if resp.getcode() != 200:
                    raise RuntimeError("HTTP %s" % resp.getcode())
                data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None, "larger than %d bytes -- refusing" % MAX_BYTES
            if len(data) < MIN_BYTES:
                return None, "only %d bytes -- not a real image" % len(data)
            return data, None
        except Exception as exc:  # noqa: BLE001
            last = "%s: %s" % (type(exc).__name__, exc)
            if attempt < RETRIES:
                time.sleep(delay)
                delay *= 2
    return None, last


def main():
    ap = argparse.ArgumentParser(description="Download NBRA headshots to assets/refs/.")
    ap.add_argument("--limit", type=int, help="stop after N downloads (a cautious first run)")
    ap.add_argument("--force", action="store_true", help="re-download even if the file exists")
    ap.add_argument("--dry-run", action="store_true", help="list what would happen, fetch nothing")
    ap.add_argument("--delay", type=float, default=DELAY_SECONDS,
                    help="seconds between downloads (default %.1f)" % DELAY_SECONDS)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass

    rows = load_rows()
    os.makedirs(OUT_DIR, exist_ok=True)
    print("NBRA headshot downloader")
    print("=" * 78)
    print("source : %s (%d row(s) with a headshot URL)"
          % (os.path.relpath(BIOS_CSV, REPO_ROOT), len(rows)))
    print("target : %s/<official_id>.jpg" % os.path.relpath(OUT_DIR, REPO_ROOT))
    print("mode   : %s%s%s"
          % ("dry run, no downloads" if args.dry_run else "download",
             ", force re-download" if args.force else ", skipping files already on disk",
             ", limit %d" % args.limit if args.limit else ""))
    print("")

    got = skipped = failed = 0
    total_bytes = 0
    odd_formats, failures = [], []

    for oid, url, bio_url in rows:
        if args.limit and got >= args.limit:
            print("\n(limit of %d reached; %d row(s) not attempted)"
                  % (args.limit, len(rows) - got - skipped - failed))
            break

        have = existing_file(oid)
        if have and not args.force:
            skipped += 1
            continue

        if args.dry_run:
            print("  would fetch %-26s <- %s" % (oid, url.rsplit("/", 1)[-1]))
            got += 1
            continue

        data, err = download(url, bio_url)
        if err:
            failed += 1
            failures.append((oid, err))
            print("  FAIL %-26s %s" % (oid, err))
            time.sleep(args.delay)
            continue

        ext = sniff_ext(data)
        if ext is None:
            failed += 1
            failures.append((oid, "response is not an image (first bytes: %r)" % data[:8]))
            print("  FAIL %-26s not an image" % oid)
            time.sleep(args.delay)
            continue
        if ext != ".jpg":
            odd_formats.append((oid, ext))

        # Write to .part, then rename: an interrupt can never leave a
        # half-written file that a later run would skip as complete.
        final = os.path.join(OUT_DIR, oid + ext)
        part = final + ".part"
        with open(part, "wb") as fh:
            fh.write(data)
        os.replace(part, final)
        # A --force re-download that changes format would otherwise leave the
        # old file behind and shadow the new one.
        for other in KNOWN_EXTS:
            stale = os.path.join(OUT_DIR, oid + other)
            if other != ext and os.path.exists(stale):
                os.remove(stale)
                print("       (removed stale %s)" % os.path.basename(stale))

        got += 1
        total_bytes += len(data)
        print("  ok   %-26s %6.1f KB  %s" % (oid, len(data) / 1024.0, os.path.basename(final)))
        time.sleep(args.delay)

    print("")
    print("=" * 78)
    print("downloaded : %d (%.1f MB)" % (got, total_bytes / 1048576.0))
    print("skipped    : %d (already on disk)" % skipped)
    print("failed     : %d" % failed)
    if odd_formats:
        print("")
        print("Not JPEG -- saved with the real extension instead of mislabeled:")
        for oid, ext in odd_formats:
            print("  %-26s %s" % (oid, ext))
    if failures:
        print("")
        print("Failures (re-run to retry; nothing was written for these):")
        for oid, err in failures:
            print("  %-26s %s" % (oid, err))
    if not args.dry_run and got:
        print("")
        print("Next: rebuild the site so the pages pick the images up --")
        print("  python scripts/build.py && python scripts/render_pages.py")


if __name__ == "__main__":
    main()
