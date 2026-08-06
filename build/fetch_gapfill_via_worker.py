"""
Calls this pipeline's own Cloudflare Worker to fetch ESPN box scores, rather than fetching
ESPN directly from GitHub Actions. GitHub Actions runner IPs are a well-known, publicly
documented range that ESPN's anti-bot system blocks outright (confirmed: every direct ESPN
call from this pipeline gets a 403). Cloudflare Workers run on Cloudflare's edge network
instead — a completely different IP reputation — so the identical fetch, issued from there,
has a real chance of getting through. This is why the Worker needs a `/gapfill/box` route
(see odds-api-proxy-worker.js) rather than just calling ESPN from here directly again.

Requires the WORKER_URL and NOTIFY_SECRET environment variables (same secrets already used
by check_gold_bets.py) to be set on the "Build app" step in the GitHub Actions workflow.
"""
import os
import sys

import pandas as pd
import requests


def fetch_via_worker(after_date_str, through_date_str):
    """Returns a DataFrame in the main box CSV's column shape, or an empty DataFrame (never
    raises) if the Worker isn't configured, isn't reachable, or comes back empty."""
    worker_url = os.environ.get('WORKER_URL')
    notify_secret = os.environ.get('NOTIFY_SECRET')
    if not worker_url:
        print('Worker gap-fill skipped: WORKER_URL environment variable is not set on this step.')
        return pd.DataFrame()
    if not notify_secret:
        print('Worker gap-fill skipped: NOTIFY_SECRET environment variable is not set on this step.')
        return pd.DataFrame()

    try:
        resp = requests.get(
            f'{worker_url.rstrip("/")}/gapfill/box',
            params={'after': after_date_str, 'through': through_date_str},
            headers={'x-gapfill-secret': notify_secret},
            timeout=90,
        )
    except Exception as e:
        print(f'WARNING: Worker gap-fill request failed (network-level, never got a response): {e}', file=sys.stderr)
        return pd.DataFrame()

    if resp.status_code != 200:
        print(f'WARNING: Worker gap-fill returned HTTP {resp.status_code}. Body (first 500 chars): {resp.text[:500]!r}', file=sys.stderr)
        return pd.DataFrame()

    try:
        payload = resp.json()
    except Exception as e:
        # This is the case that actually happened once already: a 200 status but a body that
        # isn't valid JSON (empty, truncated, or an HTML error page instead of the expected
        # JSON) — printing the raw body here is the whole point, so next time this is
        # diagnosable instead of just "Expecting value: line 1 column 1 (char 0)" again.
        print(f'WARNING: Worker gap-fill returned HTTP 200 but the body wasn\'t valid JSON ({e}). '
              f'Body length: {len(resp.text)} chars. Body (first 500 chars): {resp.text[:500]!r}', file=sys.stderr)
        return pd.DataFrame()

    for w in payload.get('warnings', []):
        print(f'WARNING: Worker gap-fill reported: {w}', file=sys.stderr)

    rows = payload.get('rows', [])
    if not rows:
        print(f'Worker gap-fill: no rows returned for {after_date_str} to {through_date_str}.')
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    print(f'Worker gap-fill: {len(df)} player-rows across {df["game_id"].nunique()} games, '
          f'dates {df["game_date"].min()} to {df["game_date"].max()}')
    return df


if __name__ == '__main__':
    after_date = sys.argv[1] if len(sys.argv) > 1 else '2026-08-01'
    through_date = sys.argv[2] if len(sys.argv) > 2 else '2026-08-04'
    df = fetch_via_worker(after_date, through_date)
    print(df.head(20))
    print(f'Total rows: {len(df)}')
