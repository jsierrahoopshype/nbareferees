"""
probe_assignments_source.py  --  LOCAL, READ-ONLY investigation of WHERE the
referee-assignment data on official.nba.com actually comes from.

Context: the GitHub Actions reachability probe
(.github/workflows/probe-network-reachability.yml) confirmed official.nba.com
answers from a GitHub runner -- HTTP 200, ~111KB of real HTML -- but the crew
list is NOT in that initial page body. So the assignments are either rendered
client-side from an embedded JSON blob, fetched from a separate endpoint after
load, or served inside an embedded widget/iframe.

This script does not parse assignments and does not build anything. It answers
one question: what is the fetchable data source? It looks for:

  1. Embedded JSON blobs in <script> tags (application/json, JSON-LD, and
     `var X = {...}` / `window.X = {...}` assignments inside inline JS).
  2. API endpoint URLs referenced in inline JS and in linked JS bundles
     (/wp-json/, admin-ajax.php, /api/, *.json, apiUrl/endpoint/fetch(...)).
  3. WordPress REST API routes -- discovers the real REST base from the
     api.w.org <link>/Link header, lists namespaces, and filters routes for
     referee/assignment/official/crew/schedule/game wording.
  4. data-* attributes and JSON-LD carrying assignment-shaped data, plus
     <iframe>/<noscript> fallbacks (a widget embed would explain an empty
     page body just as well as client-side rendering).

The full page HTML (and every JS bundle it downloads) is cached under
source-data/_assignments_raw/ so follow-up rounds can re-analyze without
re-fetching. That directory is gitignored -- it is a scraping cache, not a
source extract.

  python scripts\\local\\probe_assignments_source.py              # fetch + analyze
  python scripts\\local\\probe_assignments_source.py --cached     # reuse cache, no network
  python scripts\\local\\probe_assignments_source.py --refresh    # force refetch
  python scripts\\local\\probe_assignments_source.py --max-bundles 20

Writes NOTHING to games.csv.gz / officials.csv.gz / any other extract. Output
is this report on stdout, appended to source-data/_probe_assignments_source.txt,
plus the gitignored HTML/JS cache.
"""

import argparse
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
CACHE_DIR = os.path.join(SOURCE_DIR, "_assignments_raw")
JS_CACHE_DIR = os.path.join(CACHE_DIR, "js")
PAGE_CACHE = os.path.join(CACHE_DIR, "referee-assignments.html")
HEADERS_CACHE = os.path.join(CACHE_DIR, "referee-assignments.headers.txt")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_assignments_source.txt")

PAGE_URL = "https://official.nba.com/referee-assignments/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

TIMEOUT = 30
DELAY_SECONDS = 1.0  # politeness between requests

# Words that would appear in a route/URL/blob actually carrying crew data.
CREW_WORDS = ("referee", "assignment", "official", "crew", "umpire", "schedule",
              "gameday", "game-day", "roster")

# Substrings that make a URL worth a closer look as a data source.
ENDPOINT_HINTS = ("/wp-json/", "admin-ajax.php", "/api/", ".json", "/graphql",
                  "/feed/", "wp-admin/admin-ajax", "/v1/", "/v2/", "/v3/")

# Bundles that are never the data source -- skip so the fetch budget goes to
# theme/app code. (Matched as substrings against the URL, case-insensitive.)
NOISE_JS = ("googletagmanager", "google-analytics", "gtag", "doubleclick",
            "facebook.net", "connect.facebook", "adobedtm", "demdex",
            "comscore", "scorecardresearch", "chartbeat", "parsely",
            "onetrust", "cookielaw", "hotjar", "newrelic", "cdn.segment",
            "jquery-migrate", "sentry", "amazon-adsystem", "adsbygoogle",
            "taboola", "outbrain", "quantserve", "krxd.net", "branch.io")

# Filenames that are MORE likely to hold the page's own data wiring.
PRIORITY_JS = ("referee", "assignment", "official", "crew", "app", "main",
               "bundle", "theme", "site", "custom", "scoreboard", "schedule")

_lines = []


def emit(msg=""):
    """Print and record. Everything printed is ASCII-safe: this runs on a
    Windows console (cp1252), where a stray non-ASCII byte from a scraped page
    would otherwise raise UnicodeEncodeError mid-report."""
    text = str(msg)
    safe = text.encode("ascii", "backslashreplace").decode("ascii")
    print(safe)
    _lines.append(safe)


def one_line(text, limit=200):
    """Collapse whitespace and truncate, for printing a snippet inline."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + " ...[+%d chars]" % (len(flat) - limit)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def decode_body(raw, content_encoding):
    """urllib does not auto-decompress; handle gzip/deflate ourselves."""
    enc = (content_encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 - report what we got rather than dying
        return raw
    return raw


def fetch(url, accept=None, retries=2):
    """GET url -> (status, headers dict, text, error). Never raises."""
    headers = dict(BASE_HEADERS)
    if accept:
        headers["Accept"] = accept
    delay = 2.0
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                hdrs = dict(resp.headers)
                status = resp.getcode()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            hdrs = dict(exc.headers or {})
            status = exc.code
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
            continue
        body = decode_body(raw, hdrs.get("Content-Encoding", hdrs.get("content-encoding", "")))
        return status, hdrs, body.decode("utf-8", "replace"), None
    return None, {}, "", last_err


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #
class PageParser(HTMLParser):
    """Collects the tags that could carry or point at the crew data. Using
    html.parser (stdlib) rather than regex so attribute extraction is reliable
    -- HTMLParser also hands back <script> bodies as raw text, unescaped."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=False)
        self.scripts = []       # {"src", "type", "id", "text"}
        self.links = []         # {"rel", "href", "type"}
        self.iframes = []       # {"src", "id", "class"}
        self.data_attrs = []    # {"tag", "attr", "value"}
        self.noscripts = []
        self._script = None
        self._in_noscript = False
        self._noscript_buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        for key, value in a.items():
            if key.startswith("data-") and value:
                self.data_attrs.append({"tag": tag, "attr": key, "value": value})
        if tag == "script":
            self._script = {"src": a.get("src", ""), "type": a.get("type", ""),
                            "id": a.get("id", "") or a.get("class", ""), "text": ""}
        elif tag == "link":
            self.links.append({"rel": a.get("rel", ""), "href": a.get("href", ""),
                               "type": a.get("type", "")})
        elif tag == "iframe":
            self.iframes.append({"src": a.get("src", "") or a.get("data-src", ""),
                                 "id": a.get("id", ""), "class": a.get("class", "")})
        elif tag == "noscript":
            self._in_noscript = True
            self._noscript_buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append(self._script)
            self._script = None
        elif tag == "noscript" and self._in_noscript:
            self.noscripts.append("".join(self._noscript_buf))
            self._in_noscript = False

    def handle_data(self, data):
        if self._script is not None:
            self._script["text"] += data
        if self._in_noscript:
            self._noscript_buf.append(data)


# --------------------------------------------------------------------------- #
# JSON blob extraction from inline JS
# --------------------------------------------------------------------------- #
ASSIGN_RE = re.compile(
    r"(?:var|let|const)?\s*"
    r"((?:window\.|self\.|globalThis\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    r"\s*=\s*(?=[\{\[])"
)


def balanced_slice(text, start):
    """Return the {...} or [...] beginning at text[start], respecting strings,
    escapes and template literals. Returns None if it never closes."""
    opener = text[start]
    closer = {"{": "}", "[": "]"}[opener]
    depth = 0
    i = start
    quote = None
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


TOP_KEY_RE = re.compile(r'["\']?([A-Za-z_$][\w$\-]*)["\']?\s*:')


def describe_blob(blob):
    """(kind, keys, count) for a candidate JSON/JS-object literal."""
    try:
        parsed = json.loads(blob)
    except ValueError:
        # Still useful: a JS object literal with unquoted keys or trailing
        # commas is not strict JSON but tells us the data shape.
        keys = []
        for m in TOP_KEY_RE.finditer(blob[:20000]):
            if m.group(1) not in keys:
                keys.append(m.group(1))
        return "js-object (not strict JSON)", keys[:25], None
    if isinstance(parsed, dict):
        return "json object", list(parsed.keys())[:25], len(parsed)
    if isinstance(parsed, list):
        first = parsed[0] if parsed else None
        keys = list(first.keys())[:25] if isinstance(first, dict) else []
        return "json array", keys, len(parsed)
    return "json scalar", [], None


def find_blobs(js_text, min_chars=120):
    """Find `NAME = {...}` / `NAME = [...]` assignments in inline JS."""
    found = []
    for m in ASSIGN_RE.finditer(js_text):
        start = m.end()
        blob = balanced_slice(js_text, start)
        if not blob or len(blob) < min_chars:
            continue
        found.append({"name": m.group(1), "blob": blob, "offset": start})
    return found


def crew_hits(text):
    lowered = text.lower()
    return [w for w in CREW_WORDS if w in lowered]


def context_snippets(text, words, before=120, after=200, limit=3):
    """One snippet per distinct location. Several crew words often sit inside
    the same blob; printing the identical excerpt once per word is just noise."""
    low = text.lower()
    out, used = [], []
    for w in words:
        idx = low.find(w)
        if idx < 0 or any(abs(idx - u) < before for u in used):
            continue
        used.append(idx)
        out.append(one_line(text[max(0, idx - before):idx + after], before + after))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# URL harvesting
# --------------------------------------------------------------------------- #
ABS_URL_RE = re.compile(r"""https?://[^\s"'<>()\\]{4,300}""")
REL_URL_RE = re.compile(r"""["'](/(?!/)[A-Za-z0-9_\-./]{3,200}(?:\?[^"']{0,200})?)["']""")
AJAX_ACTION_RE = re.compile(r"""["']?action["']?\s*:\s*["']([A-Za-z0-9_\-]{3,60})["']""")
API_VAR_RE = re.compile(
    r"""["']?((?:[A-Za-z_$][\w$]*)?(?:api|endpoint|ajax|rest|feed|url|uri|base)[\w$]*)["']?\s*[:=]\s*["']([^"']{4,300})["']""",
    re.IGNORECASE)


def harvest_urls(text, page_url):
    """Return (absolute_urls, relative_paths) deduped, order preserved."""
    absolute, relative = [], []
    for m in ABS_URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;)")
        if u not in absolute:
            absolute.append(u)
    for m in REL_URL_RE.finditer(text):
        p = m.group(1)
        if p not in relative:
            relative.append(p)
    return absolute, relative


def interesting(url):
    low = url.lower()
    return ([h for h in ENDPOINT_HINTS if h in low],
            [w for w in CREW_WORDS if w in low])


def report_urls(label, absolute, relative, page_url, limit=40):
    scored = []
    for u in absolute + [urllib.parse.urljoin(page_url, r) for r in relative]:
        hints, words = interesting(u)
        if hints or words:
            scored.append((len(words) * 2 + len(hints), u, hints, words))
    scored.sort(key=lambda t: -t[0])
    if not scored:
        emit("    no endpoint-shaped URLs found in %s" % label)
        return []
    emit("    %d endpoint-shaped URL(s) in %s (top %d):" % (len(scored), label, min(limit, len(scored))))
    for _, u, hints, words in scored[:limit]:
        tags = []
        if hints:
            tags.append("hints=%s" % ",".join(hints))
        if words:
            tags.append("CREW=%s" % ",".join(words))
        emit("      %s   [%s]" % (one_line(u, 180), "; ".join(tags)))
    return [s[1] for s in scored]


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def section(title):
    emit("")
    emit("=" * 78)
    emit(title)
    emit("=" * 78)


def load_page(args):
    """Return (html, headers) from cache or network, caching on fetch."""
    have_cache = os.path.exists(PAGE_CACHE)
    if have_cache and not args.refresh:
        with open(PAGE_CACHE, "r", encoding="utf-8", errors="replace") as fh:
            html = fh.read()
        headers = {}
        if os.path.exists(HEADERS_CACHE):
            with open(HEADERS_CACHE, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip()] = v.strip()
        age = time.time() - os.path.getmtime(PAGE_CACHE)
        emit("Using cached page: %s (%.1f hours old, %d chars)"
             % (os.path.relpath(PAGE_CACHE, REPO_ROOT), age / 3600.0, len(html)))
        emit("  (--refresh to refetch)")
        return html, headers

    if args.cached:
        emit("ERROR: --cached given but no cache at %s. Run once without --cached first."
             % os.path.relpath(PAGE_CACHE, REPO_ROOT))
        sys.exit(1)

    emit("Fetching %s ..." % PAGE_URL)
    status, headers, html, err = fetch(PAGE_URL)
    if err is not None:
        emit("UNREACHABLE: %s: %s" % (type(err).__name__, err))
        sys.exit(1)
    emit("  HTTP %s, %d chars, Content-Type: %s"
         % (status, len(html), headers.get("Content-Type", "(none)")))
    if status != 200:
        emit("  WARNING: non-200 status -- analysis below may be of an error page.")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PAGE_CACHE, "w", encoding="utf-8") as fh:
        fh.write(html)
    with open(HEADERS_CACHE, "w", encoding="utf-8") as fh:
        for k, v in headers.items():
            fh.write("%s: %s\n" % (k, v))
    emit("  cached -> %s" % os.path.relpath(PAGE_CACHE, REPO_ROOT))
    return html, headers


def analyze_scripts(parser, page_url):
    section("1. <script> TAGS: embedded JSON blobs and inline data")
    inline = [s for s in parser.scripts if not s["src"]]
    external = [s for s in parser.scripts if s["src"]]
    emit("%d script tag(s): %d inline, %d external" % (len(parser.scripts), len(inline), len(external)))

    # 1a. Typed JSON script tags (application/json, ld+json, wp templates).
    emit("")
    emit("-- typed JSON script tags --")
    typed = [s for s in inline if "json" in (s["type"] or "").lower()]
    if not typed:
        emit("  none (no application/json or application/ld+json script tags)")
    for s in typed:
        text = s["text"].strip()
        kind, keys, count = describe_blob(text) if text else ("empty", [], None)
        emit("  type=%s id/class=%s  %d chars  -> %s" % (s["type"], s["id"] or "-", len(text), kind))
        if keys:
            emit("      top-level keys: %s" % ", ".join(str(k) for k in keys))
        if count is not None:
            emit("      element/key count: %d" % count)
        hits = crew_hits(text)
        if hits:
            emit("      *** CREW WORDS: %s" % ", ".join(hits))
            for snippet in context_snippets(text, hits, before=120, after=200, limit=3):
                emit("        ...%s..." % snippet)

    # 1b. JSON-LD specifically (may carry SportsEvent objects).
    emit("")
    emit("-- JSON-LD (application/ld+json) --")
    ldjson = [s for s in typed if "ld+json" in (s["type"] or "").lower()]
    if not ldjson:
        emit("  none")
    for s in ldjson:
        try:
            parsed = json.loads(s["text"].strip())
        except ValueError as exc:
            emit("  JSON-LD present but failed to parse: %s" % exc)
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if isinstance(item, dict):
                emit("  @type=%s  keys=%s" % (item.get("@type", "?"),
                                              ", ".join(list(item.keys())[:15])))
                if "@graph" in item and isinstance(item["@graph"], list):
                    types = [g.get("@type", "?") for g in item["@graph"] if isinstance(g, dict)]
                    emit("      @graph types: %s" % ", ".join(str(t) for t in types[:20]))

    # 1c. Object literals assigned in plain inline JS.
    emit("")
    emit("-- object/array literals in inline JS (var X = {...}) --")
    total = 0
    for s in inline:
        if "json" in (s["type"] or "").lower():
            continue  # already covered above
        blobs = find_blobs(s["text"])
        for b in blobs:
            kind, keys, count = describe_blob(b["blob"])
            hits = crew_hits(b["blob"])
            # Report the big ones and anything mentioning crew words at all.
            if len(b["blob"]) < 400 and not hits:
                continue
            total += 1
            emit("  %s = <%s, %d chars>%s"
                 % (b["name"], kind, len(b["blob"]), "   *** CREW WORDS: %s" % ",".join(hits) if hits else ""))
            if keys:
                emit("      top-level keys: %s" % ", ".join(str(k) for k in keys))
            if count is not None:
                emit("      element/key count: %d" % count)
            if hits:
                low = b["blob"].lower()
                idx = low.find(hits[0])
                emit("      ...%s..." % one_line(b["blob"][max(0, idx - 150):idx + 250], 400))
    if not total:
        emit("  no sizeable literals found (nothing >=400 chars, none with crew words)")

    # 1d. WP ajax settings and named api/endpoint variables anywhere inline.
    emit("")
    emit("-- api/endpoint/ajax variables in inline JS --")
    seen = set()
    for s in inline:
        for m in API_VAR_RE.finditer(s["text"]):
            name, value = m.group(1), m.group(2)
            if not value.startswith(("http", "/")) or (name, value) in seen:
                continue
            seen.add((name, value))
            emit("  %s = %s" % (name, one_line(value, 200)))
    actions = []
    for s in inline:
        for m in AJAX_ACTION_RE.finditer(s["text"]):
            if m.group(1) not in actions:
                actions.append(m.group(1))
    if actions:
        emit("  admin-ajax action names referenced: %s" % ", ".join(actions[:30]))
    if not seen and not actions:
        emit("  none found")

    return external


def analyze_data_attrs(parser):
    section("2. data-* ATTRIBUTES and OTHER EMBEDS")
    if not parser.data_attrs:
        emit("no data-* attributes at all")
    else:
        by_name = {}
        for d in parser.data_attrs:
            by_name.setdefault(d["attr"], []).append(d)
        emit("%d data-* attribute instance(s) across %d distinct names"
             % (len(parser.data_attrs), len(by_name)))
        emit("")
        emit("-- data-* attributes whose NAME or VALUE looks assignment-related --")
        flagged = False
        for name in sorted(by_name):
            entries = by_name[name]
            name_hits = crew_hits(name)
            val_hits = [e for e in entries if crew_hits(e["value"])]
            json_vals = [e for e in entries if e["value"].strip()[:1] in "{["]
            url_vals = [e for e in entries if e["value"].strip().startswith(("http", "/wp-json", "/api"))]
            if not (name_hits or val_hits or json_vals or url_vals):
                continue
            flagged = True
            emit("  %s  (x%d, on <%s>)" % (name, len(entries), entries[0]["tag"]))
            for e in (val_hits or json_vals or url_vals)[:3]:
                emit("      %s" % one_line(e["value"], 300))
                if e["value"].strip()[:1] in "{[":
                    kind, keys, count = describe_blob(e["value"].strip())
                    emit("        -> %s, keys: %s" % (kind, ", ".join(str(k) for k in keys[:15])))
        if not flagged:
            emit("  none flagged. All data-* names seen: %s"
                 % ", ".join(sorted(by_name)[:60]))

    emit("")
    emit("-- iframes (a widget embed would also explain an empty page body) --")
    if not parser.iframes:
        emit("  none")
    for f in parser.iframes:
        emit("  src=%s  id=%s class=%s" % (one_line(f["src"], 220), f["id"] or "-", f["class"] or "-"))

    emit("")
    emit("-- <noscript> blocks (may hold a server-rendered fallback) --")
    if not parser.noscripts:
        emit("  none")
    for ns in parser.noscripts:
        hits = crew_hits(ns)
        emit("  %d chars%s" % (len(ns), "   *** CREW WORDS: %s" % ",".join(hits) if hits else ""))
        if hits:
            emit("      %s" % one_line(ns, 400))


def analyze_page_urls(html, page_url):
    section("3. URLs REFERENCED IN THE PAGE HTML")
    absolute, relative = harvest_urls(html, page_url)
    emit("  %d absolute URL(s), %d root-relative path(s) in the raw HTML"
         % (len(absolute), len(relative)))
    hosts = {}
    for u in absolute:
        host = urllib.parse.urlparse(u).netloc
        hosts[host] = hosts.get(host, 0) + 1
    emit("  hosts referenced: %s"
         % ", ".join("%s (%d)" % (h, c) for h, c in sorted(hosts.items(), key=lambda kv: -kv[1])[:20]))
    emit("")
    report_urls("page HTML", absolute, relative, page_url)


def find_rest_base(html, headers, page_url):
    """WordPress advertises its REST base via <link rel="https://api.w.org/">
    and/or a Link: header. Fall back to origin + /wp-json/."""
    m = re.search(r'<link[^>]+rel=["\']https://api\.w\.org/["\'][^>]*href=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1), 'link rel="https://api.w.org/"'
    m = re.search(r'href=["\']([^"\']+)["\'][^>]*rel=["\']https://api\.w\.org/["\']', html, re.I)
    if m:
        return m.group(1), 'link rel="https://api.w.org/" (href first)'
    link_header = headers.get("Link", headers.get("link", ""))
    m = re.search(r'<([^>]+)>;\s*rel="https://api\.w\.org/"', link_header)
    if m:
        return m.group(1), "Link: response header"
    parts = urllib.parse.urlparse(page_url)
    return "%s://%s/wp-json/" % (parts.scheme, parts.netloc), "assumed default (not advertised)"


def probe_wp_rest(html, headers, page_url, allow_network):
    section("4. WORDPRESS REST API (/wp-json/)")
    base, how = find_rest_base(html, headers, page_url)
    emit("REST base: %s" % base)
    emit("  discovered via: %s" % how)
    if not allow_network:
        emit("  --cached given: skipping live route discovery.")
        return
    emit("")
    emit("Fetching route index ...")
    time.sleep(DELAY_SECONDS)
    status, _, body, err = fetch(base, accept="application/json")
    if err is not None:
        emit("  UNREACHABLE: %s: %s" % (type(err).__name__, err))
        return
    emit("  HTTP %s, %d chars" % (status, len(body)))
    try:
        index = json.loads(body)
    except ValueError:
        emit("  Response is not JSON -- REST API may be disabled or blocked.")
        emit("  First 300 chars: %s" % one_line(body, 300))
        return

    namespaces = index.get("namespaces", [])
    routes = index.get("routes", {}) or {}
    emit("  site: %s" % one_line(index.get("name", "?"), 80))
    emit("  namespaces (%d): %s" % (len(namespaces), ", ".join(namespaces)))
    emit("  routes advertised at root: %d" % len(routes))

    emit("")
    emit("-- routes matching crew wording --")
    matched = [r for r in routes if crew_hits(r)]
    if matched:
        for r in matched[:40]:
            methods = routes[r].get("methods", []) if isinstance(routes[r], dict) else []
            emit("  *** %s   methods=%s" % (r, ",".join(methods)))
    else:
        emit("  none at the root index")

    # Custom namespaces are where a bespoke assignments endpoint would live.
    custom = [ns for ns in namespaces if not ns.startswith(("wp/", "oembed/"))]
    emit("")
    emit("-- custom (non-core) namespaces --")
    if not custom:
        emit("  none; only WordPress core namespaces are exposed")
    for ns in custom[:10]:
        ns_url = urllib.parse.urljoin(base if base.endswith("/") else base + "/", ns)
        emit("  %s -> %s" % (ns, ns_url))
        time.sleep(DELAY_SECONDS)
        st, _, nb, e = fetch(ns_url, accept="application/json")
        if e is not None:
            emit("      unreachable: %s" % e)
            continue
        try:
            nsi = json.loads(nb)
        except ValueError:
            emit("      HTTP %s but non-JSON response (%d chars)" % (st, len(nb)))
            continue
        nsroutes = list((nsi.get("routes") or {}).keys())
        emit("      HTTP %s, %d route(s)" % (st, len(nsroutes)))
        for r in nsroutes[:40]:
            flag = "  *** CREW" if crew_hits(r) else ""
            emit("        %s%s" % (r, flag))

    # The page itself as a REST object: if the crew list is stored in post
    # content or meta, this is where it shows up.
    emit("")
    emit("-- the referee-assignments page as a REST object --")
    slug_url = urllib.parse.urljoin(
        base if base.endswith("/") else base + "/",
        "wp/v2/pages?slug=referee-assignments&_fields=id,slug,link,title,modified")
    time.sleep(DELAY_SECONDS)
    st, _, pb, e = fetch(slug_url, accept="application/json")
    emit("  GET %s" % slug_url)
    if e is not None:
        emit("      unreachable: %s" % e)
        return
    emit("      HTTP %s, %d chars" % (st, len(pb)))
    try:
        pages = json.loads(pb)
    except ValueError:
        emit("      non-JSON response: %s" % one_line(pb, 200))
        return
    if not pages:
        emit("      no page with that slug (it may be a custom post type or a different slug)")
        return
    for p in pages if isinstance(pages, list) else [pages]:
        if isinstance(p, dict):
            emit("      id=%s slug=%s modified=%s" % (p.get("id"), p.get("slug"), p.get("modified")))
            emit("      link=%s" % p.get("link"))
            emit("      -> full object: %swp/v2/pages/%s"
                 % (base if base.endswith("/") else base + "/", p.get("id")))


def cache_name_for(url):
    parts = urllib.parse.urlparse(url)
    raw = (parts.netloc + parts.path + ("?" + parts.query if parts.query else ""))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    return safe[-150:] or "bundle.js"


def rank_bundles(external, page_url):
    """Order external scripts so the fetch budget goes to likely candidates."""
    ranked = []
    for s in external:
        url = urllib.parse.urljoin(page_url, s["src"])
        low = url.lower()
        if any(n in low for n in NOISE_JS):
            continue
        score = 0
        if any(p in low for p in PRIORITY_JS):
            score += 5
        if any(w in low for w in CREW_WORDS):
            score += 10
        if urllib.parse.urlparse(url).netloc.endswith("nba.com"):
            score += 3
        if "/wp-content/themes/" in low or "/wp-content/plugins/" in low:
            score += 2
        ranked.append((score, url))
    # Stable: highest score first, original order within a score.
    ranked.sort(key=lambda t: -t[0])
    seen, out = set(), []
    for _, url in ranked:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def analyze_bundles(external, page_url, args):
    section("5. LINKED JS BUNDLES: endpoints referenced inside them")
    ranked = rank_bundles(external, page_url)
    emit("%d external script(s); %d after dropping known analytics/ad noise"
         % (len(external), len(ranked)))
    if args.cached:
        emit("--cached given: only bundles already in %s will be analyzed."
             % os.path.relpath(JS_CACHE_DIR, REPO_ROOT))
    emit("Analyzing up to %d, highest-priority first." % args.max_bundles)
    os.makedirs(JS_CACHE_DIR, exist_ok=True)

    analyzed = 0
    for url in ranked:
        if analyzed >= args.max_bundles:
            emit("")
            emit("  (budget reached; %d bundle(s) not analyzed -- raise --max-bundles)"
                 % (len(ranked) - analyzed))
            break
        path = os.path.join(JS_CACHE_DIR, cache_name_for(url))
        text = None
        if os.path.exists(path) and not args.refresh:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            source = "cache"
        elif args.cached:
            continue
        else:
            time.sleep(DELAY_SECONDS)
            st, _, body, err = fetch(url, accept="*/*")
            if err is not None:
                emit("")
                emit("  %s" % one_line(url, 180))
                emit("      FETCH FAILED: %s: %s" % (type(err).__name__, err))
                analyzed += 1
                continue
            text = body
            source = "HTTP %s" % st
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        analyzed += 1

        absolute, relative = harvest_urls(text, page_url)
        hits = crew_hits(text)
        scored = [u for u in absolute + relative if any(interesting(u))]
        emit("")
        emit("  %s" % one_line(url, 180))
        emit("      %s, %d chars, cached as js/%s" % (source, len(text), os.path.basename(path)))
        if hits:
            emit("      *** CREW WORDS in bundle: %s" % ", ".join(hits))
            for snippet in context_snippets(text, hits, before=150, after=250, limit=3):
                emit("        ...%s..." % snippet)
        if scored:
            report_urls("this bundle", absolute, relative, page_url, limit=15)
        else:
            emit("      no endpoint-shaped URLs inside")
        api_vars = []
        for m in API_VAR_RE.finditer(text):
            name, value = m.group(1), m.group(2)
            if value.startswith(("http", "/")) and (name, value) not in api_vars:
                api_vars.append((name, value))
        for name, value in api_vars[:10]:
            emit("      var %s = %s" % (name, one_line(value, 180)))

    if analyzed == 0:
        emit("")
        emit("  no bundles analyzed (nothing cached yet -- rerun without --cached)"
             if args.cached else "  no bundles analyzed")


def crew_word_context(html):
    section("6. CREW WORDS IN THE RAW HTML (is it data, or just nav text?)")
    low = html.lower()
    for word in ("referee", "assignment", "crew", "official"):
        idxs = []
        start = 0
        while True:
            i = low.find(word, start)
            if i < 0 or len(idxs) >= 6:
                break
            idxs.append(i)
            start = i + 1
        emit("")
        emit("-- '%s': %d occurrence(s) total, first %d shown --"
             % (word, low.count(word), len(idxs)))
        for i in idxs:
            emit("   ...%s..." % one_line(html[max(0, i - 110):i + 190], 300))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--refresh", action="store_true",
                    help="refetch the page and all bundles, overwriting the cache")
    ap.add_argument("--cached", action="store_true",
                    help="analyze the existing cache only; make no network requests")
    ap.add_argument("--max-bundles", type=int, default=12,
                    help="max linked JS bundles to download/analyze (default 12)")
    args = ap.parse_args()

    if args.refresh and args.cached:
        print("ERROR: --refresh and --cached are contradictory.")
        sys.exit(2)

    # Windows consoles are cp1252; scraped pages are not. Never let an
    # un-encodable character kill the report.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # noqa: BLE001 - older Python, emit() still sanitizes
        pass

    emit("official.nba.com referee-assignments SOURCE probe (probe_assignments_source.py)")
    emit("=" * 78)
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("Goal: find where the crew data comes from. No parsing, no extracts touched.")
    emit("Page: %s" % PAGE_URL)

    html, headers = load_page(args)
    allow_network = not args.cached

    parser = PageParser()
    try:
        parser.feed(html)
    except Exception as exc:  # noqa: BLE001 - malformed markup should not stop the report
        emit("WARNING: HTML parse stopped early: %s: %s" % (type(exc).__name__, exc))

    external = analyze_scripts(parser, PAGE_URL)
    analyze_data_attrs(parser)
    analyze_page_urls(html, PAGE_URL)
    probe_wp_rest(html, headers, PAGE_URL, allow_network)
    analyze_bundles(external, PAGE_URL, args)
    crew_word_context(html)

    section("NEXT STEP")
    emit("Look above for, in rough order of usefulness:")
    emit("  - a /wp-json/ route or custom namespace with crew wording  -> fetch it directly")
    emit("  - an api/endpoint variable or fetch() URL in a bundle      -> replay that request")
    emit("  - a JSON blob with crew words already in the page          -> no second request needed")
    emit("  - an iframe src                                            -> the data lives in the embed")
    emit("If every section came back empty, the next round should capture the")
    emit("browser's Network tab (F12 -> Network -> XHR, reload) -- the request")
    emit("exists, it is just built at runtime in a way static analysis cannot see.")
    emit("")
    emit("Cache kept at %s (gitignored) -- rerun with --cached to re-analyze offline."
         % os.path.relpath(CACHE_DIR, REPO_ROOT))

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n\n" + "\n".join(_lines) + "\n")
    print("\n-> appended findings to %s" % os.path.relpath(OUT_PATH, REPO_ROOT))


if __name__ == "__main__":
    main()
