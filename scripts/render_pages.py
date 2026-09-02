#!/usr/bin/env python3
"""
render_pages.py -- static site generator for the NBA Referee Database.

Reads the committed data/ produced by build.py and writes a fully pre-rendered
static site (real HTML content, not JS-injected -- the SEO pattern from the
nba-draft-combine tool):

    index.html                       leaderboards + searchable referee list
    referee/{slug}/index.html  x N   one page per referee
    assets/style.css                 shared styling (self-contained, no CDNs)
    assets/app.js                    vanilla JS: search, sortable tables, tabs

Specs: docs/PHASE1_SPEC.md (section 5) + docs/RENDER_SPEC.md.

Design note: the visual language matches the HoopsHype NBA Polymarket tracker
(jsierrahoopshype/nba-polymarket) -- Apple-neutral light surfaces, a blue
accent, DM Sans body with a JetBrains Mono data/label voice, and 12px cards.
Those fonts are named first in each stack but fall back to system fonts, so the
site keeps zero network dependencies and renders offline.

Editorial rule (carried from both specs): every generated string is a
descriptive fact. Nothing implies a referee causes outcomes or favors a team;
no betting framing.

Run from repo root:  python scripts/render_pages.py
"""

import os
import re
import sys
import json
import glob
import html

# Shared tricode → full-team-name map lives with the local scripts.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "local"))
import nba_tricodes  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")
ASSETS = os.path.join(REPO, "assets")
REFEREE_DIR = os.path.join(REPO, "referee")

CURRENT_SEASON = "2025-26"

# Mirrors build.py's WHISTLE_STATS exactly (key, n_column, label, slug) -- the
# six whistle-profile stats eligible for percentile coloring and a dedicated
# /leaderboard/{slug}/ ranking page. n_column isn't used on the render side
# (build.py already resolved qualification into *_pctile) but is kept so the
# two lists stay copy-paste identical and don't drift.
WHISTLE_STATS = [
    ("avg_total_points", "n", "Combined points", "combined-points"),
    ("avg_total_fta", "n_boxscore", "Combined free-throw attempts", "combined-fta"),
    ("avg_total_pf", "n_boxscore", "Combined personal fouls", "combined-fouls"),
    ("avg_abs_margin", "n", "Avg. margin of victory", "avg-margin"),
    ("home_win_pct", "n", "Home team win rate", "home-win-rate"),
    ("ot_rate", "n_boxscore", "Games to overtime", "ot-rate"),
]

ATTRIBUTION = [
    ("Wyatt Walsh's NBA Database", "https://www.kaggle.com/datasets/wyattowalsh/basketball",
     "Kaggle, CC BY-SA 4.0"),
    ("ESPN's public API", "https://www.espn.com/nba/",
     "games, officials and player logs for 1993-94–1999-00, 2000-01–02-03, "
     "2012-13, and 2023-24 on"),
    ("szymonjwiak's NBA box scores", "https://www.kaggle.com/datasets/szymonjwiak/nba-traditional",
     "Kaggle; player logs for the nbadb-only seasons"),
]


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s), quote=True)


def i(n):
    return "—" if n is None else "{:,}".format(int(n))


def dec(n, places=1):
    return "—" if n is None else "{:.{p}f}".format(float(n), p=places)


def pct(x, places=1):
    """0.605 -> '60.5%'."""
    return "—" if x is None else "{:.{p}f}%".format(float(x) * 100, p=places)


def signed(x, places=1):
    if x is None:
        return "—"
    return "{:+.{p}f}".format(float(x), p=places)


def signed_pct(x, places=1):
    """Signed percentage-point differential: 0.052 -> '+5.2%', -0.031 -> '-3.1%'."""
    if x is None:
        return "—"
    return "{:+.{p}f}%".format(float(x) * 100, p=places)


def ordinal(n):
    """12 -> '12th', 21 -> '21st', 3 -> '3rd'."""
    if n is None:
        return "—"
    n = int(n)
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suf)


def diff_rank_label(rank, total, diff):
    """League Context (docs/LEAGUE_CONTEXT_SPEC.md section 4): 'rank among
    qualifying referees', e.g. '12th lowest of 118'. rank=1 is the HIGHEST
    differential (build.py's sort order) -- a negative differential reads
    more naturally counted from the bottom of the qualifying pool, so a
    below-baseline referee's rank is reported as an ordinal position from the
    low end instead of a large raw rank number."""
    if not total:
        return "ranking not available"
    if rank is None:
        return "not enough games to rank"
    if total <= 1:
        return "only qualifying official"
    if diff is not None and diff < 0:
        return "%s lowest of %d" % (ordinal(total - rank + 1), total)
    return "%s highest of %d" % (ordinal(rank), total)


def career_span(first, last):
    """Career span as calendar years: '2015-16'..'2025-26' -> '2015-2026'
    (first-season start year to last-season end year). A single season such as
    '2021-22' renders '2021-2022'."""
    start = int(str(first)[:4])
    end = int(str(last)[:4]) + 1
    return "%d-%d" % (start, end)


# ---------------------------------------------------------------------------
# shared chrome
# ---------------------------------------------------------------------------
def head(title, description, depth):
    """depth = number of '../' needed to reach repo root (0 index, 2 ref page)."""
    root = "../" * depth
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{root}assets/style.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="masthead">
  <a class="brand" href="{root}index.html">
    <span class="brand-stripe" aria-hidden="true"></span>
    <span class="brand-name">Referee Database</span>
  </a>
  <nav class="masthead-nav"><a href="{root}compare/index.html">Compare</a>
  <a href="{root}matchup/index.html">Matchup</a></nav>
  <span class="brand-sub">NBA officiating record &middot; 1993-94 to {cur}</span>
</header>
<main id="main">""".format(title=esc(title), desc=esc(description), root=root,
                           cur=CURRENT_SEASON)


def footer(depth):
    root = "../" * depth
    return """</main>
<footer class="site-foot">
  <p class="foot-editorial">Every figure here is a descriptive record of games
  as they were officiated. Nothing on this site implies a referee causes a
  result or favors a team.</p>
  <p class="foot-links"><a href="{root}sources/index.html">Data sources</a></p>
</footer>
<script src="{root}assets/app.js"></script>
</body>
</html>""".format(root=root)


def page(title, description, depth, body):
    return head(title, description, depth) + body + footer(depth)


def ref_search(depth, position, type_filter=None, compare_slot=None):
    """Client-side navigate-search over data/search-index.json (referees, teams
    and players). data-root is the page's path back to the repo root, so the JS
    can build the correct depth for referee/, team/ and player/ targets.

    type_filter restricts results to one type (e.g. "ref" for the comparator's
    two boxes). compare_slot ("a"/"b") makes selecting a result update that
    query-string param on the CURRENT page instead of navigating to the
    referee's own page -- both are read by the same shared JS block, so this
    is the same search component, not a new one."""
    root = "../" * depth
    extra = ""
    if type_filter:
        extra += ' data-type-filter="%s"' % esc(type_filter)
    if compare_slot:
        extra += ' data-compare="%s"' % esc(compare_slot)
    placeholder = "Search referees…" if type_filter == "ref" else "Search referees, teams, players…"
    return ('<div class="refsearch-wrap" data-json="{root}data/search-index.json" '
            'data-root="{root}" data-pos="{pos}"{extra}>'
            '<input type="search" class="refsearch" autocomplete="off" '
            'placeholder="{ph}" '
            'aria-label="{ph}">'
            '<div class="refsearch-results" role="listbox" hidden></div>'
            '</div>').format(root=root, pos=position, extra=extra, ph=esc(placeholder))


def swing_class(v):
    """Diverging heat bucket for a swing / margin value, on the Polymarket
    palette: green positive, red negative, bucketed by magnitude
    (~ +/- 0.5 / 1.5 / 3 / 5+). The number and n stay visible; color supplements."""
    if v is None:
        return "sw s-zero"
    a = abs(v)
    lvl = 4 if a >= 5 else 3 if a >= 3 else 2 if a >= 1.5 else 1 if a >= 0.5 else 0
    if lvl == 0:
        return "sw s-zero"
    return "sw s-%s-%d" % ("pos" if v > 0 else "neg", lvl)


# ---------------------------------------------------------------------------
# referee page
# ---------------------------------------------------------------------------
def stat_chip(label, value, accent=False):
    return ('<div class="chip{a}"><span class="chip-val">{v}</span>'
            '<span class="chip-label">{l}</span></div>').format(
        a=" chip-accent" if accent else "", v=value, l=esc(label))


def whistle_intensity_class(pctile):
    """Single-hue 'how unusual' intensity for a whistle-profile stat card, from
    its percentile rank -- NOT directional (no red/green, no good/bad
    implication): distance from the median (pctile 50) only. None (ref doesn't
    clear the min-games gate for this stat) -> no class, card renders plain."""
    if pctile is None:
        return ""
    d = abs(pctile - 50)
    lvl = 4 if d >= 40 else 3 if d >= 30 else 2 if d >= 20 else 1 if d >= 10 else 0
    return "wm-i%d" % lvl


# valfmt per whistle stat key -- shared by ref-page cards and leaderboard
# pages, for both the raw value and the era-adjusted league baseline (same
# scale, same formatter). The differential gets its own signed formatter.
WHISTLE_VALFMT = {
    "avg_total_points": dec, "avg_total_fta": dec, "avg_total_pf": dec,
    "avg_abs_margin": dec, "home_win_pct": pct, "ot_rate": pct,
}
WHISTLE_DIFFFMT = {
    "avg_total_points": signed, "avg_total_fta": signed, "avg_total_pf": signed,
    "avg_abs_margin": signed, "home_win_pct": signed_pct, "ot_rate": signed_pct,
}
WHISTLE_LABEL = {k: lab for k, _n, lab, _slug in WHISTLE_STATS}
WHISTLE_SLUG = {k: slug for k, _n, _lab, slug in WHISTLE_STATS}
WHISTLE_SHORT_LABEL = {
    "avg_total_points": "Pts", "avg_total_fta": "FTA", "avg_total_pf": "PF",
    "avg_abs_margin": "Margin", "home_win_pct": "Home win%", "ot_rate": "OT rate",
}


def whistle_column(kind_label, kind, w):
    """kind is 'rs' or 'po' -- used both to read this stat's n-column choice
    (already baked into w) and to deep-link each card to the right section of
    its /leaderboard/{slug}/ page.

    League Context (docs/LEAGUE_CONTEXT_SPEC.md section 4): each card shows
    raw value, the era-adjusted league baseline, the signed differential, and
    rank among qualifying referees -- replacing the old bare-number card, not
    adding a second one. Percentile coloring (whistle_intensity_class) already
    keys off {key}_pctile, which build.py computes from the differential."""
    n = w["n"]
    if not n:
        return ""
    nb = w["n_boxscore"]
    # (key, raw value, n for this stat) -- label/slug come from WHISTLE_STATS.
    rows = [
        ("avg_total_points", w["avg_total_points"], n),
        ("avg_total_fta", w["avg_total_fta"], nb),
        ("avg_total_pf", w["avg_total_pf"], nb),
        ("avg_abs_margin", w["avg_abs_margin"], n),
        ("home_win_pct", w["home_win_pct"], n),
        ("ot_rate", w["ot_rate"], nb),
    ]
    expected = w.get("expected") or {}
    differential = w.get("differential") or {}
    cells = []
    for key, raw, nn in rows:
        v = "—" if raw is None else WHISTLE_VALFMT[key](raw)
        lgraw = expected.get(key)
        lg = "—" if lgraw is None else WHISTLE_VALFMT[key](lgraw)
        d = differential.get(key)
        dv = WHISTLE_DIFFFMT[key](d)
        cls = whistle_intensity_class(w.get(key + "_pctile"))
        rank_txt = diff_rank_label(w.get(key + "_rank"), w.get(key + "_qualifying"), d)
        href = "%sleaderboard/%s/index.html#%s" % (ROOT2, WHISTLE_SLUG[key], kind)
        cells.append(
            '<a class="wm {cls}" href="{href}">'
            '<div class="wm-val">{v} <span class="wm-lg">lg {lg}</span> '
            '<span class="wm-diff">{d}</span></div>'
            '<div class="wm-label">{l}</div>'
            '<div class="wm-rank">{rk}</div>'
            '<div class="wm-n">n = {n}</div></a>'.format(
                cls=cls, href=href, v=v, lg=lg, d=dv, l=esc(WHISTLE_LABEL[key]),
                rk=esc(rank_txt), n=i(nn)))
    return ('<div class="whistle-col"><h3 class="whistle-kind">{k} '
            '<span class="whistle-n">{n} games</span></h3>'
            '<div class="whistle-grid">{c}</div></div>').format(
        k=esc(kind_label), n=i(n), c="".join(cells))


def season_splits_table(doc):
    """League Context (docs/LEAGUE_CONTEXT_SPEC.md section 5): a sortable
    per-season table under the whistle profile -- season, games, each stat's
    differential versus that season's league average, with a career row at
    the bottom. RS and PO are separate rows per season (each is its own
    scoring/pace regime, same split the whistle profile itself uses), tagged
    by a Type column rather than a dropdown, so both stay visible and
    sortable in one table -- the season selector this spec asks for."""
    splits = doc.get("season_splits") or []
    s = doc["summary"]
    wp = doc["whistle_profile"]

    stat_ths = "".join(
        '<th class="sortable col-num" data-type="num" scope="col" '
        'title="{full} &mdash; differential vs. that season&rsquo;s league average">'
        '&Delta; {short}</th>'.format(full=esc(WHISTLE_LABEL[key]), short=esc(WHISTLE_SHORT_LABEL[key]))
        for key, *_r in WHISTLE_STATS)
    ths = ('<th class="sortable col-text" data-type="text" scope="col">Season</th>'
           '<th class="sortable col-text" data-type="text" scope="col">Type</th>'
           '<th class="sortable col-num" data-type="num" scope="col">Games</th>' + stat_ths)

    def row_html(season_label, season_sort, kind_label, games, stats):
        cells = [
            '<td data-label="Season" data-sort="{ss}">{sl}</td>'.format(
                ss=esc(season_sort), sl=esc(season_label)),
            '<td data-label="Type">{k}</td>'.format(k=esc(kind_label)),
            '<td data-label="Games" data-sort="{g}">{gi}</td>'.format(g=games, gi=i(games)),
        ]
        for key, *_r in WHISTLE_STATS:
            d = (stats or {}).get(key, {}).get("diff")
            cells.append(
                '<td data-label="{lab}" data-sort="{ds}">{dv}</td>'.format(
                    lab=esc(WHISTLE_SHORT_LABEL[key]), ds=(d if d is not None else 0),
                    dv=WHISTLE_DIFFFMT[key](d)))
        return "<tr>" + "".join(cells) + "</tr>"

    body = []
    for row in splits:
        for kind, kind_label, games_key in (("rs", "RS", "games_rs"), ("po", "PO", "games_po")):
            games = row.get(games_key, 0)
            if not games:
                continue
            body.append(row_html(row["season"], row["season"], kind_label, games,
                                 row["stats"].get(kind)))

    # Career row(s) at the bottom (data-sort="9999" keeps them last even if
    # the reader clicks the Season column, since real seasons top out at
    # "2025-26" -> the numeric sort value 2025).
    for kind, kind_label, games_key in (("rs", "RS", "games_rs"), ("po", "PO", "games_po")):
        games = s.get(games_key, 0)
        if not games:
            continue
        career_stats = {key: {"diff": (wp[kind].get("differential") or {}).get(key)}
                        for key, *_r in WHISTLE_STATS}
        body.append(row_html("Career", "9999", kind_label, games, career_stats))

    if not body:
        return '<p class="empty-note">No season-by-season data on file for this official.</p>'
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


# Existence sets for cross-linking (populated in main). A name is linkified only
# when its target page exists; otherwise it renders as plain text (no dead links).
TEAM_EXISTS = set()      # tricodes (upper) with a /team/ page
PLAYER_EXISTS = set()    # player slugs with a /player/ page
# Most table helpers run on depth-2 pages (referee/, team/, player/,
# leaderboard/{slug}/), so links from within them reach the repo root via
# "../../" by default -- but a few of these same helpers are also reused on
# depth-0 (index.html) and depth-1 (crews/, team-officials/, debuts/, eras/,
# swings/) pages, which need a shallower root. Each of the four link helpers
# below takes an explicit root= override for those call sites (matching the
# root= convention leaderboard_row/era_panel/debuts_farewells_section already
# use); the default keeps every depth-2 call site unchanged.
ROOT2 = "../../"


def team_cell(abbr, root=ROOT2):
    """Full franchise name with the tricode as a small secondary chip; the name
    links to the team page when one exists."""
    full = nba_tricodes.display_name(abbr)
    tag = '<span class="team-tag">%s</span>' % esc(abbr)
    if full == abbr:                      # unrecognized code: chip only, no dupe
        return '<span class="team-cell">%s</span>' % tag
    name = esc(full)
    if abbr in TEAM_EXISTS:
        name = '<a href="%steam/%s/index.html">%s</a>' % (root, esc(abbr.lower()), name)
    return '<span class="team-cell"><span class="team-name">%s</span>%s</span>' % (name, tag)


def player_link(name, slug, root=ROOT2):
    """Player name linked to its page when one exists, else plain text."""
    if slug and slug in PLAYER_EXISTS:
        return '<a href="%splayer/%s/index.html">%s</a>' % (root, esc(slug), esc(name))
    return esc(name)


def ref_link(name, slug, root=ROOT2):
    """Referee name linked back to the ref page (always exists)."""
    return '<a href="%sreferee/%s/index.html">%s</a>' % (root, esc(slug), esc(name))


def back_home(label="All referees", root=ROOT2):
    return '<a class="backlink" href="%sindex.html">&larr; %s</a>' % (root, esc(label))


def partners_card(partners):
    """Compact 'Most frequent crewmates' card for a ref page; names linked."""
    if not partners:
        return ""
    items = "".join(
        '<li class="partner"><a class="partner-name" href="%sreferee/%s/index.html">%s</a>'
        '<span class="partner-n">%s g</span></li>'
        % (ROOT2, esc(p["slug"]), esc(p["name"]), i(p["games"])) for p in partners)
    return ('<section class="block"><div class="block-head">'
            '<span class="eyebrow">Crew</span><h2>Most frequent crewmates</h2></div>'
            '<ul class="partners">%s</ul>'
            '<p class="caption">Officials this referee has shared a three-person '
            'crew with most often.</p></section>' % items)


def team_records_table(records):
    head_cols = [
        ("Team", "text", "team"), ("G", "num", "games"), ("W", "num", "wins"),
        ("L", "num", "losses"), ("Win%", "num", "win_pct"),
        ("Home G", "num", "home_games"), ("Home W", "num", "home_wins"),
        ("Avg margin", "num", "avg_margin_for_team"),
    ]
    ths = "".join(
        '<th class="sortable {cls}" data-type="{t}" scope="col">{lab}</th>'.format(
            cls="col-text" if t == "text" else "col-num", t=t, lab=esc(lab))
        for lab, t, _ in head_cols)
    body = []
    for r in records:
        margin = r["avg_margin_for_team"]
        body.append(
            "<tr>"
            '<td data-label="Team" data-sort="{teamsort}">{teamcell}</td>'
            '<td data-label="G" data-sort="{g}">{gi}</td>'
            '<td data-label="W" data-sort="{w}">{wi}</td>'
            '<td data-label="L" data-sort="{l}">{li}</td>'
            '<td data-label="Win%" data-sort="{wp}">{wpf}</td>'
            '<td data-label="Home G" data-sort="{hg}">{hgi}</td>'
            '<td data-label="Home W" data-sort="{hw}">{hwi}</td>'
            '<td data-label="Avg margin" data-sort="{m}"><span class="{mc}">{ms}</span></td>'
            "</tr>".format(
                teamcell=team_cell(r["team_abbr"]),
                teamsort=esc(nba_tricodes.display_name(r["team_abbr"]).lower()),
                g=r["games"], gi=i(r["games"]), w=r["wins"], wi=i(r["wins"]),
                l=r["losses"], li=i(r["losses"]),
                wp=(r["win_pct"] if r["win_pct"] is not None else -1), wpf=pct(r["win_pct"]),
                hg=r["home_games"], hgi=i(r["home_games"]),
                hw=r["home_wins"], hwi=i(r["home_wins"]),
                m=(margin if margin is not None else 0),
                mc=swing_class(margin),
                ms=signed(margin)))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def swings_table(swings):
    ths = "".join('<th class="sortable {c}" data-type="{t}" scope="col">{l}</th>'.format(
        c="col-text" if t == "text" else "col-num", t=t, l=esc(l))
        for l, t in [("Player", "text"), ("Games", "num"), ("PTS with", "num"),
                     ("PTS baseline", "num"), ("PTS swing", "num"),
                     ("FTA swing", "num"), ("PF swing", "num")])
    body = []
    for s in swings:
        body.append(
            "<tr>"
            '<td data-label="Player" data-sort="{nm}">{cell}</td>'
            '<td data-label="Games" data-sort="{n}">{ni}</td>'
            '<td data-label="PTS with" data-sort="{pw}">{pwf}</td>'
            '<td data-label="PTS baseline" data-sort="{pb}">{pbf}</td>'
            '<td data-label="PTS swing" data-sort="{ps}"><span class="{psc}">{pss}</span></td>'
            '<td data-label="FTA swing" data-sort="{fs}"><span class="{fsc}">{fss}</span></td>'
            '<td data-label="PF swing" data-sort="{ff}"><span class="{ffc}">{ffs}</span></td>'
            "</tr>".format(
                nm=esc(s["name"].lower()), cell=player_link(s["name"], s.get("slug")),
                n=s["n_games"], ni=i(s["n_games"]),
                pw=s["pts_with_ref"], pwf=dec(s["pts_with_ref"]),
                pb=s["pts_baseline"], pbf=dec(s["pts_baseline"]),
                ps=s["pts_swing"], pss=signed(s["pts_swing"]), psc=swing_class(s["pts_swing"]),
                fs=s["fta_swing"], fss=signed(s["fta_swing"]), fsc=swing_class(s["fta_swing"]),
                ff=s["pf_swing"], ffs=signed(s["pf_swing"]), ffc=swing_class(s["pf_swing"])))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def top_perf_table(perfs):
    body = []
    for rank, p in enumerate(perfs, 1):
        body.append(
            "<tr>"
            '<td data-label="#" class="rank">{r}</td>'
            '<td data-label="Player">{pl}</td>'
            '<td data-label="PTS"><span class="big-num">{pt}</span></td>'
            '<td data-label="Matchup" class="matchup">{tm} <span class="vs">vs</span> {op}</td>'
            '<td data-label="Date">{dt}</td>'
            "</tr>".format(r=rank, pl=player_link(p["player_name"], p.get("player_slug")),
                           pt=i(p["pts"]),
                           tm=team_cell(p["team_abbr"]), op=team_cell(p["opp_abbr"]),
                           dt=esc(p["game_date"])))
    return ('<table class="data-table"><thead><tr>'
            '<th scope="col">#</th><th scope="col">Player</th><th scope="col">PTS</th>'
            '<th scope="col">Matchup</th><th scope="col">Date</th></tr></thead>'
            '<tbody>{body}</tbody></table>').format(body="".join(body))


def notable_table(games):
    body = []
    for g in games:
        rnd = g["round"] or "—"
        gnum = ("Game %s" % g["game_num"]) if g.get("game_num") else "—"
        body.append(
            "<tr>"
            '<td data-label="Date">{dt}</td>'
            '<td data-label="Matchup">{mu}</td>'
            '<td data-label="Result">{rs}</td>'
            '<td data-label="Round"><span class="round-tag">{rd}</span></td>'
            '<td data-label="Game">{gn}</td>'
            "</tr>".format(dt=esc(g["date"]), mu=esc(g["matchup"]),
                           rs=esc(g["result"]), rd=esc(rnd), gn=esc(gnum)))
    return ('<table class="data-table"><thead><tr>'
            '<th scope="col">Date</th><th scope="col">Matchup</th>'
            '<th scope="col">Result</th><th scope="col">Round</th>'
            '<th scope="col">Game</th></tr></thead><tbody>{body}</tbody></table>'
            ).format(body="".join(body))


def section(num, title, inner, extra_head=""):
    """num=None/"" omits the small eyebrow label entirely (ref pages don't use
    section numbers); team/player pages still pass one."""
    eyebrow = ('<span class="eyebrow"><span class="eyebrow-stripe" aria-hidden="true"></span>'
               '{num}</span>'.format(num=esc(num))) if num else ""
    return ('<section class="block"><div class="block-head">{eyebrow}'
            '<h2>{title}</h2>{extra}</div>{inner}</section>').format(
        eyebrow=eyebrow, title=esc(title), extra=extra_head, inner=inner)


def render_ref(doc):
    s = doc["summary"]
    name = s["name"]
    seasons = career_span(s["first_season"], s["last_season"])
    title = "%s NBA referee stats: career games, team records, player splits" % name
    desc = ("%s NBA referee profile — %s career games across %s (regular season "
            "%s, playoffs %s). Team records, whistle profile, notable games." % (
                name, i(s["games_total"]), seasons, i(s["games_rs"]), i(s["games_po"])))

    active = ('<span class="badge badge-active">Active {cur}</span>'.format(cur=CURRENT_SEASON)
              if s["active"] else '<span class="badge badge-past">Last worked {ls}</span>'.format(
                  ls=s["last_season"]))

    chips = [
        stat_chip("Career games", i(s["games_total"]), accent=True),
        stat_chip("Seasons", seasons),
        stat_chip("Regular season", i(s["games_rs"])),
        stat_chip("Playoffs", i(s["games_po"])),
    ]
    if s.get("games_pi"):
        chips.append(stat_chip("Play-in", i(s["games_pi"])))

    hero = """<section class="ref-hero">
  <div class="ref-hero-stripe" aria-hidden="true"></div>
  <div class="ref-hero-body">
    <p class="ref-kicker">NBA on-court official</p>
    <h1 class="ref-name">{name}</h1>
    <div class="ref-badges">{active} <a class="compare-btn" href="{root}compare/index.html?a={slug}">Compare</a>
    <a class="compare-btn" href="{root}referee/{slug}/games/index.html">Full game log</a>
    <a class="compare-btn" href="{root}matchup/index.html?ref={slug}">Team matchups</a></div>
    <div class="chip-row">{chips}</div>
  </div>
</section>""".format(name=esc(name), active=active, chips="".join(chips),
                     root=ROOT2, slug=esc(s["slug"]))

    blocks = [back_home(), hero, ref_search(2, "top")]
    blocks.append(partners_card(doc.get("top_partners")))

    # whistle profile
    cols = whistle_column("Regular season", "rs", doc["whistle_profile"]["rs"]) + \
        whistle_column("Playoffs", "po", doc["whistle_profile"]["po"])
    caption = ('<p class="caption">Averages describe the box score in games this '
               'official worked — combined totals for both teams. Each figure '
               'shows its sample size (n).</p>')
    blocks.append(section(None, "Whistle profile",
                          '<div class="whistle-cols">%s</div>' % cols, caption))

    # season splits (League Context section 5) -- the season selector: a
    # sortable table, not a dropdown that hides data.
    blocks.append(section(None, "Season-by-season",
                          '<div class="table-wrap">%s</div>' % season_splits_table(doc),
                          '<p class="caption">Each stat\'s differential against that season\'s '
                          'league average (regular season and playoffs shown separately). '
                          'Tap a column to sort; career totals are the bottom rows.</p>'))

    # team records
    blocks.append(section(None, "Team records under %s" % name,
                          '<div class="table-wrap">%s</div>' % team_records_table(doc["team_records"]),
                          '<p class="caption">A team’s record in games this official worked. '
                          'Descriptive only. Tap a column to sort.</p>'))

    # player swings (only when present)
    if doc["player_swings"]:
        note = ('<p class="caption">Players with at least 15 games under {name}. '
                '“Swing” is the average difference between a player’s output in '
                'these games and that player’s own same-season average. This is a '
                'descriptive split, not a causal claim — it does not mean the official '
                'affected the player. <a href="{root}swings/index.html">See the biggest '
                'swings across every referee &rarr;</a></p>').format(name=esc(name), root=ROOT2)
        blocks.append(section(None, "Player splits",
                              '<div class="table-wrap">%s</div>' % swings_table(doc["player_swings"]),
                              note))

    # top performances
    if doc["top_performances"]:
        blocks.append(section(None, "Top scoring games",
                              '<div class="table-wrap">%s</div>' % top_perf_table(doc["top_performances"]),
                              '<p class="caption">Highest individual point totals in games '
                              '%s officiated.</p>' % esc(name)))

    # notable games
    finals_line = ('<p class="notable-counts">Finals games: <b>{f}</b> '
                   '&middot; Game 7s: <b>{g}</b></p>').format(
        f=i(s["finals_games"]), g=i(s["game7s"]))
    if doc["notable_games"]:
        inner = '<div class="table-wrap">%s</div>' % notable_table(doc["notable_games"])
    elif s["games_po"] or s.get("games_pi"):
        inner = ('<p class="empty-note">No Finals or Game 7 games are labeled for this '
                 'official.</p>')
    else:
        inner = '<p class="empty-note">No playoff games on record for this official.</p>'
    blocks.append(section(None, "Notable games", inner, finals_line))
    blocks.append(ref_search(2, "bottom"))

    return page(title, desc, 2, "".join(blocks))


# ---------------------------------------------------------------------------
# referee game log (docs/TIER_C_SPEC.md section 3) -- one level deeper than
# any other content page (referee/{slug}/games/), so team_cell/ref_link's
# hardcoded ROOT2 ("../../", correct for depth 2) would silently produce a
# broken "referee/referee/..." link here. Depth-3-correct local variants only
# for this page rather than touching ROOT2 (used everywhere else).
# ---------------------------------------------------------------------------
ROOT3 = "../../../"


def team_cell_d3(abbr):
    full = nba_tricodes.display_name(abbr)
    tag = '<span class="team-tag">%s</span>' % esc(abbr)
    if full == abbr:
        return '<span class="team-cell">%s</span>' % tag
    name = esc(full)
    if abbr in TEAM_EXISTS:
        name = '<a href="%steam/%s/index.html">%s</a>' % (ROOT3, esc(abbr.lower()), name)
    return '<span class="team-cell"><span class="team-name">%s</span>%s</span>' % (name, tag)


def ref_link_d3(name, slug):
    return '<a href="%sreferee/%s/index.html">%s</a>' % (ROOT3, esc(slug), esc(name))


def referee_game_log_table(games):
    cols = [("Date", "text"), ("Matchup", "text"), ("Score", "text"),
            ("Round", "text"), ("Co-officials", "text")]
    ths = "".join('<th class="sortable {c}" data-type="{t}" scope="col">{l}</th>'.format(
        c="col-text" if t == "text" else "col-num", t=t, l=esc(l)) for l, t in cols)
    body = []
    for g in games:
        hp, ap = g["home_pts"], g["away_pts"]
        score = ("%s %s, %s %s" % (g["home_team_abbr"], i(hp), g["away_team_abbr"], i(ap))
                 if hp is not None and ap is not None else "—")
        co = " &middot; ".join(ref_link_d3(c["name"], c["slug"]) for c in g["co_officials"]) or "—"
        body.append(
            "<tr>"
            '<td data-label="Date">{dt}</td>'
            '<td data-label="Matchup" class="matchup">{away} <span class="vs">@</span> {home}</td>'
            '<td data-label="Score">{sc}</td>'
            '<td data-label="Round"><span class="round-tag">{rd}</span></td>'
            '<td data-label="Co-officials" class="crew">{co}</td>'
            "</tr>".format(dt=esc(g["date"]), away=team_cell_d3(g["away_team_abbr"]),
                           home=team_cell_d3(g["home_team_abbr"]), sc=esc(score),
                           rd=esc(g["round_label"] or "—"), co=co))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def render_ref_games(doc):
    name = doc["name"]
    n_seasons = len(doc["by_season"])
    title = "%s — every game officiated, full game log" % name
    desc = ("Complete game-by-game officiating log for %s: %s games across %d seasons, "
            "with matchup, final score, playoff round, and co-officials for every game." % (
                name, i(doc["games_total"]), n_seasons))
    back = '<a class="backlink" href="../index.html">&larr; Back to %s</a>' % esc(name)
    chips = [stat_chip("Career games", i(doc["games_total"]), accent=True),
            stat_chip("Seasons logged", i(n_seasons))]
    hero = hero_block("Full game log", name, "", chips)
    blocks = [back, hero]
    for block in doc["by_season"]:
        inner = '<div class="table-wrap">%s</div>' % referee_game_log_table(block["games"])
        blocks.append(section(None, "%s (%d games)" % (block["season"], len(block["games"])), inner))
    blocks.append(back)
    return page(title, desc, 3, "".join(blocks))


# ---------------------------------------------------------------------------
# team & player pages (TEAMS_PLAYERS_SPEC §2)
# ---------------------------------------------------------------------------
def hero_block(kicker, name, badges, chips):
    return """<section class="ref-hero"><div class="ref-hero-body">
    <p class="ref-kicker">{kicker}</p>
    <h1 class="ref-name">{name}</h1>
    <div class="ref-badges">{badges}</div>
    <div class="chip-row">{chips}</div>
  </div></section>""".format(kicker=esc(kicker), name=esc(name),
                             badges=badges, chips="".join(chips))


def team_ref_table(records):
    cols = [("Referee", "text"), ("G", "num"), ("W", "num"), ("L", "num"),
            ("Win%", "num"), ("Home", "num"), ("Away", "num"), ("Avg margin", "num")]
    ths = "".join('<th class="sortable {c}" data-type="{t}" scope="col">{l}</th>'.format(
        c="col-text" if t == "text" else "col-num", t=t, l=esc(l)) for l, t in cols)
    body = []
    for r in records:
        m = r["avg_margin_for_team"]
        body.append(
            "<tr>"
            '<td data-label="Referee" data-sort="{rs}">{ref}</td>'
            '<td data-label="G" data-sort="{g}">{gi}</td>'
            '<td data-label="W" data-sort="{w}">{wi}</td>'
            '<td data-label="L" data-sort="{l}">{li}</td>'
            '<td data-label="Win%" data-sort="{wp}">{wpf}</td>'
            '<td data-label="Home" data-sort="{hg}">{hw}-{hl}</td>'
            '<td data-label="Away" data-sort="{ag}">{aw}-{al}</td>'
            '<td data-label="Avg margin" data-sort="{m}"><span class="{mc}">{ms}</span></td>'
            "</tr>".format(
                rs=esc(r["ref_name"].lower()), ref=ref_link(r["ref_name"], r["ref_slug"]),
                g=r["games"], gi=i(r["games"]), w=r["wins"], wi=i(r["wins"]),
                l=r["losses"], li=i(r["losses"]),
                wp=(r["win_pct"] if r["win_pct"] is not None else -1), wpf=pct(r["win_pct"]),
                hg=r["home_games"], hw=i(r["home_wins"]), hl=i(r["home_games"] - r["home_wins"]),
                ag=r["away_games"], aw=i(r["away_wins"]), al=i(r["away_games"] - r["away_wins"]),
                m=(m if m is not None else 0), mc=swing_class(m), ms=signed(m)))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def render_team(doc):
    s = doc["summary"]
    name, tri = s["name"], s["tricode"]
    span = career_span(s["first_season"], s["last_season"]) if s["first_season"] else "—"
    title = "How the %s perform with every NBA referee" % name
    desc = ("%s (%s) record with every NBA referee since %s: games, win rate, "
            "home/away split, and average margin under each official." % (
                name, tri, s["first_season"] or "1993-94"))
    badges = ('<span class="badge badge-past">Historical franchise</span>'
              if s.get("historical") else "")
    badges += ' <a class="compare-btn" href="../../matchup/index.html?team=%s">Referee matchups</a>' % esc(s["slug"])
    chips = [
        stat_chip("Games in dataset", i(s["games_total"]), accent=True),
        stat_chip("Tricode", esc(tri)),
        stat_chip("Seasons", span),
        stat_chip("Referees", i(len(doc["ref_records"]))),
    ]
    blocks = [back_home(), hero_block("NBA franchise", name, badges, chips),
              ref_search(2, "top")]
    methods = ('<p class="caption">A team’s record in games each official worked — '
               'a descriptive split, not a causal claim. Referees with at least 10 '
               'games of this team are listed; tap a column to sort.</p>')
    blocks.append(section("01", "Record by referee",
                          '<div class="table-wrap">%s</div>' % team_ref_table(doc["ref_records"]),
                          methods))
    blocks.append(ref_search(2, "bottom"))
    return page(title, desc, 2, "".join(blocks))


def whistle_rank_table(rows, key):
    """League Context (docs/LEAGUE_CONTEXT_SPEC.md section 6): the raw value
    stays -- "games with the fewest total points" is a legitimate factual
    leaderboard, it just isn't a claim about the official -- alongside a new
    sortable Differential column, clearly labeled so the two are never
    confused. Row order (and the # column) is the ranking build.py already
    computed on the differential; clicking a header re-sorts client-side."""
    if not rows:
        return '<p class="empty-note">No officials currently qualify for this ranking.</p>'
    valfmt = WHISTLE_VALFMT[key]
    difffmt = WHISTLE_DIFFFMT[key]
    body = []
    for r in rows:
        d = r.get("diff")
        body.append(
            "<tr>"
            '<td data-label="#" class="rank" data-sort="{rk}">{rk}</td>'
            '<td data-label="Referee">{ref}</td>'
            '<td data-label="Differential" data-sort="{ds}"><span class="big-num">{dv}</span></td>'
            '<td data-label="Raw value" data-sort="{vs}">{v}</td>'
            '<td data-label="Games" data-sort="{n}">{ni}</td>'
            "</tr>".format(rk=r["rank"], ref=ref_link(r["name"], r["slug"]),
                           ds=(d if d is not None else 0), dv=difffmt(d),
                           vs=r["value"], v=valfmt(r["value"]), n=r["n"], ni=i(r["n"])))
    return ('<table class="data-table sortable-table"><thead><tr>'
            '<th class="sortable col-num" data-type="num" scope="col">#</th>'
            '<th class="sortable col-text" data-type="text" scope="col">Referee</th>'
            '<th class="sortable col-num" data-type="num" scope="col" '
            'title="vs. the era-adjusted league baseline for the seasons this official worked '
            '-- this is what the ranking is sorted by">Differential</th>'
            '<th class="sortable col-num" data-type="num" scope="col" '
            'title="Raw career figure across qualifying games -- not era-adjusted">Raw value</th>'
            '<th class="sortable col-num" data-type="num" scope="col">Games</th>'
            '</tr></thead><tbody>%s</tbody></table>') % "".join(body)


def render_whistle_leaderboard(doc):
    label = doc["label"]
    mg_rs, mg_po = doc["min_games"]["rs"], doc["min_games"]["po"]
    key = doc["key"]
    title = "%s leaderboard: every qualifying NBA referee ranked" % label
    desc = ("Every NBA official ranked by %s -- regular season (min. %s games) and "
            "playoffs (min. %s games) ranked separately, by an era-adjusted differential. "
            "A descriptive ranking, not a causal claim." % (label.lower(), i(mg_rs), i(mg_po)))
    chips = [
        stat_chip("Regular season refs", i(len(doc["rs"])), accent=True),
        stat_chip("Playoff refs", i(len(doc["po"]))),
        stat_chip("Min. RS games", i(mg_rs)),
        stat_chip("Min. PO games", i(mg_po)),
    ]
    blocks = [back_home(),
              hero_block("Whistle-profile leaderboard", label, "", chips),
              ref_search(2, "top")]
    rank_note = ('Ranked by <b>Differential</b> -- each official\'s value against the '
                 'era-adjusted league baseline for the seasons they worked, which is what '
                 'keeps this ranking from being dominated by which years an official happened '
                 'to be on the floor. <b>Raw value</b> is the plain career figure, shown '
                 'alongside for reference; both columns sort by tapping their header.')
    rs_methods = ('<p class="caption">Referees with at least {mg} regular-season games. '
                  '{note} A descriptive ranking of on-court numbers, not a causal claim about '
                  'officiating.</p>').format(mg=i(mg_rs), note=rank_note)
    po_methods = ('<p class="caption">Referees with at least {mg} playoff games -- a lower '
                  'bar than the regular-season ranking, since playoff games are far scarcer '
                  'per career. {note} A descriptive ranking, not a causal claim.</p>').format(
        mg=i(mg_po), note=rank_note)
    blocks.append(
        '<section class="block" id="rs"><div class="block-head"><h2>Regular season</h2></div>'
        '<div class="table-wrap">%s</div>%s</section>'
        % (whistle_rank_table(doc["rs"], key), rs_methods))
    blocks.append(
        '<section class="block" id="po"><div class="block-head"><h2>Playoffs</h2></div>'
        '<div class="table-wrap">%s</div>%s</section>'
        % (whistle_rank_table(doc["po"], key), po_methods))
    blocks.append(ref_search(2, "bottom"))
    return page(title, desc, 2, "".join(blocks))


def player_ref_table(splits):
    cols = [("Referee", "text"), ("G", "num"), ("PTS", "num"), ("PTS base", "num"),
            ("PTS Δ", "num"), ("REB Δ", "num"), ("AST Δ", "num")]
    ths = "".join('<th class="sortable {c}" data-type="{t}" scope="col">{l}</th>'.format(
        c="col-text" if t == "text" else "col-num", t=t, l=esc(l)) for l, t in cols)
    body = []
    for r in splits:
        body.append(
            "<tr>"
            '<td data-label="Referee" data-sort="{rs}">{ref}</td>'
            '<td data-label="G" data-sort="{n}">{ni}</td>'
            '<td data-label="PTS" data-sort="{pw}">{pwf}</td>'
            '<td data-label="PTS base" data-sort="{pb}">{pbf}</td>'
            '<td data-label="PTS Δ" data-sort="{ps}"><span class="{psc}">{pss}</span></td>'
            '<td data-label="REB Δ" data-sort="{rd}"><span class="{rdc}">{rds}</span></td>'
            '<td data-label="AST Δ" data-sort="{ad}"><span class="{adc}">{ads}</span></td>'
            "</tr>".format(
                rs=esc(r["ref_name"].lower()), ref=ref_link(r["ref_name"], r["ref_slug"]),
                n=r["n_games"], ni=i(r["n_games"]),
                pw=r["pts_with_ref"], pwf=dec(r["pts_with_ref"]),
                pb=r["pts_baseline"], pbf=dec(r["pts_baseline"]),
                ps=r["pts_swing"], pss=signed(r["pts_swing"]), psc=swing_class(r["pts_swing"]),
                rd=r["reb_swing"], rds=signed(r["reb_swing"]), rdc=swing_class(r["reb_swing"]),
                ad=r["ast_swing"], ads=signed(r["ast_swing"]), adc=swing_class(r["ast_swing"])))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def player_top_games_table(games):
    body = []
    for rank, g in enumerate(games, 1):
        crew = " &middot; ".join(ref_link(c["name"], c["slug"]) for c in g.get("crew", [])) or "—"
        body.append(
            "<tr>"
            '<td data-label="#" class="rank">{r}</td>'
            '<td data-label="PTS"><span class="big-num">{pt}</span></td>'
            '<td data-label="REB">{rb}</td><td data-label="AST">{as_}</td>'
            '<td data-label="Matchup" class="matchup">{tm} <span class="vs">vs</span> {op}</td>'
            '<td data-label="Date">{dt}</td>'
            '<td data-label="Crew" class="crew">{crew}</td>'
            "</tr>".format(r=rank, pt=i(g["pts"]), rb=i(g["reb"]), as_=i(g["ast"]),
                           tm=team_cell(g["team_abbr"]), op=team_cell(g["opp_abbr"]),
                           dt=esc(g["game_date"]), crew=crew))
    return ('<table class="data-table"><thead><tr>'
            '<th scope="col">#</th><th scope="col">PTS</th><th scope="col">REB</th>'
            '<th scope="col">AST</th><th scope="col">Matchup</th><th scope="col">Date</th>'
            '<th scope="col">Crew</th></tr></thead><tbody>{body}</tbody></table>'
            ).format(body="".join(body))


def render_player(doc):
    s = doc["summary"]
    name = s["name"]
    span = career_span(s["first_season"], s["last_season"]) if s["first_season"] else "—"
    title = "%s stats by referee" % name
    desc = ("%s career stats by NBA referee: points, rebounds and assists with each "
            "official versus %s’s own season averages, plus top scoring games." % (name, name))
    teamtags = " ".join('<span class="team-tag">%s</span>' % esc(t) for t in s["teams"])
    chips = [
        stat_chip("Games in dataset", i(s["games_total"]), accent=True),
        stat_chip("Seasons", span),
        stat_chip("Referees", i(len(doc["ref_splits"]))),
    ]
    badges = '<div class="ref-badges team-list">%s</div>' % teamtags if teamtags else ""
    blocks = [back_home(), hero_block("NBA player", name, badges, chips),
              ref_search(2, "top")]
    methods = ('<p class="caption">“Swing” (Δ) is the average difference between '
               '%s’s output in games each official worked and %s’s own same-season '
               'average — a descriptive split, not a causal claim. Referees with at '
               'least 15 games are listed. <a href="%sswings/index.html">See the biggest '
               'swings across every player &rarr;</a></p>' % (esc(name), esc(name), ROOT2))
    blocks.append(section("01", "Splits by referee",
                          '<div class="table-wrap">%s</div>' % player_ref_table(doc["ref_splits"]),
                          methods))
    if doc["top_games"]:
        blocks.append(section("02", "Top scoring games",
                              '<div class="table-wrap">%s</div>' % player_top_games_table(doc["top_games"]),
                              '<p class="caption">%s’s highest-scoring games in the dataset, '
                              'with the crew that worked each.</p>' % esc(name)))
    blocks.append(ref_search(2, "bottom"))
    return page(title, desc, 2, "".join(blocks))


# ---------------------------------------------------------------------------
# index page
# ---------------------------------------------------------------------------
LEADERBOARD_TABS = [
    # (tab_id, label, leaderboards.json key, valkey, valfmt, n_key)
    ("career", "Career games", "most_career_games", "games_total", i, None),
    ("active", "Active", "most_career_games_active", "games_total", i, None),
    ("playoffs", "Playoff games", "most_playoff_games", "games_po", i, None),
    ("finals", "Finals games", "most_finals_games", "finals_games", i, None),
    ("game7s", "Game 7s", "most_game7s", "game7s", i, None),
    ("season", "This season", "most_games_current_season", "games_current", i, None),
    # DASHBOARD_SPEC section 2: playoff-weight quality score (R1=1/R2=2/R3=4/
    # R4=8 per game). Per-season is gated at seasons_active>=3 at build time;
    # n_key surfaces that season count on each row so the gate is visible, not
    # just applied silently.
    ("quality", "Playoff weight — career", "most_quality_total", "quality_total", i, None),
    ("quality_season", "Playoff weight — per season", "most_quality_per_season",
     "quality_per_season", dec, "n"),
]


def leaderboard_row(rank, r, valkey, valfmt, n_key=None, root="", diff_key=None, difffmt=None):
    n_span = ""
    if n_key and r.get(n_key) is not None:
        n_span = ' <span class="lb-n">n=%s</span>' % i(r[n_key])
    diff_span = ""
    if diff_key and difffmt and r.get(diff_key) is not None:
        diff_span = ' <span class="lb-diff">%s</span>' % esc(difffmt(r[diff_key]))
    return ('<li class="lb-row"><span class="lb-rank">{rk}</span>'
            '<a class="lb-name" href="{root}referee/{slug}/index.html">{name}</a>'
            '<span class="lb-val">{val}{diffspan}{nspan}</span></li>').format(
        rk=rank, root=root, slug=esc(r["slug"]), name=esc(r["name"]), val=valfmt(r[valkey]),
        diffspan=diff_span, nspan=n_span)


def paired_panel(tab_id, active, title_hi, rows_hi, title_lo, rows_lo, valkey, valfmt,
                 footnote="", diff_key=None, difffmt=None):
    def col(title, rows):
        items = "".join(leaderboard_row(n, r, valkey, valfmt, diff_key=diff_key, difffmt=difffmt)
                        for n, r in enumerate(rows, 1))
        return '<div class="lb-col"><h3 class="lb-subhead">{t}</h3><ol class="lb-list">{it}</ol></div>'.format(
            t=esc(title), it=items)
    return ('<div class="lb-panel lb-paired{act}" data-panel="{id}">{a}{b}{fn}</div>').format(
        act=" is-active" if active else "", id=tab_id,
        a=col(title_hi, rows_hi), b=col(title_lo, rows_lo), fn=footnote)


# ---------------------------------------------------------------------------
# frontpage dashboard sections (DASHBOARD_SPEC section 3)
#
# Information-design round: these used to each wrap themselves in a full
# <section class="block"> with their own repeated "DASHBOARD" eyebrow --
# eight near-identical boxed headers stacked down the page. render_index now
# owns that scaffolding (three tiers: fresh/statistics/reference, each with
# ONE header), and every function below returns bare inner content only, no
# outer section/heading -- typography, spacing and dividers carry the
# hierarchy instead of another rounded rectangle per module.
# ---------------------------------------------------------------------------
def dashboard_records_strip(records):
    """Horizontally scannable strip of small stat facts, each linking to the
    ref. Static/known at build time, so rendered server-side like everything
    else on the site (real HTML, not JS-injected). Items are divided by a
    vertical rule, not individually boxed."""
    return "".join(
        '<a class="record-item" href="referee/{slug}/index.html">'
        '<span class="record-label">{label}</span>'
        '<span class="record-val">{val}</span>'
        '<span class="record-ref">{name} <span class="record-n">n={n}</span></span>'
        '</a>'.format(slug=esc(r["ref_slug"]), label=esc(r["label"]),
                     val=esc(r["value"]), name=esc(r["ref_name"]), n=i(r["n"]))
        for r in records)


def dashboard_history_strip(history):
    """Top scoring games (linked crew + player) and the most frequent
    3-official crew ever, plus a few fixed factual notes about the dataset.
    Rendered only on index.html (depth 0), so every link helper here needs
    root="" instead of its depth-2 default."""
    game_items = []
    for rank, g in enumerate(history["top_scoring_games"], 1):
        crew = " &middot; ".join(ref_link(c["name"], c["slug"], root="") for c in g.get("crew") or []) or "—"
        game_items.append(
            '<li class="history-row"><span class="history-rank">{rk}</span>'
            '<span class="history-pts">{pts}</span> {player} '
            '<span class="history-matchup">{team} <span class="vs">vs</span> {opp}</span> '
            '<span class="history-date">{date}</span>'
            '<span class="history-crew">Crew: {crew}</span></li>'.format(
                rk=rank, pts=i(g["pts"]),
                player=player_link(g["player_name"], g.get("player_slug"), root=""),
                team=team_cell(g["team_abbr"], root=""), opp=team_cell(g["opp_abbr"], root=""),
                date=esc(g["game_date"]), crew=crew))

    trio = history.get("top_crew_trio")
    trio_html = '<p class="empty-note">No three-official crew on record.</p>'
    if trio:
        names = ", ".join(ref_link(r["name"], r["slug"], root="") for r in trio["refs"])
        trio_html = ('<p class="history-trio">{names} — {n} games together, more than '
                    'any other three-official crew.</p>').format(names=names, n=i(trio["games"]))

    curiosities = "".join("<li>%s</li>" % esc(c) for c in history.get("curiosities") or [])

    return ("""<div class="history-cols">
    <div class="history-col"><span class="lb-subhead">Top scoring games</span>
      <ol class="history-list">{games}</ol></div>
    <div class="history-col"><span class="lb-subhead">Most frequent crew</span>{trio}
      <span class="lb-subhead">Notes on this data</span>
      <ul class="curiosity-list">{cur}</ul></div>
  </div>""").format(games="".join(game_items), trio=trio_html, cur=curiosities)


def dashboard_rotation_slots(dashboard):
    """Empty containers for the day-of-year spotlight pick and 'on this date'
    line, plus the data they need embedded inline (not fetched) so there's no
    flash of missing content on first paint. The SELECTION is inherently
    client-side (must reflect the viewer's actual today on a statically-built
    site), but the underlying data ships as real JSON in the page source."""
    payload = json.dumps(
        {"spotlight": dashboard["spotlight"], "date_index": dashboard["date_index"]},
        ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return ("""<div class="today-grid">
  <div class="today-col"><span class="lb-subhead">Spotlight of the day</span>
    <div id="spotlight-card"><p class="empty-note">Loading…</p></div></div>
  <div class="today-col"><span class="lb-subhead">On this date</span>
    <div id="ondate-card"><p class="empty-note">Loading…</p></div></div>
</div>
<script type="application/json" id="dashboard-rotation-data">{payload}</script>""").format(payload=payload)


def dashboard_tonights_crews_slot():
    """Empty, hidden by default. app.js fetches data/tonights-crews.json (a
    September-pipeline output that doesn't exist yet) and only un-hides this
    if it's present AND dated today/yesterday (US time) -- absent or stale
    renders nothing at all, no placeholder."""
    return ("""<div id="tonights-crews" hidden>
  <span class="lb-subhead">Tonight's crews</span>
  <div id="tonights-crews-body"></div>
</div>""")


# ---------------------------------------------------------------------------
# Tier C index widgets (docs/TIER_C_SPEC.md section 3) -- each links to its
# own full-list page.
# ---------------------------------------------------------------------------
def dashboard_crews_strip(crews):
    """Rendered only on index.html (depth 0) -- ref_link needs root=""."""
    top5 = crews[:5]
    return "".join(
        '<li class="history-row"><span class="history-rank">{rk}</span>'
        '<span class="crew-names">{names}</span> '
        '<span class="lb-val">{g} games together</span></li>'.format(
            rk=rank, names=", ".join(ref_link(r["name"], r["slug"], root="") for r in c["refs"]),
            g=i(c["games"]))
        for rank, c in enumerate(top5, 1))


def dashboard_team_officials_strip(team_officials):
    return "".join(
        '<a class="record-item" href="referee/{slug}/index.html">'
        '<span class="record-label">{team}</span>'
        '<span class="record-val">{ref}</span>'
        '<span class="record-ref">{g} games <span class="record-n">n={n}</span></span>'
        '</a>'.format(slug=esc(t["ref_slug"]), team=esc(t["team_name"]),
                     ref=esc(t["ref_name"]), g=i(t["games"]), n=i(t["n"]))
        for t in team_officials)


def dashboard_debuts_widget(debuts_farewells):
    """Returns (season, inner_html) for the most recent season, or None if
    there's no data -- the caller decides how to present the empty case."""
    if not debuts_farewells:
        return None
    block = debuts_farewells[0]           # already sorted most-recent-season first
    return block["season"], debuts_farewells_section(block)


def dashboard_era_leaders_widget(era_leaders):
    return era_leaders_block(era_leaders)


def render_index(refs, lb, dashboard):
    total = len(refs)
    span = "%s to %s" % (min(r["first_season"] for r in refs), CURRENT_SEASON)
    active_n = sum(1 for r in refs if r["active"])

    # hero
    hero = """<section class="index-hero">
  <div class="hero-rule" aria-hidden="true"></div>
  <p class="hero-kicker">Every whistle, on the record</p>
  <h1 class="hero-title">The NBA Referee Database</h1>
  <p class="hero-lead">Career profiles for {total} on-court officials — games worked,
  team records under each crew, whistle tendencies, and notable playoff games,
  from {span}.</p>
  <div class="hero-stats">
    <div class="hstat"><span class="hstat-num">{total}</span><span class="hstat-label">officials</span></div>
    <div class="hstat"><span class="hstat-num">{active}</span><span class="hstat-label">active this season</span></div>
    <div class="hstat"><span class="hstat-num">26</span><span class="hstat-label">seasons</span></div>
  </div>
</section>""".format(total=total, span=esc(span), active=active_n)

    # ---- sticky section nav ------------------------------------------------
    # Compact jump-list, one entry per anchor below. Sticky on desktop; the
    # mobile media query below drops the sticky positioning (see CSS comment
    # for why) but keeps the links themselves, since they're still useful as
    # an ordinary in-page table of contents.
    subnav = """<nav class="index-subnav" aria-label="Jump to section">
  <a href="#today">Today</a><a href="#records">Records</a><a href="#leaders">Leaders</a>
  <a href="#crews-teams">Crews &amp; teams</a><a href="#directory">Directory</a>
</nav>"""

    # ---- tier 1: fresh -- today's rotation + the database's oddities ------
    today_inner = dashboard_rotation_slots(dashboard) + dashboard_tonights_crews_slot()
    records_inner = ('<span class="lb-subhead">Records &amp; oddities</span>'
                     '<div class="record-strip">%s</div>'
                     % dashboard_records_strip(dashboard["records"])
                     + dashboard_history_strip(dashboard["history"]))
    tier_fresh = """<section class="tier">
  <div class="tier-head"><span class="tier-eyebrow">Fresh</span>
  <h2>Today, records &amp; history</h2>
  <p class="tier-desc">What's new and what's unusual — refreshed daily where the data allows.</p></div>
  <div class="submod-anchor" id="today">{today}</div>
  <div class="submod-anchor" id="records">{records}</div>
</section>""".format(today=today_inner, records=records_inner)

    # ---- tier 2: statistics -- the deep leaderboards + cross-official cuts
    tabs_btns = []
    panels = []
    for idx, (tab_id, label, key, valkey, valfmt, n_key) in enumerate(LEADERBOARD_TABS):
        active = idx == 0
        tabs_btns.append(
            '<button class="lb-tab{act}" data-tab="{id}" role="tab" '
            'aria-selected="{sel}">{lab}</button>'.format(
                act=" is-active" if active else "", id=tab_id,
                sel="true" if active else "false", lab=esc(label)))
        items = "".join(leaderboard_row(n, r, valkey, valfmt, n_key)
                        for n, r in enumerate(lb[key], 1))
        panels.append(
            '<div class="lb-panel{act}" data-panel="{id}" role="tabpanel">'
            '<ol class="lb-list lb-list-wide">{items}</ol></div>'.format(
                act=" is-active" if active else "", id=tab_id, items=items))

    # paired panels: home win% and total FTA. League Context: ranked by the
    # era-adjusted differential (matching the six dedicated /leaderboard/
    # pages), with the differential shown alongside the raw value.
    tabs_btns.append('<button class="lb-tab" data-tab="homewin" role="tab" aria-selected="false">Home win%</button>')
    panels.append(paired_panel(
        "homewin", False,
        "Home win rate — most above baseline", lb["highest_home_win_pct"],
        "Home win rate — most below baseline", lb["lowest_home_win_pct"],
        "home_win_pct", lambda v: pct(v),
        diff_key="diff", difffmt=WHISTLE_DIFFFMT["home_win_pct"]))
    tabs_btns.append('<button class="lb-tab" data-tab="fta" role="tab" aria-selected="false">Free throws</button>')
    panels.append(paired_panel(
        "fta", False,
        "Combined FTA (RS) — most above baseline", lb["highest_avg_total_fta_rs"],
        "Combined FTA (RS) — most below baseline", lb["lowest_avg_total_fta_rs"],
        "avg_total_fta", lambda v: dec(v),
        diff_key="diff", difffmt=WHISTLE_DIFFFMT["avg_total_fta"]))

    min_n = lb["_meta"]["min_games_for_rate_leaderboards"]
    leaders_inner = """<span class="lb-subhead">Career leaders</span>
  <div class="lb-tabs" role="tablist">{tabs}</div>
  <div class="lb-panels">{panels}</div>
  <p class="caption">Rate leaderboards (home win rate, free throws) include only
  officials with at least {min_n} qualifying games, ranked by the differential
  against the era-adjusted league baseline for the seasons each official worked
  (shown alongside the raw value) — see any <a href="leaderboard/home-win-rate/index.html">
  dedicated leaderboard page</a> for the full methodology.</p>""".format(
        tabs="".join(tabs_btns), panels="".join(panels), min_n=min_n)

    era_tabs, era_panels = dashboard_era_leaders_widget(dashboard["era_leaders"])
    debuts = dashboard_debuts_widget(dashboard["debuts_farewells"])
    debuts_block = ""
    if debuts:
        season, debuts_inner = debuts
        debuts_block = ("""<div class="submod-anchor"><span class="lb-subhead">Debuts &amp; farewells &mdash; {season}</span>
      <div class="history-cols">{inner}</div>
      <p class="caption">"First"/"last game in this database" describe this site's own
      coverage (starting 1993-94), not an official's actual NBA career.
      <a href="debuts/index.html">See every season &rarr;</a></p></div>"""
                     .format(season=esc(season), inner=debuts_inner))

    # Crew chemistry + team officials pair comfortably at half width (a
    # ranked list and a horizontal-scroll strip). Debuts/farewells and the
    # decade leaders each keep the full row -- the latter is a 3-column
    # mini-grid internally (games/playoffs/Finals) that only reads cleanly
    # with the full page width behind it.
    crews_teams_inner = """<div class="submod-anchor"><div class="stat-grid-2">
    <div><span class="lb-subhead">Crew chemistry</span>
      <ol class="history-list">{crews}</ol>
      <p class="caption">The most frequent three-official crews in the database, by games
      worked together. <a href="crews/index.html">See all {n_crews} ranked crews &rarr;</a></p></div>
    <div><span class="lb-subhead">Most frequent official, by team</span>
      <div class="record-strip">{team_officials}</div>
      <p class="caption">Frequency only — not a "best" or "worst" referee ranking by outcome.
      <a href="team-officials/index.html">See the full list &rarr;</a></p></div>
  </div></div>
  {debuts_block}
  <div class="submod-anchor"><span class="lb-subhead">Leaders by decade</span>
    <div class="lb-tabs" role="tablist">{era_tabs}</div>
    <div class="lb-panels">{era_panels}</div>
    <p class="caption">Each decade counts only games within it, not career totals.
    <a href="eras/index.html">See full decade tables &rarr;</a></p>
  </div>""".format(
        crews=dashboard_crews_strip(dashboard["crews"]), n_crews=i(len(dashboard["crews"])),
        team_officials=dashboard_team_officials_strip(dashboard["team_officials"]),
        debuts_block=debuts_block, era_tabs=era_tabs, era_panels=era_panels)

    tier_stats = """<section class="tier">
  <div class="tier-head"><span class="tier-eyebrow">Statistics</span>
  <h2>Leaders, crews &amp; teams</h2>
  <p class="tier-desc">Career leaderboards and cross-official patterns across the full
  1993-94–2025-26 dataset.</p></div>
  <div class="submod-anchor" id="leaders">{leaders}</div>
  <div class="submod-anchor" id="crews-teams">{crews_teams}</div>
</section>""".format(leaders=leaders_inner, crews_teams=crews_teams_inner)

    # ---- tier 3: reference -- deep-dive links + the full directory --------
    more_data_links = ['<a href="%sindex.html">%s</a>' % (esc("leaderboard/%s/" % slug), esc(label))
                       for _key, _ncol, label, slug in WHISTLE_STATS]
    more_data_links += [
        '<a href="crews/index.html">All crews</a>',
        '<a href="team-officials/index.html">Team officials, full list</a>',
        '<a href="debuts/index.html">Debuts &amp; farewells, all seasons</a>',
        '<a href="eras/index.html">Era leaders, all decades</a>',
        '<a href="swings/index.html">Biggest player swings</a>',
        '<a href="compare/index.html">Compare two referees</a>',
    ]

    rows = []
    for r in sorted(refs, key=lambda x: x["name"].lower()):
        rows.append(
            '<tr class="ref-row" data-name="{nm}">'
            '<td data-label="Referee"><a href="referee/{slug}/index.html">{name}</a>'
            '{badge}</td>'
            '<td data-label="Seasons">{seasons}</td>'
            '<td data-label="Games" data-sort="{g}">{gi}</td>'
            '<td data-label="RS" data-sort="{rs}">{rsi}</td>'
            '<td data-label="PO" data-sort="{po}">{poi}</td>'
            "</tr>".format(
                nm=esc(r["name"].lower()), slug=esc(r["slug"]), name=esc(r["name"]),
                badge=' <span class="dot-active" title="Active this season">●</span>' if r["active"] else "",
                seasons=esc(career_span(r["first_season"], r["last_season"])),
                g=r["games_total"], gi=i(r["games_total"]),
                rs=r["games_rs"], rsi=i(r["games_rs"]),
                po=r["games_po"], poi=i(r["games_po"])))

    tier_reference = """<section class="tier">
  <div class="tier-head"><span class="tier-eyebrow">Reference</span>
  <h2>Look up an official</h2>
  <p class="tier-desc">Search or sort all {total} referees, or jump straight to a deeper
  per-stat page.</p></div>
  <div class="submod-anchor" id="directory">
    <div class="more-data-links">{more_data}</div>
    <div class="search-wrap">
      <input type="search" id="ref-search" class="search-input"
        placeholder="Search {total} referees by name…" autocomplete="off"
        aria-label="Search referees by name">
      <p class="search-empty" id="search-empty" hidden>No referee matches that name.</p>
    </div>
    <div class="table-wrap">
      <table class="data-table sortable-table" id="ref-directory"><thead><tr>
        <th class="sortable col-text" data-type="text" scope="col">Referee</th>
        <th scope="col">Seasons</th>
        <th class="sortable col-num" data-type="num" scope="col">Games</th>
        <th class="sortable col-num" data-type="num" scope="col">RS</th>
        <th class="sortable col-num" data-type="num" scope="col">PO</th>
      </tr></thead><tbody>{rows}</tbody></table>
    </div>
  </div>
</section>""".format(total=total, more_data="".join(more_data_links), rows="".join(rows))

    body = hero + subnav + tier_fresh + tier_stats + tier_reference + ref_search(0, "bottom")
    title = "NBA Referee Database — career stats for every on-court official since 1993-94"
    desc = ("Searchable career profiles for %d NBA referees since 1993-94: games worked, "
            "team records, whistle tendencies, playoff appearances, and leaderboards." % total)
    return page(title, desc, 0, body)


# ---------------------------------------------------------------------------
# data-sources page (carries the attribution moved out of the footer)
# ---------------------------------------------------------------------------
def render_sources():
    items = "".join(
        '<li class="src-item"><a href="{u}" rel="noopener">{name}</a>'
        '<span class="src-note">{note}</span></li>'.format(
            u=esc(u), name=esc(name), note=esc(note))
        for name, u, note in ATTRIBUTION)
    body = """<section class="block">
  <div class="block-head"><span class="eyebrow">Attribution</span>
  <h2>Data sources</h2></div>
  <p class="caption">The NBA Referee Database is built from three public datasets,
  combined across eras. Credit and licensing for each:</p>
  <ul class="src-list">{items}</ul>
  <p class="caption">The historical NBA database is published under the Creative
  Commons Attribution-ShareAlike 4.0 licence (CC BY-SA 4.0); the derived
  statistics on this site are shared under the same terms.</p>
</section>""".format(items=items)
    title = "Data sources — NBA Referee Database"
    desc = ("Attribution and licensing for the NBA Referee Database: Wyatt Walsh's "
            "NBA Database (CC BY-SA 4.0), ESPN's public API, and szymonjwiak's box scores.")
    return page(title, desc, 1, body)


# ---------------------------------------------------------------------------
# comparator (DASHBOARD_SPEC section 3) -- a static shell; the actual
# comparison is client-side (any of ~159*158/2 pairs is impossible to
# pre-render), reading the same data/referees/{slug}.json every other page
# already uses. Shareable via ?a=slug&b=slug.
# ---------------------------------------------------------------------------
def render_compare():
    title = "Compare two NBA referees side by side"
    desc = ("Head-to-head comparison of any two NBA referees: whistle profiles, career "
            "summary, and team-record extremes for each, regular season and playoffs. "
            "Share a comparison by its URL.")
    box_a = ref_search(1, "top", type_filter="ref", compare_slot="a")
    box_b = ref_search(1, "top", type_filter="ref", compare_slot="b")
    body = """<section class="block">
  <div class="block-head"><span class="eyebrow"><span class="eyebrow-stripe" aria-hidden="true"></span>
  Compare</span><h2>Compare two referees</h2></div>
  <div class="compare-pickers">
    <div class="compare-picker">{box_a}</div>
    <div class="compare-picker">{box_b}</div>
  </div>
  <p class="empty-note" id="compare-prompt">Select two referees above to compare their
  career numbers side by side.</p>
  <div class="compare-cols">
    <div class="compare-col" id="compare-col-a"></div>
    <div class="compare-col" id="compare-col-b"></div>
  </div>
</section>""".format(box_a=box_a, box_b=box_b)
    return page(title, desc, 1, body)


# ---------------------------------------------------------------------------
# team x referee matchup lookup (docs/MATCHUP_SPEC.md) -- static shell,
# content fetched and rendered client-side keyed off ?team=&ref=, same
# pattern as render_compare() above (data/matchups/{official_id}.json,
# data/referee_games/{official_id}.json and data/teams/{slug}.json already
# exist; nothing pre-rendered per pairing, there are ~5,000 of them).
# ---------------------------------------------------------------------------
def render_matchup():
    title = "NBA team x referee lookup: a team's record in games one official worked"
    desc = ("A team's record in games a specific NBA official worked -- games, win rate, "
            "home/road splits, points for and against, fouls and free throws, regular "
            "season and playoffs -- with the full auditable game log underneath. "
            "Share a lookup by its URL.")
    box_team = ref_search(1, "top", type_filter="team", compare_slot="team")
    box_ref = ref_search(1, "top", type_filter="ref", compare_slot="ref")
    notice = ('<div class="matchup-notice"><p><strong>What this page shows:</strong> a '
              'team’s results in games a specific official worked — never a claim '
              'that the official caused those results. Officials are assigned to games; '
              'nothing on this page implies they influenced who won, and every number can '
              'be checked against the actual games listed below it.</p></div>')
    body = """<section class="block">
  <div class="block-head"><span class="eyebrow"><span class="eyebrow-stripe" aria-hidden="true"></span>
  Matchup</span><h2>Team &times; referee lookup</h2></div>
  {notice}
  <div class="compare-pickers">
    <div class="compare-picker">{box_team}</div>
    <div class="compare-picker">{box_ref}</div>
  </div>
  <p class="empty-note" id="matchup-prompt">Pick a team and an official above to see that
  team's record in games they worked.</p>
  <div id="matchup-result"></div>
</section>""".format(notice=notice, box_team=box_team, box_ref=box_ref)
    return page(title, desc, 1, body)


# ---------------------------------------------------------------------------
# Tier C full-list pages (docs/TIER_C_SPEC.md)
# ---------------------------------------------------------------------------
def crews_table(crews):
    """Rendered only on /crews/ (depth 1) -- ref_link needs root='../'."""
    cols = [("Crew", "text"), ("Games together", "num"), ("First season", "text"),
            ("Last season", "text"), ("Avg total pts", "num"), ("Avg total FTA", "num")]
    ths = "".join('<th class="sortable {c}" data-type="{t}" scope="col">{l}</th>'.format(
        c="col-text" if t == "text" else "col-num", t=t, l=esc(l)) for l, t in cols)
    body = []
    for c in crews:
        names_sort = esc(", ".join(r["name"] for r in c["refs"]).lower())
        names = ", ".join(ref_link(r["name"], r["slug"], root="../") for r in c["refs"])
        pts = ("%s <span class=\"lb-n\">n=%s</span>" % (dec(c["avg_total_points"]), i(c["pts_n"]))
              if c["avg_total_points"] is not None else "—")
        fta = ("%s <span class=\"lb-n\">n=%s</span>" % (dec(c["avg_total_fta"]), i(c["fta_n"]))
              if c["avg_total_fta"] is not None else "—")
        body.append(
            "<tr>"
            '<td data-label="Crew" data-sort="{sortnm}">{names}</td>'
            '<td data-label="Games together" data-sort="{g}">{gi}</td>'
            '<td data-label="First season">{fs}</td>'
            '<td data-label="Last season">{ls}</td>'
            '<td data-label="Avg total pts">{pts}</td>'
            '<td data-label="Avg total FTA">{fta}</td>'
            "</tr>".format(sortnm=names_sort, names=names, g=c["games"], gi=i(c["games"]),
                           fs=esc(c["first_season"]), ls=esc(c["last_season"]), pts=pts, fta=fta))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def render_crews(crews):
    title = "Most frequent 3-official crews in NBA history"
    desc = ("The %d most frequent three-official officiating crews in the database, "
            "ranked by games worked together, with combined scoring and free-throw "
            "averages for each trio." % len(crews))
    chips = [stat_chip("Crews ranked", i(len(crews)), accent=True)]
    blocks = [back_home(root="../"), hero_block("Crew chemistry", "Most frequent officiating crews", "", chips),
              ref_search(1, "top")]
    blocks.append(section(None, "Top crew trios",
                          '<div class="table-wrap">%s</div>' % crews_table(crews),
                          '<p class="caption">Crew-of-three games only (alternate officials '
                          'excluded, per the site-wide first-3-by-row-order rule). A '
                          'descriptive ranking of how often three officials have worked '
                          'together, not a causal claim.</p>'))
    blocks.append(ref_search(1, "bottom"))
    return page(title, desc, 1, "".join(blocks))


def team_officials_table(rows):
    """Rendered only on /team-officials/ (depth 1) -- team_cell/ref_link need
    root='../'."""
    ths = "".join('<th class="sortable {c}" data-type="{t}" scope="col">{l}</th>'.format(
        c="col-text" if t == "text" else "col-num", t=t, l=esc(l))
        for l, t in [("Team", "text"), ("Official", "text"), ("Games", "num")])
    body = []
    for t in rows:
        body.append(
            "<tr>"
            '<td data-label="Team" data-sort="{tsort}">{team}</td>'
            '<td data-label="Official" data-sort="{rsort}">{ref}</td>'
            '<td data-label="Games" data-sort="{g}">{gi} <span class="lb-n">n={n}</span></td>'
            "</tr>".format(tsort=esc(t["team_name"].lower()), team=team_cell(t["tricode"], root="../"),
                           rsort=esc(t["ref_name"].lower()),
                           ref=ref_link(t["ref_name"], t["ref_slug"], root="../"),
                           g=t["games"], gi=i(t["games"]), n=i(t["n"])))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def render_team_officials(team_officials):
    title = "The most frequent official for every NBA team"
    desc = ("For every one of the %d NBA franchises in this database: the official who "
            "has worked the most of that team's games. Frequency only, not a win-rate "
            "ranking." % len(team_officials))
    chips = [stat_chip("Teams", i(len(team_officials)), accent=True)]
    blocks = [back_home(root="../"), hero_block("Team officials", "Most frequent official, by team", "", chips),
              ref_search(1, "top")]
    blocks.append(section(None, "Most frequent official by team",
                          '<div class="table-wrap">%s</div>' % team_officials_table(team_officials),
                          '<p class="caption">Frequency only — the official who has worked the '
                          'most games for each franchise, not a "best" or "worst" ranking by '
                          'outcome. Per-referee win rates for every team appear on that team’s '
                          'own page.</p>'))
    blocks.append(ref_search(1, "bottom"))
    return page(title, desc, 1, "".join(blocks))


def debuts_farewells_list(entries, root=""):
    if not entries:
        return '<p class="empty-note">None on record.</p>'
    items = "".join('<li class="partner"><a class="partner-name" href="%sreferee/%s/index.html">%s'
                    '</a></li>' % (root, esc(r["slug"]), esc(r["name"])) for r in entries)
    return '<ul class="partners">%s</ul>' % items


def debuts_farewells_section(block, root=""):
    """root="" for the index widget (depth 0); root="../" for the full
    /debuts/ page (depth 1)."""
    debuts = debuts_farewells_list(block["debuts"], root)
    farewells = debuts_farewells_list(block["farewells"], root)
    return ('<div class="history-col"><h3 class="lb-subhead">First game in this database '
            '&mdash; {season}</h3>{debuts}</div>'
            '<div class="history-col"><h3 class="lb-subhead">Last game in this database '
            '&mdash; {season}</h3>{farewells}</div>').format(
        season=esc(block["season"]), debuts=debuts, farewells=farewells)


def render_debuts(debuts_farewells):
    title = "NBA referees' first and last games in this database, by season"
    desc = ("Every season since 1993-94: which officials' first game in this database "
            "falls that season, and whose last game does. Not NBA debuts or retirements "
            "— this dataset's own coverage starts at 1993-94, so officials active before "
            "then have real careers predating it.")
    chips = [stat_chip("Seasons", i(len(debuts_farewells)), accent=True)]
    blocks = [back_home(root="../"), hero_block("Debuts & farewells", "First and last games, by season", "", chips),
              ref_search(1, "top")]
    note = ('<p class="caption">"First game" / "last game" describe this DATABASE’s coverage, '
            'not an official’s actual NBA career — officials active before the 1993-94 floor '
            'have real games this site doesn’t carry. The current season (%s) never lists a '
            '"last game" here, since those officials are presumably still active.</p>'
            % esc(CURRENT_SEASON))
    inner = "".join('<section class="block"><div class="block-head"><h2>%s</h2></div>'
                    '<div class="history-cols">%s</div></section>'
                    % (esc(block["season"]), debuts_farewells_section(block, root="../"))
                    for block in debuts_farewells)
    blocks.append(note)
    blocks.append(inner)
    blocks.append(ref_search(1, "bottom"))
    return page(title, desc, 1, "".join(blocks))


def era_panel(tab_id, active, era, root=""):
    def col(title, rows):
        items = "".join(leaderboard_row(n, r, "value", i, root=root) for n, r in enumerate(rows, 1))
        if not items:
            items = '<li class="empty-note">No qualifying referees this decade.</li>'
        return '<div class="lb-col"><h3 class="lb-subhead">{t}</h3><ol class="lb-list">{it}</ol></div>'.format(
            t=esc(title), it=items)
    cols = (col("Total games", era["total_games"]) + col("Playoff games", era["playoff_games"])
           + col("Finals games", era["finals_games"]))
    return '<div class="lb-panel lb-triple{act}" data-panel="{id}">{cols}</div>'.format(
        act=" is-active" if active else "", id=tab_id, cols=cols)


def era_leaders_block(era_leaders, active_first=True, root=""):
    """Shared tabbed markup for era_leaders -- used identically on the index
    widget (root="", depth 0) and the full /eras/ page (root="../", depth 1)
    (same reusable .lb-tab/.lb-panel component the whistle-profile
    leaderboards already use elsewhere on the index)."""
    order = ["1990s", "2000s", "2010s", "2020s"]
    tabs, panels = [], []
    for idx, label in enumerate(order):
        era = era_leaders.get(label)
        if not era:
            continue
        active = active_first and idx == 0
        tab_label = era["label"] + (" (partial)" if era["partial"] else "")
        tabs.append('<button class="lb-tab{act}" data-tab="era-{id}" role="tab" '
                   'aria-selected="{sel}">{lab}</button>'.format(
                       act=" is-active" if active else "", id=label,
                       sel="true" if active else "false", lab=esc(tab_label)))
        panels.append(era_panel("era-%s" % label, active, era, root=root))
    return "".join(tabs), "".join(panels)


def render_eras(era_leaders):
    title = "NBA referee leaders by decade, 1990s to today"
    desc = ("Officiating leaders by decade — total games, playoff games, and Finals "
            "games worked in the 1990s (partial), 2000s, 2010s, and 2020s.")
    tabs, panels = era_leaders_block(era_leaders, root="../")
    blocks = [back_home(root="../"), hero_block("Era leaders", "Referee leaders by decade", "", [])]
    blocks.append("""<section class="block" id="eras">
  <div class="block-head"><h2>Leaders by decade</h2></div>
  <div class="lb-tabs" role="tablist">{tabs}</div>
  <div class="lb-panels">{panels}</div>
  <p class="caption">Each decade counts only games that fall within it — not career
  totals. The 1990s bucket is partial: this database starts at 1993-94, not
  1990-91.</p>
</section>""".format(tabs=tabs, panels=panels))
    return page(title, desc, 1, "".join(blocks))


def swings_all_table(rows):
    """Rendered only on /swings/ (depth 1) -- player_link/ref_link need
    root='../'."""
    ths = "".join('<th class="sortable {c}" data-type="{t}" scope="col">{l}</th>'.format(
        c="col-text" if t == "text" else "col-num", t=t, l=esc(l))
        for l, t in [("Player", "text"), ("Referee", "text"), ("Games", "num"),
                     ("PTS with", "num"), ("PTS baseline", "num"), ("PTS swing", "num")])
    body = []
    for r in rows:
        body.append(
            "<tr>"
            '<td data-label="Player" data-sort="{pn}">{pcell}</td>'
            '<td data-label="Referee" data-sort="{rn}">{rcell}</td>'
            '<td data-label="Games" data-sort="{n}">{ni}</td>'
            '<td data-label="PTS with" data-sort="{pw}">{pwf}</td>'
            '<td data-label="PTS baseline" data-sort="{pb}">{pbf}</td>'
            '<td data-label="PTS swing" data-sort="{ps}"><span class="{psc}">{pss}</span></td>'
            "</tr>".format(
                pn=esc(r["player_name"].lower()),
                pcell=player_link(r["player_name"], r["player_slug"], root="../"),
                rn=esc(r["ref_name"].lower()), rcell=ref_link(r["ref_name"], r["ref_slug"], root="../"),
                n=r["n_games"], ni=i(r["n_games"]),
                pw=r["pts_with_ref"], pwf=dec(r["pts_with_ref"]),
                pb=r["pts_baseline"], pbf=dec(r["pts_baseline"]),
                ps=r["pts_swing"], pss=signed(r["pts_swing"]), psc=swing_class(r["pts_swing"])))
    return ('<table class="data-table sortable-table"><thead><tr>{ths}</tr></thead>'
            '<tbody>{body}</tbody></table>').format(ths=ths, body="".join(body))


def render_swings(swings_all):
    top, bottom = swings_all["top"], swings_all["bottom"]
    title = "Biggest player scoring swings by NBA referee"
    desc = ("The %d biggest positive and negative scoring swings across every "
            "qualifying (15+ game) player-referee pair in the database — the average "
            "difference between a player's output with a given official and that "
            "player's own same-season baseline." % (len(top) + len(bottom)))
    chips = [stat_chip("Qualifying pairs", i(swings_all["total_pairs"]), accent=True),
            stat_chip("Min. games", "15")]
    blocks = [back_home(root="../"), hero_block("Swings", "Biggest player scoring swings", "", chips),
              ref_search(1, "top")]
    note = ('<p class="caption">"Swing" is the average difference between a player’s output '
            'in games a given official worked and that player’s own same-season average — '
            'a descriptive split, not a causal claim. It does not mean the official affected '
            'the player. Pairs with at least 15 games together.</p>')
    blocks.append(note)
    blocks.append(
        '<section class="block" id="top"><div class="block-head"><h2>Biggest positive swings</h2></div>'
        '<div class="table-wrap">%s</div></section>' % swings_all_table(top))
    blocks.append(
        '<section class="block" id="bottom"><div class="block-head"><h2>Biggest negative swings</h2></div>'
        '<div class="table-wrap">%s</div></section>' % swings_all_table(bottom))
    blocks.append(ref_search(1, "bottom"))
    return page(title, desc, 1, "".join(blocks))


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
CSS = r"""/* ==========================================================================
   NBA Referee Database — shared stylesheet.

   Visual language adopted from the HoopsHype NBA Polymarket tracker
   (jsierrahoopshype/nba-polymarket) so this reads as the same family of
   tools: Apple-neutral light surfaces, blue accent, DM Sans body with a
   JetBrains Mono data/label voice, 12px cards, uppercase-mono table headers.

   Self-contained: Polymarket loads DM Sans / JetBrains Mono from Google
   Fonts; here they are named first in each stack but fall back to system
   fonts, so the site keeps zero network dependencies. Light-only, matching
   the tracker (it ships no dark theme).
   ========================================================================== */

:root{
  --bg:#f5f5f7; --surface:#fff; --surface-hover:#f0f0f2; --border:#d1d1d6;
  --text:#1d1d1f; --text-secondary:#6e6e73;
  --accent:#3b82f6; --accent-dim:rgba(59,130,246,.15);
  --green:#1d8a40; --green-dim:rgba(52,199,89,.16); --green-bar:#34c759;
  --red:#d12c2c; --red-dim:rgba(239,68,68,.13);
  --orange:#b26b00; --orange-dim:rgba(245,158,11,.16);
  --sans:'DM Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace;
  --maxw:1200px;
}
*{margin:0;padding:0;box-sizing:border-box}
html{font-size:115%;-webkit-text-size-adjust:100%}
body{font-family:var(--sans);background:var(--bg);color:var(--text);
  line-height:1.5;min-height:100vh;-webkit-font-smoothing:antialiased;
  font-feature-settings:"tnum" 1;}
a{color:var(--text);text-decoration:none}
a:hover,a:focus,a:active{color:var(--accent);text-decoration:underline}
h1,h2,h3{font-weight:700;letter-spacing:-.02em;line-height:1.2}
.mono,.chip-val,.wm-val,.hstat-num,.lb-val,.big-num,.rank,
.data-table td,.team-tag,.round-tag{font-family:var(--mono);font-variant-numeric:tabular-nums}
.skip{position:absolute;left:-999px}
.skip:focus{left:8px;top:8px;background:var(--text);color:#fff;padding:8px 12px;z-index:20;border-radius:8px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
::selection{background:var(--accent-dim)}

/* decorative stripe motif from the old theme is dropped for the Polymarket look */
.brand-stripe,.eyebrow-stripe,.ref-hero-stripe,.hero-rule{display:none}

/* ---- masthead ---- */
.masthead{display:flex;flex-wrap:wrap;align-items:baseline;gap:.4rem 1rem;
  max-width:var(--maxw);margin:0 auto;padding:1.3rem 1.5rem .9rem}
.brand{display:inline-flex;align-items:baseline;gap:.5rem;color:var(--text);font-weight:700}
.brand:hover{text-decoration:none}
.brand-name{font-size:1.05rem;letter-spacing:-.02em}
.brand-sub{color:var(--text-secondary);font-family:var(--mono);font-size:.68rem;
  text-transform:uppercase;letter-spacing:.06em;margin-left:auto}
.masthead-nav a{font-family:var(--mono);font-size:.72rem;font-weight:600;
  color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em}
.masthead-nav a:hover{color:var(--accent)}
main{max-width:var(--maxw);margin:0 auto;padding:0 1.5rem}

/* ---- index hero ---- */
.index-hero{padding:1.6rem 0 1.4rem;border-bottom:1px solid var(--border)}
.hero-kicker,.ref-kicker{font-family:var(--mono);text-transform:uppercase;
  letter-spacing:.08em;font-size:.68rem;font-weight:600;color:var(--accent);margin:0 0 .6rem}
.hero-title{font-size:1.9rem;letter-spacing:-.03em}
.hero-lead{max-width:60rem;color:var(--text-secondary);font-size:.95rem;margin:.6rem 0 0}
.hero-stats{display:flex;flex-wrap:wrap;gap:1.6rem;margin-top:1.3rem}
.hstat{display:flex;flex-direction:column}
.hstat-num{font-size:1.6rem;font-weight:700;line-height:1;letter-spacing:-.02em}
.hstat-label{font-family:var(--mono);font-size:.62rem;color:var(--text-secondary);
  text-transform:uppercase;letter-spacing:.06em;margin-top:.4rem}

/* ---- blocks / section headings ---- */
.block{padding:1.5rem 0;border-bottom:1px solid var(--border)}
.block:last-of-type{border-bottom:0}
.block-head{margin-bottom:.9rem}
.eyebrow{display:inline-flex;align-items:center;gap:.4rem;font-family:var(--mono);
  text-transform:uppercase;letter-spacing:.06em;font-size:.64rem;font-weight:600;
  color:var(--text-secondary);margin-bottom:.4rem}
.block-head h2{font-size:1.15rem;font-weight:700;letter-spacing:-.02em}
.caption{color:var(--text-secondary);font-size:.78rem;max-width:70ch;margin:.7rem 0 0}

/* ---- index page: three-tier information hierarchy ----------------------
   PRIMARY (fresh: today's rotation + oddities), SECONDARY (statistics:
   leaderboards + crews/teams), UTILITY (reference: directory + deep links).
   One .tier-head per tier carries the section-level framing; individual
   modules inside a tier are separated by dividers/typography (.submod-anchor,
   .lb-subhead), not by another bordered/rounded card each -- that repetition
   ("DASHBOARD" stamped on eight boxes) was the thing this round removes. */
.index-subnav{position:sticky;top:0;z-index:6;background:var(--bg);
  border-bottom:1px solid var(--border);display:flex;gap:1.3rem;
  padding:.65rem 0;margin-top:.4rem;overflow-x:auto;white-space:nowrap;scrollbar-width:thin}
.index-subnav a{font-family:var(--mono);font-size:.72rem;font-weight:600;
  color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em;flex:0 0 auto}
.index-subnav a:hover{color:var(--accent);text-decoration:none}
.tier{padding:2rem 0;border-bottom:1px solid var(--border)}
.tier:last-of-type{border-bottom:0}
.tier-head{padding-bottom:1.1rem;margin-bottom:1.3rem;border-bottom:1px solid var(--border)}
.tier-eyebrow{display:block;font-family:var(--mono);text-transform:uppercase;
  letter-spacing:.08em;font-size:.68rem;font-weight:600;color:var(--accent);margin-bottom:.35rem}
.tier-head h2{font-size:1.4rem;letter-spacing:-.03em}
.tier-desc{color:var(--text-secondary);font-size:.88rem;max-width:60rem;margin:.4rem 0 0}
.submod-anchor{scroll-margin-top:3.6rem}
.submod-anchor+.submod-anchor{margin-top:2rem;padding-top:2rem;border-top:1px solid var(--border)}
.stat-grid-2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1.8rem}
.today-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1.8rem}
.today-col #spotlight-card,.today-col #ondate-card{margin-top:.5rem}
#tonights-crews{margin-top:1.6rem;padding-top:1.6rem;border-top:1px solid var(--border)}
.more-data-links{display:flex;flex-wrap:wrap;gap:.4rem 1.1rem;margin-bottom:1.3rem;
  font-family:var(--mono);font-size:.72rem}
.more-data-links a{color:var(--text-secondary);font-weight:600}
.more-data-links a:hover{color:var(--accent)}
@media(max-width:860px){
  /* A permanently pinned bar costs real vertical space on a short mobile
     viewport for the whole length of an already-long page; keep the jump
     links (still useful for a quick table of contents) but stop pinning
     them so they don't sit on top of content while scrolling. */
  .index-subnav{position:static;overflow-x:visible;white-space:normal;flex-wrap:wrap;
    background:none;border-bottom:0;padding:.2rem 0 1rem}
  .stat-grid-2,.today-grid{grid-template-columns:minmax(0,1fr);gap:1.6rem}
}

/* ---- referee hero (flat header, Polymarket .phead treatment) ---- */
.ref-hero{margin-top:1.3rem;padding-bottom:1.1rem;border-bottom:1px solid var(--border)}
.ref-hero-body{padding:0}
.ref-name{font-size:1.7rem;letter-spacing:-.02em;margin:.15rem 0 0}
.ref-badges{margin-top:.6rem}
.badge{display:inline-block;font-family:var(--mono);font-size:.64rem;font-weight:700;
  padding:.16rem .5rem;border-radius:5px;text-transform:uppercase;letter-spacing:.04em}
.badge-active{background:var(--green-dim);color:var(--green)}
.badge-past{background:var(--surface-hover);color:var(--text-secondary)}
.compare-btn{display:inline-block;font-family:var(--mono);font-size:.64rem;font-weight:700;
  padding:.16rem .55rem;border-radius:5px;text-transform:uppercase;letter-spacing:.04em;
  background:var(--accent-dim);color:var(--accent)}
.compare-btn:hover{background:var(--accent);color:#fff;text-decoration:none}
.chip-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));
  gap:.7rem;margin-top:1.1rem}
.chip{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.75rem .9rem}
.chip-val{display:block;font-size:1.5rem;font-weight:700;letter-spacing:-.02em}
.chip-accent .chip-val{color:var(--accent)}
.chip-label{display:block;font-family:var(--mono);font-size:.58rem;text-transform:uppercase;
  letter-spacing:.06em;color:var(--text-secondary);margin-top:.35rem}

/* ---- whistle profile ---- */
.whistle-cols{display:grid;grid-template-columns:1fr 1fr;gap:1.1rem}
.whistle-kind{font-size:.84rem;font-weight:700;padding-bottom:.5rem;
  border-bottom:1px solid var(--border);margin-bottom:.7rem;
  display:flex;justify-content:space-between;align-items:baseline}
.whistle-n{font-family:var(--mono);font-size:.66rem;font-weight:500;color:var(--text-secondary)}
.whistle-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;
  background:var(--border);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.wm{display:block;background:var(--surface);padding:.7rem .8rem;color:inherit;
  text-decoration:none;transition:background-color .12s}
.wm:hover{background:var(--accent-dim)}
.wm-val{font-size:1.1rem;font-weight:700;letter-spacing:-.02em;
  display:flex;flex-wrap:wrap;align-items:baseline;gap:.3rem}
.wm-lg{font-size:.68rem;font-weight:600;color:var(--text-secondary)}
.wm-diff{font-family:var(--mono);font-size:.72rem;font-weight:700}
.wm-label{font-size:.74rem;color:var(--text-secondary);margin-top:.2rem;line-height:1.3}
.wm-rank{font-family:var(--mono);font-size:.6rem;color:var(--text-secondary);margin-top:.3rem}
.wm-n{font-family:var(--mono);font-size:.62rem;color:var(--text-secondary);margin-top:.2rem}
/* Single-hue "how unusual" intensity (distance from the field's median) --
   deliberately NOT red/green: no directional good/bad implication, just how
   far a card's value sits from typical. wm-i0 = near median (no tint) up to
   wm-i4 = most extreme (either direction alike). */
.wm-i1{background:rgba(59,130,246,.08)}
.wm-i2{background:rgba(59,130,246,.16)}
.wm-i3{background:rgba(59,130,246,.26)}
.wm-i4{background:rgba(59,130,246,.38)}
.wm-i3 .wm-val,.wm-i4 .wm-val{color:var(--accent)}
.wm-i1:hover,.wm-i2:hover,.wm-i3:hover,.wm-i4:hover{background:rgba(59,130,246,.46)}

/* ---- tables (Polymarket table.lb treatment) ---- */
.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:auto}
.data-table{width:100%;border-collapse:collapse;font-size:.82rem}
.data-table th,.data-table td{padding:.55rem .6rem;text-align:right;white-space:nowrap}
.data-table thead th{position:sticky;top:0;z-index:2;background:var(--surface-hover);
  color:var(--text-secondary);font-family:var(--mono);text-transform:uppercase;
  letter-spacing:.04em;font-size:.62rem;font-weight:600;border-bottom:1px solid var(--border)}
.data-table td{font-weight:500}
.data-table th:first-child,.data-table td:first-child,
.data-table .col-text,.data-table td[data-label="Player"],
.data-table td[data-label="Referee"],.data-table td[data-label="Matchup"],
.data-table td[data-label="Team"],.data-table td[data-label="Date"],
.data-table td[data-label="Result"]{text-align:left}
.data-table td[data-label="Player"],.data-table td[data-label="Referee"]{font-family:var(--sans);font-weight:600}
.data-table tbody tr{border-top:1px solid var(--border);transition:background .12s}
.data-table tbody tr:hover{background:var(--surface-hover)}
.sortable{cursor:pointer;user-select:none;transition:color .15s}
.sortable:hover{color:var(--accent)}
.sortable::after{content:"\2195";opacity:.5;margin-left:.25rem;font-size:.8em}
.sortable.sort-asc::after{content:"\2191";opacity:1;color:var(--accent)}
.sortable.sort-desc::after{content:"\2193";opacity:1;color:var(--accent)}
.team-cell{display:inline-flex;align-items:baseline;gap:.4rem}
.team-name{font-family:var(--sans);font-weight:600;font-size:.86rem}
.team-tag{display:inline-block;font-family:var(--mono);font-size:.6rem;font-weight:600;
  letter-spacing:.03em;color:var(--text-secondary);background:var(--surface-hover);
  border-radius:4px;padding:.05rem .3rem}
.matchup .vs{color:var(--text-secondary);font-size:.72rem;margin:0 .1rem}
.data-table td[data-label="Crew"]{text-align:left;white-space:normal}
.crew{font-family:var(--sans);font-size:.76rem;color:var(--text-secondary);
  line-height:1.5;min-width:12rem}
.team-list{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.5rem}
.backlink{display:inline-block;margin:1.2rem 0 .2rem;font-family:var(--mono);font-size:.72rem;
  font-weight:600;color:var(--text-secondary)}
.round-tag{display:inline-block;font-family:var(--mono);font-size:.6rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.04em;padding:.1rem .4rem;border-radius:4px;
  background:var(--accent-dim);color:var(--accent)}
.big-num{font-size:.95rem;font-weight:700}
.rank{color:var(--text-secondary);font-size:.76rem}
.pos{color:var(--green);font-weight:600}
.neg{color:var(--red);font-weight:600}

/* diverging heat scale for swing / margin values (bucketed by magnitude) */
.sw{display:inline-block;min-width:2.9rem;text-align:right;padding:.06rem .4rem;
  border-radius:5px;font-weight:600;font-variant-numeric:tabular-nums}
.s-zero{color:var(--text-secondary)}
.s-pos-1{color:var(--green);background:rgba(52,199,89,.10)}
.s-pos-2{color:var(--green);background:rgba(52,199,89,.18)}
.s-pos-3{color:#136b31;background:rgba(52,199,89,.30)}
.s-pos-4{color:#0e5325;background:rgba(52,199,89,.44);font-weight:700}
.s-neg-1{color:var(--red);background:rgba(239,68,68,.09)}
.s-neg-2{color:var(--red);background:rgba(239,68,68,.17)}
.s-neg-3{color:#a52218;background:rgba(239,68,68,.28)}
.s-neg-4{color:#851a12;background:rgba(239,68,68,.42);font-weight:700}

/* ---- notable counts ---- */
.notable-counts{font-family:var(--mono);font-size:.8rem;color:var(--text-secondary);margin:.3rem 0 0}
.notable-counts b{color:var(--text)}
.empty-note{color:var(--text-secondary);font-size:.85rem;padding:.4rem 0}

/* ---- search + directory ---- */
.search-wrap{margin-bottom:1rem}
.search-input{width:100%;max-width:30rem;padding:.6rem .9rem;font-size:16px;font-family:inherit;
  background:var(--surface);border:1px solid var(--border);border-radius:10px;color:var(--text)}
.search-input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim)}
.search-empty{color:var(--text-secondary);font-size:.82rem;margin:.7rem .2rem 0}
.dot-active{color:var(--green);font-size:.55rem;vertical-align:middle;margin-left:.4rem}

/* navigate-search (top + bottom of ref pages, bottom of index) */
.refsearch-wrap{position:relative;max-width:32rem;margin:1.1rem 0}
.refsearch-wrap[data-pos="top"]{margin:1.3rem 0 .3rem}
.refsearch{width:100%;padding:.6rem .9rem;font-size:16px;font-family:inherit;
  background:var(--surface);border:1px solid var(--border);border-radius:10px;color:var(--text)}
.refsearch:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim)}
.refsearch-results{position:absolute;top:100%;left:0;right:0;margin-top:.25rem;z-index:200;
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  box-shadow:0 8px 24px rgba(0,0,0,.12);max-height:20rem;overflow-y:auto}
.refsearch-results[hidden]{display:none}
.rs-item{display:flex;align-items:baseline;gap:.6rem;padding:.5rem .8rem;
  border-bottom:1px solid var(--border);color:var(--text);text-decoration:none}
.rs-item:last-child{border-bottom:none}
.rs-item:hover,.rs-item.active{background:var(--surface-hover);text-decoration:none}
.rs-name{flex:1;font-weight:600;font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rs-meta{font-family:var(--mono);font-size:.66rem;color:var(--text-secondary);flex:none}
.rs-empty{padding:.6rem .8rem;color:var(--text-secondary);font-size:.82rem}
.rs-badge{flex:none;font-family:var(--mono);font-size:.52rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;padding:.1rem .3rem;border-radius:4px;color:#fff;width:3.1em;text-align:center}
.rs-ref{background:var(--accent)}
.rs-team{background:var(--green)}
.rs-player{background:var(--orange)}

/* most-frequent-crewmates card (ref pages) */
.partners{list-style:none;display:grid;grid-template-columns:repeat(auto-fill,minmax(13rem,1fr));gap:.5rem}
.partner{display:flex;justify-content:space-between;align-items:baseline;gap:.6rem;
  background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:.5rem .75rem}
.partner-name{font-weight:600;font-size:.86rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.partner-n{font-family:var(--mono);font-size:.72rem;color:var(--text-secondary);flex:none}

/* data-sources page list */
.src-list{list-style:none;margin:.4rem 0 1.2rem;display:flex;flex-direction:column;gap:.7rem}
.src-item{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.8rem 1rem}
.src-item a{font-weight:600}
.src-note{display:block;font-family:var(--mono);font-size:.72rem;color:var(--text-secondary);margin-top:.25rem}

/* ---- leaderboards ---- */
.lb-tabs{display:flex;gap:.3rem;flex-wrap:wrap;margin-bottom:1.1rem}
.lb-tab{font-family:var(--mono);font-size:.7rem;font-weight:600;padding:.34rem .7rem;
  border:1px solid var(--border);border-radius:8px;background:var(--surface);
  color:var(--text-secondary);cursor:pointer;transition:.12s;white-space:nowrap}
.lb-tab:hover{border-color:var(--accent);color:var(--accent)}
.lb-tab.is-active{background:var(--accent);border-color:var(--accent);color:#fff}
.lb-panel{display:none}
.lb-panel.is-active{display:block}
.lb-paired.is-active{display:grid;grid-template-columns:1fr 1fr;gap:1.4rem}
.lb-triple.is-active{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1.2rem}
.lb-subhead{font-family:var(--mono);font-size:.64rem;text-transform:uppercase;
  letter-spacing:.06em;color:var(--text-secondary);font-weight:600;margin-bottom:.6rem}
.lb-list{list-style:none}
.lb-list-wide{columns:2;column-gap:2.4rem}
.lb-row{display:flex;align-items:baseline;gap:.7rem;padding:.5rem 0;
  border-bottom:1px solid var(--border);break-inside:avoid}
.lb-rank{font-family:var(--mono);color:var(--text-secondary);font-size:.74rem;width:1.8em;flex:none;text-align:right}
.lb-name{flex:1;color:var(--text);font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lb-name:hover{color:var(--accent)}
.lb-val{font-family:var(--mono);font-weight:700;font-size:.86rem}
.lb-diff{font-weight:600;color:var(--text-secondary);font-size:.74rem;margin-left:.35rem}
.lb-n{font-weight:500;color:var(--text-secondary);font-size:.7rem;margin-left:.3rem}

/* ---- frontpage dashboard (spotlight, on this date, records, history) ----
   No card boxes here by design (see the tier-system comment above) --
   typography and the tier/submodule dividers carry the framing instead. */
#spotlight-card,#ondate-card{margin-top:0}
.spotlight-name{font-size:1.3rem;font-weight:700;color:var(--text)}
.spotlight-name:hover{color:var(--accent)}
.spotlight-meta{font-family:var(--mono);font-size:.74rem;color:var(--text-secondary);margin:.4rem 0 0}
.spotlight-sig{font-size:.88rem;margin:.6rem 0 0;max-width:60ch}
#ondate-card{font-size:.92rem}

.record-strip{display:flex;gap:0;overflow-x:auto;padding-bottom:.3rem;scrollbar-width:thin}
.record-item{flex:0 0 auto;min-width:9.5rem;padding:0 1.2rem;border-right:1px solid var(--border);
  color:inherit;text-decoration:none;display:flex;flex-direction:column}
.record-item:first-child{padding-left:0}
.record-item:last-child{border-right:0}
.record-item:hover .record-val{color:var(--accent)}
.record-label{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;
  letter-spacing:.05em;color:var(--text-secondary)}
.record-val{font-size:1.2rem;font-weight:700;margin-top:.3rem;letter-spacing:-.01em}
.record-ref{font-size:.76rem;color:var(--text-secondary);margin-top:.35rem}
.record-n{font-family:var(--mono);font-size:.66rem}

.history-cols{display:grid;grid-template-columns:1.4fr 1fr;gap:1.6rem}
.history-list{list-style:none}
.history-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:.5rem;
  padding:.5rem 0;border-bottom:1px solid var(--border);font-size:.84rem}
.history-rank{font-family:var(--mono);color:var(--text-secondary);font-size:.72rem;width:1.4em}
.history-pts{font-family:var(--mono);font-weight:700}
.history-matchup{font-size:.78rem;color:var(--text-secondary)}
.history-date{font-family:var(--mono);font-size:.7rem;color:var(--text-secondary)}
.history-crew{flex-basis:100%;font-size:.72rem;color:var(--text-secondary)}
.history-trio{font-size:.85rem}
.curiosity-list{font-size:.8rem;color:var(--text-secondary);padding-left:1.1rem}
.curiosity-list li{margin-bottom:.5rem}

#tonights-crews-body{display:flex;flex-direction:column;margin-top:.5rem}
.crew-game{display:flex;flex-wrap:wrap;gap:.6rem;align-items:baseline;
  padding:.55rem 0;border-bottom:1px solid var(--border);font-size:.84rem}
.crew-game:last-child{border-bottom:0}
.crew-matchup{font-weight:700}
.crew-tip{font-family:var(--mono);font-size:.72rem;color:var(--text-secondary)}
.crew-names{font-size:.8rem}

/* ---- comparator ---- */
.compare-pickers{display:grid;grid-template-columns:1fr 1fr;gap:1.2rem;margin-bottom:.4rem}
.compare-cols{display:grid;grid-template-columns:1fr 1fr;gap:1.6rem;margin-top:1.1rem}
.compare-col:empty{display:none}
.compare-line{font-size:.84rem;margin:.3rem 0}

/* ---- /matchup/ (docs/MATCHUP_SPEC.md) --------------------------------
   Reuses the whistle-profile card shell (.whistle-cols/.whistle-col/
   .whistle-kind/.whistle-grid/.wm/.wm-val/.wm-label/.wm-n) for the per-kind
   stat grid -- same "raw value, label, n" card language already used on
   every referee page, so this page doesn't invent a second visual system.
   Only the suppression/flag states below are new. */
.matchup-notice{border-left:3px solid var(--accent);background:var(--accent-dim);
  padding:.9rem 1.1rem;border-radius:0 8px 8px 0;margin:1rem 0 1.4rem;max-width:70rem}
.matchup-notice p{font-size:.86rem;line-height:1.5}
.matchup-notice strong{color:var(--text)}
.matchup-heading{font-size:1.25rem;letter-spacing:-.02em;margin:1.8rem 0 .8rem}
.matchup-heading:first-child{margin-top:0}
.mu-record-line{font-size:.86rem;color:var(--text-secondary);margin-bottom:.8rem}
.wm-val.wm-suppressed{font-size:.76rem;font-weight:600;font-style:italic;
  color:var(--text-secondary);letter-spacing:0}
.mu-flag{display:inline-block;font-family:var(--mono);font-size:.58rem;font-weight:700;
  color:var(--orange);background:var(--orange-dim);padding:.1rem .4rem;
  border-radius:4px;text-transform:uppercase;letter-spacing:.03em;vertical-align:middle}
.mu-td-suppressed{color:var(--text-secondary);font-style:italic;font-size:.8rem}
.mu-win{color:var(--green);font-weight:700}
.mu-loss{color:var(--red);font-weight:700}
.mu-section-caption{margin-top:.3rem}

/* ---- footer ---- */
.site-foot{max-width:var(--maxw);margin:0 auto;padding:1.6rem 1.5rem 3rem;
  border-top:1px solid var(--border);color:var(--text-secondary);
  font-family:var(--mono);font-size:.7rem;line-height:1.7;text-align:center}
.foot-editorial{max-width:74ch;margin:0 auto;opacity:.85}
.foot-links{margin-top:.7rem}
.foot-links a{font-weight:600}

/* ---- responsive: tables collapse to labeled cards ---- */
@media(max-width:860px){
  .masthead{padding:1rem 1rem .8rem}
  .brand-sub{width:100%;margin-left:0;margin-top:.2rem}
  main{padding:0 1rem}
  .hero-title{font-size:1.5rem}
  .whistle-cols{grid-template-columns:1fr}
  .lb-paired.is-active,.lb-triple.is-active{grid-template-columns:1fr}
  .lb-list-wide{columns:1}
  .history-cols{grid-template-columns:1fr}
  .compare-pickers,.compare-cols{grid-template-columns:1fr}
  .table-wrap{border:0;background:none;overflow:visible}
  .data-table,.data-table tbody,.data-table tr{display:block;width:100%}
  .data-table thead{position:absolute;left:-9999px}
  .data-table tr{background:var(--surface);border:1px solid var(--border);border-radius:12px;
    margin-bottom:.6rem;padding:.3rem .2rem}
  .data-table td{display:flex;justify-content:space-between;gap:1rem;text-align:right;
    white-space:normal;border:0;padding:.4rem .8rem}
  .data-table td::before{content:attr(data-label);font-family:var(--mono);color:var(--text-secondary);
    font-weight:600;text-transform:uppercase;font-size:.6rem;letter-spacing:.04em;text-align:left;flex:none}
  .data-table td:first-child{text-align:right}
  .data-table td[data-label]:only-child::before{content:""}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

JS = r"""(function(){
  "use strict";
  // --- referee search (filters directory rows by name) ---
  var search=document.getElementById("ref-search");
  if(search){
    var table=document.getElementById("ref-directory");
    var empty=document.getElementById("search-empty");
    var rows=[].slice.call(table.querySelectorAll("tbody .ref-row"));
    search.addEventListener("input",function(){
      var q=search.value.trim().toLowerCase();
      var shown=0;
      rows.forEach(function(r){
        var hit=!q||r.getAttribute("data-name").indexOf(q)!==-1;
        r.style.display=hit?"":"none";
        if(hit)shown++;
      });
      if(empty)empty.hidden=shown!==0;
    });
  }
  // --- navigate-search (top/bottom of ref pages, bottom of index) ---
  var _idxCache={};
  function loadIndex(url){
    if(!_idxCache[url]){
      _idxCache[url]=fetch(url).then(function(r){return r.json();}).catch(function(){return [];});
    }
    return _idxCache[url];
  }
  var TYPE_DIR={ref:"referee",team:"team",player:"player"};
  var TYPE_LABEL={ref:"Ref",team:"Team",player:"Player"};
  function escHtml(s){return String(s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
  [].slice.call(document.querySelectorAll(".refsearch-wrap")).forEach(function(wrap){
    var input=wrap.querySelector(".refsearch");
    var out=wrap.querySelector(".refsearch-results");
    var root=wrap.getAttribute("data-root")||"";
    var url=wrap.getAttribute("data-json");
    var typeFilter=wrap.getAttribute("data-type-filter");   // e.g. "ref" -- comparator boxes
    var compareSlot=wrap.getAttribute("data-compare");      // "a" or "b" -- comparator boxes
    var idx=null, active=-1;
    function href(e){
      if(compareSlot){
        var params=new URLSearchParams(window.location.search);
        params.set(compareSlot,e.s);
        return "?"+params.toString();
      }
      return root+TYPE_DIR[e.t]+"/"+e.s+"/index.html";
    }
    function close(){out.hidden=true;out.innerHTML="";active=-1;}
    function render(q){
      if(!q){close();return;}
      var pool=typeFilter?(idx||[]).filter(function(e){return e.t===typeFilter;}):(idx||[]);
      var hits=pool.filter(function(e){return e.n.toLowerCase().indexOf(q)!==-1;});
      hits.sort(function(a,b){
        var ap=a.n.toLowerCase().indexOf(q)===0?0:1, bp=b.n.toLowerCase().indexOf(q)===0?0:1;
        if(ap!==bp)return ap-bp;
        return a.n.length-b.n.length;
      });
      hits=hits.slice(0,12);
      if(!hits.length){
        out.innerHTML='<div class="rs-empty">No '+(typeFilter?"referee":"referee, team or player")+' matches.</div>';
        out.hidden=false;active=-1;return;
      }
      out.innerHTML=hits.map(function(e){
        return '<a class="rs-item" href="'+href(e)+'">'+
          '<span class="rs-badge rs-'+e.t+'">'+TYPE_LABEL[e.t]+'</span>'+
          '<span class="rs-name">'+escHtml(e.n)+'</span>'+
          '<span class="rs-meta">'+escHtml(e.u||"")+'</span></a>';
      }).join("");
      out.hidden=false;active=-1;
    }
    function items(){return [].slice.call(out.querySelectorAll(".rs-item"));}
    function setActive(i){var el=items();el.forEach(function(x){x.classList.remove("active");});
      if(i>=0&&i<el.length){active=i;el[i].classList.add("active");el[i].scrollIntoView({block:"nearest"});}}
    input.addEventListener("input",function(){
      var q=input.value.trim().toLowerCase();
      loadIndex(url).then(function(data){idx=data;if(input.value.trim().toLowerCase()===q)render(q);});
    });
    input.addEventListener("keydown",function(e){
      var el=items();
      if(e.key==="ArrowDown"){e.preventDefault();setActive(Math.min(active+1,el.length-1));}
      else if(e.key==="ArrowUp"){e.preventDefault();setActive(Math.max(active-1,0));}
      else if(e.key==="Enter"){var t=active>=0?el[active]:el[0];if(t){e.preventDefault();window.location.href=t.getAttribute("href");}}
      else if(e.key==="Escape"){close();}
    });
    document.addEventListener("click",function(e){if(!wrap.contains(e.target))close();});
  });
  // --- sortable tables ---
  // Exposed on window so content injected after page load (the /matchup/
  // page's client-fetched tables -- everything else on the site is
  // server-rendered before this runs at DOMContentLoaded) can wire up the
  // same sort behavior on demand instead of duplicating it.
  function cellVal(td){
    var s=td.getAttribute("data-sort");
    if(s!==null){var n=parseFloat(s);return isNaN(n)?s.toLowerCase():n;}
    return td.textContent.trim().toLowerCase();
  }
  function initSortableTables(root){
    [].slice.call((root||document).querySelectorAll(".sortable-table")).forEach(function(table){
      if(table.__sortInit)return;
      table.__sortInit=true;
      var ths=[].slice.call(table.querySelectorAll("th.sortable"));
      ths.forEach(function(th,col){
        th.addEventListener("click",function(){
          var tbody=table.tBodies[0];
          var rows=[].slice.call(tbody.querySelectorAll("tr"));
          var asc=!th.classList.contains("sort-asc");
          ths.forEach(function(o){o.classList.remove("sort-asc","sort-desc");});
          th.classList.add(asc?"sort-asc":"sort-desc");
          rows.sort(function(a,b){
            var x=cellVal(a.cells[col]),y=cellVal(b.cells[col]);
            if(x<y)return asc?-1:1;
            if(x>y)return asc?1:-1;
            return 0;
          });
          rows.forEach(function(r){tbody.appendChild(r);});
        });
      });
    });
  }
  window.initSortableTables=initSortableTables;
  initSortableTables(document);
  // --- leaderboard tabs ---
  // Scoped PER .lb-tabs container (its .lb-panels sibling), not globally --
  // a page can carry more than one independent tab group (e.g. the index's
  // Career-leaders tabs AND its separate Era-leaders tabs), and a single
  // shared tabs/panels array would cross-wire them: clicking a tab in one
  // group would deactivate every tab in the OTHER group too, with no
  // matching panel id to reactivate, leaving it blank.
  [].slice.call(document.querySelectorAll(".lb-tabs")).forEach(function(tabsEl){
    var tabs=[].slice.call(tabsEl.querySelectorAll(".lb-tab"));
    var panelsEl=tabsEl.nextElementSibling;
    var panels=panelsEl?[].slice.call(panelsEl.querySelectorAll(".lb-panel")):[];
    tabs.forEach(function(tab){
      tab.addEventListener("click",function(){
        var id=tab.getAttribute("data-tab");
        tabs.forEach(function(t){var on=t===tab;t.classList.toggle("is-active",on);
          t.setAttribute("aria-selected",on?"true":"false");});
        panels.forEach(function(p){p.classList.toggle("is-active",p.getAttribute("data-panel")===id);});
      });
    });
  });
  // --- dashboard: spotlight of the day + on this date (index only) ---
  // Rotation is deterministic client-side: day-of-year modulo the spotlight
  // array (ordered by slug at build time for a stable rotation). The data
  // itself ships inline in the page (real content in the HTML); only the
  // day-dependent SELECTION runs in JS, since a statically-built site can't
  // otherwise know the viewer's "today".
  var WHISTLE_LABELS={avg_total_points:"Combined points",avg_total_fta:"Combined free-throw attempts",
    avg_total_pf:"Combined personal fouls",home_win_pct:"Home team win rate",ot_rate:"Games to overtime"};
  var WHISTLE_ISPCT={home_win_pct:1,ot_rate:1};
  function fmtWhistle(key,v){return WHISTLE_ISPCT[key]?(v*100).toFixed(1)+"%":v.toFixed(1);}
  var dashData=document.getElementById("dashboard-rotation-data");
  if(dashData){
    try{
      var dash=JSON.parse(dashData.textContent);
      var spotlight=dash.spotlight||[];
      var dateIndex=dash.date_index||{};
      var now=new Date();
      var startOfYear=new Date(now.getFullYear(),0,0);
      var doy=Math.floor((now-startOfYear)/86400000);

      var spotCard=document.getElementById("spotlight-card");
      if(spotCard&&spotlight.length){
        var pick=spotlight[doy%spotlight.length];
        var sigHtml;
        if(pick.signature){
          var sig=pick.signature, dir=sig.pctile>=50?"higher":"lower",
            pctShow=(sig.pctile>=50?sig.pctile:(100-sig.pctile)).toFixed(0),
            label=WHISTLE_LABELS[sig.key]||sig.key, val=fmtWhistle(sig.key,sig.value);
          sigHtml='<p class="spotlight-sig">'+escHtml(label)+": "+escHtml(val)+" — "+dir+
            " than "+pctShow+"% of qualifying officials (n="+sig.n+").</p>";
        }else{
          sigHtml='<p class="spotlight-sig">'+pick.seasons_active+" seasons officiating, "+
            pick.games_total+" career games.</p>";
        }
        var badge=pick.active?' <span class="badge badge-active">Active</span>':"";
        spotCard.innerHTML='<a class="spotlight-name" href="referee/'+pick.slug+'/index.html">'+
          escHtml(pick.name)+'</a>'+badge+
          '<p class="spotlight-meta">'+pick.games_total+' games &middot; '+pick.first_season+'–'+pick.last_season+'</p>'+sigHtml;
      }

      var mm=("0"+(now.getMonth()+1)).slice(-2), dd=("0"+now.getDate()).slice(-2);
      var entry=dateIndex[mm+"-"+dd];
      var onDate=document.getElementById("ondate-card");
      if(onDate&&entry){
        var playerBit=entry.player_slug
          ?'<a href="player/'+entry.player_slug+'/index.html">'+escHtml(entry.player_name)+'</a>'
          :escHtml(entry.player_name);
        var fallbackNote=entry.month_day===(mm+"-"+dd)?"":
          ' <span class="caption">(nearest date with games on record; from '+entry.date+')</span>';
        onDate.innerHTML='<span class="history-pts">'+entry.pts+'</span> '+playerBit+' '+
          escHtml(entry.team_abbr)+' <span class="vs">vs</span> '+escHtml(entry.opp_abbr)+
          ' — '+escHtml(entry.date)+fallbackNote;
      }
    }catch(e){/* dashboard rotation is decorative -- fail silently */}
  }
  // --- Tonight's Crews (September pipeline; absent all season until then) ---
  // Expected data/tonights-crews.json schema once the pipeline ships:
  //   {date, games:[{away, home, tipoff_et, crew:[{name, slug}], crew_note}]}
  // Absent, unparseable, or dated anything other than today/yesterday (US
  // Eastern time, since that's the NBA's scheduling clock) -- render NOTHING,
  // not even a placeholder.
  (function(){
    var slot=document.getElementById("tonights-crews");
    if(!slot)return;
    function usEasternISO(offsetDays){
      var d=new Date(Date.now()+offsetDays*86400000);
      var parts=new Intl.DateTimeFormat("en-CA",{timeZone:"America/New_York",
        year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(d);
      var o={};parts.forEach(function(p){o[p.type]=p.value;});
      return o.year+"-"+o.month+"-"+o.day;
    }
    fetch("data/tonights-crews.json").then(function(r){
      if(!r.ok)throw new Error("absent");
      return r.json();
    }).then(function(data){
      var valid=data&&data.date&&(data.date===usEasternISO(0)||data.date===usEasternISO(-1));
      if(!valid||!Array.isArray(data.games)||!data.games.length)return;
      var body=slot.querySelector("#tonights-crews-body");
      body.innerHTML=data.games.map(function(g){
        var crew=(g.crew||[]).map(function(c){
          return '<a href="referee/'+c.slug+'/index.html">'+escHtml(c.name)+'</a>';
        }).join(", ");
        var note=g.crew_note?' <span class="caption">'+escHtml(g.crew_note)+'</span>':"";
        return '<div class="crew-game"><span class="crew-matchup">'+escHtml(g.away)+' @ '+escHtml(g.home)+'</span>'+
          '<span class="crew-tip">'+escHtml(g.tipoff_et||"")+'</span>'+
          '<span class="crew-names">'+crew+'</span>'+note+'</div>';
      }).join("");
      slot.hidden=false;
    }).catch(function(){/* absent or unparseable -- render nothing, by design */});
  })();
  // --- comparator (/compare/) -- reads existing data/referees/{slug}.json
  // client-side, keyed off the ?a=/?b= query string so any pair is shareable
  // without pre-rendering the ~159*158/2 possible combinations. ---
  (function(){
    var colA=document.getElementById("compare-col-a"), colB=document.getElementById("compare-col-b");
    if(!colA||!colB)return;
    var WHISTLE_ALL_LABELS={avg_total_points:"Combined points",avg_total_fta:"Combined free-throw attempts",
      avg_total_pf:"Combined personal fouls",avg_abs_margin:"Avg. margin of victory",
      home_win_pct:"Home team win rate",ot_rate:"Games to overtime"};
    var WHISTLE_ALL_ISPCT={home_win_pct:1,ot_rate:1};
    function fmtWhistleAll(key,v){return WHISTLE_ALL_ISPCT[key]?(v*100).toFixed(1)+"%":v.toFixed(1);}
    function fmtDiffAll(key,v){
      if(v==null)return "—";
      var sign=v>=0?"+":"";
      return WHISTLE_ALL_ISPCT[key]?sign+(v*100).toFixed(1)+"%":sign+v.toFixed(1);
    }
    function ordinalAll(n){
      if(n==null)return "—";
      n=Math.trunc(n);
      var m10=n%10,m100=n%100;
      var suf=(m10===1&&m100!==11)?"st":(m10===2&&m100!==12)?"nd":(m10===3&&m100!==13)?"rd":"th";
      return n+suf;
    }
    function diffRankLabelAll(rank,total,diff){
      if(!total)return "ranking not available";
      if(rank==null)return "not enough games to rank";
      if(total<=1)return "only qualifying official";
      if(diff!=null&&diff<0)return ordinalAll(total-rank+1)+" lowest of "+total;
      return ordinalAll(rank)+" highest of "+total;
    }
    function intensityClass(pctile){
      if(pctile==null)return"";
      var d=Math.abs(pctile-50),lvl=d>=40?4:d>=30?3:d>=20?2:d>=10?1:0;
      return "wm-i"+lvl;
    }
    function whistleColHtml(kindLabel,w){
      if(!w||!w.n)return "";
      var keys=["avg_total_points","avg_total_fta","avg_total_pf","avg_abs_margin","home_win_pct","ot_rate"];
      var nMap={avg_total_points:w.n,avg_total_fta:w.n_boxscore,avg_total_pf:w.n_boxscore,
        avg_abs_margin:w.n,home_win_pct:w.n,ot_rate:w.n_boxscore};
      var exp=w.expected||{}, dif=w.differential||{};
      var cells=keys.map(function(k){
        var v=w[k],cls=intensityClass(w[k+"_pctile"]),vs=(v==null)?"—":fmtWhistleAll(k,v);
        var lg=exp[k],lgs=(lg==null)?"—":fmtWhistleAll(k,lg);
        var d=(dif[k]==null)?null:dif[k],ds=fmtDiffAll(k,d);
        var rankTxt=diffRankLabelAll(w[k+"_rank"],w[k+"_qualifying"],d);
        return '<div class="wm '+cls+'"><div class="wm-val">'+vs+' <span class="wm-lg">lg '+lgs+
          '</span> <span class="wm-diff">'+ds+'</span></div>'+
          '<div class="wm-label">'+WHISTLE_ALL_LABELS[k]+'</div>'+
          '<div class="wm-rank">'+rankTxt+'</div>'+
          '<div class="wm-n">n = '+(nMap[k]||0)+'</div></div>';
      }).join("");
      return '<div class="whistle-col"><h3 class="whistle-kind">'+kindLabel+
        ' <span class="whistle-n">'+(w.n||0)+' games</span></h3><div class="whistle-grid">'+cells+'</div></div>';
    }
    function teamExtremes(records){
      if(!records||!records.length)return '<p class="empty-note">No team records on file.</p>';
      var byGames=records.slice().sort(function(a,b){return b.games-a.games;})[0];
      var qualifying=records.filter(function(r){return r.games>=10&&r.win_pct!=null;});
      var byWin=qualifying.length?qualifying.slice().sort(function(a,b){return b.win_pct-a.win_pct;})[0]:null;
      var out='<p class="compare-line">Most games: '+escHtml(byGames.team_abbr)+' ('+byGames.games+' games)</p>';
      if(byWin)out+='<p class="compare-line">Best record: '+escHtml(byWin.team_abbr)+' ('+
        (byWin.win_pct*100).toFixed(1)+'%, '+byWin.games+' games)</p>';
      return out;
    }
    function renderCompareCol(container,doc){
      var s=doc.summary;
      var badge=s.active?' <span class="badge badge-active">Active</span>':"";
      container.innerHTML=
        '<a class="spotlight-name" href="../referee/'+s.slug+'/index.html">'+escHtml(s.name)+'</a>'+badge+
        '<p class="spotlight-meta">'+s.games_total+' games &middot; '+s.first_season+'–'+s.last_season+
        ' &middot; RS '+s.games_rs+' &middot; PO '+s.games_po+' &middot; Finals '+s.finals_games+
        ' &middot; G7s '+s.game7s+'</p>'+
        '<div class="whistle-cols">'+whistleColHtml("Regular season",doc.whistle_profile.rs)+
        whistleColHtml("Playoffs",doc.whistle_profile.po)+'</div>'+
        '<h3 class="lb-subhead">Team records</h3>'+teamExtremes(doc.team_records);
    }
    var params=new URLSearchParams(window.location.search);
    var aSlug=params.get("a"), bSlug=params.get("b");
    var promptEl=document.getElementById("compare-prompt");
    if(promptEl)promptEl.hidden=!!(aSlug||bSlug);
    function loadCol(container,slug){
      if(!slug){container.innerHTML='<p class="empty-note">Select a referee above.</p>';return;}
      fetch("../data/referees/"+slug+".json").then(function(r){
        if(!r.ok)throw new Error("not found");
        return r.json();
      }).then(function(doc){renderCompareCol(container,doc);})
        .catch(function(){container.innerHTML='<p class="empty-note">Referee not found.</p>';});
    }
    loadCol(colA,aSlug);
    loadCol(colB,bSlug);
  })();
  // --- /matchup/ team x referee lookup (docs/MATCHUP_SPEC.md) -- reads
  // data/matchups/{official_id}.json (career + per-season, RS/PO) and
  // data/referee_games/{official_id}.json (the auditable log, filtered
  // client-side to the selected team) keyed off ?team=&ref=. ---
  (function(){
    var resultEl=document.getElementById("matchup-result");
    if(!resultEl)return;
    var MU_SUPPRESS=3, MU_FLAG=10;
    var promptEl=document.getElementById("matchup-prompt");
    var params=new URLSearchParams(window.location.search);
    var teamSlug=(params.get("team")||"").toLowerCase();
    var refSlug=params.get("ref");
    // Prefilled from a referee or team page: the picker itself must show
    // which one is already chosen, not just silently use it -- a reader
    // arriving via a "Team matchups" link should see their referee's name
    // sitting in the box, not two blank pickers.
    var teamInput=document.querySelector('.refsearch-wrap[data-compare="team"] .refsearch');
    var refInput=document.querySelector('.refsearch-wrap[data-compare="ref"] .refsearch');

    function fmtPct(v){return (v*100).toFixed(1)+"%";}
    function fmtDec(v){return v.toFixed(1);}
    function fmtSigned(v){return (v>=0?"+":"")+v.toFixed(1);}

    // One stat cell in the whistle-profile card shell (.wm/.wm-val/.wm-label/
    // .wm-n, same markup referee pages already use). games is what gates
    // suppression for every stat; n is the sample actually backing THIS
    // number (n_box for FTA/PF, since box scores can be missing even when
    // games themselves are on record) -- the two can differ, and the
    // suppressed-state message says which one is short.
    function wmCell(label,val,fmt,games,n){
      var body;
      if(val==null){
        var reason=games<MU_SUPPRESS
          ?("too few games (n="+games+", min "+MU_SUPPRESS+")")
          :("box score unavailable for these games (n="+n+")");
        body='<div class="wm-val wm-suppressed">'+escHtml(reason)+'</div>';
      }else{
        var flag=n<MU_FLAG?' <span class="mu-flag" title="Fewer than '+MU_FLAG+
          ' games back this number (n='+n+') -- shown, but a small sample.">small sample</span>':"";
        body='<div class="wm-val">'+fmt(val)+flag+'</div>';
      }
      return '<div class="wm">'+body+'<div class="wm-label">'+escHtml(label)+'</div>'+
        '<div class="wm-n">n = '+n+'</div></div>';
    }
    function matchupBlock(kindLabel,b){
      if(!b||!b.games)return "";
      var winTxt=b.win_pct!=null?fmtPct(b.win_pct):
        (b.games<MU_SUPPRESS?("too few games (n="+b.games+")"):"—");
      var header=b.wins+"-"+b.losses+" ("+winTxt+")"+
        " &middot; Home "+b.home_games+" ("+b.home_wins+"-"+(b.home_games-b.home_wins)+")"+
        " &middot; Road "+b.away_games+" ("+b.away_wins+"-"+(b.away_games-b.away_wins)+")";
      var cells=[
        ["Avg. margin",b.avg_margin,fmtSigned,b.games,b.games],
        ["Points for",b.avg_pts_for,fmtDec,b.games,b.games],
        ["Points against",b.avg_pts_against,fmtDec,b.games,b.games],
        ["Team FTA",b.avg_team_fta,fmtDec,b.games,b.n_box],
        ["Opponent FTA",b.avg_opp_fta,fmtDec,b.games,b.n_box],
        ["Team fouls",b.avg_team_pf,fmtDec,b.games,b.n_box],
        ["Opponent fouls",b.avg_opp_pf,fmtDec,b.games,b.n_box]
      ].map(function(c){return wmCell(c[0],c[1],c[2],c[3],c[4]);}).join("");
      return '<div class="whistle-col"><h3 class="whistle-kind">'+escHtml(kindLabel)+
        ' <span class="whistle-n">'+b.games+' games</span></h3>'+
        '<p class="mu-record-line">'+header+'</p>'+
        '<div class="whistle-grid">'+cells+'</div></div>';
    }
    // Table-cell twin of wmCell for the season-by-season table -- same
    // suppress/flag policy, td markup instead of a card.
    function muTd(label,val,fmt,games,n){
      if(val==null){
        var reason=games<MU_SUPPRESS
          ?("too few games (n="+games+", min "+MU_SUPPRESS+")")
          :("box score unavailable (n="+n+")");
        return '<td data-label="'+escHtml(label)+'" data-sort="-999999">'+
          '<span class="mu-td-suppressed" title="'+escHtml(reason)+'">'+escHtml(reason)+'</span></td>';
      }
      var flag=n<MU_FLAG?' <span class="mu-flag" title="Fewer than '+MU_FLAG+
        ' games back this number (n='+n+')">small</span>':"";
      return '<td data-label="'+escHtml(label)+'" data-sort="'+val+'">'+fmt(val)+flag+'</td>';
    }
    function seasonRow(seasonLabel,sortKey,kindLabel,b){
      if(!b||!b.games)return "";
      return "<tr>"+
        '<td data-label="Season" data-sort="'+escHtml(sortKey)+'">'+escHtml(seasonLabel)+'</td>'+
        '<td data-label="Type">'+kindLabel+'</td>'+
        '<td data-label="Games" data-sort="'+b.games+'">'+b.games+'</td>'+
        '<td data-label="W-L">'+b.wins+'-'+b.losses+'</td>'+
        muTd("Win%",b.win_pct,fmtPct,b.games,b.games)+
        muTd("Margin",b.avg_margin,fmtSigned,b.games,b.games)+
        muTd("Team FTA",b.avg_team_fta,fmtDec,b.games,b.n_box)+
        muTd("Opp FTA",b.avg_opp_fta,fmtDec,b.games,b.n_box)+
        muTd("Team PF",b.avg_team_pf,fmtDec,b.games,b.n_box)+
        muTd("Opp PF",b.avg_opp_pf,fmtDec,b.games,b.n_box)+
        "</tr>";
    }
    function seasonTable(seasons,career){
      var heads=["Season","Type","Games","W-L","Win%","Margin","Team FTA","Opp FTA","Team PF","Opp PF"];
      var ths=heads.map(function(h,idx){
        return '<th class="sortable '+(idx<2?"col-text":"col-num")+'" data-type="'+
          (idx<2?"text":"num")+'" scope="col">'+h+'</th>';
      }).join("");
      var rows="";
      seasons.forEach(function(s){
        rows+=seasonRow(s.season,s.season,"RS",s.rs);
        rows+=seasonRow(s.season,s.season,"PO",s.po);
      });
      rows+=seasonRow("Career","9999","RS",career.rs);
      rows+=seasonRow("Career","9999","PO",career.po);
      return '<table class="data-table sortable-table"><thead><tr>'+ths+
        '</tr></thead><tbody>'+rows+'</tbody></table>';
    }
    function filterGameLog(doc,tricode){
      var out=[];
      (doc.by_season||[]).forEach(function(season){
        (season.games||[]).forEach(function(g){
          if(g.kind==="PI")return;
          if(g.home_canon===tricode||g.away_canon===tricode)out.push(g);
        });
      });
      out.sort(function(a,b){return a.date<b.date?1:(a.date>b.date?-1:0);});
      return out;
    }
    function gameLogTable(games,tricode){
      if(!games.length)return '<p class="empty-note">No games on record for this pairing.</p>';
      var heads=["Date","Matchup","Score","Result","Round","Crew"];
      var ths=heads.map(function(h){
        return '<th class="sortable col-text" data-type="text" scope="col">'+h+'</th>';
      }).join("");
      var rows=games.map(function(g){
        var isHome=g.home_canon===tricode;
        var teamPts=isHome?g.home_pts:g.away_pts, oppPts=isHome?g.away_pts:g.home_pts;
        var result="—", resultClass="";
        if(teamPts!=null&&oppPts!=null){
          result=(teamPts>oppPts?"W ":"L ")+teamPts+"-"+oppPts;
          resultClass=teamPts>oppPts?"mu-win":"mu-loss";
        }
        var crew=(g.co_officials||[]).map(function(c){
          return '<a href="../referee/'+c.slug+'/index.html">'+escHtml(c.name)+'</a>';
        }).join(" &middot; ")||"—";
        // Away-home order, matching the Matchup column's "away @ home" reading --
        // a separate home-away order here would read backwards against it.
        var score=(g.home_pts!=null&&g.away_pts!=null)?(g.away_pts+"-"+g.home_pts):"—";
        return "<tr>"+
          '<td data-label="Date" data-sort="'+esc0(g.date)+'">'+escHtml(g.date)+'</td>'+
          '<td data-label="Matchup">'+escHtml(g.away_team_abbr)+' <span class="vs">@</span> '+
            escHtml(g.home_team_abbr)+'</td>'+
          '<td data-label="Score">'+score+'</td>'+
          '<td data-label="Result"><span class="'+resultClass+'">'+result+'</span></td>'+
          '<td data-label="Round">'+escHtml(g.round_label||"—")+'</td>'+
          '<td data-label="Crew" class="crew">'+crew+'</td>'+
        "</tr>";
      }).join("");
      return '<table class="data-table sortable-table"><thead><tr>'+ths+
        '</tr></thead><tbody>'+rows+'</tbody></table>';
    }
    function esc0(s){return String(s).replace(/"/g,"&quot;");}

    function renderMatchup(teamDoc,refDoc,matchupDoc,gameLogDoc){
      var tricode=teamDoc.summary.tricode, teamName=teamDoc.summary.name;
      var refName=refDoc.summary.name, rSlug=refDoc.summary.slug;
      var heading='<h3 class="matchup-heading">'+escHtml(teamName)+' &times; '+
        '<a href="../referee/'+rSlug+'/index.html">'+escHtml(refName)+'</a></h3>';
      var teamData=(matchupDoc.teams||{})[tricode];
      if(!teamData||(!teamData.career.rs&&!teamData.career.po)){
        resultEl.innerHTML=heading+
          '<p class="empty-note">No games on record for '+escHtml(refName)+
          ' officiating '+escHtml(teamName)+'.</p>';
        return;
      }
      var career=teamData.career;
      var summaryHtml=matchupBlock("Regular season",career.rs)+matchupBlock("Playoffs",career.po);
      var filtered=filterGameLog(gameLogDoc,tricode);
      resultEl.innerHTML=heading+
        '<div class="whistle-cols">'+summaryHtml+'</div>'+
        '<h3 class="lb-subhead" style="margin-top:1.6rem">Season by season</h3>'+
        '<div class="table-wrap">'+seasonTable(teamData.seasons,career)+'</div>'+
        '<h3 class="lb-subhead" style="margin-top:1.6rem">Game log ('+filtered.length+' games)</h3>'+
        '<p class="caption mu-section-caption">Every game on record for this pairing -- the '+
        'numbers above are computed from exactly these rows.</p>'+
        '<div class="table-wrap">'+gameLogTable(filtered,tricode)+'</div>';
      if(window.initSortableTables)window.initSortableTables(resultEl);
    }

    var teamPromise=teamSlug
      ?fetch("../data/teams/"+teamSlug+".json").then(function(r){
        if(!r.ok)throw new Error("team not found");return r.json();
      }).catch(function(){return null;})
      :Promise.resolve(null);
    var refPromise=refSlug
      ?fetch("../data/referees/"+refSlug+".json").then(function(r){
        if(!r.ok)throw new Error("ref not found");return r.json();
      }).catch(function(){return null;})
      :Promise.resolve(null);

    Promise.all([teamPromise,refPromise]).then(function(docs){
      var teamDoc=docs[0], refDoc=docs[1];
      if(teamDoc&&teamInput)teamInput.value=teamDoc.summary.name;
      if(refDoc&&refInput)refInput.value=refDoc.summary.name;
      if(promptEl)promptEl.hidden=!!(teamDoc&&refDoc);
      if(!teamDoc||!refDoc){
        resultEl.innerHTML=(teamSlug&&!teamDoc)||(refSlug&&!refDoc)
          ?'<p class="empty-note">Could not find that team or official -- check the URL.</p>':"";
        return;
      }
      var officialId=refDoc.summary.official_id;
      resultEl.innerHTML='<p class="empty-note">Loading…</p>';
      Promise.all([
        fetch("../data/matchups/"+officialId+".json").then(function(r){
          if(!r.ok)throw new Error("matchup data not found");
          return r.json();
        }),
        fetch("../data/referee_games/"+officialId+".json").then(function(r){
          if(!r.ok)throw new Error("game log not found");
          return r.json();
        })
      ]).then(function(results){
        renderMatchup(teamDoc,refDoc,results[0],results[1]);
      }).catch(function(){
        resultEl.innerHTML='<p class="empty-note">Could not load data for this pairing.</p>';
      });
    });
  })();
})();
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _render_dir(out_root, docs_by_slug, render_fn):
    """Write one index.html per slug under out_root/{slug}/, removing any stale
    directories whose slug is no longer produced."""
    os.makedirs(out_root, exist_ok=True)
    keep = set(docs_by_slug)
    for entry in os.listdir(out_root):
        d = os.path.join(out_root, entry)
        if os.path.isdir(d) and entry not in keep:
            f = os.path.join(d, "index.html")
            if os.path.exists(f):
                os.remove(f)
            if not os.listdir(d):
                os.rmdir(d)
    for slug, doc in docs_by_slug.items():
        d = os.path.join(out_root, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_fn(doc))
    return len(docs_by_slug)


def build_search_index(refs, team_docs, player_docs):
    """Slim global index for the navigate-search: {n:name, s:slug, t:type,
    u:one-line subtitle}. type in {ref, team, player}."""
    def sub(games, first, last):
        span = career_span(first, last) if first else "—"
        return "%s g · %s" % (i(games), span)
    out = []
    for r in refs:
        out.append({"n": r["name"], "s": r["slug"], "t": "ref",
                    "u": sub(r["games_total"], r["first_season"], r["last_season"])})
    for slug in sorted(team_docs):
        s = team_docs[slug]["summary"]
        out.append({"n": s["name"], "s": s["slug"], "t": "team",
                    "u": sub(s["games_total"], s["first_season"], s["last_season"])})
    for slug in sorted(player_docs):
        s = player_docs[slug]["summary"]
        out.append({"n": s["name"], "s": s["slug"], "t": "player",
                    "u": sub(s["games_total"], s["first_season"], s["last_season"])})
    return out


def main():
    refs = json.load(open(os.path.join(DATA, "referees.json"), encoding="utf-8"))
    lb = json.load(open(os.path.join(DATA, "leaderboards.json"), encoding="utf-8"))
    dashboard = json.load(open(os.path.join(DATA, "dashboard.json"), encoding="utf-8"))
    team_index = json.load(open(os.path.join(DATA, "teams.json"), encoding="utf-8"))
    player_index = json.load(open(os.path.join(DATA, "players.json"), encoding="utf-8"))

    # populate the cross-link existence sets BEFORE rendering anything, so ref
    # pages linkify only teams/players that actually have a page.
    TEAM_EXISTS.update(t["tricode"] for t in team_index)
    PLAYER_EXISTS.update(p["slug"] for p in player_index)

    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)
    with open(os.path.join(ASSETS, "app.js"), "w", encoding="utf-8") as f:
        f.write(JS)

    # index
    with open(os.path.join(REPO, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(refs, lb, dashboard))

    # data-sources page (attribution moved out of the footer)
    os.makedirs(os.path.join(REPO, "sources"), exist_ok=True)
    with open(os.path.join(REPO, "sources", "index.html"), "w", encoding="utf-8") as f:
        f.write(render_sources())

    # comparator page (static shell; content loads client-side)
    os.makedirs(os.path.join(REPO, "compare"), exist_ok=True)
    with open(os.path.join(REPO, "compare", "index.html"), "w", encoding="utf-8") as f:
        f.write(render_compare())

    # team x referee matchup lookup (static shell; content loads client-side)
    os.makedirs(os.path.join(REPO, "matchup"), exist_ok=True)
    with open(os.path.join(REPO, "matchup", "index.html"), "w", encoding="utf-8") as f:
        f.write(render_matchup())

    # referee pages
    docs = [json.load(open(p, encoding="utf-8"))
            for p in sorted(glob.glob(os.path.join(DATA, "referees", "*.json")))]
    current_slugs = {d["summary"]["slug"] for d in docs}

    # Remove pages for referees that no longer exist (e.g. after an identity
    # merge dropped a slug) so stale pages don't linger, mirroring build.py's
    # output-dir hygiene. Each slug dir can now also hold a games/ subdirectory
    # (Tier C game log), which must be cleared FIRST -- otherwise the dir is
    # never empty and the old "if not os.listdir(d): rmdir" check silently
    # leaves an orphaned referee/{slug}/ directory behind.
    removed = 0
    if os.path.isdir(REFEREE_DIR):
        for entry in os.listdir(REFEREE_DIR):
            d = os.path.join(REFEREE_DIR, entry)
            if os.path.isdir(d) and entry not in current_slugs:
                page_file = os.path.join(d, "index.html")
                if os.path.exists(page_file):
                    os.remove(page_file)
                games_dir = os.path.join(d, "games")
                games_file = os.path.join(games_dir, "index.html")
                if os.path.exists(games_file):
                    os.remove(games_file)
                if os.path.isdir(games_dir) and not os.listdir(games_dir):
                    os.rmdir(games_dir)
                if not os.listdir(d):
                    os.rmdir(d)
                removed += 1

    n = 0
    n_game_logs = 0
    for doc in docs:
        slug = doc["summary"]["slug"]
        official_id = doc["summary"]["official_id"]
        out_dir = os.path.join(REFEREE_DIR, slug)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_ref(doc))
        n += 1

        # Tier C per-referee game log (docs/TIER_C_SPEC.md section 3) --
        # written from a separate data/referee_games/{official_id}.json so
        # the main referee JSON above doesn't balloon for the busiest refs.
        log_path = os.path.join(DATA, "referee_games", "%s.json" % official_id)
        if os.path.exists(log_path):
            log_doc = json.load(open(log_path, encoding="utf-8"))
            games_dir = os.path.join(out_dir, "games")
            os.makedirs(games_dir, exist_ok=True)
            with open(os.path.join(games_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(render_ref_games(log_doc))
            n_game_logs += 1

    # team pages
    team_docs = {t["slug"]: json.load(open(os.path.join(DATA, "teams", "%s.json" % t["slug"]),
                                           encoding="utf-8")) for t in team_index}
    n_teams = _render_dir(os.path.join(REPO, "team"), team_docs, render_team)

    # player pages
    player_docs = {p["slug"]: json.load(open(os.path.join(DATA, "players", "%s.json" % p["slug"]),
                                             encoding="utf-8")) for p in player_index}
    n_players = _render_dir(os.path.join(REPO, "player"), player_docs, render_player)

    # whistle-profile leaderboard pages (one per stat, RS + PO sections)
    whistle_lb = json.load(open(os.path.join(DATA, "whistle_leaderboards.json"), encoding="utf-8"))
    min_games = whistle_lb["_meta"]["min_games"]
    leaderboard_docs = {}
    for key, doc in whistle_lb.items():
        if key == "_meta":
            continue
        leaderboard_docs[doc["slug"]] = {"key": key, "label": doc["label"],
                                         "rs": doc["rs"], "po": doc["po"],
                                         "min_games": min_games}
    n_leaderboards = _render_dir(os.path.join(REPO, "leaderboard"), leaderboard_docs,
                                 render_whistle_leaderboard)

    # global search index (referees + teams + players) for the navigate-search
    search_index = build_search_index(refs, team_docs, player_docs)
    with open(os.path.join(DATA, "search-index.json"), "w", encoding="utf-8") as f:
        json.dump(search_index, f, ensure_ascii=False, separators=(",", ":"))

    # Tier C full-list pages (docs/TIER_C_SPEC.md section 3)
    swings_all = json.load(open(os.path.join(DATA, "swings_all.json"), encoding="utf-8"))
    tier_c_pages = [
        ("crews", render_crews(dashboard["crews"])),
        ("team-officials", render_team_officials(dashboard["team_officials"])),
        ("debuts", render_debuts(dashboard["debuts_farewells"])),
        ("eras", render_eras(dashboard["era_leaders"])),
        ("swings", render_swings(swings_all)),
    ]
    for slug, html in tier_c_pages:
        d = os.path.join(REPO, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)

    print("wrote index.html")
    print("wrote sources/index.html")
    print("wrote compare/index.html")
    print("wrote matchup/index.html")
    print("wrote assets/style.css, assets/app.js")
    if removed:
        print("removed %d stale referee page(s)" % removed)
    print("wrote %d referee pages" % n)
    print("wrote %d referee game-log pages" % n_game_logs)
    print("wrote %d team pages, %d player pages" % (n_teams, n_players))
    print("wrote %d whistle-leaderboard pages" % n_leaderboards)
    print("wrote %d Tier C pages: %s" % (len(tier_c_pages), ", ".join(s for s, _ in tier_c_pages)))
    print("sample URLs:")
    for u in ["referee/scott-foster/", "referee/scott-foster/games/", "team/bos/",
              "player/lebron-james/", "leaderboard/ot-rate/", "compare/", "matchup/",
              "crews/", "team-officials/", "debuts/", "eras/", "swings/"]:
        print("  %sindex.html" % u)


if __name__ == "__main__":
    main()
