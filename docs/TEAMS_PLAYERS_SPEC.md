# NBA Referee Database — Team & Player Pages Spec

**Goal:** every team and player name on a referee page becomes a link to that team's or player's own page, showing how they performed with each referee. The inverse view of the existing ref pages.

**Prerequisite:** Round C (full team names, nav header, black links) is merged. This round touches both `scripts/build.py` and `scripts/render_pages.py`, so it must not run concurrently with any other session editing those files.

**Hard rules:** all standing rules apply (additive, string ids, n visible everywhere, min-n thresholds enforced, descriptive-only editorial language, confirm before push).

---

## 1. build.py additions (new outputs, nothing existing changes)

### data/teams/{tricode}.json — one per franchise (~36: 30 current + 6 frozen historical)

- `summary`: tricode, display name, seasons covered, total games in dataset
- `ref_records`: for every referee with ≥10 games of this team: canonical ref name + slug, games, W-L, win%, home/away split, avg margin (signed, team perspective), each with n. Sorted by games desc.

Frozen historical franchises (SEA, VAN, NJN, CHH, NOH, NOK) get their own pages, consistent with the existing frozen-identity treatment. No merging into modern successors.

### data/players/{player_key}.json — one per qualifying player

- Qualifies: any player with ≥15 games under at least one referee (i.e., anyone who already appears in at least one ref page's player-splits table).
- `summary`: display name, seasons covered, teams played for (tricodes), total games in dataset
- `ref_splits`: for every referee with ≥15 shared games: ref name + slug, n, pts/reb/ast with that ref, season-weighted baseline, swings (same method as ref pages, computed once and reused — the numbers on a player page and the corresponding ref page MUST be identical, so derive both views from one computation, not two implementations)
- `top_games`: this player's 10 best scoring games in the dataset, with which crew worked each

### Player identity across eras (the hard part — read carefully)

Player ids differ by scheme: NBA player ids in Kaggle/nbadb-era logs, ESPN athlete ids in ESPN-era logs (2000-03, 2012-13, 2023-26). Reconcile by name, with two rules that differ deliberately from the referee pipeline:

1. **Keep suffixes in the identity key.** Do NOT strip Jr./Sr./II/III/IV the way the ref pipeline does. Tim Hardaway Sr. and Jr., Gary Payton and Payton II, Larry Nance and Nance Jr. are different people whose careers both touch this dataset's eras.
2. **Adjacency-gated merging.** Same-key segments from different id schemes merge ONLY if their season ranges are adjacent or overlapping (gap ≤ 1 season). A same-name pair with a multi-season gap stays split (this is what separates Jaren Jackson the elder, ESPN era 2000-02, from Jaren Jackson Jr., nbadb era 2018+, even when a source drops the suffix).
3. **Audit output:** print (a) every cross-era merge performed, with the season ranges of each segment, and (b) every same-key pair left UNmerged due to the gap rule. The list will be long; write it to `source-data/_player_identity_audit.txt` rather than only console. Reuse the overrides-file pattern: `data/player_identity_overrides.csv`, same columns/semantics as the referee one, shipped empty.
4. Slug collisions across distinct players: suffix `-2`, `-3` by first-appearance order, and note them in the audit.

### QA gate additions

- Every player-splits row on every ref JSON has a matching, numerically identical row in that player's JSON (spot-check 20 random pairs programmatically, fail loudly on mismatch).
- Every team-records row on every ref JSON reconciles with the team's JSON the same way.
- No player page with zero qualifying ref splits (the ≥15 threshold means qualification guarantees ≥1 row).

## 2. render_pages.py additions

- `/team/{tricode}/index.html` and `/player/{slug}/index.html`, same shell as ref pages: nav header, search boxes top and bottom, footer, unique SEO title/meta ("How the Boston Celtics perform with every NBA referee", "LeBron James stats by referee").
- Ref pages: linkify team names in team-records tables → team pages; linkify player names in player-splits and top-performances tables → player pages (only when the target page exists; otherwise plain text, no dead links).
- Team/player pages: linkify every referee name back to the ref page. Full cross-navigation both directions.
- Heat scale (existing CSS classes) applied to swings and margins on the new pages identically.
- Each new page carries one short methods line: swings are descriptive comparisons to the player's own season averages, not causal claims — same register as the existing editorial line, no new hedging.
- Gap-disclosure note appears on team/player pages only where playoff-detail claims are made (top_games has no round labels, so likely only the team pages' playoff splits need it, if rendered).

## 3. Scale expectations (so nobody is surprised)

~2,500-3,000 player pages plus 36 team pages, added to 159 ref pages. Comparable to the draft-combine tool's page count; fine for GitHub Pages. The repo grows by roughly 30-60MB of HTML+JSON; acceptable. Generation time in the cloud session may be a few minutes; that's normal.

## 4. Kickoff prompt

> Read docs/PHASE1_SPEC.md, docs/BUILD_SPEC.md, and docs/TEAMS_PLAYERS_SPEC.md in full. Fresh branch off main. Implement the team and player pages per docs/TEAMS_PLAYERS_SPEC.md: build.py additions first (including the player identity rules in §1 exactly — suffix-preserving keys and adjacency-gated merging), run the build, review the player identity audit and QA gate output with me before proceeding to the render changes. Then render_pages.py additions per §2, regenerate everything, and show me screenshots of one team page, one player page, and a ref page with the new links before any push. Local commit, confirm before push.
