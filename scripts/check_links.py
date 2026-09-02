#!/usr/bin/env python3
"""
check_links.py -- site-wide dead-link scanner for the rendered static site.

Walks every *.html file under the repo, extracts local (non-http(s)/mailto,
non-bare-fragment) href targets, resolves each relative to its source file's
own directory, and reports any target that doesn't exist on disk.

Self-verifying: a link checker that always says "clean" is unfalsifiable --
nothing proves it can actually detect a broken link. This script proved
exactly that failure mode once already (docs/TIER_C_SPEC.md's QA pass
reported "zero dead links" on a render that in fact had 1,265 of them, from
a link-depth bug that predated this script). So every run of main() first
executes self_test(): it takes a real copy of a real rendered page, confirms
the checker reports it clean, injects one deliberately-broken href, confirms
the checker flags exactly that one, then removes the injection and confirms
clean again. If that round-trip doesn't behave, the script refuses to report
on the real site at all -- a "0 dead links" result is only ever printed after
the tool has just demonstrated it can find one.

Run from repo root:  python3 scripts/check_links.py
"""

import os
import re
import sys
import shutil
import tempfile
from urllib.parse import urlsplit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HREF_RE = re.compile(r'href="([^"]+)"')

# Top-level entries a rendered page's relative links can plausibly resolve
# into. Kept as an explicit list (not "everything in REPO") so the self-test
# fixture below only pulls in real site content, never .git/scripts/source-data.
SITE_ENTRIES = [
    "index.html", "referee", "team", "player", "leaderboard", "crews",
    "team-officials", "debuts", "eras", "swings", "compare", "matchup", "sources",
    "assets", "data",
]


def find_dead_links(root):
    """Scan every *.html under root; return a list of
    (source_file, href, resolved_target) for every local link whose target
    doesn't exist. External (http/https/mailto) links and bare "#..."
    fragments are skipped -- this checker only verifies same-site paths."""
    html_files = []
    for dirpath, _dirs, files in os.walk(root):
        if os.path.join(root, ".git") in dirpath or "/.git" in dirpath:
            continue
        for f in files:
            if f.endswith(".html"):
                html_files.append(os.path.join(dirpath, f))

    dead = []
    for hf in html_files:
        with open(hf, encoding="utf-8") as fh:
            content = fh.read()
        base_dir = os.path.dirname(hf)
        for m in HREF_RE.finditer(content):
            href = m.group(1)
            if href.startswith("#"):
                continue
            parts = urlsplit(href)
            if parts.scheme in ("http", "https", "mailto"):
                continue
            if not parts.path:
                continue
            target = os.path.normpath(os.path.join(base_dir, parts.path))
            if not os.path.exists(target):
                dead.append((hf, href, target))
    return html_files, dead


def self_test():
    """Prove the checker can both pass clean AND catch a real break, using a
    real copy of a real rendered page (index.html -- the same page whose
    depth-relative links were the actual bug this checker exists to catch).

    The fixture tree symlinks the real site's top-level dirs (referee/,
    team/, etc. -- tens/hundreds of MB, so symlinked rather than copied) and
    places a real, independently-owned COPY of index.html at the fixture
    root, so mutating it never touches the tracked file."""
    real_index = os.path.join(REPO, "index.html")
    if not os.path.exists(real_index):
        print("[self-test] SKIPPED: no rendered index.html to copy from "
              "(run render_pages.py first)")
        return None

    tmpdir = tempfile.mkdtemp(prefix="check_links_selftest_")
    try:
        # Symlink every other top-level entry (referee/, team/, etc. -- tens
        # to hundreds of MB, so symlinked rather than copied) so the page's
        # genuine relative links resolve against real site content, making
        # the "clean" result below real rather than vacuous. index.html
        # itself is a real, independently-owned copy -- the one file this
        # self-test mutates -- so nothing here touches the tracked file.
        for entry in SITE_ENTRIES:
            if entry == "index.html":
                continue
            src = os.path.join(REPO, entry)
            if os.path.exists(src):
                os.symlink(src, os.path.join(tmpdir, entry))
        shutil.copy(real_index, os.path.join(tmpdir, "index.html"))
        target_page = os.path.join(tmpdir, "index.html")

        with open(target_page, encoding="utf-8") as fh:
            original = fh.read()

        # Step 1: a real copy of a real page, unmodified, must come back clean.
        _files, dead = find_dead_links(tmpdir)
        if dead:
            print("[self-test] FAILED: unmodified copy of index.html already "
                  "reports dead links (fixture problem, not a real site bug):")
            for hf, href, tgt in dead[:5]:
                print("    ", hf, "->", href)
            return False

        # Step 2: inject one deliberately-broken href; the checker must catch
        # exactly it.
        needle = 'href="referee/____self-test-injected-dead-link____/index.html"'
        injected = original.replace(
            "<body>", '<body><a ' + needle + '>injected</a>', 1)
        if injected == original:
            print("[self-test] FAILED: could not inject test href "
                  "(index.html structure changed -- update this self-test)")
            return False
        with open(target_page, "w", encoding="utf-8") as fh:
            fh.write(injected)

        _files, dead = find_dead_links(tmpdir)
        hit = [d for d in dead if "____self-test-injected-dead-link____" in d[1]]
        if not hit:
            print("[self-test] FAILED: injected a dead link and the checker "
                  "did not flag it -- the checker cannot be trusted")
            return False
        if len(dead) != 1:
            print("[self-test] FAILED: expected exactly 1 dead link after "
                  "injection (the one just added), found %d" % len(dead))
            return False

        # Step 3: remove the injection; the checker must go clean again (not
        # just "always reports 1", proving the flag tracks the actual href).
        with open(target_page, "w", encoding="utf-8") as fh:
            fh.write(original)
        _files, dead = find_dead_links(tmpdir)
        if dead:
            print("[self-test] FAILED: removing the injected href should "
                  "clear the finding, but the checker still reports:")
            for hf, href, tgt in dead[:5]:
                print("    ", hf, "->", href)
            return False

        print("[self-test] PASSED: clean before injection, catches the "
              "injected dead link, clean again after removing it.")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ok = self_test()
    if ok is False:
        print("\nRefusing to trust a real-site scan: the checker just failed "
              "its own self-test. Fix check_links.py before relying on its "
              "output.")
        sys.exit(1)

    print()
    html_files, dead = find_dead_links(REPO)
    total_links = 0
    for hf in html_files:
        with open(hf, encoding="utf-8") as fh:
            content = fh.read()
        for m in HREF_RE.finditer(content):
            href = m.group(1)
            if href.startswith("#"):
                continue
            parts = urlsplit(href)
            if parts.scheme in ("http", "https", "mailto") or not parts.path:
                continue
            total_links += 1

    print("HTML files scanned:", len(html_files))
    print("Total local links checked:", total_links)
    print("Dead links found:", len(dead))
    for hf, href, target in dead[:50]:
        print("  DEAD:", os.path.relpath(hf, REPO), "->", href,
              "resolved:", os.path.relpath(target, REPO))
    if len(dead) > 50:
        print("  ... and %d more" % (len(dead) - 50))

    if dead:
        sys.exit(1)


if __name__ == "__main__":
    main()
