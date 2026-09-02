# League Context & Season Splits

## The problem
League-average combined points ranges from 172.6 (1998-99) to 230.5 (2025-26). Any career-wide league average, or any ranking of raw values, is dominated by era rather than by the official. The 1990s cohort added recently would sweep the bottom of every scoring leaderboard as a pure artifact. Era adjustment is therefore mandatory, not optional.

## 1. build.py — per-season league baselines
For each season and season_type, compute league averages for every whistle stat currently on referee pages: combined points, combined FTA, combined personal fouls, avg margin of victory, home team win rate, OT rate. Write to data/league_baselines.json. Include n (games) per season.

## 2. build.py — per-referee era-adjusted context
For each referee and each stat, compute an expected baseline as the games-weighted average of the league value across exactly the seasons that referee worked (weight = that referee's games in that season). Then compute the differential: actual minus expected. Store raw value, expected baseline, differential, and n.

Rank and percentile must be computed on the DIFFERENTIAL, not the raw value. This is a correction to what currently ships. Existing min-n thresholds apply unchanged (200 RS, 75 PO).

## 3. build.py — per-season splits per referee
For each referee, a per-season breakdown: season, games (RS and PO), each whistle stat, that season's league value, and the differential. This is the data behind the season selector.

## 4. render_pages.py — whistle profile cards
Each card shows: raw value, the era-adjusted league baseline, the signed differential, and rank among qualifying referees. Format along the lines of "195.4 · lg 199.1 · −3.7 · 12th lowest of 118". Keep it compact; this replaces the current bare number, it does not add a second card. Percentile coloring now keys off the differential.

## 5. render_pages.py — season splits table on referee pages
A sortable per-season table under the whistle profile: season, games, each stat with its differential versus that season's league average. Career row at the bottom. This is the season selector — a table the reader can scan and sort, not a dropdown that hides data.

## 6. The six stat leaderboards
Each gains a sortable differential column alongside the raw value, and defaults to sorting by differential. Keep the raw column: "games with the fewest total points" is a legitimate factual leaderboard, it just isn't a claim about the official. Label both columns so the distinction is unmistakable.

## Labeling requirement
Every one of these stats describes games an official worked, not actions that official personally took. Existing wording must stay descriptive ("combined points in games officiated"). Differentials describe how those games compared to the league in the same seasons. No causal framing anywhere.

## QA
Hand-verify one referee's era-adjusted baseline against a manual games-weighted computation from games.csv.gz. Confirm at least one 1990s-heavy referee's scoring rank moves substantially once ranking switches from raw to differential, and report the before/after — that is the specific distortion this round exists to fix. No NaN. Every season split's game counts must reconcile with that referee's games_total.
