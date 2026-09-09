"""
probe_rest_namespaces.py  --  LOCAL, READ-ONLY. Finds official.nba.com's REST
routes when the route index refuses to list them.

Where this comes from: the first source probe found that
https://official.nba.com/wp-json/ returns 404, but
/wp-json/wp/v2/pages?slug=referee-assignments returns 200. So the REST API is
alive with its index disabled -- a common WordPress hardening setting. Custom
namespaces may well exist; they just are not advertised. Discovery therefore
has to be by direct request, not by reading the index.

The trick that makes guessing productive is WordPress's own error codes. A
namespace or route that does not exist answers 404 with JSON
{"code":"rest_no_route",...}. That is a DIFFERENT answer from:

  - 200                                    -> the route exists and is public
  - 401/403 rest_forbidden/not_logged_in   -> the route EXISTS but needs auth
  - 404 with HTML instead of JSON          -> an edge/CDN/security layer, not WP
  - rest_disabled                          -> the REST API is switched off

So every probe below is interpretable, and "no route" is a real answer rather
than a dead end.

Four tiers:
  0. Diagnostics -- confirm the index-disabled reading (root vs wp/v2 index vs
     a known-good route), and list post types via wp/v2/types, since a custom
     post type is the most likely home for daily crew data.
  1. Namespace roots -- /wp-json/nba/v1/, officials/v1, assignments/v1, etc.
  2. Route guesses under the namespaces that answered, plus a small blind set
     under the most plausible ones (--deep widens this).
  3. The full page object (wp/v2/pages/6203, every field) to see whether the
     crew list lives in post content, post meta, ACF fields, or is produced by
     a shortcode or a page template.

  python scripts\\local\\probe_rest_namespaces.py
  python scripts\\local\\probe_rest_namespaces.py --deep
  python scripts\\local\\probe_rest_namespaces.py --page-id 6203

Read-only: GET requests only, no auth, nothing written to any extract. Report
goes to stdout and appends to source-data/_probe_rest_namespaces.txt.
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_rest_namespaces.txt")

DEFAULT_BASE = "https://official.nba.com/wp-json/"
DEFAULT_PAGE_ID = 6203

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
DELAY_SECONDS = 1.0

CREW_WORDS = ("referee", "assignment", "official", "crew", "umpire", "gameday",
              "game-day", "schedule", "roster")

# Namespace guesses. Ordered most- to least-plausible; the first few also get
# blind route probes in tier 2 even if their root does not answer.
NAMESPACES = [
    "nba/v1", "nba/v2", "officials/v1", "assignments/v1", "referees/v1",
    "referee/v1", "official/v1", "officiating/v1", "assignment/v1",
    "crew/v1", "crews/v1", "nba-officials/v1", "nbaofficial/v1",
    "nba-official/v1", "gameday/v1", "games/v1", "game/v1", "schedule/v1",
    "scores/v1", "stats/v1", "api/v1", "custom/v1", "theme/v1", "site/v1",
    "acf/v3",
]

# Route names to try under a namespace.
ROUTE_GUESSES = [
    "referee-assignments", "referee_assignments", "assignments", "assignment",
    "referees", "officials", "crews", "crew", "games", "schedule", "today",
]

# Namespaces worth blind route probes even when the root gives rest_no_route
# (a namespace root index can be disabled while its routes still answer).
BLIND_NAMESPACES = ["nba/v1", "officials/v1", "assignments/v1", "referees/v1"]
BLIND_ROUTES = ["referee-assignments", "assignments", "officials", "crews"]

_lines = []


def emit(msg=""):
    """Print and record, ASCII-safe for a cp1252 Windows console."""
    text = str(msg)
    print(text.encode("ascii", "backslashreplace").decode("ascii"))
    _lines.append(text.encode("ascii", "backslashreplace").decode("ascii"))


def one_line(text, limit=220):
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + " ...[+%d chars]" % (len(flat) - limit)


def crew_hits(text):
    low = str(text).lower()
    return [w for w in CREW_WORDS if w in low]


def crew_hits_path(url):
    """crew_hits on a URL's path/query only. The host is official.nba.com, so
    matching the whole URL would flag 'official' on literally every row."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        return crew_hits(parts.path + "?" + parts.query)
    except ValueError:
        return crew_hits(url)


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


def get(url):
    """GET -> (status, ctype, text, error). Never raises."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw, hdrs, status = resp.read(), dict(resp.headers), resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        hdrs = dict(exc.headers or {})
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return None, "", "", exc
    body = decode_body(raw, hdrs.get("Content-Encoding", hdrs.get("content-encoding", "")))
    return status, hdrs.get("Content-Type", ""), body.decode("utf-8", "replace"), None


# --------------------------------------------------------------------------- #
# The interpretation layer -- what a given answer actually means
# --------------------------------------------------------------------------- #
# verdict -> (label, is_a_hit)
VERDICTS = {
    "OK": ("200 -- route exists and is public", True),
    "AUTH": ("exists but requires authentication", True),
    "NO_ROUTE": ("rest_no_route -- WP answered, this route does not exist", False),
    "REST_OFF": ("REST API disabled", False),
    "NOT_WP": ("non-JSON body -- an edge/CDN/security layer answered, not WP", False),
    "ERROR": ("request failed", False),
    "OTHER": ("unclassified response", False),
}

AUTH_CODES = ("rest_forbidden", "rest_not_logged_in", "rest_cannot_read",
              "rest_cannot_view", "rest_forbidden_context")


def classify(status, ctype, body, err):
    """Return (verdict, code, parsed_or_None)."""
    if err is not None:
        return "ERROR", "%s: %s" % (type(err).__name__, err), None
    text = (body or "").strip()
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        return "NOT_WP", "non-JSON (%s)" % (ctype or "no content-type"), None
    if parsed is None:
        return "OTHER", "empty body", None

    code = parsed.get("code") if isinstance(parsed, dict) else None
    if code:
        if code == "rest_no_route":
            return "NO_ROUTE", code, parsed
        if code in AUTH_CODES or status in (401, 403):
            return "AUTH", code, parsed
        if "disabled" in str(code):
            return "REST_OFF", code, parsed
        return "OTHER", code, parsed
    if status == 200:
        return "OK", "", parsed
    return "OTHER", "HTTP %s" % status, parsed


def probe(url, label=None):
    time.sleep(DELAY_SECONDS)
    status, ctype, body, err = get(url)
    verdict, code, parsed = classify(status, ctype, body, err)
    desc, is_hit = VERDICTS[verdict]
    mark = "  *** " if is_hit else "      "
    emit("%s%-52s HTTP %-5s %s%s"
         % (mark, label or url.replace(DEFAULT_BASE, ""), status if status else "-",
            verdict, "  (%s)" % code if code else ""))
    return {"url": url, "status": status, "verdict": verdict, "code": code,
            "parsed": parsed, "body": body, "is_hit": is_hit}


def routes_of(parsed):
    if isinstance(parsed, dict) and isinstance(parsed.get("routes"), dict):
        return list(parsed["routes"].keys())
    return []


def section(title):
    emit("")
    emit("=" * 78)
    emit(title)
    emit("=" * 78)


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #
def tier0_diagnostics(base, page_id):
    section("TIER 0: is the index really disabled, and what post types exist?")
    emit("Legend: *** marks a response worth chasing.")
    emit("")
    root = probe(base, "/ (route index)")
    wpv2 = probe(urllib.parse.urljoin(base, "wp/v2"), "wp/v2 (namespace index)")
    known = probe(urllib.parse.urljoin(base, "wp/v2/pages?slug=referee-assignments&_fields=id,slug"),
                  "wp/v2/pages?slug=referee-assignments (known good)")

    emit("")
    emit("-- reading --")
    if known["verdict"] == "OK" and root["verdict"] != "OK":
        if wpv2["verdict"] == "OK":
            emit("  Only the ROOT index is blocked. Namespace indexes still answer, so a")
            emit("  custom namespace root in tier 1 should list its own routes.")
        else:
            emit("  BOTH the root and the wp/v2 index are blocked, but a concrete route")
            emit("  works. Index listing is off across the board -- namespace roots in")
            emit("  tier 1 will likely 404 even where the namespace exists, so tier 2's")
            emit("  full-route guesses carry the weight. Read a rest_no_route on a")
            emit("  namespace root as 'no answer', not as proof of absence.")
    elif root["verdict"] == "OK":
        emit("  The root index answered after all -- routes are listed above; the")
        emit("  earlier 404 may have been transient or edge-cached.")
        for r in routes_of(root["parsed"]):
            if crew_hits(r):
                emit("    *** %s" % r)
    else:
        emit("  The known-good route did not answer either. Something changed since the")
        emit("  last probe (or this network is being blocked); treat everything below")
        emit("  with suspicion.")

    # Custom post types are the most likely home for daily crew data.
    type_routes = []
    types = probe(urllib.parse.urljoin(base, "wp/v2/types"), "wp/v2/types (post type list)")
    if types["verdict"] == "OK" and isinstance(types["parsed"], dict):
        emit("")
        emit("-- registered post types (rest_base is the route under wp/v2/) --")
        for slug, info in types["parsed"].items():
            if not isinstance(info, dict):
                continue
            rest_base = info.get("rest_base") or "(not REST-exposed)"
            ns = info.get("rest_namespace", "wp/v2")
            hits = crew_hits(slug) + crew_hits(info.get("name", "")) + crew_hits(str(rest_base))
            flag = "  *** CREW WORDS" if hits else ""
            # Every non-core type is worth fetching; a crew-named one doubly so.
            if info.get("rest_base") and slug not in ("post", "page", "attachment",
                                                      "nav_menu_item", "wp_block",
                                                      "wp_template", "wp_template_part",
                                                      "wp_navigation", "wp_font_family",
                                                      "wp_font_face", "wp_global_styles"):
                route = "%s/%s" % (info.get("rest_namespace", "wp/v2"), info["rest_base"])
                if route not in type_routes:
                    type_routes.insert(0, route) if hits else type_routes.append(route)
            emit("  %-24s name=%-24s rest_base=%-22s ns=%s%s"
                 % (slug, one_line(info.get("name", "?"), 24), rest_base, ns, flag))
        emit("")
        emit("  Any type flagged above is directly fetchable at:")
        emit("    %swp/v2/<rest_base>?per_page=100" % base)
    return type_routes


def tier1_namespace_roots(base):
    section("TIER 1: namespace roots tried directly (index not required)")
    results = {}
    for ns in NAMESPACES:
        url = urllib.parse.urljoin(base, ns)
        results[ns] = probe(url, ns)
    hits = [ns for ns, r in results.items() if r["is_hit"]]
    emit("")
    emit("-- %d of %d namespace roots answered with something other than 'no route' --"
         % (len(hits), len(results)))
    for ns in hits:
        r = results[ns]
        emit("  *** %s -> %s (%s)" % (ns, r["verdict"], r["code"] or "-"))
        for route in routes_of(r["parsed"]):
            flag = "  *** CREW" if crew_hits(route) else ""
            emit("        %s%s" % (route, flag))
    if not hits:
        emit("  none. Either these namespaces do not exist, or namespace-root indexes")
        emit("  are disabled too -- tier 2 distinguishes those cases.")
    return results


def tier2_routes(base, ns_results, deep, type_routes=()):
    section("TIER 2: concrete routes (custom post types first, then namespaces)")
    # Namespaces that answered get their real routes probed; the most plausible
    # ones get blind guesses regardless, since a root can be blocked while its
    # routes answer.
    # A custom post type found in tier 0 is the single most likely home for
    # daily crew data, so it goes to the front of the queue.
    plan = [("", "%s?per_page=3" % r) for r in type_routes]
    for ns, r in ns_results.items():
        advertised = routes_of(r["parsed"])
        if advertised:
            for route in advertised:
                if crew_hits(route) or deep:
                    plan.append((ns, route.lstrip("/")))
        elif r["is_hit"]:
            for route in (ROUTE_GUESSES if deep else BLIND_ROUTES):
                plan.append((ns, route))
    for ns in BLIND_NAMESPACES:
        for route in (ROUTE_GUESSES if deep else BLIND_ROUTES):
            if (ns, route) not in plan:
                plan.append((ns, route))

    # De-dupe while preserving order, and keep the request count sane.
    seen, ordered = set(), []
    for item in plan:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    cap = 200 if deep else 60
    if len(ordered) > cap:
        emit("  (%d candidates, capped at %d -- use --deep to widen)" % (len(ordered), cap))
        ordered = ordered[:cap]
    emit("Probing %d concrete route(s), ~%.0fs at %.1fs apart."
         % (len(ordered), len(ordered) * DELAY_SECONDS, DELAY_SECONDS))
    emit("")

    hits = []
    for ns, route in ordered:
        path = "%s/%s" % (ns.rstrip("/"), route) if ns else route
        r = probe(urllib.parse.urljoin(base, path), path)
        if r["is_hit"]:
            hits.append((path, r))
    emit("")
    if not hits:
        emit("-- no route answered. Every probe above came back rest_no_route (WP")
        emit("   replying normally, route absent) or failed outright. --")
    for path, r in hits:
        emit("-- *** %s: %s --" % (path, VERDICTS[r["verdict"]][0]))
        if r["verdict"] == "OK":
            body = r["body"]
            emit("   %d chars; crew words: %s" % (len(body), ", ".join(crew_hits(body)) or "none"))
            emit("   %s" % one_line(body, 600))
        else:
            emit("   %s" % one_line(r["body"], 300))
    return hits


def describe_value(value, indent="      "):
    """Print a JSON value compactly enough to see its shape."""
    if isinstance(value, dict):
        emit("%skeys: %s" % (indent, ", ".join(list(value.keys())[:25])))
    elif isinstance(value, list):
        emit("%slist of %d" % (indent, len(value)))
        if value and isinstance(value[0], dict):
            emit("%sfirst element keys: %s" % (indent, ", ".join(list(value[0].keys())[:25])))
    else:
        emit("%s%s" % (indent, one_line(value, 300)))


SHORTCODE_RE = re.compile(r"\[([a-zA-Z0-9_\-]{3,40})(\s[^\]]{0,200})?\]")


def tier3_page_object(base, page_id):
    section("TIER 3: the full page object (wp/v2/pages/%s, all fields)" % page_id)
    url = urllib.parse.urljoin(base, "wp/v2/pages/%s" % page_id)
    emit("GET %s" % url)
    r = probe(url, "wp/v2/pages/%s" % page_id)
    if r["verdict"] != "OK" or not isinstance(r["parsed"], dict):
        emit("  Could not read the page object; nothing more to say here.")
        return
    page = r["parsed"]
    emit("")
    emit("-- top-level fields --")
    emit("  %s" % ", ".join(sorted(page.keys())))
    emit("")
    emit("  slug:     %s" % page.get("slug"))
    emit("  link:     %s" % page.get("link"))
    emit("  modified: %s   (how fresh is this page's stored content?)" % page.get("modified"))
    # A named page template means rendering happens in a theme PHP file, which
    # would explain content being absent from the stored post content.
    emit("  template: %s" % (page.get("template") or "(default)"))

    content = ((page.get("content") or {}).get("rendered") or "") if isinstance(page.get("content"), dict) else ""
    raw = ((page.get("content") or {}).get("raw") or "") if isinstance(page.get("content"), dict) else ""
    emit("")
    emit("-- content --")
    emit("  content.rendered: %d chars" % len(content))
    if raw:
        emit("  content.raw:      %d chars (unusual without auth -- worth reading)" % len(raw))
    hits = crew_hits(content)
    emit("  crew words in rendered content: %s" % (", ".join(hits) if hits else "none"))
    if content:
        emit("  first 400 chars: %s" % one_line(content, 400))
    for label, blob in (("rendered", content), ("raw", raw)):
        if not blob:
            continue
        codes = []
        for m in SHORTCODE_RE.finditer(blob):
            name = m.group(1)
            if name.lower() in ("caption", "embed", "gallery", "video", "audio"):
                continue
            if name not in codes:
                codes.append(name)
                attrs = one_line(m.group(2) or "", 120)
                emit("  *** shortcode in content.%s: [%s%s]"
                     % (label, name, " " + attrs if attrs else ""))
        if not codes:
            emit("  no shortcodes in content.%s" % label)

    # Where a plugin would stash structured data.
    for field in ("meta", "acf", "yoast_head_json", "class_list", "excerpt"):
        if field in page and page[field] not in (None, "", [], {}):
            emit("")
            emit("-- %s --" % field)
            describe_value(page[field])
            blob = json.dumps(page[field])[:20000]
            fhits = crew_hits(blob)
            if fhits:
                emit("      *** CREW WORDS: %s" % ", ".join(fhits))
                low = blob.lower()
                idx = low.find(fhits[0])
                emit("      ...%s..." % one_line(blob[max(0, idx - 150):idx + 300], 450))

    links = page.get("_links")
    if isinstance(links, dict):
        emit("")
        emit("-- _links (related endpoints WP itself advertises for this page) --")
        for rel, entries in links.items():
            hrefs = [e.get("href", "") for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            flag = "  *** CREW" if crew_hits(rel) or any(crew_hits_path(h) for h in hrefs) else ""
            emit("  %-34s %s%s" % (rel, one_line(", ".join(hrefs), 150), flag))


def main():
    ap = argparse.ArgumentParser(description="Probe official.nba.com REST routes directly.")
    ap.add_argument("--base", default=DEFAULT_BASE, help="REST base (default %s)" % DEFAULT_BASE)
    ap.add_argument("--page-id", type=int, default=DEFAULT_PAGE_ID,
                    help="page ID to fetch in full (default %d)" % DEFAULT_PAGE_ID)
    ap.add_argument("--deep", action="store_true",
                    help="probe every route guess under every namespace, not just the likely ones")
    ap.add_argument("--delay", type=float, default=DELAY_SECONDS,
                    help="seconds between requests (default %.1f)" % DELAY_SECONDS)
    args = ap.parse_args()

    globals()["DELAY_SECONDS"] = args.delay
    base = args.base if args.base.endswith("/") else args.base + "/"

    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass

    emit("official.nba.com REST namespace probe (probe_rest_namespaces.py)")
    emit("=" * 78)
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("REST base: %s" % base)
    emit("Premise: the route index 404s but concrete routes answer, so namespaces")
    emit("are probed directly. WordPress's rest_no_route error makes a miss")
    emit("distinguishable from a block, which is what makes guessing worthwhile.")

    type_routes = tier0_diagnostics(base, args.page_id)
    ns_results = tier1_namespace_roots(base)
    route_hits = tier2_routes(base, ns_results, args.deep, type_routes)
    tier3_page_object(base, args.page_id)

    section("SUMMARY")
    ns_hits = [ns for ns, r in ns_results.items() if r["is_hit"]]
    emit("namespace roots that answered: %s" % (", ".join(ns_hits) if ns_hits else "none"))
    emit("concrete routes that answered: %s"
         % (", ".join(p for p, _ in route_hits) if route_hits else "none"))
    emit("")
    if route_hits or ns_hits:
        emit("Next: fetch the answering route(s) above and look at the payload shape.")
    else:
        emit("Nothing custom answered. That points away from a bespoke REST endpoint")
        emit("and toward one of: a custom post type under wp/v2 (see tier 0's type")
        emit("list), content stored in the page object (tier 3), or a non-REST")
        emit("admin-ajax call. The JS analysis in analyze_official_js.py speaks to")
        emit("the last of those.")

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n\n" + "\n".join(_lines) + "\n")
    print("\n-> appended findings to %s" % os.path.relpath(OUT_PATH, REPO_ROOT))


if __name__ == "__main__":
    main()
