"""
Checks today's live DraftKings odds against this app's historical data for any
"gold-tier" bet — 96%+ hit rate with at least 10 games of history behind it —
and sends a push notification to every subscribed device if a genuinely new one
appears. Run after build.py in the same GitHub Actions job (reuses the same
freshly-downloaded data and the just-generated app_data.json).

Requires these GitHub Actions secrets:
  WORKER_URL          - your Cloudflare Worker's base URL
  NOTIFY_SECRET        - the same shared secret configured on the Worker
  VAPID_PRIVATE_KEY    - from generate_vapid_keys.py (raw base64url, NOT a PEM file)
  VAPID_SUBJECT        - a mailto: or https: URL identifying you, e.g. mailto:you@example.com

If any of these aren't set, this script exits quietly without doing anything —
it will never break the main build/deploy step.
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_JSON_PATH = os.path.join(REPO_ROOT, 'data', 'app_data.json')

WORKER_URL = os.environ.get('WORKER_URL', '').rstrip('/')
NOTIFY_SECRET = os.environ.get('NOTIFY_SECRET', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_SUBJECT = os.environ.get('VAPID_SUBJECT', '')

GOLD_THRESHOLD = 0.96
MIN_GAMES_HISTORY = 10
WINDOW = 15  # "at least 10 games of history" is checked over each entity's last 15 games

SHORT_TEAM_NAME_TO_FULL = {
    'Aces': 'Las Vegas Aces', 'Dream': 'Atlanta Dream', 'Fever': 'Indiana Fever', 'Fire': 'Portland Fire',
    'Liberty': 'New York Liberty', 'Lynx': 'Minnesota Lynx', 'Mercury': 'Phoenix Mercury', 'Mystics': 'Washington Mystics',
    'Sky': 'Chicago Sky', 'Sparks': 'Los Angeles Sparks', 'Storm': 'Seattle Storm', 'Sun': 'Connecticut Sun',
    'Tempo': 'Toronto Tempo', 'Valkyries': 'Golden State Valkyries', 'Wings': 'Dallas Wings',
}
FULL_TO_SHORT = {v: k for k, v in SHORT_TEAM_NAME_TO_FULL.items()}

PROP_MARKET_TO_STAT = {
    'player_points': 'pts', 'player_rebounds': 'reb', 'player_assists': 'ast', 'player_threes': 'tpm',
}
PROP_MARKET_LABEL = {
    'player_points': 'Points', 'player_rebounds': 'Rebounds', 'player_assists': 'Assists', 'player_threes': 'Threes',
}


def log(msg):
    print(f'[check_gold_bets] {msg}')


def window_slice(log_list, n):
    return log_list[-n:] if n else log_list


def team_moneyline_rate(full_team_games, team):
    games = window_slice(full_team_games.get(team, []), WINDOW)
    gp = len(games)
    if gp == 0:
        return None
    hits = sum(1 for g in games if g['teamPts'] > g['oppPts'])
    return {'gp': gp, 'rate': hits / gp}


def team_spread_rate(full_team_games, team, point):
    games = window_slice(full_team_games.get(team, []), WINDOW)
    gp = len(games)
    if gp == 0 or point is None:
        return None
    hits = sum(1 for g in games if (g['teamPts'] - g['oppPts'] + point) > 0)
    return {'gp': gp, 'rate': hits / gp}


def team_total_rate(full_team_games, team, point):
    games = window_slice(full_team_games.get(team, []), WINDOW)
    gp = len(games)
    if gp == 0 or point is None:
        return None
    hits = sum(1 for g in games if (g['teamPts'] + g['oppPts']) > point)
    return {'gp': gp, 'rate': hits / gp}


def team_own_total_rate(full_team_games, team, point):
    games = window_slice(full_team_games.get(team, []), WINDOW)
    gp = len(games)
    if gp == 0 or point is None:
        return None
    hits = sum(1 for g in games if g['teamPts'] > point)
    return {'gp': gp, 'rate': hits / gp}


def player_prop_rate(full_player_logs, player_name, team, stat_key, line):
    import math
    key = f'{player_name}|{team}'
    pl = full_player_logs.get(key)
    if not pl:
        return None
    played = [g for g in window_slice(pl['log'], WINDOW) if not g['dnp']]
    gp = len(played)
    if gp == 0:
        return None
    threshold = line + 1 if float(line).is_integer() else math.ceil(line)
    hits = sum(1 for g in played if g.get(stat_key) is not None and g[stat_key] >= threshold)
    return {'gp': gp, 'rate': hits / gp}


def find_player_team(full_player_logs, player_name, team_a, team_b):
    if f'{player_name}|{team_a}' in full_player_logs:
        return team_a
    if f'{player_name}|{team_b}' in full_player_logs:
        return team_b
    return None


def qualifies_gold(rate_info):
    """Returns ('over'|'under', displayed_pct) if this clears the gold bar with enough history, else None."""
    if not rate_info or rate_info['gp'] < MIN_GAMES_HISTORY:
        return None
    rate = rate_info['rate']
    if rate >= GOLD_THRESHOLD:
        return ('over', round(rate * 100))
    if (1 - rate) >= GOLD_THRESHOLD:
        return ('under', round((1 - rate) * 100))
    return None


def dk_outcome(market, name):
    if not market:
        return None
    for o in market.get('outcomes', []):
        if o.get('name') == name:
            return o
    return None


def markets_by_key(bookmaker):
    out = {}
    if bookmaker:
        for m in bookmaker.get('markets', []):
            out[m['key']] = m
    return out


def gather_gold_legs(ev, detail, full_team_games, full_player_logs):
    """Mirrors the app's gatherCandidateLegs, but only returns legs that clear the gold bar
    (96%+) AND have at least 10 games of history — for the specific side that qualifies."""
    home_short = FULL_TO_SHORT.get(ev['home_team'], ev['home_team'])
    away_short = FULL_TO_SHORT.get(ev['away_team'], ev['away_team'])

    dk = next((b for b in ev.get('bookmakers', []) if b['key'] == 'draftkings'), None)
    dk_detail = None
    if detail:
        dk_detail = next((b for b in detail.get('bookmakers', []) if b['key'] == 'draftkings'), None)
    game_markets = markets_by_key(dk)
    detail_markets = markets_by_key(dk_detail)

    legs = []
    game_label = f"{ev['away_team']} @ {ev['home_team']}"

    # Moneyline
    for short, full in [(away_short, ev['away_team']), (home_short, ev['home_team'])]:
        outcome = dk_outcome(game_markets.get('h2h'), full)
        if not outcome:
            continue
        q = qualifies_gold(team_moneyline_rate(full_team_games, short))
        if q:
            side, pct = q
            legs.append({'label': f'{full} Moneyline', 'price': outcome['price'], 'pct': pct, 'game': game_label})

    # Spread
    for short, full in [(away_short, ev['away_team']), (home_short, ev['home_team'])]:
        outcome = dk_outcome(game_markets.get('spreads'), full)
        if not outcome:
            continue
        q = qualifies_gold(team_spread_rate(full_team_games, short, outcome.get('point')))
        if q:
            side, pct = q
            pt = outcome['point']
            legs.append({'label': f"{full} {'+' if pt>0 else ''}{pt}", 'price': outcome['price'], 'pct': pct, 'game': game_label})

    # Full-game total
    totals_market = game_markets.get('totals')
    if totals_market:
        over = dk_outcome(totals_market, 'Over')
        under = dk_outcome(totals_market, 'Under')
        point = (over or under or {}).get('point')
        if over:
            q = qualifies_gold(team_total_rate(full_team_games, away_short, point))
            if q and q[0] == 'over':
                legs.append({'label': f'Game Total Over {point}', 'price': over['price'], 'pct': q[1], 'game': game_label})
        if under:
            r = team_total_rate(full_team_games, away_short, point)
            if r:
                under_pct = round((1 - r['rate']) * 100)
                if r['gp'] >= MIN_GAMES_HISTORY and under_pct >= GOLD_THRESHOLD * 100:
                    legs.append({'label': f'Game Total Under {point}', 'price': under['price'], 'pct': under_pct, 'game': game_label})

    # Team totals
    team_totals_market = detail_markets.get('team_totals')
    if team_totals_market:
        for short, full in [(away_short, ev['away_team']), (home_short, ev['home_team'])]:
            over = next((o for o in team_totals_market['outcomes'] if o['name']=='Over' and o.get('description')==full), None)
            if over:
                q = qualifies_gold(team_own_total_rate(full_team_games, short, over.get('point')))
                if q and q[0] == 'over':
                    legs.append({'label': f'{full} Team Total Over {over["point"]}', 'price': over['price'], 'pct': q[1], 'game': game_label})

    # Player props (base markets only, not alt lines — mirrors the app's parlay-candidate scope)
    for mkey, stat_key in PROP_MARKET_TO_STAT.items():
        market = detail_markets.get(mkey)
        if not market:
            continue
        players = set(o.get('description') for o in market['outcomes'])
        for pname in players:
            over = next((o for o in market['outcomes'] if o['name']=='Over' and o.get('description')==pname), None)
            under = next((o for o in market['outcomes'] if o['name']=='Under' and o.get('description')==pname), None)
            line = (over or under or {}).get('point')
            if line is None:
                continue
            team = find_player_team(full_player_logs, pname, home_short, away_short)
            if not team:
                continue
            rate_info = player_prop_rate(full_player_logs, pname, team, stat_key, line)
            q = qualifies_gold(rate_info)
            if q and over and q[0] == 'over':
                legs.append({'label': f'{pname} Over {line} {PROP_MARKET_LABEL[mkey]}', 'price': over['price'], 'pct': q[1], 'game': game_label})
            if q and under and q[0] == 'under':
                legs.append({'label': f'{pname} Under {line} {PROP_MARKET_LABEL[mkey]}', 'price': under['price'], 'pct': q[1], 'game': game_label})

    return legs


def main():
    if not WORKER_URL:
        log('WORKER_URL not set — skipping gold-bet check entirely (not configured yet).')
        return
    if webpush is None:
        log('pywebpush not installed — skipping (this should not happen if requirements.txt was installed).')
        return
    if not (VAPID_PRIVATE_KEY and VAPID_SUBJECT):
        log('VAPID_PRIVATE_KEY / VAPID_SUBJECT not set — skipping (push notifications not configured yet).')
        return
    if not os.path.exists(DATA_JSON_PATH):
        log(f'{DATA_JSON_PATH} not found — run build.py first. Skipping.')
        return

    with open(DATA_JSON_PATH) as f:
        app_data = json.load(f)
    full_team_games = app_data['fullTeamGames']
    full_player_logs = app_data['fullPlayerLogs']

    log('Fetching today\'s live DraftKings odds...')
    try:
        events = requests.get(
            f'{WORKER_URL}/v4/sports/basketball_wnba/odds',
            params={'regions': 'us', 'bookmakers': 'draftkings', 'markets': 'h2h,spreads,totals', 'oddsFormat': 'american'},
            timeout=30,
        ).json()
    except Exception as e:
        log(f'Failed to fetch odds: {e}')
        return

    if not isinstance(events, list) or not events:
        log('No games currently listed by DraftKings. Nothing to check.')
        return

    today = datetime.now(timezone.utc).date()
    todays_games = [ev for ev in events if _parse_date(ev.get('commence_time')) == today]
    log(f'{len(todays_games)} of {len(events)} fetched game(s) are scheduled for today (UTC).')
    if not todays_games:
        return

    all_legs = []
    for ev in todays_games:
        detail = None
        try:
            detail = requests.get(
                f'{WORKER_URL}/v4/sports/basketball_wnba/events/{ev["id"]}/odds',
                params={
                    'regions': 'us', 'bookmakers': 'draftkings',
                    'markets': 'team_totals,player_points,player_rebounds,player_assists,player_threes',
                    'oddsFormat': 'american',
                },
                timeout=30,
            ).json()
        except Exception as e:
            log(f'Detail fetch failed for {ev.get("id")}: {e}')
        all_legs.extend(gather_gold_legs(ev, detail, full_team_games, full_player_logs))

    if not all_legs:
        log('No gold-tier (96%+, 10+ games history) bets found on today\'s slate.')
        return

    log(f'Found {len(all_legs)} qualifying gold-tier leg(s) today.')

    leg_ids = [f'{leg["game"]}|{leg["label"]}' for leg in all_legs]
    try:
        resp = requests.post(
            f'{WORKER_URL}/push/check-and-mark',
            json={'legIds': leg_ids},
            headers={'X-Notify-Secret': NOTIFY_SECRET},
            timeout=30,
        )
        resp.raise_for_status()
        new_leg_ids = set(resp.json().get('newLegIds', []))
    except Exception as e:
        log(f'check-and-mark call failed: {e} — aborting rather than risk duplicate notifications.')
        return

    new_legs = [leg for leg, lid in zip(all_legs, leg_ids) if lid in new_leg_ids]
    if not new_legs:
        log('All qualifying legs have already been notified about recently. Nothing new to send.')
        return

    log(f'{len(new_legs)} genuinely new gold-tier leg(s) to notify about.')

    try:
        resp = requests.get(f'{WORKER_URL}/push/list', headers={'X-Notify-Secret': NOTIFY_SECRET}, timeout=30)
        resp.raise_for_status()
        subscriptions = resp.json()
    except Exception as e:
        log(f'Failed to fetch subscription list: {e}')
        return

    if not subscriptions:
        log('No devices are subscribed to push notifications yet.')
        return

    if len(new_legs) == 1:
        leg = new_legs[0]
        title = '\U0001F3C6 Gold-Tier Bet Alert'
        body = f'{leg["label"]} \u2014 {leg["pct"]}% historical hit rate ({leg["game"]})'
    else:
        title = f'\U0001F3C6 {len(new_legs)} New Gold-Tier Bets'
        body = '; '.join(f'{leg["label"]} ({leg["pct"]}%)' for leg in new_legs[:3])
        if len(new_legs) > 3:
            body += f'; +{len(new_legs) - 3} more'

    payload = json.dumps({'title': title, 'body': body, 'url': '/'})

    sent, failed = 0, 0
    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={'sub': VAPID_SUBJECT},
            )
            sent += 1
        except WebPushException as e:
            failed += 1
            log(f'Push failed for one subscriber: {e}')

    log(f'Sent {sent} notification(s), {failed} failed.')


def _parse_date(commence_time):
    if not commence_time:
        return None
    try:
        return datetime.fromisoformat(commence_time.replace('Z', '+00:00')).date()
    except ValueError:
        return None


if __name__ == '__main__':
    main()
