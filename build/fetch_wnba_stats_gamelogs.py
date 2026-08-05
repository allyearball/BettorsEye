"""
Primary gap-filler attempt: stats.wnba.com's playergamelogs endpoint, which can return every
player's box-score line for an entire date range in ONE call (DateFrom/DateTo), instead of the
one-call-per-game approach fetch_espn_recent_boxscores.py needs. Falls back to that script if
this one fails for any reason \u2014 see build.py for the try-this-then-that-then-give-up chain.

Honesty note, same as the ESPN script: stats.nba.com/stats.wnba.com are well-known for requiring
specific spoofed headers to respond at all, and have a documented history of blocking
datacenter/cloud IP ranges \u2014 the same category of problem that already blocked the ESPN
attempt from GitHub Actions. I was not able to test this endpoint's actual reachability from
this environment (it's not on my sandbox's allowed domains, and it's not something a web search
surfaces a directly fetchable URL for). This is built from the endpoint's documented, standard
shape (confirmed via sportsdataverse-py's own endpoint reference), with loud per-step warnings
rather than silent failure, so the first real Actions run will tell us definitively whether it
works \u2014 check the logs for `WARNING:` lines from this script specifically if it doesn't.
"""
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests

STATS_URL = "https://stats.wnba.com/stats/playergamelogs"
LEAGUE_ID_WNBA = "10"

# stats.nba.com/stats.wnba.com reject requests without a browser-like header set \u2014
# this is the standard set documented by nba_api/wehoop for reaching these hosts at all.
HEADERS = {
    "Host": "stats.wnba.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.wnba.com",
    "Referer": "https://www.wnba.com/",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}

# Column renames from the endpoint's PascalCase-ish headers to this pipeline's existing
# lowercase/underscore convention, matching what make_app_data.py already expects.
COLUMN_MAP = {
    "PLAYER_ID": "athlete_id", "PLAYER_NAME": "athlete_display_name",
    "TEAM_ID": "team_id", "TEAM_ABBREVIATION": "team_name",
    "GAME_ID": "game_id", "GAME_DATE": "game_date",
    "MIN": "minutes", "FGM": "field_goals_made", "FGA": "field_goals_attempted",
    "FG3M": "three_point_field_goals_made", "FG3A": "three_point_field_goals_attempted",
    "FTM": "free_throws_made", "FTA": "free_throws_attempted",
    "OREB": "offensive_rebounds", "DREB": "defensive_rebounds", "REB": "rebounds",
    "AST": "assists", "STL": "steals", "BLK": "blocks", "TOV": "turnovers",
    "PF": "fouls", "PTS": "points", "PLUS_MINUS": "plus_minus", "MATCHUP": "matchup",
}
ESPN_TEAM_ID_TO_SHORT = {
    20: "Dream", 19: "Sky", 18: "Sun", 3: "Wings", 129689: "Valkyries",
    5: "Fever", 17: "Aces", 6: "Sparks", 8: "Lynx", 9: "Liberty",
    11: "Mercury", 132052: "Fire", 14: "Storm", 131935: "Tempo", 16: "Mystics",
}


def fetch_wnba_stats_gamelogs(after_date_str, through_date_str, season_year):
    """after_date_str exclusive, through_date_str inclusive, both 'YYYY-MM-DD'. season_year is
    the season to query (e.g. '2026'). Returns a DataFrame in the main box CSV's column shape,
    or an empty DataFrame (never raises) if the endpoint doesn't respond as expected."""
    date_from = (datetime.strptime(after_date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%m/%d/%Y")
    date_to = datetime.strptime(through_date_str, "%Y-%m-%d").strftime("%m/%d/%Y")

    params = {
        "LeagueID": LEAGUE_ID_WNBA, "Season": season_year, "SeasonType": "Regular Season",
        "DateFrom": date_from, "DateTo": date_to,
        "PlayerID": "", "TeamID": "", "Outcome": "", "Location": "",
        "Month": "0", "SeasonSegment": "", "DateFrom_nullable": "", "OppTeamID": "",
        "VsConference": "", "VsDivision": "", "GameSegment": "", "Period": "0",
        "LastNGames": "0", "MeasureType": "Base", "PerMode": "Totals", "PORound": "0",
        "ShotClockRange": "",
    }
    try:
        resp = requests.get(STATS_URL, params=params, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
        result_sets = payload.get("resultSets") or payload.get("resultSet")
        if isinstance(result_sets, dict):
            result_sets = [result_sets]
        if not result_sets:
            print("WARNING: stats.wnba.com playergamelogs returned no resultSets \u2014 unexpected shape.", file=sys.stderr)
            return pd.DataFrame()
        rs = result_sets[0]
        headers_list = rs.get("headers", [])
        rows = rs.get("rowSet", [])
        if not rows:
            print(f"stats.wnba.com gap-fill: no rows returned for {date_from} to {date_to} (nothing new, or endpoint format changed).")
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=headers_list)
    except Exception as e:
        print(f"WARNING: stats.wnba.com playergamelogs request failed: {e}", file=sys.stderr)
        return pd.DataFrame()

    try:
        df = df.rename(columns=COLUMN_MAP)
        keep_cols = [c for c in COLUMN_MAP.values() if c in df.columns]
        df = df[keep_cols].copy()
        # team_name currently holds the short abbreviation from TEAM_ABBREVIATION (e.g. 'LAS')
        # via the rename above \u2014 map that through the same short-name convention as the rest
        # of this pipeline, falling back to whatever string came back if it's unrecognized.
        # (TEAM_ID -> short name is more reliable than the abbreviation string, so prefer that.)
        if "team_id" in df.columns:
            df["team_name"] = df["team_id"].map(ESPN_TEAM_ID_TO_SHORT).fillna(df.get("team_name"))
        if "game_date" in df.columns:
            df["game_date"] = pd.to_datetime(df["game_date"]).dt.strftime("%Y-%m-%d")
        if "minutes" in df.columns:
            df["minutes"] = pd.to_numeric(df["minutes"], errors="coerce")
        df["did_not_play"] = False
        df["starter"] = None  # not provided by this endpoint; left unknown rather than guessed
        print(f"stats.wnba.com gap-fill: {len(df)} player-rows, dates {df['game_date'].min()} to {df['game_date'].max()}")
        return df
    except Exception as e:
        print(f"WARNING: stats.wnba.com playergamelogs came back but couldn't be normalized: {e}", file=sys.stderr)
        return pd.DataFrame()


if __name__ == "__main__":
    after_date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-01"
    through_date = sys.argv[2] if len(sys.argv) > 2 else "2026-08-04"
    df = fetch_wnba_stats_gamelogs(after_date, through_date, "2026")
    print(df.head(20))
    print(f"Total rows: {len(df)}")
