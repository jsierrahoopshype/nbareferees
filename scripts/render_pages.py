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

# Seasons for which ESPN-scheme sourcing means playoff round / Game-7 labeling
# is unavailable (BUILD_SPEC section 5 / RENDER_SPEC section 2).
GAP_BLOCKS = [
    ({"2000-01", "2001-02", "2002-03"}, "the 2000-01 to 2002-03 seasons"),
    ({"2012-13"}, "the 2012-13 season"),
    ({"2023-24", "2024-25", "2025-26"}, "the 2023-24 season onward"),
]
GAP_SEASONS = set().union(*[b[0] for b in GAP_BLOCKS])
CURRENT_SEASON = "2025-26"

ATTRIBUTION = [
    ("Wyatt Walsh's NBA Database", "https://www.kaggle.com/datasets/wyattowalsh/basketball",
     "Kaggle, CC BY-SA 4.0"),
    ("ESPN's public API", "https://www.espn.com/nba/",
     "games, officials and player logs for 2000-01–02-03, 2012-13, and 2023-24 on"),
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


def career_span(first, last):
    """Career span as calendar years: '2015-16'..'2025-26' -> '2015-2026'
    (first-season start year to last-season end year). A single season such as
    '2021-22' renders '2021-2022'."""
    start = int(str(first)[:4])
    end = int(str(last)[:4]) + 1
    return "%d-%d" % (start, end)


def gap_phrase(per_season):
    """Human phrase naming which gap era(s) a referee's career overlaps."""
    seasons = set(per_season)
    parts = [label for block, label in GAP_BLOCKS if block & seasons]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


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
  <span class="brand-sub">NBA officiating record &middot; 2000-01 to {cur}</span>
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


def ref_search(depth, position):
    """Client-side navigate-search over data/search-index.json (referees, teams
    and players). data-root is the page's path back to the repo root, so the JS
    can build the correct depth for referee/, team/ and player/ targets."""
    root = "../" * depth
    return ('<div class="refsearch-wrap" data-json="{root}data/search-index.json" '
            'data-root="{root}" data-pos="{pos}">'
            '<input type="search" class="refsearch" autocomplete="off" '
            'placeholder="Search referees, teams, players…" '
            'aria-label="Search referees, teams and players">'
            '<div class="refsearch-results" role="listbox" hidden></div>'
            '</div>').format(root=root, pos=position)


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


def whistle_column(kind_label, w):
    n = w["n"]
    if not n:
        return ""
    nb = w["n_boxscore"]
    rows = [
        ("Combined points", dec(w["avg_total_points"]), n),
        ("Combined free-throw attempts", dec(w["avg_total_fta"]), nb),
        ("Combined personal fouls", dec(w["avg_total_pf"]), nb),
        ("Avg. margin of victory", dec(w["avg_abs_margin"]), n),
        ("Home team win rate", pct(w["home_win_pct"]), n),
        ("Games to overtime", pct(w["ot_rate"]) if w["ot_rate"] is not None else "—", nb),
    ]
    cells = "".join(
        '<div class="wm"><div class="wm-val">{v}</div>'
        '<div class="wm-label">{l}</div><div class="wm-n">n = {n}</div></div>'.format(
            v=v, l=esc(l), n=i(nn)) for l, v, nn in rows)
    return ('<div class="whistle-col"><h3 class="whistle-kind">{k} '
            '<span class="whistle-n">{n} games</span></h3>'
            '<div class="whistle-grid">{c}</div></div>').format(
        k=esc(kind_label), n=i(n), c=cells)


# Existence sets for cross-linking (populated in main). A name is linkified only
# when its target page exists; otherwise it renders as plain text (no dead links).
TEAM_EXISTS = set()      # tricodes (upper) with a /team/ page
PLAYER_EXISTS = set()    # player slugs with a /player/ page
# All table helpers run on depth-2 pages (referee/, team/, player/), so links
# from within them reach the repo root via "../../".
ROOT2 = "../../"


def team_cell(abbr):
    """Full franchise name with the tricode as a small secondary chip; the name
    links to the team page when one exists."""
    full = nba_tricodes.display_name(abbr)
    tag = '<span class="team-tag">%s</span>' % esc(abbr)
    if full == abbr:                      # unrecognized code: chip only, no dupe
        return '<span class="team-cell">%s</span>' % tag
    name = esc(full)
    if abbr in TEAM_EXISTS:
        name = '<a href="%steam/%s/index.html">%s</a>' % (ROOT2, esc(abbr.lower()), name)
    return '<span class="team-cell"><span class="team-name">%s</span>%s</span>' % (name, tag)


def player_link(name, slug):
    """Player name linked to its page when one exists, else plain text."""
    if slug and slug in PLAYER_EXISTS:
        return '<a href="%splayer/%s/index.html">%s</a>' % (ROOT2, esc(slug), esc(name))
    return esc(name)


def ref_link(name, slug):
    """Referee name linked back to the ref page (always exists)."""
    return '<a href="%sreferee/%s/index.html">%s</a>' % (ROOT2, esc(slug), esc(name))


def back_home(label="All referees"):
    return '<a class="backlink" href="%sindex.html">&larr; %s</a>' % (ROOT2, esc(label))


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
    return ('<section class="block"><div class="block-head">'
            '<span class="eyebrow"><span class="eyebrow-stripe" aria-hidden="true"></span>'
            '{num}</span><h2>{title}</h2>{extra}</div>{inner}</section>').format(
        num=esc(num), title=esc(title), extra=extra_head, inner=inner)


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
    <div class="ref-badges">{active}</div>
    <div class="chip-row">{chips}</div>
  </div>
</section>""".format(name=esc(name), active=active, chips="".join(chips))

    blocks = [back_home(), hero, ref_search(2, "top")]
    blocks.append(partners_card(doc.get("top_partners")))

    # whistle profile
    cols = whistle_column("Regular season", doc["whistle_profile"]["rs"]) + \
        whistle_column("Playoffs", doc["whistle_profile"]["po"])
    caption = ('<p class="caption">Averages describe the box score in games this '
               'official worked — combined totals for both teams. Each figure '
               'shows its sample size (n).</p>')
    blocks.append(section("01", "Whistle profile",
                          '<div class="whistle-cols">%s</div>' % cols, caption))

    # team records
    blocks.append(section("02", "Team records under %s" % name,
                          '<div class="table-wrap">%s</div>' % team_records_table(doc["team_records"]),
                          '<p class="caption">A team’s record in games this official worked. '
                          'Descriptive only. Tap a column to sort.</p>'))

    # player swings (only when present)
    if doc["player_swings"]:
        note = ('<p class="caption">Players with at least 15 games under {name}. '
                '“Swing” is the average difference between a player’s output in '
                'these games and that player’s own same-season average. This is a '
                'descriptive split, not a causal claim — it does not mean the official '
                'affected the player.</p>').format(name=esc(name))
        blocks.append(section("03", "Player splits",
                              '<div class="table-wrap">%s</div>' % swings_table(doc["player_swings"]),
                              note))
        next_num = "04"
    else:
        next_num = "03"

    # top performances
    if doc["top_performances"]:
        blocks.append(section(next_num, "Top scoring games",
                              '<div class="table-wrap">%s</div>' % top_perf_table(doc["top_performances"]),
                              '<p class="caption">Highest individual point totals in games '
                              '%s officiated.</p>' % esc(name)))
        next_num = "%02d" % (int(next_num) + 1)

    # notable games + gap disclosure
    gp = gap_phrase(s["per_season"])
    finals_line = ('<p class="notable-counts">Finals games: <b>{f}</b> '
                   '&middot; Game 7s: <b>{g}</b></p>').format(
        f=i(s["finals_games"]), g=i(s["game7s"]))
    disclosure = ""
    if gp:
        disclosure = ('<p class="disclosure"><span class="disclosure-mark" aria-hidden="true"></span>'
                      'Round and Game 7 detail isn’t available for {ph}; playoff '
                      'appearances from those seasons may be missing from this list.</p>').format(ph=esc(gp))
    if doc["notable_games"]:
        inner = '<div class="table-wrap">%s</div>' % notable_table(doc["notable_games"])
    elif s["games_po"] or s.get("games_pi"):
        inner = ('<p class="empty-note">No Finals or Game 7 games are labeled for this '
                 'official. See the note below.</p>')
    else:
        inner = '<p class="empty-note">No playoff games on record for this official.</p>'
    blocks.append(section(next_num, "Notable games", inner, finals_line + disclosure))
    blocks.append(ref_search(2, "bottom"))

    return page(title, desc, 2, "".join(blocks))


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
                name, tri, s["first_season"] or "2000-01"))
    badges = ('<span class="badge badge-past">Historical franchise</span>'
              if s.get("historical") else "")
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
               'least 15 games are listed.</p>' % (esc(name), esc(name)))
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
    ("career", "Career games", "most_career_games", "games_total", i, None),
    ("active", "Active", "most_career_games_active", "games_total", i, None),
    ("playoffs", "Playoff games", "most_playoff_games", "games_po", i, None),
    ("finals", "Finals games", "most_finals_games", "finals_games", i, "gap"),
    ("game7s", "Game 7s", "most_game7s", "game7s", i, "gap"),
    ("season", "This season", "most_games_current_season", "games_current", i, None),
]


def leaderboard_row(rank, r, valkey, valfmt):
    return ('<li class="lb-row"><span class="lb-rank">{rk}</span>'
            '<a class="lb-name" href="referee/{slug}/index.html">{name}</a>'
            '<span class="lb-val">{val}</span></li>').format(
        rk=rank, slug=esc(r["slug"]), name=esc(r["name"]), val=valfmt(r[valkey]))


def paired_panel(tab_id, active, title_hi, rows_hi, title_lo, rows_lo, valkey, valfmt, footnote=""):
    def col(title, rows):
        items = "".join(leaderboard_row(n, r, valkey, valfmt) for n, r in enumerate(rows, 1))
        return '<div class="lb-col"><h3 class="lb-subhead">{t}</h3><ol class="lb-list">{it}</ol></div>'.format(
            t=esc(title), it=items)
    return ('<div class="lb-panel lb-paired{act}" data-panel="{id}">{a}{b}{fn}</div>').format(
        act=" is-active" if active else "", id=tab_id,
        a=col(title_hi, rows_hi), b=col(title_lo, rows_lo), fn=footnote)


def render_index(refs, lb):
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

    # search + directory
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
    directory = """<section class="block" id="directory">
  <div class="block-head"><span class="eyebrow"><span class="eyebrow-stripe" aria-hidden="true"></span>
  Find an official</span><h2>All referees</h2></div>
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
</section>""".format(total=total, rows="".join(rows))

    # leaderboards
    tabs_btns = []
    panels = []
    gap_footnote = ('<p class="caption lb-foot">Finals and Game 7 totals undercount '
                    'officials active in the 2000-01 to 2002-03, 2012-13, and '
                    '2023-24-onward seasons, where playoff round labeling isn’t '
                    'available.</p>')
    for idx, (tab_id, label, key, valkey, valfmt, flag) in enumerate(LEADERBOARD_TABS):
        active = idx == 0
        tabs_btns.append(
            '<button class="lb-tab{act}" data-tab="{id}" role="tab" '
            'aria-selected="{sel}">{lab}</button>'.format(
                act=" is-active" if active else "", id=tab_id,
                sel="true" if active else "false", lab=esc(label)))
        items = "".join(leaderboard_row(n, r, valkey, valfmt)
                        for n, r in enumerate(lb[key], 1))
        fn = gap_footnote if flag == "gap" else ""
        panels.append(
            '<div class="lb-panel{act}" data-panel="{id}" role="tabpanel">'
            '<ol class="lb-list lb-list-wide">{items}</ol>{fn}</div>'.format(
                act=" is-active" if active else "", id=tab_id, items=items, fn=fn))

    # paired panels: home win% and total FTA
    tabs_btns.append('<button class="lb-tab" data-tab="homewin" role="tab" aria-selected="false">Home win%</button>')
    panels.append(paired_panel(
        "homewin", False,
        "Highest home win rate", lb["highest_home_win_pct"],
        "Lowest home win rate", lb["lowest_home_win_pct"],
        "home_win_pct", lambda v: pct(v)))
    tabs_btns.append('<button class="lb-tab" data-tab="fta" role="tab" aria-selected="false">Free throws</button>')
    panels.append(paired_panel(
        "fta", False,
        "Most combined FTA (RS)", lb["highest_avg_total_fta_rs"],
        "Fewest combined FTA (RS)", lb["lowest_avg_total_fta_rs"],
        "avg_total_fta", lambda v: dec(v)))

    min_n = lb["_meta"]["min_games_for_rate_leaderboards"]
    leaderboards = """<section class="block" id="leaderboards">
  <div class="block-head"><span class="eyebrow"><span class="eyebrow-stripe" aria-hidden="true"></span>
  Leaderboards</span><h2>Career leaders</h2></div>
  <div class="lb-tabs" role="tablist">{tabs}</div>
  <div class="lb-panels">{panels}</div>
  <p class="caption">Rate leaderboards (home win rate, free throws) include only
  officials with at least {min_n} qualifying games.</p>
</section>""".format(tabs="".join(tabs_btns), panels="".join(panels), min_n=min_n)

    body = hero + leaderboards + directory + ref_search(0, "bottom")
    title = "NBA Referee Database — career stats for every on-court official since 2000-01"
    desc = ("Searchable career profiles for %d NBA referees since 2000-01: games worked, "
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

/* ---- referee hero (flat header, Polymarket .phead treatment) ---- */
.ref-hero{margin-top:1.3rem;padding-bottom:1.1rem;border-bottom:1px solid var(--border)}
.ref-hero-body{padding:0}
.ref-name{font-size:1.7rem;letter-spacing:-.02em;margin:.15rem 0 0}
.ref-badges{margin-top:.6rem}
.badge{display:inline-block;font-family:var(--mono);font-size:.64rem;font-weight:700;
  padding:.16rem .5rem;border-radius:5px;text-transform:uppercase;letter-spacing:.04em}
.badge-active{background:var(--green-dim);color:var(--green)}
.badge-past{background:var(--surface-hover);color:var(--text-secondary)}
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
.wm{background:var(--surface);padding:.7rem .8rem}
.wm-val{font-size:1.35rem;font-weight:700;letter-spacing:-.02em}
.wm-label{font-size:.74rem;color:var(--text-secondary);margin-top:.15rem;line-height:1.3}
.wm-n{font-family:var(--mono);font-size:.62rem;color:var(--text-secondary);margin-top:.4rem}

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

/* ---- notable counts + gap disclosure ---- */
.notable-counts{font-family:var(--mono);font-size:.8rem;color:var(--text-secondary);margin:.3rem 0 0}
.notable-counts b{color:var(--text)}
.disclosure{display:flex;gap:.6rem;align-items:flex-start;margin:.9rem 0 0;
  padding:.7rem .85rem;background:var(--surface);border:1px solid var(--border);
  border-left:3px solid var(--orange);border-radius:10px;
  font-size:.8rem;color:var(--text-secondary);max-width:74ch}
.disclosure-mark{flex:none;width:.5rem;height:.5rem;border-radius:50%;
  background:var(--orange);margin-top:.4rem}
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
.lb-foot{grid-column:1/-1}

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
  .lb-paired.is-active{grid-template-columns:1fr}
  .lb-list-wide{columns:1}
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
    var idx=null, active=-1;
    function href(e){return root+TYPE_DIR[e.t]+"/"+e.s+"/index.html";}
    function close(){out.hidden=true;out.innerHTML="";active=-1;}
    function render(q){
      if(!q){close();return;}
      var hits=(idx||[]).filter(function(e){return e.n.toLowerCase().indexOf(q)!==-1;});
      hits.sort(function(a,b){
        var ap=a.n.toLowerCase().indexOf(q)===0?0:1, bp=b.n.toLowerCase().indexOf(q)===0?0:1;
        if(ap!==bp)return ap-bp;
        return a.n.length-b.n.length;
      });
      hits=hits.slice(0,12);
      if(!hits.length){out.innerHTML='<div class="rs-empty">No referee, team or player matches.</div>';out.hidden=false;active=-1;return;}
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
  function cellVal(td){
    var s=td.getAttribute("data-sort");
    if(s!==null){var n=parseFloat(s);return isNaN(n)?s.toLowerCase():n;}
    return td.textContent.trim().toLowerCase();
  }
  [].slice.call(document.querySelectorAll(".sortable-table")).forEach(function(table){
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
  // --- leaderboard tabs ---
  var tabs=[].slice.call(document.querySelectorAll(".lb-tab"));
  if(tabs.length){
    var panels=[].slice.call(document.querySelectorAll(".lb-panel"));
    tabs.forEach(function(tab){
      tab.addEventListener("click",function(){
        var id=tab.getAttribute("data-tab");
        tabs.forEach(function(t){var on=t===tab;t.classList.toggle("is-active",on);
          t.setAttribute("aria-selected",on?"true":"false");});
        panels.forEach(function(p){p.classList.toggle("is-active",p.getAttribute("data-panel")===id);});
      });
    });
  }
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
        f.write(render_index(refs, lb))

    # data-sources page (attribution moved out of the footer)
    os.makedirs(os.path.join(REPO, "sources"), exist_ok=True)
    with open(os.path.join(REPO, "sources", "index.html"), "w", encoding="utf-8") as f:
        f.write(render_sources())

    # referee pages
    docs = [json.load(open(p, encoding="utf-8"))
            for p in sorted(glob.glob(os.path.join(DATA, "referees", "*.json")))]
    current_slugs = {d["summary"]["slug"] for d in docs}

    # Remove pages for referees that no longer exist (e.g. after an identity
    # merge dropped a slug) so stale pages don't linger, mirroring build.py's
    # output-dir hygiene.
    removed = 0
    if os.path.isdir(REFEREE_DIR):
        for entry in os.listdir(REFEREE_DIR):
            d = os.path.join(REFEREE_DIR, entry)
            if os.path.isdir(d) and entry not in current_slugs:
                page_file = os.path.join(d, "index.html")
                if os.path.exists(page_file):
                    os.remove(page_file)
                if not os.listdir(d):
                    os.rmdir(d)
                removed += 1

    n = 0
    disclosed = 0
    for doc in docs:
        slug = doc["summary"]["slug"]
        out_dir = os.path.join(REFEREE_DIR, slug)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_ref(doc))
        if gap_phrase(doc["summary"]["per_season"]):
            disclosed += 1
        n += 1

    # team pages
    team_docs = {t["slug"]: json.load(open(os.path.join(DATA, "teams", "%s.json" % t["slug"]),
                                           encoding="utf-8")) for t in team_index}
    n_teams = _render_dir(os.path.join(REPO, "team"), team_docs, render_team)

    # player pages
    player_docs = {p["slug"]: json.load(open(os.path.join(DATA, "players", "%s.json" % p["slug"]),
                                             encoding="utf-8")) for p in player_index}
    n_players = _render_dir(os.path.join(REPO, "player"), player_docs, render_player)

    # global search index (referees + teams + players) for the navigate-search
    search_index = build_search_index(refs, team_docs, player_docs)
    with open(os.path.join(DATA, "search-index.json"), "w", encoding="utf-8") as f:
        json.dump(search_index, f, ensure_ascii=False, separators=(",", ":"))

    print("wrote index.html")
    print("wrote sources/index.html")
    print("wrote assets/style.css, assets/app.js")
    if removed:
        print("removed %d stale referee page(s)" % removed)
    print("wrote %d referee pages (%d carry the gap-season disclosure)" % (n, disclosed))
    print("wrote %d team pages, %d player pages" % (n_teams, n_players))
    print("sample URLs:")
    for u in ["referee/scott-foster/", "team/bos/", "player/lebron-james/"]:
        print("  %sindex.html" % u)


if __name__ == "__main__":
    main()
