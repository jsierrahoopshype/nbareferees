# game_date: UTC instant vs local game date

## The defect

`source-data/games.csv.gz` dated every ESPN-sourced game by the **UTC instant of
tip-off**, not by the local calendar date the game was played on.

One line in `scripts/local/fetch_espn_seasons.py` caused it. ESPN's
`event.date` is a UTC instant, and it was being formatted in UTC:

```python
game_date = pd.to_datetime(raw_date, utc=True, ...).strftime("%Y-%m-%d")
```

An 8:00pm EST tip is already midnight UTC, so it filed under the following day.

**Scope: 17,349 rows across 14 seasons** — every ESPN-scheme row.
1993-94..2002-03, the isolated 2012-13, and 2023-24..2025-26. The 19
NBA-scheme seasons (2003-04..2022-23, minus 2012-13) came from stats.nba.com,
which publishes a local `GAME_DATE`, and are correct.

This is **not** a handful of games. `probe_game_date_timezone.py` fits the
per-season rollover rate at **0.71 to 1.00**. Under standard time a routine
7:00pm tip rolls over in *every* zone in the league, Eastern included — the
"late Pacific games" intuition only holds from March to November.

## Evidence

Four independent angles, in `source-data/_probe_game_date_timezone.txt`:

1. **Scheme census.** The defect falls exactly on the feed boundary.
2. **Christmas.** Every NBA-scheme season has 0 games on Christmas Eve, a
   league-wide off day; every ESPN-scheme season has 3–14. And the 2023
   Christmas slate resolves game by game from its published tip times — the
   three afternoon games stay on the 25th, the 8:00pm and 10:30pm ET games
   land on the 26th, 5 of 5 as predicted. Only a UTC rollover splits one day's
   slate by tip time.
3. **Weekday signature** across scheme boundaries. All four adjacent-season
   pairs fit better against the neighbour shifted a day forward. The lone
   2012-13 ESPN season between two NBA ones: 1.87 shifted vs 8.82 as-is.
4. **The game-id bridge.** `games.csv.gz` agrees with the UTC-dated source on
   3,916 of 3,916 games and disagrees with the local-dated source on 22 of 25.

## Why Eastern, and why no per-arena timezone table

No NBA game tips after midnight ET — the latest start is about 10:30pm ET,
which is 7:30pm on the west coast. So for **every** game in the league the
Eastern calendar date and the home arena's calendar date are the same day.
Converting the UTC instant to Eastern therefore yields the arena-local date
without knowing where the arena is, and it matches what stats.nba.com's
`GAME_DATE` already holds, putting both eras of the file on one convention.

A per-team table would also have to track relocations and Arizona's lack of
daylight saving. This avoids all of it.

## What has shipped

| | |
|---|---|
| `scripts/local/probe_game_date_timezone.py` | read-only diagnosis, writes `_probe_game_date_timezone.txt` |
| `scripts/local/fix_game_dates_espn.py` | corrects 2023-26 from cached `cdnnba` `timeActual` via the bridge |
| `scripts/local/fetch_espn_seasons.py` | root cause fixed: converts to `US/Eastern`, and emits `date_is_local=1` |
| `scripts/build.py` | hard QA gate: no local-dated game may fall on December 24 |

### The `date_is_local` column

`1` means `game_date` is the local date the game was played on. `0` (or empty,
in a pre-fix extract) means it has not been verified and may still be a UTC
date. NBA-scheme rows are `1`; ESPN-scheme rows become `1` only once corrected.

### The Christmas Eve gate

Hard-fails the build if any row claiming `date_is_local=1` falls on December 24.
Rows not yet marked local are counted and printed as `[soft]` instead, so the
1993-2003 and 2012-13 blocks do not block every build before they can be fixed
— a gate that has to be switched off is a gate that gets deleted.

This gate costs nothing and would have caught the defect years ago.

## Running the 2023-26 fix

Needs `source-data/_nba_data_cache` populated with the `cdnnba_*` archives
(gitignored; `build_game_id_bridge.py` downloads them).

```
python scripts\local\fix_game_dates_espn.py --self-test   # date arithmetic only
python scripts\local\fix_game_dates_espn.py               # dry run, writes nothing
python scripts\local\fix_game_dates_espn.py --apply       # rewrites games.csv.gz
python scripts\build.py
python scripts\render_pages.py
```

Dry run by default. `--apply` writes through a temp file, reads it back before
replacing, keeps `games.csv.gz.bak`, and **refuses to write if it corrected
nothing** — so a missing cache cannot quietly downgrade the extract.

Tip-off is the **minimum** `timeActual` in a game, not the first row seen and
not the maximum: the maximum is the final buzzer, which for a 10:30pm ET start
is after midnight Eastern and would reintroduce the off-by-one.

## Option (1): the remaining 13,376 rows

`1993-94..2002-03` and `2012-13`. No timestamp for these games exists anywhere
in the repo, so they cannot be corrected from cached data. They need
`fetch_espn_seasons.py` re-run — now that its date handling is fixed.

**This is much cheaper than a full re-fetch, because the date comes from the
scoreboard, not the per-game summary.** `parse_game_row(ev, label)` is called
with the scoreboard event, before any summary call. The summary call exists
only for officials and player box scores.

| | calls | at the 1s delay |
|---|---|---|
| Scoreboard walk only (dates) | ~2,602 days | **~43 min** |
| Full re-fetch (+1 summary per game) | ~15,500 | ~4.3 h |

### What it actually involves

1. **Clear the resume state.** `source-data/_espn_progress.json` records all
   2,602 of those dates as `dates_done`, so a plain re-run skips every one of
   them. Those keys must be removed first. This is the step most likely to be
   missed — without it the re-run appears to succeed and changes nothing.
2. **Decide date-only or full.** A date-only pass needs a flag that skips the
   summary call and merges only `game_date` and `date_is_local` on `game_id`,
   leaving officials and player logs untouched. That flag does not exist yet
   and is the only new code option (1) needs. A full re-fetch needs no new
   code but costs 4.3 hours and rewrites officials and player logs, which are
   not broken.
3. **Watch the 1998-99 window.** The lockout season runs Feb 1 – Jun 30 1999,
   already special-cased in `SEASONS`.
4. **Verify by the same signature.** After the run, Christmas Eve games in
   those seasons must be 0 and the build's hard gate will enforce it, since
   the rows now arrive marked `date_is_local=1`.

### Risks

- ESPN's archive depth for the 1990s was confirmed by
  `_probe_rounds_reach.txt`, but a re-walk could return fewer games than the
  current extract holds. Compare counts per season before merging; the
  `game_id` key means a short return replaces nothing, but a silently
  incomplete season is worth catching.
- `game_id` values must come back identical, or the merge adds duplicates
  instead of replacing. `drop_duplicates(["game_id"], keep="last")` covers the
  replace case only if the ids match exactly.
- Everything downstream keyed on date shifts by a day for most of these rows:
  the on-this-date card, game logs, notable games and swings all move. That is
  the point, but it is a visible change across 13,376 games.

## What reads game_date

Affected: the index's most-recent-crews date, the on-this-date card, referee
game logs, notable games, swings, and the recent-form `cal30` window at its
boundary plus `days_since_last_game`.

Not affected, despite looking date-shaped: `era` is keyed on the id scheme, and
debuts/farewells on season labels — a one-day shift cannot move a game out of
its season.
