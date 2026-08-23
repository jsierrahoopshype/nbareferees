# NBA Referee Database — Tier C: Informational Density

Goal: thicken the frontpage with widgets that reward exploration, each backed by a full-list page. Data-only; no new sources, everything computable from current source-data.

Standing rules: additive; n visible on every rate stat; min-n thresholds enforced; descriptive-only language (no causal claims, no betting framing); confirm before push.

## 1. build.py — extend data/dashboard.json

### crews
Every unique trio of canonical referees who worked a game together (crew-of-3 only, alternates excluded via the existing first-3-by-row-order rule). Per trio: the three names + slugs, games together, first and last season together, and combined avg total points and avg FTA with n. Top 5 for the index widget, top 40 for the full-list page.

### team_officials
For each of the 31 canonical franchises: the official who has worked the most of that team's games (name, slug, games, n). Frequency only — deliberately NOT win-rate extremes. Per-referee win rates already live on team pages where they read as reference data; surfacing "this team's best and worst referee by win%" as a frontpage highlight invites exactly the causal reading this site avoids.

### debuts_farewells
Per season, referees whose first game in the dataset falls in that season, and whose last game falls in that season. Two hard labeling requirements: call these "first game in this database" and "last game in this database," never "NBA debut" or "retired" — coverage starts at 1993-94, so referees active before then (Bavetta, Hollins and others) have real careers predating our floor. And omit the farewell list entirely for 2025-26, since those referees are presumably still active.

### era_leaders
By decade — 1990s (partial: 1993-94 through 1999-00, label it as partial), 2000s, 2010s, 2020s — the leaders in total games, playoff games, and Finals games. Now meaningful since 1990s round labels exist.

## 2. build.py — data/swings_all.json (new file, kept separate for size)
Every player-referee pair meeting the existing n>=15 threshold, sorted by signed points swing descending. Include player name + slug, referee name + slug, n, pts with, pts baseline, and the swing. Cap at the top 250 and bottom 250.

## 3. render_pages.py

Index widgets, each linking to its full page: crew chemistry (top 5 trios), team-officials (a scannable strip), debuts and farewells (most recent season), era leaders (tabbed by decade, reusing the existing leaderboard tab component).

Full-list pages on the standard shell with real SEO titles and meta: `/crews/`, `/team-officials/`, `/debuts/`, `/eras/`, `/swings/`.

Per-referee game logs at `/referee/{slug}/games/` — every game that referee worked: date, matchup (full team names with tricode chips, per the existing convention), final score, round label where applicable, and the co-officials on that crew, linked. Group by season, newest first. Link from each referee page hero. Note this is the largest item here (166 new pages, some with 1,700+ rows); if page weight looks problematic on the heaviest referees, flag it before rendering all of them rather than shipping something unusable on mobile.

## 4. QA
No NaN anywhere; every slug in every new structure resolves to a real page; crew trio counts hand-verified for the top trio against officials.csv.gz directly; game-log row counts per referee match that referee's games_total; zero dead links across all new pages.
