# NBA Referee Database — render_pages.py Spec (Site Session)

**Prerequisite:** `data/` is built and committed — 162 referees confirmed, `data/referees.json`, `data/leaderboards.json`, `data/referees/{slug}.json` × 162, `data/referee_identity_overrides.csv`. Verified directly, not just from the build log.

**This is an addendum to `docs/PHASE1_SPEC.md` §5, not a replacement.** That section's page structure, styling direction, and vanilla-JS constraint are still exactly right and still apply. This document covers only what's changed or been learned since it was written.

---

## 1. Scope for this session

Build: `index.html`, `referee/{slug}/index.html` × 162, shared `assets/` (css/js). Leaderboards are a tab/section on the index page, per the original spec — not a separate URL this session.

**Out of scope, still** (per PHASE1_SPEC.md's own boundary): PBP foul attribution, L2M accuracy layer, video tools, automated daily updates. Don't let 9 PRs of accumulated context tempt scope creep here — this session renders what `build.py` already produced, nothing more.

---

## 2. What's changed since PHASE1_SPEC.md §5 was written

### Attribution (was 1 source, now genuinely 3)

The original footer text only credited nbadb, because at the time that was the only source. It's no longer accurate. Credit all three, linked:

- Wyatt Walsh's NBA Database (Kaggle, CC BY-SA 4.0)
- ESPN's public API (games, officials, and player logs for 2000-01/01-02/02-03, 2012-13, 2023-24 onward)
- szymonjwiak's NBA traditional box scores (Kaggle) — player logs for the nbadb-only seasons

### Known-gap disclosure (new — this didn't exist when §5 was written)

`BUILD_SPEC.md` §5 documents that playoff round / Game 7 labeling is unavailable for ESPN-scheme seasons (2000-01, 2001-02, 2002-03, 2012-13, 2023-24 through 2025-26). Concretely:

- **Per-ref pages:** if a referee's career overlaps any of those seasons, don't present `notable_games` as if it's a complete Finals/Game-7 list. Add one short, factual line near that section — no apology-hedging, no throat-clearing, just the fact: something like "Round and Game 7 detail isn't available for [the overlapping era(s)]; playoff appearances from those seasons may be missing from this list." State it once, plainly, and move on.
- **Leaderboards:** "Most Game 7s" and "Most Finals games" specifically will undercount for anyone active during the gap seasons. One footnote on those two categories, not scattered everywhere else.

This is a real, disclosed limitation, not a bug to hide. Say it once, factually, and the product is still honest.

### Editorial framing — reaffirmed, not new

Same rule as the original spec: descriptive facts, no language implying a referee causes outcomes or favors a team, no betting framing. Applies to every piece of generated copy on the site, not just editorial articles — this is Jorge's standing standard for the whole product.

---

## 3. Local review gate (hard stop before Pages goes live)

Per the original run order: generate pages, review a **local** render of at least these before touching GitHub Pages settings:

1. `index.html`
2. One high-volume ref (Scott Foster — 1747 games, 2000-01 to 2025-26, the best test of cross-era rendering)
3. One ref whose career sits entirely within a single scheme (e.g., someone who started well after 2003 and has no ESPN-era games at all)
4. One ref who overlaps a gap-disclosure season, to confirm that note actually renders correctly and isn't silently dropped

Do not enable Pages or push straight to a live URL before this review happens.

---

## 4. Enabling GitHub Pages

This is a repo **setting**, not something reachable through a normal push/PR — Settings → Pages → Deploy from a branch → `main` / `(root)`, since `index.html` lives at repo root, not in a `docs/` folder. The Claude GitHub App's permissions for this are unverified (Pages settings are a different scope than Contents/PRs, and this project has hit exactly this kind of permission gap before). Budget for the possibility that this one step needs Jorge doing it manually in the browser, same as granting the app repo access did earlier in this project.

---

## 5. Kickoff prompt for Claude Code browser

> Read docs/PHASE1_SPEC.md (especially §5) and docs/RENDER_SPEC.md in full before writing anything. Also check the frontend-design skill for execution quality and consistency with the other jsierrahoopshype.github.io tools. Write scripts/render_pages.py per both documents, on a fresh branch off main. Generate the actual output locally and show me a rendered sample (Scott Foster's page, the index, and one gap-season-disclosure case) before proposing to enable GitHub Pages — that's a hard gate, not a suggestion. Local commit, confirm before push.
