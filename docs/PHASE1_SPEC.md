# NBA Referee Database — Phase 1 Spec

**Repo:** `jsierrahoopshype/nba-referees` (new repo, GitHub Pages from `main`, root)
**Goal:** Static site with a page per NBA referee (2000-01 → present, regular season + playoffs) plus leaderboards. Per ref: all games officiated, team records under them, whistle profile, top scoring performances, player performance swings vs. season averages, notable games (Finals, Game 7s).
**Out of scope for Phase 1 (do not build):** play-by-play foul attribution, L2M accuracy, pre-2000 backfill, video tools, automated daily updates. These are Phase 2/3. Anything not listed in this spec is out of scope.

---

## 0. Hard rules (apply to every session in this repo)

1. Never break working code. Additive changes only. If a change might affect previously-working behavior, stop and flag it before making it.
2. Full copy-paste files, not diffs, when handing code to Jorge for local use.
3. No secrets anywhere in the repo. This project needs none: no API keys, no tokens.
4. Confirm before any `git push`. Local commits are fine.
5. All game IDs are 10-digit **strings** with leading zeros (`"0022300001"`). Every pandas read must force `dtype={"game_id": str}` (and equivalents). Dropping leading zeros silently breaks every join. This is the single most likely bug in the whole project.
6. Do not fetch from stats.nba.com in cloud sessions. It is blocked from datacenter IPs. All nba_api calls run on Jorge's Windows machine only.
7. Output CSVs written locally use `utf-8-sig` encoding (Windows/Excel compatibility).
8. Show sample sizes (`n`) next to every rate stat and every split in data and UI. Never publish a split below its minimum-n threshold (defined in §5).

---

## 1. Architecture: what runs where and why

| Step | Where | Why |
|---|---|---|
| Download nbadb SQLite from Kaggle | Jorge, manually, in browser (logged in) | Kaggle requires auth; file is multi-GB |
| Extract slim CSVs from SQLite | **Local** (Windows, cmd) | The raw DB never enters the repo (GitHub 100MB file limit) |
| Fetch player game logs (if needed) | **Local** via nba_api | stats.nba.com blocks cloud IPs |
| Backfill missing recent officials (contingency) | **Local** via nba_api | Same |
| Aggregation build (`build.py`) | **Cloud** (Claude Code browser) | Pure pandas over repo files, no network |
| Static site + pre-rendered pages | **Cloud** (Claude Code browser) | Same |
| Deploy | GitHub Pages from `main` | Standard pattern |

Only gzipped extracts (~40MB total) are committed. Raw SQLite stays on Jorge's machine.

---

## 2. Repo structure

```
nba-referees/
├── docs/
│   └── PHASE1_SPEC.md          # this file
├── scripts/
│   ├── local/
│   │   ├── extract_from_nbadb.py
│   │   ├── fetch_player_logs.py        # only if nbadb lacks player box scores
│   │   └── backfill_officials.py       # contingency only
│   ├── build.py                # cloud: source-data → data/*.json
│   └── render_pages.py         # cloud: data/*.json → static HTML
├── source-data/
│   ├── officials.csv.gz
│   ├── games.csv.gz
│   └── player_logs/
│       └── {season}_{RS|PO}.csv.gz     # e.g. 2023-24_RS.csv.gz
├── data/                       # build outputs (JSON, committed)
│   ├── referees.json
│   ├── leaderboards.json
│   └── referees/{ref_id}.json
├── referee/                    # pre-rendered pages (generated, committed)
│   └── {slug}/index.html
├── assets/                     # css, js, shared
└── index.html
```

---

## 3. Local scripts (Claude Code writes them; Jorge runs them in cmd)

### 3.1 `extract_from_nbadb.py`

Input: path to the downloaded nbadb SQLite (passed as arg, e.g. `python extract_from_nbadb.py "C:\Users\Jorge Sierra\Downloads\nba.sqlite"`).

**Do not assume table names.** The nbadb schema was overhauled in 2025-26 into a star-schema warehouse; old names (`game`, `officials`) may not exist. The script must:

1. Introspect: `SELECT name FROM sqlite_master WHERE type='table'`, print all tables with row counts and column lists to console AND to `source-data/_schema_report.txt`.
2. Locate the **officials table** by column signature: something containing per-game official assignments (expect columns like game id + official id + first/last name, possibly jersey number). If the warehouse uses a dimension + bridge/fact split (e.g. `dim_official` + a game-official bridge), join them.
3. Locate the **games table**: game id, date, season, season type, home/away team ids + abbreviations, home/away points, and a winner-derivable field.
4. Detect whether a **player-level box score table** exists (per player per game: PTS/REB/AST/FTA/FGA/MIN at minimum). Print a clear verdict: `PLAYER BOX SCORES: FOUND (table X)` or `NOT FOUND — run fetch_player_logs.py`.
5. Export, filtered to season >= 1996-97:
   - `source-data/officials.csv.gz`: `game_id, official_id, official_name, jersey_num`
   - `source-data/games.csv.gz`: `game_id, game_date, season, season_type, home_team_id, home_team_abbr, away_team_id, away_team_abbr, home_pts, away_pts, home_win`
   - If player box scores found: `source-data/player_logs/{season}_{type}.csv.gz` with `game_id, player_id, player_name, team_id, team_abbr, min, pts, reb, ast, stl, blk, tov, fga, fgm, fg3a, fg3m, fta, ftm, pf, plus_minus`
6. **Freshness report** (print + write to `source-data/_freshness.txt`): max game_date overall; games count per season; officials coverage per season (% of games with 3 officials); explicit flag if the latest season looks incomplete or if any season after 2000-01 has officials coverage below 95%.

### 3.2 `fetch_player_logs.py` (only if 3.1 says NOT FOUND)

- nba_api `LeagueGameLog`, `player_or_team_abbreviation="P"`, per season 2000-01 → current, `season_type_all_star` in `["Regular Season", "Playoffs"]`. ~52 calls total.
- 2-second delay between calls. Resume-safe: skip seasons whose output file already exists.
- Standard nba_api headers; string dtype on GAME_ID; write `{season}_{RS|PO}.csv.gz` with the column set from §3.1.5.

### 3.3 `backfill_officials.py` (contingency only)

Run only if the freshness report shows games missing officials in recent seasons. For each `game_id` present in `games.csv.gz` but absent from `officials.csv.gz` (season >= 2000-01): call nba_api `BoxScoreSummaryV3` (V2 is degraded), extract officials, append to `officials.csv.gz`. 1.5s delay, resume-safe, progress printout. Note: fresh gaps at ~1.5s/call means ~40 games/min; even a full missing month is under an hour.

**After local runs:** Jorge commits `source-data/` and pushes. Cloud sessions take over.

---

## 4. Cloud script: `scripts/build.py`

Pure pandas over `source-data/`. No network. Outputs JSON to `data/`.

### 4.1 Shared derivations

- **Team-game stats**: aggregate player logs per (game_id, team_id): sum pts, fta, pf, etc. Use this as the single source for FTA/fouls (don't trust a separate team table; one source, no reconciliation bugs).
- **Season type**: from `games.season_type`, or fall back to game_id digit 3 (`2`=RS, `4`=PO, `5`=play-in). Treat play-in as playoffs=False, its own label.
- **Playoff round/game parsing**: for playoff game_ids (`004YYSSRMG` pattern), digit 8 = round (1-4, 4 = Finals), digit 10 = game number (7 = Game 7). **Verify against 3 known games before trusting** (e.g. a known Finals Game 7); if the pattern fails verification, log it and skip round labeling rather than guessing.
- **Player season baselines**: per (player_id, season): per-game averages across ALL that player's games that season (RS and PO baselines computed separately; compare playoff games to playoff baseline only if the player has 5+ PO games that season, else skip the game from swing calcs).
- **Ref identity**: key on `official_id`, never name. Names collide (three different Crawfords have officiated NBA games). Slug = `firstname-lastname`, with `-{official_id}` suffix appended only when two refs share a slug.

### 4.2 Outputs

**`data/referees.json`** — array, one entry per ref:
`official_id, name, slug, first_season, last_season, games_total, games_rs, games_po, finals_games, game7s, active (worked current season)`

**`data/referees/{official_id}.json`**:
- `summary`: fields above + per-season game counts
- `team_records`: per team: `games, wins, losses, win_pct, home_games, home_wins, avg_margin_for_team` (a team "wins under ref X" = won a game X officiated). Sorted by games desc.
- `whistle_profile` (RS and PO separately): `avg_total_points, avg_total_fta, avg_total_pf, home_win_pct, avg_abs_margin, ot_games, ot_rate` — each with `n`
- `top_performances`: top 25 individual scoring games under this ref: `player_name, pts, team_abbr, opp_abbr, game_date, game_id`
- `player_swings`: players with **≥15 games** under this ref: `player_id, name, n_games, pts_with_ref, pts_baseline, pts_swing, fta_swing, pf_swing`, sorted by |pts_swing| desc, capped at top 50. Method: swing = mean over qualifying games of (game stat − that player's same-season baseline).
- `notable_games`: list of Finals games and Game 7s: `game_id, date, matchup, result, round, game_num`

**`data/leaderboards.json`**:
- most career games (all-time, and active only)
- most playoff games; most Finals games; most Game 7s
- highest / lowest home team win% (min 200 games)
- highest / lowest avg total FTA (min 200 games, RS only)
- most games in current season

### 4.3 QA gate (build fails loudly if violated)

- Every game_id in officials joins to exactly one game.
- 3 officials per game for ≥98% of games 2000-01+; print exceptions.
- Sum of a ref's team_records games = 2 × games_total (two teams per game).
- Spot-check console printout: total career games for 3 veteran refs (e.g. Scott Foster, Tony Brothers, James Capers) so Jorge can eyeball against NBAstuffer's public tables before publishing.
- No NaN leaks into JSON.

---

## 5. Cloud script: `scripts/render_pages.py` + site

- Reads `data/`, generates `index.html` and `/referee/{slug}/index.html` for every ref. Pre-rendered HTML (SEO pattern from nba-draft-combine): unique `<title>` and meta description per ref ("Scott Foster NBA referee stats: career games, team records, player splits"), real content in the HTML, not JS-injected.
- Index page: search box (client-side filter over referees.json), leaderboard tabs.
- Ref page sections, in order: header (name, seasons, games, active badge) → whistle profile cards → team records (sortable table, sticky header) → player swings (table, shows `n`, footnote explaining method and its descriptive-not-causal nature) → top performances → notable games.
- Styling: follow the frontend-design skill; consistent with the other jsierrahoopshype.github.io tools; mobile-friendly tables (the garbage-time mobile card pattern is the reference).
- Footer attribution (required): "Historical data via Wyatt Walsh's NBA Database on Kaggle (CC BY-SA 4.0) and NBA Stats." Link both.
- Editorial framing rule baked into copy: records and splits are descriptive facts. No text implying refs cause outcomes or favor teams. No betting framing.
- Vanilla JS only. No build framework, no localStorage.

---

## 6. Run order

1. **Cloud session 1** (Claude Code browser, repo fresh with this spec committed): write the three `scripts/local/` files + empty folder structure + `.gitignore` (ignore `*.sqlite`). Commit.
2. **Local**: Jorge downloads nbadb SQLite from Kaggle (browser, logged in). Runs `extract_from_nbadb.py`. Reads the freshness report and the player-box-score verdict.
3. **Local (conditional)**: run `fetch_player_logs.py` and/or `backfill_officials.py` per the verdicts. Commit `source-data/`, push.
4. **Cloud session 2**: write and run `build.py`. Review QA printout together. Fix until green. Commit `data/`.
5. **Cloud session 3**: write `render_pages.py` + site, generate pages, review locally rendered output, enable GitHub Pages, confirm, push.
6. Spot-check 3 ref pages against NBAstuffer numbers before sharing anything publicly.

---

## 7. Kickoff prompt for Claude Code browser (session 1)

> Read docs/PHASE1_SPEC.md in full before doing anything. Execute step 1 of §6: create the folder structure and write the three local scripts exactly to spec, with special attention to §0 rule 5 (string game IDs) and §3.1's requirement to introspect the SQLite schema instead of assuming table names. I run local scripts myself on Windows cmd. Do not attempt any network calls to stats.nba.com or Kaggle from this environment. Confirm with me before pushing.
