"""
nba_tricodes.py  --  shared tricode normalization (imported by the local scripts).

Single source of truth so nbadb, ESPN, and Kaggle inputs converge on ONE tricode
scheme. Maps alternate abbreviation schemes (ESPN's GS/SA/NY/PHO/WSH/... and any
other source that uses two-letter or non-standard codes for CURRENT franchises)
to the 30 current NBA tricodes.

Genuinely historical / relocated tricodes (SEA, NJN, NOH, NOK, VAN, CHH, ...) are
INTENTIONALLY passed through unchanged -- they are real, distinct historical teams
and must not be rewritten to a current franchise code.

This file was factored out of fetch_espn_seasons.py's inline table so that
ingest_kaggle_player_logs.py can reuse the exact same mapping.
"""

# The 30 current NBA franchises' tricodes (the canonical target scheme).
VALID_TRICODES = {
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

# Real, distinct HISTORICAL / relocated franchises' tricodes (frozen identities,
# per the docstring) that existed in the fetched eras but are NOT among the 30
# current tricodes. Any caller that filters on VALID_TRICODES to drop exhibitions
# (All-Star, etc.) MUST also allow these, or it will silently discard legitimate
# defunct-team games (e.g. the 2000-03 Nets / Sonics / Grizzlies / Hornets).
HISTORICAL_TRICODES = {"SEA", "NJN", "NOH", "NOK", "VAN", "CHH"}

# Full franchise display names for the 30 current tricodes plus the frozen
# historical ones in HISTORICAL_TRICODES. Used for display only (the tricode
# stays the canonical join key). CHH is disambiguated from the current CHA
# ("Charlotte Hornets") by its era, per the frozen-identity convention.
TEAM_DISPLAY_NAMES = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
    # Frozen historical / relocated identities (HISTORICAL_TRICODES).
    "SEA": "Seattle SuperSonics", "VAN": "Vancouver Grizzlies", "NJN": "New Jersey Nets",
    "CHH": "Charlotte Hornets (1988-2002)", "NOH": "New Orleans Hornets",
    "NOK": "New Orleans/Oklahoma City Hornets",
}


def display_name(abbr):
    """Full franchise name for a tricode; falls back to the tricode itself if
    the code isn't recognized (so nothing ever renders blank)."""
    if abbr is None:
        return ""
    return TEAM_DISPLAY_NAMES.get(str(abbr).strip().upper(), str(abbr))

# Alternate abbreviation -> canonical NBA tricode. Identity entries are kept so a
# value that is already canonical resolves to itself. Anything NOT present here is
# passed through unchanged (see to_nba_tricode) -- that is how historical tricodes
# survive untouched.
ESPN_TO_NBA_ABBR = {
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "BRK": "BKN", "CHA": "CHA",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN", "DET": "DET",
    "GS": "GSW", "GSW": "GSW", "HOU": "HOU", "IND": "IND", "LAC": "LAC",
    "LAL": "LAL", "MEM": "MEM", "MIA": "MIA", "MIL": "MIL", "MIN": "MIN",
    "NO": "NOP", "NOP": "NOP", "NY": "NYK", "NYK": "NYK", "OKC": "OKC",
    "ORL": "ORL", "PHI": "PHI", "PHX": "PHX", "PHO": "PHX", "POR": "POR",
    "SAC": "SAC", "SA": "SAS", "SAS": "SAS", "TOR": "TOR", "UTAH": "UTA",
    "UTA": "UTA", "WSH": "WAS", "WAS": "WAS",
    # Historical alias (frozen identity, NOT a modern team): ESPN's early-2000s
    # payloads abbreviate the New Jersey Nets as 'NJ'; the already-ingested Kaggle
    # set uses 'NJN' for the same franchise. Converge both on 'NJN' -- same
    # frozen-identity treatment as SEA/VAN/CHH (do NOT remap to modern BKN).
    "NJ": "NJN",
}

# Readable alias for generic (non-ESPN-specific) use.
ABBR_ALIASES = ESPN_TO_NBA_ABBR

_warned = set()


def to_nba_tricode(abbr, warn=False):
    """Normalize one abbreviation to a canonical NBA tricode.

    Known aliases (GS->GSW, SA->SAS, PHO->PHX, WSH->WAS, ...) are mapped; anything
    else is upper-cased and passed through unchanged (historical tricodes stay as
    they are). Pass warn=True to print once per unrecognized code.
    """
    if abbr is None:
        return ""
    a = str(abbr).strip().upper()
    if not a:
        return ""
    if a in ABBR_ALIASES:
        return ABBR_ALIASES[a]
    if warn and a not in _warned:
        _warned.add(a)
        print("  ! abbreviation '{}' not in the alias map -- passing through unchanged".format(a))
    return a
