"""
Full build pipeline, run by GitHub Actions on a schedule.

REWRITTEN this session to replace SportsDataverse (which was stuck/stale) with the
now-confirmed-working ESPN pipeline (curl_cffi bypasses the TLS-fingerprint block that plain
`requests` was hitting). Box scores, venues, and quarter-by-quarter period scores all come from
one function now; rosters and injuries each come from their own dedicated ESPN endpoints.

  1. Load whatever box score / venues / periods data already exists in data/ (persists between
     runs on this self-hosted runner -- data/ is gitignored, not re-cloned fresh each time)
  2. Fetch ESPN box scores/venues/periods for just the gap since the last run through yesterday
     (today is deliberately excluded -- games may still be in progress)
  3. Merge the new rows/entries into the accumulated data (never re-fetches the whole season
     every run -- only the incremental gap)
  4. Fetch fresh rosters and injuries (both cheap, single-pass, no need to accumulate -- always
     take the latest full snapshot)
  5. Regenerate app_data.json from all of the above
  6. Scrape VSiN's DraftKings betting splits and merge them in (best-effort, unchanged from before)
  7. Gzip + base64 the dataset and inject it into the HTML template
  8. Write the final, self-contained index.html into dist/ for deployment

Run locally with:  python build/build.py
"""
import gzip
import base64
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from make_app_data import generate  # noqa: E402
from scrape_vsin_splits import fetch_vsin_splits  # noqa: E402
from fetch_injuries import fetch_league_injuries  # noqa: E402
from fetch_espn_recent_boxscores import fetch_recent_espn_boxscores  # noqa: E402
from fetch_espn_rosters import fetch_all_rosters  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
DIST_DIR = os.path.join(REPO_ROOT, 'dist')

# Exclusive -- the actual first date with real box scores this season, matches
# fetch_espn_recent_boxscores.py's own convention (2026-05-07 exclusive means the season's
# first games on 2026-05-08 are the earliest included).
SEASON_START = '2026-05-07'

VENUE_COLUMNS = ['team_name', 'venue_name', 'venue_city', 'venue_state', 'venue_capacity']


def merge_venues(existing_csv_path, new_venues_df):
    """Merges new venue rows into whatever's already on disk -- an incremental run only
    returns venues for teams that actually played during that window, so this must UPDATE
    the accumulated file rather than replace it, or every team that didn't play in this
    particular window would silently lose its venue."""
    existing = {}
    if os.path.exists(existing_csv_path):
        try:
            existing_df = pd.read_csv(existing_csv_path)
            for _, row in existing_df.iterrows():
                existing[row['team_name']] = row.to_dict()
        except Exception as e:
            print(f'WARNING: could not read existing venues file ({e}) -- starting fresh.', file=sys.stderr)

    if new_venues_df is not None and not new_venues_df.empty:
        for _, row in new_venues_df.iterrows():
            existing[row['team_name']] = row.to_dict()

    if not existing:
        return
    out_df = pd.DataFrame(existing.values())[VENUE_COLUMNS]
    out_df.to_csv(existing_csv_path, index=False)


def merge_periods(existing_json_path, new_periods_dict):
    """Same accumulation logic as merge_venues, but for quarter-score data -- an incremental
    run only returns periods for the games it just fetched, so existing games' periods must
    be preserved, not dropped."""
    existing = {}
    if os.path.exists(existing_json_path):
        try:
            with open(existing_json_path) as f:
                existing = json.load(f)
        except Exception as e:
            print(f'WARNING: could not read existing periods file ({e}) -- starting fresh.', file=sys.stderr)

    if new_periods_dict:
        existing.update(new_periods_dict)

    with open(existing_json_path, 'w') as f:
        json.dump(existing, f)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DIST_DIR, exist_ok=True)
    box_csv = os.path.join(DATA_DIR, 'player_box_2026.csv')
    ros_csv = os.path.join(DATA_DIR, 'rosters_2026.csv')
    venues_csv = os.path.join(DATA_DIR, 'venues_2026.csv')
    periods_json = os.path.join(DATA_DIR, 'periods_2026.json')
    injuries_json = os.path.join(DATA_DIR, 'injuries.json')
    data_json = os.path.join(DATA_DIR, 'app_data.json')

    # ---- Step 1: figure out what date range we actually need to fetch ----
    existing_box_df = None
    since_date = SEASON_START
    if os.path.exists(box_csv):
        try:
            existing_box_df = pd.read_csv(box_csv)
            if not existing_box_df.empty:
                since_date = str(existing_box_df['game_date'].max())
        except Exception as e:
            print(f'WARNING: could not read existing box score file ({e}) -- treating this as a first run (full season fetch).', file=sys.stderr)

    # WNBA games are scheduled and date-tagged in US Eastern time (that's the convention ESPN's
    # own data uses), not UTC -- and this runner's system clock IS UTC. During EDT, UTC is 4
    # hours ahead of Eastern, so a run happening in the first few hours after UTC midnight is
    # still evening in Eastern time, with that night's late (8pm/10pm ET) games still in
    # progress. Computing "yesterday" from UTC instead of Eastern was letting a date get marked
    # as fully fetched before its own late games had actually finished -- this is what silently
    # left last night's late games stuck un-graded in App Tracker. Eastern time fixes the
    # boundary itself.
    now_et = datetime.now(ZoneInfo('America/New_York'))
    yesterday = (now_et - timedelta(days=1)).strftime('%Y-%m-%d')

    # Second, independent issue: even with the correct Eastern boundary, a date that got
    # partially captured (say, an early game finished and got fetched, but a later game that
    # night was still in progress at the time) was never revisited once since_date advanced
    # past it -- the code trusted "we've seen this date" to mean "this date is complete." Rather
    # than exclusively fetching from since_date forward, always back up 2 days and re-fetch that
    # small overlap on every run. This is safe and cheap: the merge below dedupes on
    # (game_id, athlete_id) keeping the newest row, so re-fetching a date that's already correct
    # just re-confirms it, and a date that was incomplete gets properly completed.
    fetch_from = (datetime.strptime(since_date, '%Y-%m-%d') - timedelta(days=2)).strftime('%Y-%m-%d')
    fetch_from = max(fetch_from, SEASON_START)

    if yesterday <= fetch_from:
        print(f'No new completed-game dates since {since_date} -- skipping the ESPN fetch step, app_data.json will still regenerate from what\'s already on disk (rosters/injuries refresh regardless).')
        new_box_df, new_venues_df, new_periods = pd.DataFrame(), pd.DataFrame(), {}
    else:
        print(f'Fetching ESPN box scores/venues/periods: {fetch_from} exclusive through {yesterday} (re-covering the last 2 days as a safety overlap, not just the new gap)...')
        new_box_df, new_venues_df, new_periods = fetch_recent_espn_boxscores(fetch_from, yesterday)
        print(f'Got {len(new_box_df)} new player-rows, {len(new_venues_df)} venue updates, {len(new_periods)} games\' periods.')

    # ---- Step 2: merge and persist box scores (accumulate, dedupe on game_id+athlete_id) ----
    if existing_box_df is not None and not existing_box_df.empty:
        if not new_box_df.empty:
            for col in existing_box_df.columns:
                if col not in new_box_df.columns:
                    new_box_df[col] = None
            new_box_df = new_box_df[existing_box_df.columns]
            combined = pd.concat([existing_box_df, new_box_df], ignore_index=True)
        else:
            combined = existing_box_df
    else:
        combined = new_box_df
    if combined.empty:
        print('ERROR: no box score data at all (neither existing nor newly fetched) -- cannot proceed.', file=sys.stderr)
        sys.exit(1)
    combined = combined.drop_duplicates(subset=['game_id', 'athlete_id'], keep='last')
    combined.to_csv(box_csv, index=False)
    print(f'Box scores: {len(combined)} total player-rows across {combined["game_id"].nunique()} games, written to {box_csv}')

    # ---- Step 3: merge and persist venues + periods ----
    merge_venues(venues_csv, new_venues_df)
    merge_periods(periods_json, new_periods)

    # ---- Step 4: rosters -- always a fresh full snapshot, no accumulation needed ----
    try:
        ros_df = fetch_all_rosters()
        if ros_df.empty:
            raise RuntimeError('came back empty')
        ros_df.to_csv(ros_csv, index=False)
        print(f'Rosters: {len(ros_df)} players written to {ros_csv}')
    except Exception as e:
        if os.path.exists(ros_csv):
            print(f'WARNING: roster fetch failed ({e}) -- reusing whatever roster data is already on disk from a previous run.', file=sys.stderr)
        else:
            print(f'ERROR: roster fetch failed ({e}) and no previous roster file exists -- cannot proceed.', file=sys.stderr)
            sys.exit(1)

    # ---- Step 5: regenerate app_data.json from everything above ----
    stats = generate(
        box_csv_path=box_csv,
        ros_csv_path=ros_csv,
        out_json_path=data_json,
        pbp_csv_path=None,
        injuries_json_path=None,  # merged in separately below, same as VSiN splits
        venues_csv_path=venues_csv if os.path.exists(venues_csv) else None,
        periods_json_path=periods_json if os.path.exists(periods_json) else None,
    )
    print('Generated dataset:', stats)

    # ---- Step 6: merge in VSiN's DraftKings Handle %/Bet % splits (unchanged from before) ----
    # Best-effort: a scrape hiccup (VSiN layout change, network blip, no games today) shouldn't
    # fail the whole build -- the Odds page already renders "VSiN: —" gracefully when this key
    # is missing or a team isn't present in it.
    try:
        vsin_splits = fetch_vsin_splits()
        with open(data_json, 'r') as f:
            app_data = json.load(f)
        app_data['vsinSplits'] = vsin_splits
        with open(data_json, 'w') as f:
            json.dump(app_data, f, allow_nan=False)
        print(f'Merged VSiN splits for {len(vsin_splits)} teams into {data_json}')
    except Exception as e:
        print(f'WARNING: VSiN splits scrape failed ({e}) -- Handle%/Bet% will show as unavailable this run.')

    # ---- Step 7: merge in current ESPN injury reports ----
    # Same best-effort philosophy as VSiN above.
    try:
        injuries = fetch_league_injuries()
        with open(injuries_json, 'w') as f:
            json.dump(injuries, f)
        with open(data_json, 'r') as f:
            app_data = json.load(f)
        app_data['injuries'] = injuries
        with open(data_json, 'w') as f:
            json.dump(app_data, f, allow_nan=False)
        total = sum(len(v) for v in injuries.values())
        print(f'Merged {total} injury entries across {len(injuries)} teams into {data_json}')
    except Exception as e:
        print(f'WARNING: injury fetch failed ({e}) -- injury alerts will show as unavailable this run.')

    # ---- Step 8: gzip + base64 + inject into the template ----
    with open(data_json, 'rb') as f:
        raw = f.read()
    compressed = gzip.compress(raw, compresslevel=9)
    b64 = base64.b64encode(compressed).decode('ascii')
    print(f'Raw JSON: {len(raw):,} bytes -> gzip+base64: {len(b64):,} bytes')
    template_path = os.path.join(os.path.dirname(__file__), 'app_template.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()
    placeholder = '__APP_DATA_B64GZ__'
    if placeholder not in tmpl:
        print('ERROR: placeholder not found in template!', file=sys.stderr)
        sys.exit(1)
    out_html = tmpl.replace(placeholder, b64)

    build_info = {
        'generatedThrough': stats['generated_through'],
        'teams': stats['teams'],
        'games': stats['games'],
    }
    with open(os.path.join(DIST_DIR, 'build_info.json'), 'w') as f:
        json.dump(build_info, f, indent=2)
    out_path = os.path.join(DIST_DIR, 'index.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(out_html)

    # PWA assets -- needed for "Add to Home Screen" + push notifications to work at all.
    build_dir = os.path.dirname(os.path.abspath(__file__))
    for asset in ['manifest.json', 'sw.js', 'icon-192.png', 'icon-512.png']:
        src = os.path.join(build_dir, asset)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DIST_DIR, asset))
        else:
            print(f'WARNING: expected PWA asset {asset} not found in build/ -- Home Screen install / push notifications may not work.')

    print(f'Wrote {out_path} ({len(out_html):,} bytes)')
    print('Build info:', build_info)


if __name__ == '__main__':
    main()
