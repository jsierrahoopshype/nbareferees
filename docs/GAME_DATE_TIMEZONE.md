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
| `scripts/local/eastern_time.py` | UTC instant → Eastern game date, no timezone database, self-tested |
| `scripts/local/fetch_espn_seasons.py` | root cause fixed, and `--dates-only` recovers dates without re-fetching anything else |
| `scripts/build.py` | hard QA gate: no local-dated game may fall on December 24 |

### Eastern time without a timezone database

`scripts/local/eastern_time.py` implements the conversion from the rule rather
than through `zoneinfo` or `tz_convert`, because both fail on the machine these
scripts run on. `ZoneInfo("America/New_York")` needs the IANA database, which
Windows does not ship. pandas 3 resolves zone names through zoneinfo rather
than the pytz it used to bundle, so `tz_convert` inherits that — **and
`"US/Eastern"` is a legacy pytz alias zoneinfo does not carry at all**, so it
raises `ZoneInfoNotFoundError` even where the database is present.

That exact combination shipped once here, inside a `try/except` whose fallback
was the UTC date, so the fix silently reinstated the defect it was written to
fix. It was caught by a test, not by reading. The module now raises instead of
guessing, and the fetcher records the failure and marks the row
`date_is_local=0` rather than claiming a date it could not compute.

It also implements **both** US daylight-saving rules — first Sunday in April to
last Sunday in October for 1987-2006, second Sunday in March to first Sunday in
November from 2007 — because this repo starts at 1993-94 and the wrong rule
moves a date whenever a tip lands within an hour of midnight Eastern.

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

## `--dates-only`: the remaining 13,433 rows

`1993-94..2002-03`, `2012-13`, and 57 games in 2023-26 the bridge could not
reach. No timestamp for them exists in the repo, so their dates can only come
back from ESPN — but they do **not** need a full re-fetch. `parse_game_row()`
reads the date off the **scoreboard** event, before any summary call; the
summary call exists only for officials and player box scores.

| | calls | at the 1s delay |
|---|---|---|
| `--dates-only` (scoreboard only) | ~2,602 days | **~43 min** |
| Full re-fetch (+1 summary per game) | ~15,500 | ~4.3 h |

```
python scripts\local\fetch_espn_seasons.py --dates-only            # dry run
python scripts\local\fetch_espn_seasons.py --dates-only --apply
python scripts\local\fetch_espn_seasons.py --dates-only --seasons 2023-24,2024-25,2025-26 --apply
python scripts\build.py
python scripts\render_pages.py
```

### It cannot drop a row

This is a **field update, not a row replacement**, and that is the whole safety
argument. `merge_rows()` replaces whole rows and would lose a game if ESPN's
archive came back thinner than what we hold — for the 1990s a real
possibility. `--dates-only` instead walks the existing extract and overwrites
two fields, `game_date` and `date_is_local`, on rows it matched by `game_id`.
A game the walk misses keeps its row, keeps its old date, and keeps
`date_is_local=0`, so it stays visibly unrepaired rather than vanishing. The
row count cannot change.

It also **refuses any recovered date that is not 0 or 1 day before the stored
one**. The stored date is a UTC date, so the true local date can only be the
same day or the day before; anything else is not a rollover and is reported
rather than written.

Before merging it prints a **per-season count comparison**, re-walk against
what the extract holds, flagging every season that differs.

### The progress-file trap, and why this sidesteps it

`_espn_progress.json` records all 2,602 of those dates as `dates_done`, so a
plain `main()` re-run skips every one, prints success and changes nothing.

`--dates-only` keeps its own loop and **never reads that file**, so the trap
cannot fire. It also does not clear those keys, because clearing would tell a
future full fetch that 2,602 completed days need doing again — 4.3 hours of
summary calls to repair extracts that were never broken. `--reset-progress`
does that explicitly if a full re-fetch is ever genuinely wanted.

### ESPN's 1990s timestamps have no seconds

The first real `--dates-only` run parsed nothing. ESPN's 1990s archive emits

```
1993-11-06T00:30Z        17 characters, no seconds
```

where the modern feed emits `2023-10-25T02:00:00Z`. `parse_utc()` opened with
`if len(s) < 19: return None`, so every 1990s timestamp was rejected before it
was read. The module behaved exactly as designed — refused rather than guessed,
kept the UTC date, marked the row `date_is_local=0` — and recovered nothing
across all 13,376 rows.

The parser is now one regex covering every shape these feeds emit: seconds
optional, `T` or space separator either case, fractional seconds of any width
with `.` or `,`, and a zone that may be `Z`, `z`, `+HH:MM`, `-HHMM`, `+HH` or
absent. Each variant has its own self-test case.

Two values are still refused, deliberately:

- **Date-only** (`1993-11-06`). No time of day means no instant, so there is no
  way to know which local date it belongs to. Defaulting to midnight would
  produce a date that is sometimes right and sometimes a day out — the exact
  class of bug this module exists to prevent. `game_date()` now says *why* it
  refused, because a date-only value and a malformed one need different answers.
- **Anything malformed**: month 13, November 31, hour 25, an unknown zone letter.

A timestamp with **no zone at all** is read as UTC. The field is UTC by
contract and every observed sample carries `Z`; refusing it would risk another
43-minute walk that recovers nothing over a shape almost certainly UTC anyway.

### The shape census

Rather than assume which shapes exist across 30 seasons, `--dates-only` counts
them and prints them before merging anything:

```
TIMESTAMP SHAPES SEEN  (digits blanked; refused shapes recover nothing)
  9999-99-99T99:99Z         1314 seen  all parsed
                                 e.g. 2012-10-31T00:30Z
```

A shape that will not parse is printed as `*** n REFUSED ***`, with a real
example. That turns "the walk recovered nothing" from a mystery costing a run
into a line naming the variant to add.

### ESPN's 403, and the User-Agent

`get_json()` sends **no User-Agent header**, so urllib adds its own
`Python-urllib/3.x`. That is not an oversight to tidy up later.

This file originally carried a Chrome 120 User-Agent string and made thousands
of successful calls with it. ESPN's CDN has since started fingerprinting: a
request that claims to be Chrome without a real Chrome's TLS and header
fingerprint now gets **403 on every call**. Tested from the machine these
scripts run on, same endpoint, same minute:

| request | result |
|---|---|
| urllib default User-Agent | 200 |
| same request, Chrome User-Agent | 403 |

Pasting a browser User-Agent back in is what causes the 403, not what fixes it.

Two things also changed so this diagnoses itself next time: `get_json()` puts
the **HTTP status in the error** (the old message said only "GET failed", which
read like a network problem for a whole run), and it does **not retry** 401,
403, 404 or 451, which cannot change on a retry. `--dates-only` aborts after 5
consecutive failures with nothing recovered, rather than walking all 2,602 days
to discover the same 403 one day at a time.

### Risks

- ESPN's 1990s archive depth was confirmed by `_probe_rounds_reach.txt`, but a
  re-walk could still return fewer games than we hold. The count comparison
  reports it; those games keep their old dates and stay `date_is_local=0`.
- `game_id` must come back identical or a game simply will not match. That
  shows up as "not found in walk", never as a duplicate or a loss.
- Everything downstream keyed on date shifts by a day for most of these rows:
  the on-this-date card, game logs, notable games and swings all move. That is
  the point, but it is a visible change across ~13,000 games.

## What reads game_date

Affected: the index's most-recent-crews date, the on-this-date card, referee
game logs, notable games, swings, and the recent-form `cal30` window at its
boundary plus `days_since_last_game`.

Not affected, despite looking date-shaped: `era` is keyed on the id scheme, and
debuts/farewells on season labels — a one-day shift cannot move a game out of
its season.
