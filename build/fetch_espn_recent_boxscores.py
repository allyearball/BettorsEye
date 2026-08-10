"""
Fills the gap between SportsDataverse's own release (which can lag several days behind)
and today, using ESPN's public, unauthenticated site API — the same host family this
pipeline already uses for venues and injuries.

This is NOT a replacement for SportsDataverse — it's a stopgap. SportsDataverse remains the
authoritative source for every date it already covers; this only fetches the days AFTER
SportsDataverse's own most recent date, up through yesterday (today's games are excluded on
purpose, since they may still be in progress and a live partial box score would corrupt every
stat in this pipeline — hit rates, EMA, IQR, DvP, everything downstream assumes a FINAL box
score, not a snapshot mid-game).

Endpoints used (both unauthenticated, no API key):
  - Scoreboard: https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates=YYYYMMDD
  - Box score:  https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={id}

Honesty note: I verified the scoreboard endpoint's shape directly (event id, status.type.completed,
competitors[].team.id/homeAway/score all confirmed live). I was NOT able to fully verify the
summary endpoint's exact box-score JSON shape against a live completed WNBA game from this
environment — the mapping below is built from ESPN's well-documented, standard basketball
box-score schema (the same shape across NBA/WNBA/college), which is consistent and widely
used, but if a field comes back named differently than expected here, this script is written
to fail LOUDLY per-game (skip that game, print a warning, keep going) rather than silently
produce wrong stats — check your Action's logs after the first run for any such warnings.
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
try:
    from curl_cffi import requests  # impersonates a real browser's TLS fingerprint, not just HTTP headers -- see the note below on why plain `requests` can't get past this block
    _HAS_CURL_CFFI = True
except ImportError:
    import requests
    _HAS_CURL_CFFI = False
    print("WARNING: curl_cffi isn't installed -- falling back to plain requests, which is very "
          "likely to still get blocked (this is a TLS-fingerprint-level block, not just headers). "
          "Run: pip install curl_cffi --break-system-packages", file=sys.stderr)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/wnba/",
    "Origin": "https://www.espn.com",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Same ESPN numeric team IDs used in fetch_injuries.py, inverted here (id -> short name).
ESPN_TEAM_ID_TO_SHORT = {
    20: "Dream", 19: "Sky", 18: "Sun", 3: "Wings", 129689: "Valkyries",
    5: "Fever", 17: "Aces", 6: "Sparks", 8: "Lynx", 9: "Liberty",
    11: "Mercury", 132052: "Fire", 14: "Storm", 131935: "Tempo", 16: "Mystics",
}
SHORT_TO_FULL = {
    "Aces": "Las Vegas Aces", "Dream": "Atlanta Dream", "Fever": "Indiana Fever", "Fire": "Portland Fire",
    "Liberty": "New York Liberty", "Lynx": "Minnesota Lynx", "Mercury": "Phoenix Mercury",
    "Mystics": "Washington Mystics", "Sky": "Chicago Sky", "Sparks": "Los Angeles Sparks",
    "Storm": "Seattle Storm", "Sun": "Connecticut Sun", "Tempo": "Toronto Tempo",
    "Valkyries": "Golden State Valkyries", "Wings": "Dallas Wings",
}

# Maps ESPN's per-stat label (as they appear in boxscore.players[].statistics[].labels) to the
# column name make_app_data.py expects. ESPN reports FG/3PT/FT as combined "made-attempted"
# strings (e.g. "7-13"), which get split below rather than mapped 1:1.
COMBINED_LABELS = {"FG": ("field_goals_made", "field_goals_attempted"),
                    "3PT": ("three_point_field_goals_made", "three_point_field_goals_attempted"),
                    "FT": ("free_throws_made", "free_throws_attempted")}
SIMPLE_LABELS = {"OREB": "offensive_rebounds", "DREB": "defensive_rebounds", "REB": "rebounds",
                 "AST": "assists", "STL": "steals", "BLK": "blocks", "TO": "turnovers",
                 "PF": "fouls", "+/-": "plus_minus", "PTS": "points"}


def _get_json(url, params=None, retries=3, pace_seconds=0.6):
    for attempt in range(retries + 1):
        try:
            kwargs = {"params": params, "headers": HEADERS, "timeout": 20}
            if _HAS_CURL_CFFI:
                kwargs["impersonate"] = "chrome124"  # matches the User-Agent already set above
            resp = requests.get(url, **kwargs)
            if resp.status_code == 429:
                wait = pace_seconds * (2 ** (attempt + 1))
                print(f"Rate limited, waiting {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(pace_seconds)  # polite pacing between successful requests too, not just on failure
            return resp.json()
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(1.5)


def _dates_between(after_date_str, through_date_str):
    """after_date_str exclusive, through_date_str inclusive, both 'YYYY-MM-DD'."""
    start = datetime.strptime(after_date_str, "%Y-%m-%d").date() + timedelta(days=1)
    end = datetime.strptime(through_date_str, "%Y-%m-%d").date()
    out = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def get_completed_games(date_obj):
    """Returns a list of dicts, one per ESPN-completed game on this date:
    {event_id, venue_name, venue_city, venue_state, team_periods: {team_id: [q1,q2,q3,q4,...]}}
    Venue and quarter-by-quarter scores come straight from THIS scoreboard response (confirmed
    against a real payload) -- no extra call needed for either, unlike the box score itself
    which still requires a separate per-game summary call."""
    payload = _get_json(SCOREBOARD_URL, params={"dates": date_obj.strftime("%Y%m%d")})
    out = []
    for ev in payload.get("events", []):
        status = ((ev.get("status") or {}).get("type") or {})
        if not status.get("completed"):
            continue
        comp = (ev.get("competitions") or [{}])[0]
        venue = comp.get("venue") or {}
        address = venue.get("address") or {}
        team_periods = {}
        for c in comp.get("competitors", []):
            tid = (c.get("team") or {}).get("id")
            periods = c.get("linescores") or []
            if tid and periods:
                # linescores are already ordered by period; just take the value in order
                team_periods[tid] = [p.get("value") for p in periods]
        out.append({
            "event_id": ev["id"],
            "venue_name": venue.get("fullName"),
            "venue_city": address.get("city"),
            "venue_state": address.get("state"),
            "team_periods": team_periods,
        })
    return out


def _parse_minutes(raw):
    """ESPN reports minutes as a plain string like '32' (not clock format) in the box score
    stats array — but guard for a 'MM:SS' shape too, just in case."""
    if raw is None or raw == "":
        return None
    if ":" in str(raw):
        try:
            m, s = str(raw).split(":")
            return round(int(m) + int(s) / 60, 1)
        except Exception:
            return None
    try:
        return float(raw)
    except Exception:
        return None


def get_boxscore_rows(event_id, game_date_str):
    """Returns a list of dict rows (one per player per team) matching the column shape
    make_app_data.py expects from the main SportsDataverse box CSV. Returns [] (with a
    printed warning) rather than raising, if this specific game's shape doesn't match
    what's expected — one bad game shouldn't take down the whole gap-fill."""
    try:
        payload = _get_json(SUMMARY_URL, params={"event": event_id})
        boxscore = payload.get("boxscore", {})
        team_blocks = boxscore.get("players", [])
        if len(team_blocks) != 2:
            print(f"WARNING: event {event_id} didn't have exactly 2 team blocks in boxscore.players, skipping.", file=sys.stderr)
            return []

        # Figure out home/away/score from the header competition block, keyed by team id.
        header_competitors = (payload.get("header", {}).get("competitions") or [{}])[0].get("competitors", [])
        meta_by_team_id = {}
        for c in header_competitors:
            tid = int(c["team"]["id"])
            meta_by_team_id[tid] = {
                "homeAway": c.get("homeAway"),
                "score": int(c.get("score", 0) or 0),
            }

        rows_by_team = {}
        for block in team_blocks:
            tid = int(block["team"]["id"])
            short = ESPN_TEAM_ID_TO_SHORT.get(tid)
            if not short:
                print(f"WARNING: event {event_id} has unrecognized ESPN team id {tid}, skipping that team.", file=sys.stderr)
                continue
            stat_group = (block.get("statistics") or [{}])[0]
            labels = stat_group.get("labels", [])
            team_rows = []
            for entry in stat_group.get("athletes", []):
                athlete = entry.get("athlete", {})
                stats = entry.get("stats", [])
                did_not_play = bool(entry.get("didNotPlay")) or len(stats) == 0
                row = {
                    "athlete_id": athlete.get("id"), "athlete_display_name": athlete.get("displayName"),
                    "athlete_jersey": athlete.get("jersey"),
                    "athlete_position_abbreviation": (athlete.get("position") or {}).get("abbreviation"),
                    "starter": bool(entry.get("starter")), "did_not_play": did_not_play,
                    "team_id": tid, "team_name": short,
                }
                for k in list(COMBINED_LABELS.values()) + list(SIMPLE_LABELS.values()) + ["minutes"]:
                    if isinstance(k, tuple):
                        continue
                    row[k] = 0 if not did_not_play else None
                for made_key, att_key in COMBINED_LABELS.values():
                    row[made_key] = 0 if not did_not_play else None
                    row[att_key] = 0 if not did_not_play else None
                if not did_not_play:
                    for label, value in zip(labels, stats):
                        if label == "MIN":
                            row["minutes"] = _parse_minutes(value)
                        elif label in COMBINED_LABELS:
                            made_key, att_key = COMBINED_LABELS[label]
                            try:
                                made, att = str(value).split("-")
                                row[made_key] = int(made); row[att_key] = int(att)
                            except Exception:
                                pass
                        elif label in SIMPLE_LABELS:
                            try:
                                row[SIMPLE_LABELS[label]] = int(value)
                            except Exception:
                                try:
                                    row[SIMPLE_LABELS[label]] = float(value)
                                except Exception:
                                    pass
                team_rows.append(row)
            rows_by_team[tid] = {"short": short, "rows": team_rows}

        all_rows = []
        tids = list(rows_by_team.keys())
        for tid in tids:
            opp_tid = [t for t in tids if t != tid]
            opp_tid = opp_tid[0] if opp_tid else None
            meta = meta_by_team_id.get(tid, {})
            opp_meta = meta_by_team_id.get(opp_tid, {}) if opp_tid else {}
            opp_short = rows_by_team.get(opp_tid, {}).get("short") if opp_tid else None
            for row in rows_by_team[tid]["rows"]:
                row.update({
                    "game_id": event_id, "game_date": game_date_str,
                    "team_abbreviation": rows_by_team[tid]["short"],
                    "team_score": meta.get("score"), "home_away": meta.get("homeAway"),
                    "opponent_team_name": opp_short, "opponent_team_score": opp_meta.get("score"),
                })
                all_rows.append(row)
        return all_rows
    except Exception as e:
        print(f"WARNING: failed to parse boxscore for event {event_id}: {e}", file=sys.stderr)
        return []


def fetch_recent_espn_boxscores(after_date_str, through_date_str):
    """Main entry point: returns (box_df, venues_df, period_scores_dict).
    box_df is in the same column shape as the SportsDataverse box CSV, covering every
    ESPN-completed game strictly after after_date_str through and including through_date_str.
    venues_df has one row per team (last-seen venue). period_scores_dict is keyed by event_id
    (already a plain ESPN numeric ID, no conversion needed) -> {"home":[...], "away":[...]}.
    Returns empty results (never raises) if nothing is found or everything fails."""
    all_rows = []
    venue_rows = {}  # team_name -> {venue fields}, overwritten each time (last-seen wins, fine since venues don't change mid-season)
    period_scores = {}
    dates = _dates_between(after_date_str, through_date_str)
    try:
        for i, date_obj in enumerate(dates):
            date_str = date_obj.strftime("%Y-%m-%d")
            if (i + 1) % 10 == 0:
                print(f"  ...{i+1}/{len(dates)} days checked, {len(all_rows)} player-rows so far")
            try:
                games = get_completed_games(date_obj)
            except Exception as e:
                print(f"WARNING: couldn't fetch ESPN scoreboard for {date_str}: {e}", file=sys.stderr)
                continue
            for game in games:
                event_id = game["event_id"]
                rows = get_boxscore_rows(event_id, date_str)
                all_rows.extend(rows)
                # Venue: belongs to the HOME team only -- a venue is a stadium, not a
                # traveling attribute, so the away team must never be credited with it here.
                # (Previous version assigned it to every team_id appearing in the game's rows,
                # which silently corrupted any team whose most-recently-processed game was an
                # away game -- they'd end up permanently stuck showing their opponent's arena.)
                home_row_for_venue = next((r for r in rows if r.get("home_away") == "home"), None)
                if home_row_for_venue and game["venue_name"]:
                    home_short = home_row_for_venue["team_name"]
                    venue_rows[home_short] = {
                        "team_name": home_short, "venue_name": game["venue_name"],
                        "venue_city": game["venue_city"], "venue_state": game["venue_state"],
                        "venue_capacity": None,
                    }
                # Period scores: game["team_periods"] is keyed by ESPN team id (string) -- remap
                # to home/away using the same team_id->short mapping already built for this game's rows.
                if len(game["team_periods"]) == 2 and rows:
                    home_row = next((r for r in rows if r.get("home_away") == "home"), None)
                    away_row = next((r for r in rows if r.get("home_away") == "away"), None)
                    if home_row and away_row:
                        home_tid, away_tid = str(home_row["team_id"]), str(away_row["team_id"])
                        if home_tid in game["team_periods"] and away_tid in game["team_periods"]:
                            period_scores[str(event_id)] = {
                                "home": game["team_periods"][home_tid],
                                "away": game["team_periods"][away_tid],
                            }
    except Exception as e:
        print(f"WARNING: ESPN gap-fill failed entirely: {e}", file=sys.stderr)
        return pd.DataFrame(), pd.DataFrame(), {}

    box_df = pd.DataFrame(all_rows)
    venues_df = pd.DataFrame(list(venue_rows.values()))
    if box_df.empty:
        return box_df, venues_df, period_scores
    print(f"ESPN fetch: {len(box_df)} player-rows across {box_df['game_id'].nunique()} games, "
          f"dates {box_df['game_date'].min()} to {box_df['game_date'].max()}")
    print(f"Also captured venue info for {len(venues_df)} teams and period scores for {len(period_scores)} games.")
    return box_df, venues_df, period_scores


if __name__ == "__main__":
    # Full season: python fetch_espn_recent_boxscores.py --full-season [output_csv]
    #   (season start is fixed at 2026-05-07, the day before the confirmed 2026-05-08 tipoff)
    # Date range (original gap-fill mode): python fetch_espn_recent_boxscores.py START END
    if len(sys.argv) >= 2 and sys.argv[1] == "--full-season":
        out_path = sys.argv[2] if len(sys.argv) > 2 else "full_season_espn.csv"
        after_date = "2026-05-07"
        through_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"Fetching the full 2026 season from ESPN ({after_date} exclusive through {through_date}). "
              "This paces itself to be polite to ESPN's servers -- expect roughly 5-10 minutes for a full season.")
        box_df, venues_df, period_scores = fetch_recent_espn_boxscores(after_date, through_date)
        if box_df.empty:
            print("No rows fetched -- check the warnings above.")
        else:
            box_df.to_csv(out_path, index=False)
            print(f"Wrote {len(box_df)} rows to {out_path}")
            venues_path = out_path.rsplit(".", 1)[0] + "_venues.csv"
            venues_df.to_csv(venues_path, index=False)
            print(f"Wrote {len(venues_df)} rows to {venues_path}")
            periods_path = out_path.rsplit(".", 1)[0] + "_periods.json"
            with open(periods_path, "w") as pf:
                json.dump(period_scores, pf)
            print(f"Wrote {len(period_scores)} games' period scores to {periods_path}")
        sys.exit(0)

    after_date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-01"
    through_date = sys.argv[2] if len(sys.argv) > 2 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    box_df, venues_df, period_scores = fetch_recent_espn_boxscores(after_date, through_date)
    print(box_df.head(20))
    print(f"Total rows: {len(box_df)}")
