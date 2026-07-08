# NBA Referee Database — build.py Spec (Aggregation Session)

**Prerequisite:** all `source-data/` cleanup is committed. `games.csv.gz` / `officials.csv.gz` / `player_logs/*.csv.gz` now hold a mix of two id schemes, cleanly partitioned by season with zero known overlap. This spec assumes that state.

---

## 0. The source-data map (read this before writing anything)

Getting this table wrong undoes months of work. Two id schemes, structurally disjoint, never mixed within a season:

| Season(s) | games.csv.gz / officials.csv.gz | player_logs |
|---|---|---|
| 2000-01, 2001-02, 2002-03 | ESPN | ESPN |
| 2003-04 – 2011-12 | nbadb (NBA ids) | Kaggle (NBA ids) |
| 2012-13 | ESPN | ESPN |
| 2013-14 – 2022-23 | nbadb (NBA ids) | Kaggle (NBA ids) |
| 2023-24 – 2025-26 | ESPN | ESPN |

**NBA-scheme game_id:** 10 digits, starts `'00'`. **ESPN-scheme game_id:** 9 digits, either date-encoded (2000-03 era, e.g. `201101002`) or sequential (2012-13+, e.g. `401752955`). The two never collide — detect programmatically (`len==10 and startswith('00')`), never infer scheme from season alone.

**Officials coverage is real but not perfect.** nbadb-scheme seasons run ~87-92% (3-official games). ESPN RS seasons run ~97-100%. For ESPN playoff/play-in rows, the naive "3-officials=X%" metric reads artificially low (many games carry a 4th alternate) — use the `<3` count from `_freshness_espn.txt` as the real gap, not the headline percentage.

---

## 1. Hard rules (carried over from Phase 1, still apply)

1. Additive only. Full copy-paste files, not diffs.
2. No secrets, no network calls — this is pure pandas over already-committed `source-data/`.
3. String game_ids everywhere, zero-padded where NBA-scheme.
4. Confirm before `git push`. Local commits fine.
5. Show sample sizes (`n`) next to every rate stat and split. Never publish a split below its minimum-n threshold.
6. Team joins use normalized `team_abbr` (via `nba_tricodes`) — **never** `team_id`. NBA-scheme and ESPN-scheme `team_id` are different numbering systems; only the abbreviation is a safe cross-era key.

---

## 2. Load & era-tag

Load all three tables. Tag every row with `era = 'espn' if is_espn_scheme(game_id) else 'nba'`. This becomes the dispatch key for everything era-sensitive below: referee identity, alternate-official exclusion, and round/Game-7 labeling all branch on it.

Team-game stats (points, FTA, fouls, etc.): aggregate from `player_logs` grouped by `(game_id, team_abbr)`. One source of truth, not a separate team table — this was true in the original spec and remains true across both eras.

---

## 3. Referee identity reconciliation (the new problem this session has to solve)

This didn't exist in the original spec because there was only one id scheme then. Now there are two, and they don't share a key:

- **NBA-scheme official_id:** introspect the actual nbadb column first — print 5 sample values before assuming a format. Likely numeric, but verify, don't assume.
- **ESPN-scheme official_id:** `espn:firstname-lastname`, lowercase, hyphenated.

Build a canonical `ref_key`: normalized full name (lowercase, strip periods and accents, collapse whitespace, drop `Jr./Sr./II/III` suffixes for the *key* but retain the original for display). Group all rows — both eras — by `ref_key`.

**This will have false positives and false negatives.** Known risk, not hypothetical: the ESPN probe output already showed a real raw entry as `"Eddie F. Rush"` with a middle initial — if nbadb records the same person as `"Eddie Rush"`, naive exact matching splits one person into two entities. The reverse risk (genuinely different people colliding) is also real — three different Crawfords have officiated NBA games (Joe Crawford and Dan Crawford both appear in this project's own probe data as distinct people).

Handle it the way Jorge's other tools already handle this class of problem (same pattern as `PLAYER_COUNTRY_OVERRIDE` in the media vote tracker):

1. Auto-match on normalized `ref_key`.
2. Print a full audit: every `ref_key`, its constituent raw names, ids, and source era/count. This is for Jorge to skim once, not for the script to get perfectly right on the first pass.
3. Read `data/referee_identity_overrides.csv` if present (columns: `raw_name_or_id, canonical_ref_key, canonical_display_name`). Apply after auto-matching, before finalizing. Empty/absent file is fine — auto-matching is the default, overrides are the escape hatch.
4. Ship with this file empty. Fill it in only if the audit list surfaces real problems.

---

## 4. Alternate-official exclusion

- **ESPN-scheme:** confirmed rule, already documented in `_freshness_espn.txt` — for any ESPN game_id with more than 3 officials rows, only the first 3 (by row order as written) count as having officiated. The rest are alternates.
- **NBA-scheme:** unverified whether nbadb ever includes an alternate row. Check directly — max officials-rows-per-game_id for nbadb-scheme playoff rows. If it's never above 3, no action needed. If it is, apply the same first-3 rule. Verify, don't assume — this project has been burned by that exact mistake before.

---

## 5. Playoff round / Game 7 labeling

- **NBA-scheme:** parse from game_id per the original spec (digit 8 = round, digit 10 = game number). Verify against 3 known games before trusting.
- **ESPN-scheme:** not derivable from the id at all — `_freshness_espn.txt` already flags this as intentionally skipped. **Known Phase-1 gap:** Finals/Game-7 counts will be incomplete for 2000-03, 2012-13, and 2023-26 specifically, until a Phase 2 fix (possibly extractable from the ESPN summary payload directly — unverified, worth one probe call later, not blocking now). State this plainly on the site rather than hide it.

---

## 6. Aggregation logic (unchanged from the original Phase 1 spec)

Everything in the original spec's §4 — whistle profile, team records, top performances, player swings (15-game minimum threshold), notable games — applies as written. None of those definitions need to change. Only the join layer beneath them does: they now operate over an era-unified, identity-reconciled dataset instead of a single clean source.

---

## 7. QA gate (extends the original)

- Zero game_ids appear in both id schemes for the same real game (should already be true post-dedupe; this is a regression guard, not a one-time check).
- Every `team_abbr` resolves through `nba_tricodes`'s allow-list.
- Print the full referee-identity audit list from §3.
- Spot-check total career games for the same veteran refs used in the original spec (Scott Foster, Tony Brothers, James Capers), **plus** one referee whose raw data spans both eras (several names — Bill Spooner, Joe Crawford, Steve Javie, Dan Crawford, Bennett Salvatore — already appear in both the 2000-03 probe output and would plausibly still be active post-2003; pick one, confirm their combined cross-era game count looks sane before publishing).

---

## 8. Kickoff prompt for Claude Code browser

> Read docs/PHASE1_SPEC.md and this build spec in full, plus source-data/_freshness_espn.txt and source-data/_freshness.txt for the exact per-source notes already documented. Write scripts/build.py per this spec, on a fresh branch off main. Pay special attention to §3 (referee identity reconciliation) and §4 (alternate-official exclusion) — these are the two places a wrong assumption silently corrupts output rather than crashing. Print the full referee-identity audit list and the QA spot-checks from §7 before finishing. Local commit, confirm before push.
