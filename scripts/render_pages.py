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

Design note: the visual identity is grounded in the referee's own world -- the
black-and-white vertical stripes of the official's jersey are the signature
motif, monochrome ink on cool paper with a single restrained basketball-flame
accent. System fonts only (tabular/mono numerals for the "scoresheet" feel) so
the site has zero network dependencies and renders identically offline.

Editorial rule (carried from both specs): every generated string is a
descriptive fact. Nothing implies a referee causes outcomes or favors a team;
no betting framing.

Run from repo root:  python scripts/render_pages.py
"""

import os
import re
import json
import glob
import html

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


def season_range(first, last):
    return first if first == last else "%s–%s" % (first, last)


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


def footer():
    links = " &middot; ".join(
        '<a href="%s" rel="noopener">%s</a> <span class="foot-note">(%s)</span>'
        % (esc(u), esc(name), esc(note)) for name, u, note in ATTRIBUTION)
    return """</main>
<footer class="site-foot">
  <p class="foot-sources">Data: {links}.</p>
  <p class="foot-editorial">Every figure here is a descriptive record of games
  as they were officiated. Nothing on this site implies a referee causes a
  result or favors a team.</p>
</footer>
<script src="{js}"></script>
</body>
</html>""".format(links=links, js="__JS__")


def page(title, description, depth, body):
    f = footer().replace("__JS__", "../" * depth + "assets/app.js")
    return head(title, description, depth) + body + f


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
        ("Final margin", dec(w["avg_abs_margin"]), n),
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
            '<td data-label="Team" data-sort="{team}"><span class="team-tag">{team}</span></td>'
            '<td data-label="G" data-sort="{g}">{gi}</td>'
            '<td data-label="W" data-sort="{w}">{wi}</td>'
            '<td data-label="L" data-sort="{l}">{li}</td>'
            '<td data-label="Win%" data-sort="{wp}">{wpf}</td>'
            '<td data-label="Home G" data-sort="{hg}">{hgi}</td>'
            '<td data-label="Home W" data-sort="{hw}">{hwi}</td>'
            '<td data-label="Avg margin" data-sort="{m}"><span class="{mc}">{ms}</span></td>'
            "</tr>".format(
                team=esc(r["team_abbr"]),
                g=r["games"], gi=i(r["games"]), w=r["wins"], wi=i(r["wins"]),
                l=r["losses"], li=i(r["losses"]),
                wp=(r["win_pct"] if r["win_pct"] is not None else -1), wpf=pct(r["win_pct"]),
                hg=r["home_games"], hgi=i(r["home_games"]),
                hw=r["home_wins"], hwi=i(r["home_wins"]),
                m=(margin if margin is not None else 0),
                mc="pos" if (margin or 0) > 0 else ("neg" if (margin or 0) < 0 else ""),
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
            '<td data-label="Player" data-sort="{nm}">{nm}</td>'
            '<td data-label="Games" data-sort="{n}">{ni}</td>'
            '<td data-label="PTS with" data-sort="{pw}">{pwf}</td>'
            '<td data-label="PTS baseline" data-sort="{pb}">{pbf}</td>'
            '<td data-label="PTS swing" data-sort="{ps}"><span class="{psc}">{pss}</span></td>'
            '<td data-label="FTA swing" data-sort="{fs}">{fss}</td>'
            '<td data-label="PF swing" data-sort="{ff}">{ffs}</td>'
            "</tr>".format(
                nm=esc(s["name"]), n=s["n_games"], ni=i(s["n_games"]),
                pw=s["pts_with_ref"], pwf=dec(s["pts_with_ref"]),
                pb=s["pts_baseline"], pbf=dec(s["pts_baseline"]),
                ps=s["pts_swing"], pss=signed(s["pts_swing"]),
                psc="pos" if (s["pts_swing"] or 0) > 0 else ("neg" if (s["pts_swing"] or 0) < 0 else ""),
                fs=s["fta_swing"], fss=signed(s["fta_swing"]),
                ff=s["pf_swing"], ffs=signed(s["pf_swing"])))
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
            '<td data-label="Team"><span class="team-tag">{tm}</span> vs '
            '<span class="team-tag">{op}</span></td>'
            '<td data-label="Date">{dt}</td>'
            "</tr>".format(r=rank, pl=esc(p["player_name"]), pt=i(p["pts"]),
                           tm=esc(p["team_abbr"]), op=esc(p["opp_abbr"]),
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
    seasons = season_range(s["first_season"], s["last_season"])
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

    blocks = [hero]

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
                seasons=esc(season_range(r["first_season"], r["last_season"])),
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

    body = hero + leaderboards + directory
    title = "NBA Referee Database — career stats for every on-court official since 2000-01"
    desc = ("Searchable career profiles for %d NBA referees since 2000-01: games worked, "
            "team records, whistle tendencies, playoff appearances, and leaderboards." % total)
    return page(title, desc, 0, body)


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
CSS = r""":root{
  --paper:#edece6; --panel:#fbfaf7; --panel-2:#f4f2ec;
  --ink:#181613; --ink-soft:#6a655d; --ink-faint:#938d83;
  --line:#d8d5cc; --line-soft:#e6e3db;
  --flame:#c8410a; --flame-bright:#e2560f;
  --pos:#1c7d54; --neg:#b23a2e;
  --stripe-dark:#181613; --stripe-light:#fbfaf7;
  --shadow:0 1px 2px rgba(24,22,19,.05),0 6px 20px rgba(24,22,19,.05);
  --maxw:1120px;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#14120e; --panel:#1d1a15; --panel-2:#232019;
  --ink:#efece4; --ink-soft:#a8a297; --ink-faint:#7c766b;
  --line:#332f27; --line-soft:#2a2721;
  --flame:#f0731f; --flame-bright:#f6873a;
  --pos:#4fbb8a; --neg:#e0776b;
  --stripe-dark:#000; --stripe-light:#efece4;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;
  font-feature-settings:"tnum" 1,"cv01" 1;}
.mono,.big-num,.chip-val,.wm-val,.hstat-num,.lb-val,.rank,td[data-sort]{
  font-variant-numeric:tabular-nums;
  font-family:ui-monospace,"SF Mono","Roboto Mono",Menlo,Consolas,monospace;}
a{color:var(--flame);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
h1,h2,h3{margin:0;letter-spacing:-.02em;line-height:1.08;font-weight:800}
.skip{position:absolute;left:-999px}
.skip:focus{left:8px;top:8px;background:var(--ink);color:var(--paper);padding:8px 12px;z-index:20;border-radius:4px}
:focus-visible{outline:2.5px solid var(--flame);outline-offset:2px;border-radius:3px}

/* stripe motif = the referee jersey, the site's signature */
.brand-stripe,.eyebrow-stripe,.ref-hero-stripe,.hero-rule,.disclosure-mark{
  background-image:repeating-linear-gradient(90deg,
    var(--stripe-dark) 0,var(--stripe-dark) 3px,
    var(--stripe-light) 3px,var(--stripe-light) 6px);}

.masthead{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;
  max-width:var(--maxw);margin:0 auto;padding:18px 24px;
  border-bottom:1px solid var(--line)}
.brand{display:inline-flex;align-items:center;gap:10px;color:var(--ink);font-weight:800}
.brand:hover{text-decoration:none}
.brand-stripe{width:22px;height:26px;border:1.5px solid var(--ink);border-radius:2px}
.brand-name{font-size:1.05rem;letter-spacing:-.01em}
.brand-sub{color:var(--ink-soft);font-size:.82rem;margin-left:auto;font-variant-numeric:tabular-nums}

main{max-width:var(--maxw);margin:0 auto;padding:0 24px}

/* ---- index hero ---- */
.index-hero{padding:56px 0 40px;border-bottom:1px solid var(--line)}
.hero-rule{height:8px;width:120px;border-radius:1px;margin-bottom:26px}
.hero-kicker,.ref-kicker,.hero-kicker{text-transform:uppercase;letter-spacing:.18em;
  font-size:.74rem;font-weight:700;color:var(--flame);margin:0 0 14px}
.hero-title{font-size:clamp(2.4rem,6vw,4rem);letter-spacing:-.035em}
.hero-lead{max-width:60ch;color:var(--ink-soft);font-size:1.08rem;margin:18px 0 0}
.hero-stats{display:flex;flex-wrap:wrap;gap:36px;margin-top:34px}
.hstat{display:flex;flex-direction:column}
.hstat-num{font-size:2.1rem;font-weight:800;line-height:1}
.hstat-label{font-size:.8rem;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.08em;margin-top:6px}

/* ---- blocks ---- */
.block{padding:40px 0;border-bottom:1px solid var(--line)}
.block:last-of-type{border-bottom:0}
.block-head{margin-bottom:22px}
.eyebrow{display:inline-flex;align-items:center;gap:10px;text-transform:uppercase;
  letter-spacing:.14em;font-size:.72rem;font-weight:700;color:var(--ink-soft);margin-bottom:10px}
.eyebrow-stripe{display:inline-block;width:26px;height:12px;border-radius:1px;
  border:1px solid var(--line)}
.block-head h2{font-size:clamp(1.5rem,3.2vw,2rem)}
.caption{color:var(--ink-soft);font-size:.9rem;max-width:70ch;margin:14px 0 0}

/* ---- ref hero ---- */
.ref-hero{position:relative;display:flex;gap:0;margin-top:34px;
  background:var(--panel);border:1px solid var(--line);border-radius:12px;
  overflow:hidden;box-shadow:var(--shadow)}
.ref-hero-stripe{width:14px;flex:none}
.ref-hero-body{padding:28px 30px 30px}
.ref-name{font-size:clamp(2rem,5.5vw,3.3rem);letter-spacing:-.035em;margin:.1em 0 0}
.ref-badges{margin-top:14px}
.badge{display:inline-block;font-size:.76rem;font-weight:700;padding:5px 11px;
  border-radius:999px;letter-spacing:.02em}
.badge-active{background:var(--flame);color:#fff}
.badge-past{background:var(--panel-2);color:var(--ink-soft);border:1px solid var(--line)}
.chip-row{display:flex;flex-wrap:wrap;gap:12px;margin-top:24px}
.chip{background:var(--panel-2);border:1px solid var(--line);border-radius:9px;
  padding:12px 16px;min-width:104px}
.chip-accent{background:var(--ink);border-color:var(--ink)}
.chip-accent .chip-val{color:var(--paper)}
.chip-accent .chip-label{color:var(--ink-faint)}
.chip-val{display:block;font-size:1.5rem;font-weight:800}
.chip-label{display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;
  color:var(--ink-soft);margin-top:4px}

/* ---- whistle profile ---- */
.whistle-cols{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.whistle-kind{font-size:1rem;font-weight:800;text-transform:uppercase;letter-spacing:.05em;
  padding-bottom:10px;border-bottom:2px solid var(--ink);margin-bottom:16px;
  display:flex;justify-content:space-between;align-items:baseline}
.whistle-n{font-size:.78rem;font-weight:600;color:var(--ink-soft);letter-spacing:0}
.whistle-grid{display:grid;grid-template-columns:1fr 1fr;gap:2px;background:var(--line-soft);
  border:1px solid var(--line-soft);border-radius:8px;overflow:hidden}
.wm{background:var(--panel);padding:14px 15px}
.wm-val{font-size:1.5rem;font-weight:800}
.wm-label{font-size:.8rem;color:var(--ink-soft);margin-top:2px;line-height:1.3}
.wm-n{font-size:.72rem;color:var(--ink-faint);margin-top:6px}

/* ---- tables ---- */
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
.data-table{width:100%;border-collapse:collapse;font-size:.92rem}
.data-table th,.data-table td{padding:11px 14px;text-align:right;white-space:nowrap}
.data-table th:first-child,.data-table td:first-child,
.data-table .col-text,.data-table td[data-label="Player"],
.data-table td[data-label="Referee"],.data-table td[data-label="Matchup"],
.data-table td[data-label="Team"],.data-table td[data-label="Date"],
.data-table td[data-label="Result"]{text-align:left}
.data-table thead th{position:sticky;top:0;background:var(--panel-2);z-index:2;
  font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-soft);
  border-bottom:2px solid var(--line);font-weight:700}
.data-table tbody tr{border-top:1px solid var(--line-soft)}
.data-table tbody tr:hover{background:var(--panel-2)}
.sortable{cursor:pointer;user-select:none}
.sortable:hover{color:var(--ink)}
.sortable::after{content:"\2195";opacity:.35;margin-left:5px;font-size:.85em}
.sortable.sort-asc::after{content:"\2191";opacity:1;color:var(--flame)}
.sortable.sort-desc::after{content:"\2193";opacity:1;color:var(--flame)}
.team-tag,.round-tag{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:.82rem;
  font-weight:600;letter-spacing:.02em}
.round-tag{color:var(--flame)}
.big-num{font-size:1.05rem;font-weight:800}
.rank{color:var(--ink-faint);font-weight:600}
.pos{color:var(--pos);font-weight:600}
.neg{color:var(--neg);font-weight:600}

/* ---- notable / disclosure ---- */
.notable-counts{font-size:.95rem;color:var(--ink-soft);margin:6px 0 0}
.notable-counts b{color:var(--ink)}
.disclosure{display:flex;gap:11px;align-items:flex-start;margin:16px 0 0;
  padding:13px 15px;background:var(--panel-2);border:1px solid var(--line);
  border-left:3px solid var(--flame);border-radius:8px;
  font-size:.88rem;color:var(--ink-soft);max-width:74ch}
.disclosure-mark{flex:none;width:16px;height:20px;border:1px solid var(--line);border-radius:2px;margin-top:1px}
.empty-note{color:var(--ink-soft);font-size:.95rem;padding:8px 0}

/* ---- search + directory ---- */
.search-wrap{margin-bottom:18px}
.search-input{width:100%;max-width:440px;padding:13px 16px;font-size:1rem;
  background:var(--panel);border:1.5px solid var(--line);border-radius:9px;color:var(--ink)}
.search-input:focus{outline:none;border-color:var(--flame);box-shadow:0 0 0 3px rgba(200,65,10,.14)}
.search-empty{color:var(--ink-soft);margin:12px 2px 0}
.dot-active{color:var(--flame);font-size:.6rem;vertical-align:middle;margin-left:6px}

/* ---- leaderboards ---- */
.lb-tabs{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;margin-bottom:20px;
  border-bottom:1px solid var(--line);-webkit-overflow-scrolling:touch}
.lb-tab{flex:none;background:none;border:0;padding:10px 14px;font:inherit;font-weight:600;
  font-size:.9rem;color:var(--ink-soft);cursor:pointer;border-bottom:2.5px solid transparent;
  margin-bottom:-1px;white-space:nowrap}
.lb-tab:hover{color:var(--ink)}
.lb-tab.is-active{color:var(--ink);border-bottom-color:var(--flame)}
.lb-panel{display:none}
.lb-panel.is-active{display:block}
.lb-paired.is-active{display:grid;grid-template-columns:1fr 1fr;gap:26px}
.lb-subhead{font-size:.82rem;text-transform:uppercase;letter-spacing:.07em;
  color:var(--ink-soft);font-weight:700;margin-bottom:12px}
.lb-list{list-style:none;margin:0;padding:0}
.lb-list-wide{columns:2;column-gap:44px}
.lb-row{display:flex;align-items:baseline;gap:12px;padding:9px 0;
  border-bottom:1px solid var(--line-soft);break-inside:avoid}
.lb-rank{color:var(--ink-faint);font-size:.85rem;width:1.8em;flex:none;text-align:right}
.lb-name{flex:1;color:var(--ink);font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis}
.lb-name:hover{color:var(--flame)}
.lb-val{font-weight:800;font-size:.95rem}
.lb-foot{grid-column:1/-1}

/* ---- footer ---- */
.site-foot{max-width:var(--maxw);margin:0 auto;padding:30px 24px 60px;
  border-top:1px solid var(--line);color:var(--ink-soft);font-size:.85rem}
.foot-sources a{font-weight:600}
.foot-note{color:var(--ink-faint);font-size:.8rem}
.foot-editorial{max-width:74ch;margin-top:12px;color:var(--ink-faint)}

/* ---- responsive: garbage-time mobile card pattern ---- */
@media (max-width:720px){
  main{padding:0 16px}
  .masthead{padding:14px 16px}
  .brand-sub{width:100%;margin-left:0}
  .whistle-cols{grid-template-columns:1fr}
  .lb-paired.is-active{grid-template-columns:1fr}
  .lb-list-wide{columns:1}
  .table-wrap{border:0;background:none;overflow:visible}
  .data-table,.data-table tbody,.data-table tr{display:block;width:100%}
  .data-table thead{position:absolute;left:-9999px}
  .data-table tr{background:var(--panel);border:1px solid var(--line);border-radius:9px;
    margin-bottom:10px;padding:6px 4px}
  .data-table td{display:flex;justify-content:space-between;gap:16px;text-align:right;
    white-space:normal;border:0;padding:7px 14px}
  .data-table td::before{content:attr(data-label);color:var(--ink-soft);font-weight:600;
    text-transform:uppercase;font-size:.72rem;letter-spacing:.04em;text-align:left;flex:none}
  .data-table td:first-child{text-align:right}
  .data-table td[data-label]:only-child::before{content:""}
}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
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
def main():
    refs = json.load(open(os.path.join(DATA, "referees.json"), encoding="utf-8"))
    lb = json.load(open(os.path.join(DATA, "leaderboards.json"), encoding="utf-8"))

    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)
    with open(os.path.join(ASSETS, "app.js"), "w", encoding="utf-8") as f:
        f.write(JS)

    # index
    with open(os.path.join(REPO, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(refs, lb))

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

    print("wrote index.html")
    print("wrote assets/style.css, assets/app.js")
    if removed:
        print("removed %d stale referee page(s)" % removed)
    print("wrote %d referee pages (%d carry the gap-season disclosure)" % (n, disclosed))
    print("sample URLs:")
    for s in ["scott-foster", "joe-derosa", "jimmy-clark"]:
        print("  referee/%s/index.html" % s)


if __name__ == "__main__":
    main()
