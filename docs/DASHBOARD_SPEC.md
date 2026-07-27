# NBA Referee Database — Frontpage Dashboard Spec

**Goal:** turn the index from a directory into a dashboard people revisit: a daily-rotating referee spotlight, a head-to-head comparator, records/history strips, an officiating-quality leaderboard, and the slot (plus source probe) for the in-season "Tonight's Crews" module that ships in September.

**Prerequisite:** the franchise-consolidation round and the ref-page ranking round (whistle-profile coloring, per-stat pages, swing sort) are both merged before this starts — all three touch build.py and must not run concurrently.

**Standing rules apply:** additive, n visible, min-n thresholds, descriptive-only language (no causal claims, no betting framing), confirm before push.

---

## 1. build.py — data/dashboard.json (new output)

### spotlight (array, one entry per ref)
For each ref: slug, name, career games, span, active flag, and one precomputed "signature line" — the stat where this ref deviates most from the all-ref mean (home win rate, combined FTA, combined PF, OT rate, avg total points; min 200 games, else fall back to a tenure line). Descriptive register, rank context included.

### records (array of extremes for the strip)
Highest/lowest home team win% (min 200 games), highest combined FTA/game ("busiest whistle," never "most biased"), most OT games, most career games, longest tenure span, most playoff games, most games by an active official. Each: label, value, ref name + slug, n.

### history (array for the history strip)
Top 10 single-game scoring performances in the dataset (crew + player + date + matchup), most frequent crew trio ever (reuse the existing crewmates computation), 3-5 fixed curiosity entries.

### date_index (object keyed MM-DD)
Highest-scoring performance officiated on each calendar date across all seasons. Client JS shows today's entry; dates with no games fall back to the nearest prior date, precomputed so JS stays dumb.

## 2. build.py — officiating quality score (extends existing leaderboards.json, not a new file)

For every ref, for every game with a known round (from label_rounds — this includes the 13 2000-01 games with round kept but game_num nulled; round is all this needs): R1=1pt, R2=2pts, R3=4pts, R4=8pts.

- `quality_total` = sum across career.
- `quality_per_season` = quality_total / seasons_active. Apply a minimum of 3 seasons_active for this specific ranking (not for the total ranking) — otherwise a single lucky rookie-season Finals assignment tops the list on n=1, the same small-sample distortion the site guards against everywhere else.

Add two new categories to the existing leaderboard tab system (reuse the current UI, don't build a new one): "Playoff weight — career total" and "Playoff weight — per season" (with seasons_active shown as n on the per-season table). Each row links to the ref page as usual.

## 3. render_pages.py + assets — the dashboard itself

Index page gains, above the existing directory (directory and leaderboards stay, moved down):

1. **Spotlight of the day** — deterministic client-side pick: day-of-year modulo the spotlight array (ordered by slug for stable rotation). Card: name (linked), games, span, signature line, active badge.
2. **On this date** — small card from date_index.
3. **Records & oddities strip** — horizontally scannable row of small stat cards from `records`, each linking to the ref.
4. **History strip** — top scoring games (linked crew + player) and top crew trios.
5. **Tonight's Crews slot** — fetches data/tonights-crews.json; if absent or its date isn't today/yesterday (US time), renders nothing at all — no placeholder. Document the expected schema in app.js as a comment for September: `{date, games:[{away, home, tipoff_et, crew:[{name, slug}], crew_note}]}`.

The two new leaderboard categories from §2 render through the existing leaderboard tab component — no new markup pattern needed.

### Comparator — /compare/ page
New page on the standard shell. Two search boxes (reuse the global search component, refs only), side-by-side whistle profiles (RS + PO, all stats with n), career summary rows, team-records extremes for each. Shareable via `?a=scott-foster&b=tony-brothers` query string. Linked from index nav and from each ref page hero ("Compare" button prefilling `?a=`). Reads existing `data/referees/{slug}.json` — no new data needed.

## 4. Tonight's Crews probe (read-only, this round; pipeline is September's project)

New script `scripts/local/probe_nba_assignments.py`: fetch official.nba.com's daily referee assignments (discover the URL/JSON shape, don't assume it). Print HTTP status, response shape, whether crew names for the most recent games are parseable. The cloud session also attempts the same fetch once from its own environment, to record which of the two environments can reach it (decides GitHub Actions vs. local scheduled task in September). Findings to `source-data/_probe_assignments.txt`. July is off-season — the probe answers reachability and page shape even with nothing to parse yet.

## 5. QA + review gate

- `dashboard.json`: no NaN, every slug resolves, every records/history entry carries n or count.
- Quality-score leaderboard: spot-check 3 refs' totals by hand against known playoff careers.
- Spotlight rotation: print today's and tomorrow's pick as a sanity line.
- Screenshots before push: full new index (desktop + mobile), the comparator with two refs loaded, the two new leaderboard tabs, and confirmation the Tonight's Crews slot renders nothing when the JSON is absent.
- Comparator tested over HTTP (fetch-dependent), both clean load and a `?a=&b=` share URL.
