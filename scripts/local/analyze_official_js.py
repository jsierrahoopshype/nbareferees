"""
analyze_official_js.py  --  LOCAL, OFFLINE. Reads a cached JS bundle in full and
explains what its crew-related strings actually are.

Where this comes from: the first source probe flagged nba-official.min.js as
containing crew wording but found no literal endpoint URL in it. A grep cannot
tell the difference between three very different explanations:

  1. the file BUILDS a URL from fragments  ("/wp-json/" + ns + "/referee-...")
  2. it names an admin-ajax ACTION         (action: "get_referee_assignments")
  3. the words are just CSS class names or UI labels, and the data comes from
     somewhere else entirely

This script answers which. The file is ~8.5KB, so it is analyzed whole rather
than sampled: every string literal is extracted with a real scanner (quote
state, escapes, template literals, and regex-literal detection, so minified
code does not fool it), every crew-word string is classified by how the
surrounding code uses it, every string concatenation involving one is
reconstructed, and every AJAX/fetch call site in the file is printed with
context.

Offline: reads only the cache written by probe_assignments_source.py under
source-data/_assignments_raw/js/. Makes no network requests.

  python scripts\\local\\analyze_official_js.py
  python scripts\\local\\analyze_official_js.py --file path\\to\\some.js
  python scripts\\local\\analyze_official_js.py --print-source

A beautified copy is written next to the cached file (…​.beautified.js) so the
relevant lines can be read by eye. Report appends to
source-data/_probe_official_js.txt. Nothing else is written; no parser is built.
"""

import argparse
import datetime
import glob
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "source-data")
JS_CACHE_DIR = os.path.join(SOURCE_DIR, "_assignments_raw", "js")
OUT_PATH = os.path.join(SOURCE_DIR, "_probe_official_js.txt")

DEFAULT_GLOBS = ["*nba-official*.js", "*nba_official*.js", "*official*.js"]

CREW_WORDS = ("referee", "assignment", "official", "crew", "umpire", "gameday",
              "game-day", "schedule", "roster")

# Call sites that would fetch data.
CALL_PATTERNS = [
    # Minified jQuery is aliased ($ becomes e, t, ...), so match any receiver
    # rather than a literal "$".
    (r"[\w$)\]]\s*\.\s*ajax\s*\(", "jQuery .ajax()"),
    (r"[\w$)\]]\s*\.\s*(?:post|getJSON)\s*\(", "jQuery .post()/.getJSON()"),
    (r"[\w$)\]]\s*\.\s*get\s*\(\s*['\"]", "jQuery .get(url)"),
    (r"\.load\s*\(\s*['\"]", "jQuery .load(url)"),
    (r"\bfetch\s*\(", "fetch()"),
    (r"new\s+XMLHttpRequest", "XMLHttpRequest"),
    (r"\.open\s*\(\s*['\"](?:GET|POST)", "xhr.open"),
    (r"\baxios\b", "axios"),
    (r"wp\.ajax\b", "wp.ajax"),
    (r"\bajaxurl\b", "ajaxurl (WP admin-ajax global)"),
    (r"admin-ajax\.php", "admin-ajax.php literal"),
    (r"wpApiSettings", "wpApiSettings (WP REST global)"),
    (r"\bnonce\b", "nonce reference"),
    (r"/wp-json/", "wp-json literal"),
    (r"\bapiFetch\b", "wp.apiFetch"),
]

# Methods that tell us a string is a selector/class rather than a URL part.
DOM_METHODS = ("addclass", "removeclass", "hasclass", "toggleclass", "queryselector",
               "queryselectorall", "getelementsby", "closest", "find", "filter",
               "is", "attr", "prop", "css", "parents", "siblings", "children",
               "not", "next", "prev", "append", "prepend", "html", "text", "on",
               "off", "trigger", "classlist", "matches", "data")

_lines = []


def emit(msg=""):
    text = str(msg)
    safe = text.encode("ascii", "backslashreplace").decode("ascii")
    print(safe)
    _lines.append(safe)


def one_line(text, limit=240):
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + " ...[+%d chars]" % (len(flat) - limit)


def crew_hits(text):
    low = str(text).lower()
    return [w for w in CREW_WORDS if w in low]


def section(title):
    emit("")
    emit("=" * 78)
    emit(title)
    emit("=" * 78)


# --------------------------------------------------------------------------- #
# A real (small) JS scanner
# --------------------------------------------------------------------------- #
REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>\n\t ")


def scan(js):
    """Walk the source once, returning (strings, mask).

    strings: list of {"start","end","quote","value"} for every string literal.
    mask:    same length as js; 'S' inside a string literal, 'C' inside a
             comment, '.' otherwise. Later passes use the mask so a `+` inside
             a string is never mistaken for concatenation.

    Regex literals matter here: minified code is full of /.../ patterns that
    can contain quote characters, and treating one as a string start would
    desynchronize everything after it.
    """
    strings = []
    mask = ["."] * len(js)
    i, n = 0, len(js)
    prev_sig = ""  # last significant (non-space) char before current position
    while i < n:
        ch = js[i]
        if ch in "\"'`":
            quote = ch
            start = i
            i += 1
            buf = []
            while i < n:
                c = js[i]
                if c == "\\":
                    buf.append(js[i:i + 2])
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                buf.append(c)
                i += 1
            for k in range(start, min(i, n)):
                mask[k] = "S"
            strings.append({"start": start, "end": i, "quote": quote,
                            "value": "".join(buf)})
            prev_sig = quote
            continue
        if ch == "/" and i + 1 < n:
            nxt = js[i + 1]
            if nxt == "/":
                j = js.find("\n", i)
                j = n if j < 0 else j
                for k in range(i, j):
                    mask[k] = "C"
                i = j
                continue
            if nxt == "*":
                j = js.find("*/", i + 2)
                j = n if j < 0 else j + 2
                for k in range(i, j):
                    mask[k] = "C"
                i = j
                continue
            if prev_sig in REGEX_PRECEDERS or prev_sig == "":
                # regex literal: consume to the closing unescaped slash
                j = i + 1
                in_class = False
                while j < n:
                    c = js[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        j += 1
                        break
                    elif c == "\n":
                        break
                    j += 1
                for k in range(i, min(j, n)):
                    mask[k] = "C"  # treat like a comment: not code, not a string
                i = j
                prev_sig = "/"
                continue
        if not ch.isspace():
            prev_sig = ch
        i += 1
    return strings, "".join(mask)


def code_context(js, mask, start, end, before=120, after=160):
    """Source around a span, whitespace-collapsed."""
    return one_line(js[max(0, start - before):min(len(js), end + after)], before + after + 60)


def preceding_code(js, mask, start, window=90):
    """The code (not string content) immediately before a position."""
    lo = max(0, start - window)
    return "".join(c for k, c in zip(range(lo, start), js[lo:start]) if mask[k] != "S")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify_string(s, js, mask):
    """What is this string literal being used AS?"""
    v = s["value"]
    before = preceding_code(js, mask, s["start"]).lower()
    after_char = ""
    for k in range(s["end"], min(len(js), s["end"] + 12)):
        if not js[k].isspace():
            after_char = js[k]
            break

    if v.startswith(("http://", "https://", "//")):
        return "ABSOLUTE URL"
    if v.startswith("/") and not v.startswith("//"):
        return "URL PATH"
    if ".php" in v or ".json" in v or "wp-json" in v or "admin-ajax" in v:
        return "URL FRAGMENT"
    if v.startswith("data-"):
        return "DATA ATTRIBUTE"
    if v.startswith(("#", ".")) and re.match(r"^[.#][A-Za-z0-9_\-]+", v):
        return "CSS SELECTOR"
    if "<" in v and ">" in v:
        return "HTML TEMPLATE"
    # An `action:` key beats every other reading -- that is a WP admin-ajax
    # dispatch name, whatever the string happens to look like. Checked before
    # the DOM test because "action" contains DOM-method substrings.
    if re.search(r"""["']?action["']?\s*:\s*$""", before.rstrip()):
        return "AJAX ACTION NAME"
    # Match DOM calls as `.method(` immediately before the string, not as a
    # loose substring -- short names like "on"/"is"/"not" match anything.
    if re.search(r"\.\s*(?:%s)\s*\(\s*$" % "|".join(DOM_METHODS), before.rstrip(), re.I):
        return "CSS CLASS / SELECTOR (DOM call)"
    if re.match(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$", v):
        # snake_case: the shape of a WP action or hook name
        if "ajax" in before[-60:]:
            return "AJAX ACTION NAME"
        return "SNAKE_CASE IDENTIFIER (action/hook shape)"
    if after_char == ":" or before.rstrip().endswith(("{", ",")):
        return "OBJECT KEY"
    if re.match(r"^[A-Za-z0-9_\- ]+$", v) and " " in v:
        return "UI TEXT / LABEL"
    if re.match(r"^[a-z0-9\-]+$", v):
        return "SLUG / CLASS-LIKE TOKEN"
    return "OTHER"


def concat_chain(js, mask, s):
    """If this string participates in `a + b + c`, return the whole expression.

    Answers the key question directly: is a URL being assembled from pieces?
    Walks outward over code characters only, so `+` inside another string is
    never counted.
    """
    def sig_before(pos):
        k = pos - 1
        while k >= 0 and (js[k].isspace() or mask[k] == "C"):
            k -= 1
        return k

    def sig_after(pos):
        k = pos
        while k < len(js) and (js[k].isspace() or mask[k] == "C"):
            k += 1
        return k

    lo, hi = s["start"], s["end"]
    grew = True
    while grew:
        grew = False
        k = sig_before(lo)
        if k >= 0 and js[k] == "+" and mask[k] != "S":
            j = sig_before(k)
            # swallow the operand to the left (identifier, call, or string)
            if j >= 0:
                if mask[j] == "S":
                    m = next((t for t in ALL_STRINGS if t["end"] == j + 1), None)
                    if m:
                        lo = m["start"]
                        grew = True
                elif js[j] in ")]":
                    depth, p = 1, j - 1
                    opener = "(" if js[j] == ")" else "["
                    while p >= 0 and depth:
                        if mask[p] != "S":
                            if js[p] == js[j]:
                                depth += 1
                            elif js[p] == opener:
                                depth -= 1
                        p -= 1
                    while p >= 0 and (js[p].isalnum() or js[p] in "_$."):
                        p -= 1
                    lo = p + 1
                    grew = True
                elif js[j].isalnum() or js[j] in "_$.":
                    p = j
                    while p >= 0 and (js[p].isalnum() or js[p] in "_$."):
                        p -= 1
                    lo = p + 1
                    grew = True
        k = sig_after(hi)
        if k < len(js) and js[k] == "+" and mask[k] != "S":
            j = sig_after(k + 1)
            if j < len(js):
                if mask[j] == "S":
                    m = next((t for t in ALL_STRINGS if t["start"] == j), None)
                    if m:
                        hi = m["end"]
                        grew = True
                elif js[j].isalnum() or js[j] in "_$":
                    p = j
                    while p < len(js) and (js[p].isalnum() or js[p] in "_$."):
                        p += 1
                    if p < len(js) and js[p] == "(":
                        depth, q = 1, p + 1
                        while q < len(js) and depth:
                            if mask[q] != "S":
                                if js[q] == "(":
                                    depth += 1
                                elif js[q] == ")":
                                    depth -= 1
                            q += 1
                        p = q
                    hi = p
                    grew = True
    if lo == s["start"] and hi == s["end"]:
        return None
    return one_line(js[lo:hi], 400)


def beautify(js, mask):
    """Naive but string-safe pretty printer: newline after ; { }, indent by depth."""
    out, depth = [], 0
    for i, ch in enumerate(js):
        if mask[i] == "S":
            out.append(ch)
            continue
        if ch == "{":
            depth += 1
            out.append("{\n" + "  " * depth)
        elif ch == "}":
            depth = max(0, depth - 1)
            out.append("\n" + "  " * depth + "}")
        elif ch == ";":
            out.append(";\n" + "  " * depth)
        else:
            out.append(ch)
    text = "".join(out)
    return "\n".join(line.rstrip() for line in text.split("\n") if line.strip())


# --------------------------------------------------------------------------- #
# Report sections
# --------------------------------------------------------------------------- #
def find_target(args):
    if args.file:
        return args.file if os.path.exists(args.file) else None
    for pattern in DEFAULT_GLOBS:
        matches = sorted(glob.glob(os.path.join(JS_CACHE_DIR, pattern)))
        matches = [m for m in matches if not m.endswith(".beautified.js")]
        if matches:
            return matches[0]
    return None


def report_calls(js, mask, beautified):
    section("2. DATA-FETCHING CALL SITES IN THIS FILE")
    found_any = False
    for pattern, label in CALL_PATTERNS:
        for m in re.finditer(pattern, js, re.IGNORECASE):
            if mask[m.start()] == "S":
                continue  # the pattern is inside a string, not a call
            found_any = True
            emit("")
            emit("  %s at offset %d" % (label, m.start()))
            emit("      %s" % code_context(js, mask, m.start(), m.end(), 200, 320))
    if not found_any:
        emit("  NONE. This file makes no AJAX/fetch/XHR call and references no")
        emit("  admin-ajax, wp-json, nonce or wpApiSettings global.")
        emit("  -> whatever renders the crew list, it is not this file fetching it.")


def report_crew_strings(js, mask, strings):
    section("1. EVERY CREW-WORD STRING, AND WHAT IT IS USED AS")
    crew = [s for s in strings if crew_hits(s["value"])]
    emit("%d string literal(s) in the file; %d contain crew wording."
         % (len(strings), len(crew)))
    if not crew:
        emit("  none -- the crew wording found by grep must be in identifiers,")
        emit("  comments or regex literals rather than string values (see section 4).")
        return crew
    counts = {}
    for s in crew:
        kind = classify_string(s, js, mask)
        s["kind"] = kind
        counts[kind] = counts.get(kind, 0) + 1
    emit("")
    emit("-- breakdown by role --")
    for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        emit("  %-34s %d" % (kind, count))
    emit("")
    emit("-- each occurrence --")
    for s in crew:
        emit("")
        emit("  %s   [%s]  offset %d" % (one_line(repr(s["value"]), 200), s["kind"], s["start"]))
        chain = concat_chain(js, mask, s)
        if chain:
            emit("      *** PART OF A CONCATENATION: %s" % chain)
        emit("      context: %s" % code_context(js, mask, s["start"], s["end"]))
    return crew


def report_all_urlish(js, mask, strings):
    section("3. EVERY URL-SHAPED STRING IN THE FILE (crew-related or not)")
    urlish = []
    for s in strings:
        v = s["value"]
        if (v.startswith(("http", "//", "/")) or ".php" in v or ".json" in v
                or "wp-json" in v or "ajax" in v.lower()):
            urlish.append(s)
    if not urlish:
        emit("  none. No absolute URL, no path, no .php/.json fragment anywhere in")
        emit("  the file -- so it is not assembling a request target from literals.")
        return
    for s in urlish:
        kind = classify_string(s, js, mask)
        chain = concat_chain(js, mask, s)
        emit("  %-60s [%s]" % (one_line(repr(s["value"]), 60), kind))
        if chain:
            emit("      *** CONCATENATION: %s" % chain)


def report_identifiers(js, mask):
    section("4. CREW WORDING OUTSIDE STRING LITERALS (identifiers, keys, regex)")
    code_only = "".join(ch if mask[i] != "S" else " " for i, ch in enumerate(js))
    any_hit = False
    for word in CREW_WORDS:
        for m in re.finditer(word, code_only, re.IGNORECASE):
            any_hit = True
            emit("  '%s' at offset %d (in code, not a string)" % (word, m.start()))
            emit("      %s" % code_context(js, mask, m.start(), m.end(), 140, 200))
    if not any_hit:
        emit("  none -- all crew wording in this file sits inside string literals.")

    emit("")
    emit("-- declared function/var names (what this file is for) --")
    names = []
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)", code_only):
        if m.group(1) not in names:
            names.append(m.group(1))
    for m in re.finditer(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=", code_only):
        if m.group(1) not in names:
            names.append(m.group(1))
    emit("  %s" % (", ".join(names[:60]) if names else "(none found -- fully minified)"))


def verdict(js, mask, strings, crew):
    section("VERDICT")
    kinds = [s.get("kind", "") for s in crew]
    has_url = any(k in ("ABSOLUTE URL", "URL PATH", "URL FRAGMENT") for k in kinds)
    has_action = any("ACTION" in k or "SNAKE_CASE" in k for k in kinds)
    dom_kinds = sum(1 for k in kinds if "SELECTOR" in k or "CLASS" in k or k == "SLUG / CLASS-LIKE TOKEN")
    code_only = "".join(ch if mask[i] != "S" else " " for i, ch in enumerate(js))
    has_call = any(re.search(p, code_only, re.IGNORECASE) for p, _ in CALL_PATTERNS)

    if has_url:
        emit("This file DOES reference a URL or path alongside crew wording.")
        emit("-> see sections 1 and 3; replay that request next.")
    elif has_action:
        emit("This file references an action/hook-shaped name but no URL.")
        emit("-> likely an admin-ajax POST: the target is /wp-admin/admin-ajax.php")
        emit("   with that action name. Confirm in the browser Network tab.")
    elif has_call:
        emit("This file makes a fetch/AJAX call, but its target is not a literal")
        emit("string here -- it comes from a variable or another file.")
        emit("-> find where that variable is set (a wp_localize_script global in")
        emit("   the page HTML is the usual answer).")
    elif crew and not has_call:
        emit("No URL, no action name, and no request of any kind in this file --")
        emit("%d of %d crew strings are CSS classes, selectors or UI labels."
             % (dom_kinds, len(crew)))
        emit("-> this file only STYLES or TOGGLES markup that already exists. The")
        emit("   crew data is delivered by something else: server-rendered markup")
        emit("   behind a cookie/geo condition, another bundle, or an iframe.")
    elif crew:
        emit("Crew wording appears, but not as a URL, an action name, or a DOM")
        emit("selector. See section 1 for what each occurrence actually is.")
    else:
        emit("No crew wording in any string literal in this file.")
    emit("")
    emit("Either way, no parser is built from this. Next round decides that.")


def main():
    ap = argparse.ArgumentParser(description="Read a cached JS bundle in full and classify its crew strings.")
    ap.add_argument("--file", help="path to a .js file (default: the cached nba-official bundle)")
    ap.add_argument("--print-source", action="store_true",
                    help="also print the whole beautified file to stdout")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass

    target = find_target(args)
    if not target:
        print("ERROR: no cached bundle found in %s" % os.path.relpath(JS_CACHE_DIR, REPO_ROOT))
        print("Run scripts/local/probe_assignments_source.py first, or pass --file.")
        sys.exit(1)

    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        js = fh.read()

    emit("JS bundle analysis (analyze_official_js.py)")
    emit("=" * 78)
    emit("Run at (local clock): %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    emit("File: %s" % os.path.relpath(target, REPO_ROOT))
    emit("Size: %d chars (%.1f KB), %d line(s)" % (len(js), len(js) / 1024.0, js.count("\n") + 1))
    emit("Offline: no network requests. Analyzed in full, not sampled.")

    strings, mask = scan(js)
    globals()["ALL_STRINGS"] = strings
    beautified = beautify(js, mask)

    crew = report_crew_strings(js, mask, strings)
    report_calls(js, mask, beautified)
    report_all_urlish(js, mask, strings)
    report_identifiers(js, mask)
    verdict(js, mask, strings, crew)

    out_js = target + ".beautified.js" if not target.endswith(".beautified.js") else target
    with open(out_js, "w", encoding="utf-8") as fh:
        fh.write(beautified)
    emit("")
    emit("Beautified copy (%d lines) -> %s" % (beautified.count("\n") + 1,
                                               os.path.relpath(out_js, REPO_ROOT)))
    if args.print_source:
        section("FULL BEAUTIFIED SOURCE")
        for line in beautified.split("\n"):
            emit("  " + line)

    os.makedirs(SOURCE_DIR, exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n\n" + "\n".join(_lines) + "\n")
    print("\n-> appended findings to %s" % os.path.relpath(OUT_PATH, REPO_ROOT))


ALL_STRINGS = []

if __name__ == "__main__":
    main()
