"""
fetch_nbra_bios.py  --  LOCAL script (run by Jorge; needs network the cloud
sandbox lacks -- egress to nbra.net from that sandbox was tested and is
blocked, same class of restriction as stats.nba.com's datacenter block noted
in backfill_officials.py).

Scrapes nbra.net's referee-biographies index for every CURRENT official's
jersey number and bio-page URL, then each individual bio page for whatever
factual fields exist consistently (years of NBA experience, college,
hometown, and anything else structured), and matches each one against our
canonical data/referees.json.

  python scripts\\local\\fetch_nbra_bios.py

  pip install beautifulsoup4

IMPORTANT -- originally written blind (this environment's egress to nbra.net
is confirmed blocked), then corrected against real cached markup from the
first actual run. Confirmed real-world quirks baked into the fixes below:
  * The index page's jersey number sits immediately BEFORE the name in the
    SAME text node ("48 Scott Foster", no "#"/"No." marker) -- see
    LEADING_NUM_NAME_RE / try_name_jersey.
  * nbra.net serves some punctuation as genuinely-valid UTF-8 bytes that
    happen to decode to the WRONG characters (e.g. "...ÔÇÖ86" for "...'86")
    -- upstream corruption already baked into their served bytes, not a
    decode mistake on this end. See fix_mojibake's docstring for exactly
    what it reverses and why it's safe to apply unconditionally.
Every fetched page is still cached to disk BEFORE parsing
(source-data/_nbra_raw/, gitignored, local only) and parsing is a pure
function over that cache -- so if anything else comes up empty or wrong,
share what printed (or the cached HTML) back and the extraction logic can be
corrected and re-run against the cache with zero new network calls.

Behavior:
  * 1s polite delay between HTTP requests (index page + each bio page) --
    only charged on an actual network fetch, never on a cache hit.
  * Resume-safe via the raw-HTML cache: a re-run skips any URL already on
    disk, so an interrupted run loses at most the one in-flight request.
    Delete a file from source-data/_nbra_raw/ to force a re-fetch of just
    that page.
  * Standard desktop browser headers (User-Agent/Accept/Accept-Language) --
    a plain, honest identification, not an attempt to look like anything
    other than a script.
  * The output CSV is always rebuilt in full from whatever is in the cache,
    so re-running after fixing a parsing bug reflects that fix for every
    already-cached page, not just new ones.

Matching (the hard part -- do NOT fuzzy-match):
  * Every NBRA official is matched against data/referees.json by
    build.py's norm_ref_key(), applied to the name AS DISPLAYED on the bio
    page -- never derived from the URL slug, which is known to diverge from
    the display name for some officials (e.g. "Josh Tiven" at the URL
    /joshua-tiven/, "Che Flores" at /cheryl-flores/ -- both already match
    fine on display-name text alone, exactly because the display name is
    what norm_ref_key sees).
  * norm_ref_key's existing normalization (strip periods/apostrophes,
    collapse whitespace/hyphens, case-fold) already resolves some of the
    known index-page textual variants for free -- e.g. "Sha'rae Mitchell"
    vs. our "Sha'Rae Mitchell", and "J.T. Orr" vs. our "J.T. Orr" however
    NBRA happens to punctuate it that day. No override needed for those.
  * data/nbra_identity_overrides.csv is a pre-seeded escape hatch (same
    file format/columns as data/referee_identity_overrides.csv) for the
    real identity mismatches normalization can't resolve on its own --
    seeded here with the two known ones: "Lauren Holtkamp-Sterling" (a
    second surname we don't carry) and "Dannica Mosher" (a married name;
    we canonically use "Baroody").
  * Anything left unmatched after overrides is printed in full (name,
    jersey number, bio URL) for manual resolution -- never guessed.
  * Expect roughly 73 of our 166 to match: NBRA's biography index lists
    only CURRENT officials, a smaller and not-identical-to-our-"active"
    population.

Writes:
  source-data/nbra_bios.csv -- one row per NBRA official found, matched or
  not (a match_status column marks which), so nothing scraped is silently
  dropped even before the unmatched ones are resolved by hand.
"""

import os
import re
import sys
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 not installed.")
    print("       pip install beautifulsoup4")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
DATA_DIR = os.path.join(REPO_ROOT, "data")
CACHE_DIR = os.path.join(SOURCE_DIR, "_nbra_raw")
OUT_CSV = os.path.join(SOURCE_DIR, "nbra_bios.csv")
OVERRIDE_CSV = os.path.join(DATA_DIR, "nbra_identity_overrides.csv")
REFEREES_JSON = os.path.join(DATA_DIR, "referees.json")

INDEX_URL = "https://www.nbra.net/nba-officials/referee-biographies/"
INDEX_PATH_PREFIX = "/nba-officials/referee-biographies"

# Reuse (do not duplicate) build.py's exact name normalization -- the same
# function every other identity-matching problem in this project already
# goes through. Side-effect-safe: build.py's work is behind __main__.
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
from build import norm_ref_key  # noqa: E402

DELAY_SECONDS = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

NAME_LIKE_RE = re.compile(r"^[A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*)+$")
# NBRA's real index format: the jersey number sits immediately BEFORE the
# name in the same text node ("48 Scott Foster"), no "#"/"No." marker at all
# -- discovered on the first real run, when JERSEY_RE1/JERSEY_RE2 below (the
# only patterns originally handled) came up empty on every row and every name
# fell through all the way to the URL-slug guess (a bare leading digit fails
# NAME_LIKE_RE, which requires the string to START with a letter).
LEADING_NUM_NAME_RE = re.compile(
    r"^\s*(\d{1,3})\s+([A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*)+)\s*$")
JERSEY_RE1 = re.compile(r"#\s*(\d{1,3})\b")
JERSEY_RE2 = re.compile(r"\bNo\.?\s*(\d{1,3})\b", re.IGNORECASE)

# Known-field synonyms for the generic label:value scan on each bio page.
# Anything scanned that doesn't match one of these lands in extra_fields
# instead of being dropped -- see extract_labeled_facts's docstring.
FIELD_SYNONYMS = {
    "years_experience": [
        "years of nba experience", "nba experience", "years experience",
        "years in the nba", "experience", "seasons of nba experience",
    ],
    "college": ["college", "university", "alma mater"],
    "hometown": ["hometown", "home town", "from", "birthplace", "born"],
}
CSV_FIELDS = [
    "official_id", "match_status", "nbra_name", "name_source", "jersey_num",
    "years_experience", "college", "hometown", "bio_url", "extra_fields",
]


def fix_mojibake(text):
    """Repairs a specific upstream corruption confirmed on the first real run:
    nbra.net serves some punctuation as genuinely-valid UTF-8 bytes that
    decode (correctly, no error) to the WRONG characters -- e.g.
    "Old Dominion University ÔÇÖ86" for what should read
    "...'86" (a right single quotation mark). This isn't a decode mistake on
    this script's end (the HTTP response bytes ARE read as UTF-8, correctly,
    both here and in fetch_cached); the corruption is already baked into the
    bytes nbra.net serves, consistent with their own content pipeline having
    read genuinely-UTF-8 text as CP850 (a legacy DOS/OEM code page) at some
    point and re-saved the result as new, validly-encoded-but-wrong UTF-8.

    Reversing that exact misread -- re-encode as cp850, re-decode as UTF-8 --
    recovers the original text. Verified safe against plain ASCII and against
    genuinely-correct accented text (encoding one correct accented character
    as cp850 and decoding the result as UTF-8 fails outright with either
    UnicodeEncodeError or UnicodeDecodeError, so this never touches text that
    wasn't actually corrupted this specific way -- it's a no-op on it)."""
    try:
        return text.encode("cp850").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def hr(title=""):
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


# --------------------------------------------------------------------------- #
# fetch + cache
# --------------------------------------------------------------------------- #
def cache_path_for(url):
    slug = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1] or "index"
    return os.path.join(CACHE_DIR, slug + ".html")


def fetch_cached(url, cache_path, retries=3):
    """Returns (html, was_cached). Fetches only on a cache miss; a cache hit
    costs no network call and no delay (see the caller's time.sleep)."""
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read(), True

    delay = 2.0
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            html = raw.decode("utf-8", errors="replace")
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(html)
            return html, False
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError("GET failed after {} attempts: {} ({})".format(retries, url, last_err))


# --------------------------------------------------------------------------- #
# index-page parsing -- find every bio link + jersey number
# --------------------------------------------------------------------------- #
def is_bio_link(href):
    if not href:
        return False
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc and "nbra.net" not in parsed.netloc:
        return False
    path = parsed.path.rstrip("/")
    return path.startswith(INDEX_PATH_PREFIX) and path != INDEX_PATH_PREFIX


def find_card(link, max_up=5):
    """Walk up from the bio <a> to the nearest ancestor that looks like a
    whole "card" (has meaningfully more text than the link alone) -- that's
    where the jersey number and, often, the name live if not on the link
    itself.

    Safety rule: NEVER return an ancestor that links to more than one DISTINCT
    bio URL -- that means climbing has crossed into a shared container
    spanning multiple officials' cards (e.g. the whole grid), which would
    silently borrow a neighboring official's name or jersey number for a
    sparse card (caught by this script's own test fixture: an image-only card
    with no alt text was climbing all the way to the grid and picking up the
    next card's heading and jersey). Distinct URLs, not raw anchor count --
    one card commonly wraps BOTH its photo and its name in separate <a> tags
    to the same bio URL, which must not look like "two officials" here.
    Stops at the last ancestor that still points at exactly one bio URL, even
    if that ancestor is just the link itself."""
    node = link
    best = link
    # NOTE: get_text needs the SAME separator (" ") used everywhere else this
    # script measures/extracts text -- get_text(strip=True) with no separator
    # concatenates adjacent text nodes with nothing between them, which
    # silently shifts this length comparison by a character or two. That
    # off-by-one previously made a single-digit jersey number ("... Mosher"
    # + "#8" with no separator == the link's own length + 2, exactly at the
    # old ">link_text_len + 2" cutoff) fail to register as "more text than
    # the link alone" -- caught by this script's own test fixture.
    link_text_len = len(link.get_text(" ", strip=True))
    for _ in range(max_up):
        if node.parent is None:
            break
        node = node.parent
        bio_urls_here = {urllib.parse.urljoin(INDEX_URL, a["href"])
                        for a in node.find_all("a", href=True) if is_bio_link(a["href"])}
        if len(bio_urls_here) > 1:
            break
        if len(node.get_text(" ", strip=True)) > link_text_len + 1:
            best = node
    return best


def try_name_jersey(text):
    """Checks one candidate text for either NBRA's real combined format
    ("48 Scott Foster" -- jersey number immediately before the name, no
    marker) or a plain name alone. Returns (name, jersey_or_None) if this
    text is usable, else None. The combined-format check runs FIRST: a bare
    leading digit fails NAME_LIKE_RE outright (it requires the string to
    start with a letter), which is exactly why every name previously fell
    through this function's old name-only check and ended up as a
    URL-slug guess on every single row."""
    if not text:
        return None
    m = LEADING_NUM_NAME_RE.match(text)
    if m:
        return fix_mojibake(m.group(2)), m.group(1)
    if NAME_LIKE_RE.match(text):
        return fix_mojibake(text), None
    return None


def extract_name(link, card):
    """Priority: the link's own visible text, then its title attribute, then
    an <img alt>, then a heading/strong inside the card -- all real displayed
    text, checked via try_name_jersey so a jersey number embedded in that
    same text is captured alongside it. The URL slug is a last-resort GUESS
    only, flagged as such, because it's known to diverge from the display
    name for some officials (Josh Tiven / joshua-tiven, Che Flores /
    cheryl-flores) -- matching must never be done against it.

    Returns (name, name_source, jersey_or_None)."""
    r = try_name_jersey(link.get_text(" ", strip=True))
    if r:
        return r[0], "link-text", r[1]
    r = try_name_jersey((link.get("title") or "").strip())
    if r:
        return r[0], "link-title", r[1]
    img = link.find("img")
    if img:
        r = try_name_jersey((img.get("alt") or "").strip())
        if r:
            return r[0], "img-alt", r[1]
    for tag in card.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        r = try_name_jersey(tag.get_text(" ", strip=True))
        if r:
            return r[0], "heading", r[1]
    slug = urllib.parse.urlparse(link["href"]).path.rstrip("/").split("/")[-1]
    guess = slug.replace("-", " ").replace("_", " ").title()
    return guess, "url-slug-GUESS", None


def extract_jersey(card_text):
    m = JERSEY_RE1.search(card_text) or JERSEY_RE2.search(card_text)
    return m.group(1) if m else None


def parse_index(html):
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    entries = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not is_bio_link(href):
            continue
        abs_url = urllib.parse.urljoin(INDEX_URL, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        card = find_card(link)
        name, name_source, jersey_from_name = extract_name(link, card)
        # A jersey number found bundled with the name is unambiguously tied
        # to THIS official; only fall back to scanning the whole card's text
        # (the older "#NN"/"No. NN" patterns) when that's not available.
        jersey = jersey_from_name or extract_jersey(card.get_text(" ", strip=True))
        entries.append({
            "name": name, "name_source": name_source,
            "jersey_num": jersey, "url": abs_url,
        })
    return entries


# --------------------------------------------------------------------------- #
# bio-page parsing -- generic label:value scan, several structural strategies
# --------------------------------------------------------------------------- #
def normalize_label(label):
    return re.sub(r"[^a-z0-9 ]", "", label.strip().lower()).strip()


def canonical_field(label_norm):
    for field, synonyms in FIELD_SYNONYMS.items():
        if label_norm in synonyms:
            return field
    return None


def extract_labeled_facts(soup):
    """Scans for label:value pairs under three common structural patterns
    (definition list, two-column table, "Label: value" text in a <p>/<li>,
    optionally bolded) and returns {normalized_label: value}. Whichever
    pattern the real page actually uses, at least one of these should catch
    it; if none do, this returns {} and parse_bio's caller prints that
    loudly rather than silently writing empty fields."""
    facts = {}
    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = normalize_label(dt.get_text())
            value = fix_mojibake(dd.get_text(" ", strip=True))
            if label and value:
                facts.setdefault(label, value)

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) == 2:
                label = normalize_label(cells[0].get_text())
                value = fix_mojibake(cells[1].get_text(" ", strip=True))
                if label and value:
                    facts.setdefault(label, value)

    for el in soup.find_all(["p", "li"]):
        text = fix_mojibake(el.get_text(" ", strip=True))
        m = re.match(r"^([A-Za-z][A-Za-z /]{2,30}):\s*(.+)$", text)
        if m:
            label = normalize_label(m.group(1))
            value = m.group(2).strip()
            if label and value:
                facts.setdefault(label, value)
    return facts


def parse_bio(html):
    soup = BeautifulSoup(html, "html.parser")
    raw_facts = extract_labeled_facts(soup)
    out = {"years_experience": "", "college": "", "hometown": "", "extra_fields": {}}
    for label, value in raw_facts.items():
        field = canonical_field(label)
        if field:
            out[field] = value
        else:
            out["extra_fields"][label] = value
    return out


# --------------------------------------------------------------------------- #
# matching -- exact only, override escape hatch, explicit unmatched list
# --------------------------------------------------------------------------- #
def load_referees_index():
    with open(REFEREES_JSON, encoding="utf-8") as f:
        return json.load(f)


def load_overrides():
    """data/nbra_identity_overrides.csv columns:
        raw_name_or_id, canonical_ref_key, canonical_display_name
    Matched case-insensitively against the NBRA display name. Same format as
    data/referee_identity_overrides.csv on purpose."""
    if not os.path.exists(OVERRIDE_CSV):
        print("no NBRA override file ({}) -- auto-matching only".format(
            os.path.relpath(OVERRIDE_CSV, REPO_ROOT)))
        return {}
    mapping = {}
    with open(OVERRIDE_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(row for row in f if not row.lstrip().startswith("#"))
        for row in reader:
            raw = (row.get("raw_name_or_id") or "").strip()
            key = (row.get("canonical_ref_key") or "").strip()
            if raw and key:
                mapping[raw.lower()] = key
    print("loaded {} NBRA identity override(s)".format(len(mapping)))
    return mapping


def match_and_report(bios, referees_index):
    known_keys = {r["official_id"] for r in referees_index}
    overrides = load_overrides()

    matched, unmatched = [], []
    for b in bios:
        auto_key = norm_ref_key(b["name"])
        override_key = overrides.get(b["name"].strip().lower())
        final_key = override_key or auto_key
        if final_key in known_keys:
            b["official_id"] = final_key
            b["match_status"] = "override" if override_key else "auto"
            matched.append(b)
        else:
            b["official_id"] = ""
            b["match_status"] = "unmatched"
            unmatched.append(b)

    hr("NBRA MATCH REPORT")
    print("{}/{} NBRA officials matched to our {} referees ({} via override)".format(
        len(matched), len(bios), len(referees_index),
        sum(1 for b in matched if b["match_status"] == "override")))

    if unmatched:
        print("\nUNMATCHED ({}) -- resolve by hand via {}:".format(
            len(unmatched), os.path.relpath(OVERRIDE_CSV, REPO_ROOT)))
        for b in sorted(unmatched, key=lambda x: x["name"]):
            flag = "  [name from URL slug, verify]" if b["name_source"] == "url-slug-GUESS" else ""
            print("  {:<28} jersey={:<4} {}{}".format(
                b["name"], b.get("jersey_num") or "?", b["url"], flag))
        print("\nTo resolve, add a line to {}:".format(os.path.relpath(OVERRIDE_CSV, REPO_ROOT)))
        print("    <NBRA display name>,<our official_id>,<our display name>")
    else:
        print("\nNo unmatched officials.")

    return matched + unmatched


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    os.makedirs(CACHE_DIR, exist_ok=True)

    hr("Fetching index page")
    index_html, cached = fetch_cached(INDEX_URL, cache_path_for(INDEX_URL))
    print("{}  ({})".format(INDEX_URL, "cache hit" if cached else "fetched"))
    if not cached:
        time.sleep(DELAY_SECONDS)

    entries = parse_index(index_html)
    print("Found {} candidate officials on the index page.".format(len(entries)))
    if not entries:
        print("\nERROR: no officials found on the index page. Either the page's")
        print("structure doesn't match this parser's heuristics, or the fetch got")
        print("something other than the real page (redirect, block page, etc).")
        print("Raw HTML is cached at {} -- inspect it, or share it back, before".format(
            cache_path_for(INDEX_URL)))
        print("touching the network again.")
        sys.exit(1)

    guess_flagged = [e for e in entries if e["name_source"] == "url-slug-GUESS"]
    if guess_flagged:
        print("WARNING: {} name(s) fell back to a URL-slug guess (no real displayed "
              "name found) -- these need manual verification:".format(len(guess_flagged)))
        for e in guess_flagged:
            print("  {}  ({})".format(e["name"], e["url"]))

    hr("Fetching {} bio pages".format(len(entries)))
    bios = []
    for idx, e in enumerate(entries, 1):
        cache_path = cache_path_for(e["url"])
        try:
            html, cached = fetch_cached(e["url"], cache_path)
        except RuntimeError as ex:
            print("  [{}/{}] {}  FETCH FAILED: {}".format(idx, len(entries), e["name"], ex))
            continue
        facts = parse_bio(html)
        row = dict(e)
        row.update(facts)
        bios.append(row)
        found = [k for k in ("years_experience", "college", "hometown") if row.get(k)] \
            + list(row["extra_fields"].keys())
        print("  [{}/{}] {}  jersey={}  fields={}  ({})".format(
            idx, len(entries), e["name"], e.get("jersey_num") or "?",
            found or "none found", "cache hit" if cached else "fetched"))
        if not cached:
            time.sleep(DELAY_SECONDS)

    if not any(b.get("years_experience") or b.get("college") or b.get("hometown")
               or b["extra_fields"] for b in bios):
        print("\nWARNING: zero structured fields found on ANY bio page. The bio-page")
        print("parser's three strategies (definition list / two-column table /")
        print("'Label: value' text) likely don't match this site's real markup.")
        print("Raw HTML for every page is cached under {} for debugging --".format(CACHE_DIR))
        print("names, jersey numbers, and URLs below are still usable as-is.")

    referees_index = load_referees_index()
    rows = match_and_report(bios, referees_index)

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            out = dict(r)
            out["nbra_name"] = out.pop("name")
            out["bio_url"] = out.pop("url")
            out["extra_fields"] = json.dumps(out.get("extra_fields") or {}, ensure_ascii=False)
            writer.writerow(out)
    print("\nWrote {} rows to {}".format(len(rows), os.path.relpath(OUT_CSV, REPO_ROOT)))


if __name__ == "__main__":
    main()
