"""
Builds the WNBA app's embedded dataset (app_data.json) from the two source CSVs.
This is the same logic used throughout the project, packaged as a reusable function
so the CI pipeline (build.py) can call it after downloading fresh source data.
"""
import pandas as pd
import numpy as np
import json, re, os

TEAMS = ['Aces', 'Dream', 'Fever', 'Fire', 'Liberty', 'Lynx', 'Mercury', 'Mystics', 'Sky',
         'Sparks', 'Storm', 'Sun', 'Tempo', 'Valkyries', 'Wings']

# Manual bio overrides for players missing from the bulk roster release. If new gaps like this
# show up for other players over the course of the season, add them here the same way.
MANUAL_BIO_OVERRIDES = {
    4898383: {'height': '6\' 1"', 'weight': None, 'age': 22, 'date_of_birth': '2003-06-28'},   # Bree Hall
    4898400: {'height': '6\' 1"', 'weight': None, 'age': 23, 'date_of_birth': '2003-01-08'},   # Taylor Thierry
}


def n(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        f = float(v)
        return None if np.isnan(f) else f
    return v


def height_to_inches(h):
    if h is None or (isinstance(h, float) and np.isnan(h)):
        return None
    m = re.match(r"(\d+)'\s*(\d+)", str(h))
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2))


def weight_to_lbs(w):
    if w is None or (isinstance(w, float) and np.isnan(w)):
        return None
    m = re.search(r"(\d+)", str(w))
    return int(m.group(1)) if m else None


def dob_fmt(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return str(v).split('T')[0]


def pct(a, b):
    a = n(a); b = n(b)
    if a is None:
        return None
    if not b:
        return 0.0
    return round(a / b, 4)


def compute_halftime_splits(pbp_csv_path, id_to_short_name):
    """
    Returns {game_id: {'home': {'h1': pts, 'h2': pts}, 'away': {'h1': pts, 'h2': pts}}}
    derived from the running score in the play-by-play log — the score at the last play
    of period 2 is the halftime score; final - halftime = second-half points.
    Games with incomplete/missing period data are simply omitted (caller treats as unavailable).
    """
    cols = ['game_id', 'game_play_number', 'period_number', 'away_score', 'home_score',
            'home_team_id', 'away_team_id']
    pbp = pd.read_csv(pbp_csv_path, usecols=cols)
    pbp = pbp.sort_values(['game_id', 'game_play_number'])

    half_end = pbp[pbp['period_number'] <= 2].groupby('game_id').tail(1)
    final_end = pbp.groupby('game_id').tail(1)

    half_by_game = half_end.set_index('game_id')[['away_score', 'home_score']].to_dict('index')
    final_by_game = final_end.set_index('game_id')[['away_score', 'home_score']].to_dict('index')

    splits = {}
    for gid, half in half_by_game.items():
        final = final_by_game.get(gid)
        if final is None:
            continue
        h1_away, h1_home = half['away_score'], half['home_score']
        f_away, f_home = final['away_score'], final['home_score']
        splits[gid] = {
            'home': {'h1': int(h1_home), 'h2': int(f_home) - int(h1_home)},
            'away': {'h1': int(h1_away), 'h2': int(f_away) - int(h1_away)},
        }
    return splits


def generate(box_csv_path, ros_csv_path, out_json_path, pbp_csv_path=None, injuries_json_path=None, venues_csv_path=None, periods_json_path=None):
    box = pd.read_csv(box_csv_path)
    ros = pd.read_csv(ros_csv_path)

    bios = ros.set_index('athlete_id')[['height', 'weight', 'age', 'date_of_birth']].to_dict('index')
    bios.update(MANUAL_BIO_OVERRIDES)

    def bio_fields(r):
        return {
            'height': n(r['bio_height']) if 'bio_height' in r else None,
            'heightIn': height_to_inches(r.get('bio_height')),
            'weight': weight_to_lbs(r.get('bio_weight')),
            'age': n(r.get('bio_age')),
            'dob': dob_fmt(r.get('bio_dob')),
        }

    box['bio_height'] = box['athlete_id'].map(lambda x: bios.get(x, {}).get('height'))
    box['bio_weight'] = box['athlete_id'].map(lambda x: bios.get(x, {}).get('weight'))
    box['bio_age'] = box['athlete_id'].map(lambda x: bios.get(x, {}).get('age'))
    box['bio_dob'] = box['athlete_id'].map(lambda x: bios.get(x, {}).get('date_of_birth'))

    team_last10, team_full_season = {}, {}
    for t in TEAMS:
        all_t = (box[box['team_name'] == t][['game_id', 'game_date']]
                 .drop_duplicates().sort_values('game_date', ascending=False))
        team_last10[t] = all_t['game_id'].head(10).tolist()
        team_full_season[t] = all_t['game_id'].tolist()

    all_game_ids = sorted(set(box['game_id'].unique().tolist()))

    def game_row(r):
        return {
            'team': r['team_name'], 'player': r['athlete_display_name'],
            'athleteId': int(r['athlete_id']) if not pd.isna(r['athlete_id']) else None,
            'jersey': n(r['athlete_jersey']), 'pos': r['athlete_position_abbreviation'],
            'starter': bool(r['starter']), 'dnp': bool(r['did_not_play']),
            'min': n(r['minutes']),
            'fgm': n(r['field_goals_made']), 'fga': n(r['field_goals_attempted']),
            'fgPct': pct(r['field_goals_made'], r['field_goals_attempted']),
            'tpm': n(r['three_point_field_goals_made']), 'tpa': n(r['three_point_field_goals_attempted']),
            'tpPct': pct(r['three_point_field_goals_made'], r['three_point_field_goals_attempted']),
            'ftm': n(r['free_throws_made']), 'fta': n(r['free_throws_attempted']),
            'ftPct': pct(r['free_throws_made'], r['free_throws_attempted']),
            'oreb': n(r['offensive_rebounds']), 'dreb': n(r['defensive_rebounds']), 'reb': n(r['rebounds']),
            'ast': n(r['assists']), 'stl': n(r['steals']), 'blk': n(r['blocks']),
            'to': n(r['turnovers']), 'pf': n(r['fouls']), 'plusMinus': n(r['plus_minus']),
            'pts': n(r['points']),
        }

    # Quarter-by-quarter scores, from fetch_sportradar_wnba.py's period_scores output. Keys
    # are the SAME stable-int game IDs used everywhere else, but written as JSON object keys
    # (always strings) -- compared as strings below to match reliably.
    periods_by_game = {}
    if periods_json_path and os.path.exists(periods_json_path):
        with open(periods_json_path) as f:
            periods_by_game = json.load(f)

    games_out = {}
    for gid in all_game_ids:
        g = box[box['game_id'] == gid].copy()
        date = g['game_date'].iloc[0]
        teams_meta = g[['team_name', 'team_abbreviation', 'team_score', 'home_away']].drop_duplicates()
        home = teams_meta[teams_meta['home_away'] == 'home'].iloc[0]
        away = teams_meta[teams_meta['home_away'] == 'away'].iloc[0]
        label = f"{date} — {away['team_name']} at {home['team_name']} ({away['team_score']}-{home['team_score']})"
        rows = [game_row(r) for _, r in g.iterrows()]
        game_entry = {
            'id': int(gid), 'label': label, 'date': date,
            'home': home['team_name'], 'away': away['team_name'],
            'homeScore': n(home['team_score']), 'awayScore': n(away['team_score']),
            'rows': rows,
        }
        period_entry = periods_by_game.get(str(int(gid)))
        if period_entry:
            game_entry['periods'] = period_entry
        games_out[str(int(gid))] = game_entry

    def team_summary(team_name, game_ids):
        sub = box[(box['team_name'] == team_name) & (box['game_id'].isin(game_ids))].copy()
        opp = box[(box['team_name'] != team_name) & (box['game_id'].isin(game_ids))].copy()
        players = sub[['athlete_id']].drop_duplicates()['athlete_id'].tolist()
        totals_rows, avg_rows = [], []
        for aid in players:
            prow = sub[sub['athlete_id'] == aid]
            name = prow['athlete_display_name'].iloc[0]
            gp = int((~prow['did_not_play']).sum())
            gs = int(prow['starter'].sum())
            sums = {
                'min': prow['minutes'].sum(min_count=1),
                'fgm': prow['field_goals_made'].sum(), 'fga': prow['field_goals_attempted'].sum(),
                'tpm': prow['three_point_field_goals_made'].sum(), 'tpa': prow['three_point_field_goals_attempted'].sum(),
                'ftm': prow['free_throws_made'].sum(), 'fta': prow['free_throws_attempted'].sum(),
                'oreb': prow['offensive_rebounds'].sum(), 'dreb': prow['defensive_rebounds'].sum(), 'reb': prow['rebounds'].sum(),
                'ast': prow['assists'].sum(), 'stl': prow['steals'].sum(), 'blk': prow['blocks'].sum(),
                'to': prow['turnovers'].sum(), 'pf': prow['fouls'].sum(), 'pts': prow['points'].sum(),
            }
            for k in sums:
                if sums[k] is None or (isinstance(sums[k], float) and np.isnan(sums[k])):
                    sums[k] = 0
            bio = bio_fields(prow.iloc[0])
            trow = {'player': name, 'athleteId': int(aid), 'gp': gp, 'gs': gs}
            trow.update({k: n(v) for k, v in sums.items()})
            trow['fgPct'] = pct(sums['fgm'], sums['fga']); trow['tpPct'] = pct(sums['tpm'], sums['tpa']); trow['ftPct'] = pct(sums['ftm'], sums['fta'])
            trow.update(bio)
            totals_rows.append(trow)

            arow = {'player': name, 'athleteId': int(aid), 'gp': gp, 'gs': gs}
            for k, v in sums.items():
                arow[k] = round(v / gp, 2) if gp else None
            arow['fgPct'] = trow['fgPct']; arow['tpPct'] = trow['tpPct']; arow['ftPct'] = trow['ftPct']
            arow.update(bio)
            avg_rows.append(arow)

        opp_game_rows = []
        gp_df = sub[['game_id', 'game_date', 'opponent_team_name', 'team_score', 'opponent_team_score']].drop_duplicates().sort_values('game_date')
        for _, grow in gp_df.iterrows():
            og = opp[opp['game_id'] == grow['game_id']]
            opp_game_rows.append({
                'date': grow['game_date'], 'opponent': grow['opponent_team_name'],
                'teamPts': n(grow['team_score']), 'oppPts': n(grow['opponent_team_score']),
                'oppFgm': n(og['field_goals_made'].sum()), 'oppFga': n(og['field_goals_attempted'].sum()),
                'oppFgPct': pct(og['field_goals_made'].sum(), og['field_goals_attempted'].sum()),
                'oppTpm': n(og['three_point_field_goals_made'].sum()), 'oppTpa': n(og['three_point_field_goals_attempted'].sum()),
                'oppFtm': n(og['free_throws_made'].sum()), 'oppFta': n(og['free_throws_attempted'].sum()),
                'oppOreb': n(og['offensive_rebounds'].sum()), 'oppDreb': n(og['defensive_rebounds'].sum()), 'oppReb': n(og['rebounds'].sum()),
                'oppAst': n(og['assists'].sum()), 'oppStl': n(og['steals'].sum()), 'oppBlk': n(og['blocks'].sum()),
                'oppTo': n(og['turnovers'].sum()), 'oppPf': n(og['fouls'].sum()),
            })
        return {'totals': totals_rows, 'averages': avg_rows, 'opponents': opp_game_rows}

    teams_out = {}
    for t in TEAMS:
        gids10 = team_last10[t]
        gidsFull = team_full_season[t]
        teams_out[t] = {
            'gameIds': [int(x) for x in gids10],
            'gameIdsFull': [int(x) for x in gidsFull],
            'summary': team_summary(t, gids10),
            'summaryFull': team_summary(t, gidsFull),
        }

    full_player_logs = {}
    has_reason_col = 'not_playing_reason' in box.columns  # older CSVs (pre-dating this field) won't have it -- handled gracefully below rather than crashing
    for t in TEAMS:
        sub_full = box[box['team_name'] == t].copy().sort_values('game_date')
        for aid, prow in sub_full.groupby('athlete_id'):
            # Defensive dedup on game_id, on top of whatever build.py already did upstream --
            # a genuine duplicate row here (same game, same player) previously showed up as the
            # same game appearing twice in "Vs Opponent This Season" and similar columns. This
            # is a second layer, not a replacement for fixing the actual upstream cause.
            if 'game_id' in prow.columns:
                prow = prow.drop_duplicates(subset=['game_id'], keep='last')
            name = prow['athlete_display_name'].iloc[0]
            key = f"{name}|{t}"
            log = []
            for _, r in prow.iterrows():
                log.append({
                    'date': r['game_date'], 'opp': r['opponent_team_name'], 'homeAway': r['home_away'],
                    'dnp': bool(r['did_not_play']), 'starter': bool(r['starter']),
                    'notPlayingReason': (r['not_playing_reason'] if has_reason_col and pd.notna(r.get('not_playing_reason')) else None),
                    'min': n(r['minutes']), 'pts': n(r['points']),
                    'fgm': n(r['field_goals_made']), 'fga': n(r['field_goals_attempted']),
                    'fgPct': pct(r['field_goals_made'], r['field_goals_attempted']),
                    'tpm': n(r['three_point_field_goals_made']), 'tpa': n(r['three_point_field_goals_attempted']),
                    'tpPct': pct(r['three_point_field_goals_made'], r['three_point_field_goals_attempted']),
                    'ftm': n(r['free_throws_made']), 'fta': n(r['free_throws_attempted']),
                    'ftPct': pct(r['free_throws_made'], r['free_throws_attempted']),
                    'oreb': n(r['offensive_rebounds']), 'dreb': n(r['defensive_rebounds']), 'reb': n(r['rebounds']),
                    'ast': n(r['assists']), 'stl': n(r['steals']), 'blk': n(r['blocks']),
                    'to': n(r['turnovers']), 'pf': n(r['fouls']), 'plusMinus': n(r['plus_minus']),
                })
            full_player_logs[key] = {'player': name, 'team': t, 'athleteId': int(aid), 'log': log}

    halftime_splits = {}
    if pbp_csv_path and os.path.exists(pbp_csv_path):
        try:
            id_to_short_name = box.set_index('team_id')['team_name'].to_dict()
            halftime_splits = compute_halftime_splits(pbp_csv_path, id_to_short_name)
        except Exception as e:
            print(f'WARNING: halftime split computation failed, half markets will show as unavailable: {e}')
            halftime_splits = {}

    full_team_games = {}
    for t in TEAMS:
        sub_full = box[box['team_name'] == t][['game_id', 'game_date', 'opponent_team_name', 'home_away', 'team_score', 'opponent_team_score']].drop_duplicates().sort_values('game_date')
        rows = []
        for _, r in sub_full.iterrows():
            row = {'date': r['game_date'], 'opponent': r['opponent_team_name'], 'homeAway': r['home_away'],
                   'teamPts': n(r['team_score']), 'oppPts': n(r['opponent_team_score'])}
            split = halftime_splits.get(r['game_id'])
            if split:
                side = 'home' if r['home_away'] == 'home' else 'away'
                other = 'away' if side == 'home' else 'home'
                row['h1TeamPts'] = split[side]['h1']; row['h1OppPts'] = split[other]['h1']
                row['h2TeamPts'] = split[side]['h2']; row['h2OppPts'] = split[other]['h2']
            else:
                row['h1TeamPts'] = None; row['h1OppPts'] = None
                row['h2TeamPts'] = None; row['h2OppPts'] = None
            rows.append(row)
        full_team_games[t] = rows

    team_records = {}
    for t in TEAMS:
        sub = box[box['team_name'] == t][['game_id', 'game_date', 'opponent_team_name', 'home_away', 'team_score', 'opponent_team_score']].drop_duplicates()

        def wl(df):
            w = int((df['team_score'] > df['opponent_team_score']).sum())
            l = int((df['team_score'] < df['opponent_team_score']).sum())
            return {'w': w, 'l': l}

        overall = wl(sub)
        home = wl(sub[sub['home_away'] == 'home'])
        away = wl(sub[sub['home_away'] == 'away'])
        vs_opp = {}
        for opp_name, opp_df in sub.groupby('opponent_team_name'):
            vs_opp[opp_name] = wl(opp_df)
        team_records[t] = {'overall': overall, 'home': home, 'away': away, 'vsOpponent': vs_opp}

    # Both optional -- injuries from fetch_sportradar_injuries.py's output (already in the
    # exact {team: [{player, position, status, detail, date}, ...]} shape the frontend
    # expects), venues from fetch_sportradar_rosters.py's venues.csv. Neither file existing
    # yet isn't an error -- the frontend already handles DATA.injuries/DATA.teamVenues being
    # absent gracefully (shows nothing rather than breaking).
    injuries_out = {}
    if injuries_json_path and os.path.exists(injuries_json_path):
        with open(injuries_json_path) as f:
            loaded = json.load(f)
        # fetch_injuries.py (ESPN) wraps as {"teams": {...}, "_fetchedAt":...}; the Sportradar
        # version wrote the {team: [...]} dict directly. Handle either without caring which.
        injuries_out = loaded.get("teams", loaded) if isinstance(loaded, dict) else {}

    venues_out = {}
    if venues_csv_path and os.path.exists(venues_csv_path):
        venues_df = pd.read_csv(venues_csv_path)
        for _, vr in venues_df.iterrows():
            venues_out[vr['team_name']] = {
                'name': n(vr.get('venue_name')), 'city': n(vr.get('venue_city')),
                'state': n(vr.get('venue_state')), 'capacity': n(vr.get('venue_capacity')),
            }

    data = {
        'generatedThrough': str(box['game_date'].max()),
        'teams': teams_out,
        'games': games_out,
        'teamRecords': team_records,
        'fullPlayerLogs': full_player_logs,
        'fullTeamGames': full_team_games,
        'injuries': injuries_out,
        'teamVenues': venues_out,
    }

    with open(out_json_path, 'w') as f:
        json.dump(data, f, allow_nan=False)

    return {
        'teams': len(teams_out),
        'games': len(games_out),
        'full_player_logs': len(full_player_logs),
        'generated_through': data['generatedThrough'],
    }


if __name__ == '__main__':
    import sys
    box_path = sys.argv[1] if len(sys.argv) > 1 else 'data/player_box_2026.csv'
    ros_path = sys.argv[2] if len(sys.argv) > 2 else 'data/rosters_2026.csv'
    out_path = sys.argv[3] if len(sys.argv) > 3 else 'build_output/app_data.json'
    pbp_path = sys.argv[4] if len(sys.argv) > 4 else 'data/pbp_2026.csv'
    injuries_path = sys.argv[5] if len(sys.argv) > 5 else 'data/injuries.json'
    venues_path = sys.argv[6] if len(sys.argv) > 6 else 'data/venues.csv'
    periods_path = sys.argv[7] if len(sys.argv) > 7 else 'data/full_season_2026_periods.json'
    stats = generate(box_path, ros_path, out_path, pbp_path, injuries_path, venues_path, periods_path)
    print(stats)
