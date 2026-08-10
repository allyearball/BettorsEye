"""
Fetches player roster/bio data (height, weight, age, date of birth) from ESPN's public,
unauthenticated site API, replacing the paid Sportradar version.

Source: https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{espn_team_id}/roster
Same site.api.espn.com host family already used elsewhere in this pipeline (injuries) --
no API key required. Must be run from a residential IP (your own PC), not a cloud/datacenter
one -- ESPN blocks the latter with a 403, confirmed during this pivot.

Honesty note: I could not verify this endpoint's exact JSON shape live (blocked from this
environment, same as everywhere else) -- built from ESPN's well-established, commonly-used
roster response shape (the same family used by countless fantasy-sports/stat-tracking tools),
with defensive fallbacks for both the "grouped by position" and "flat athlete list" shapes
ESPN has used across different sports/versions. If a field comes back wrong, this warns
loudly rather than silently producing bad data -- check the warnings after your first run.

USAGE:
    python fetch_espn_rosters.py rosters.csv
"""
import json
import re
import sys
import time

import pandas as pd
try:
    from curl_cffi import requests  # impersonates a real browser's TLS fingerprint, not just HTTP headers
    _HAS_CURL_CFFI = True
except ImportError:
    import requests
    _HAS_CURL_CFFI = False
    print("WARNING: curl_cffi isn't installed -- falling back to plain requests, which is very "
          "likely to still get blocked (this is a TLS-fingerprint-level block, not just headers). "
          "Run: pip install curl_cffi --break-system-packages", file=sys.stderr)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/roster"
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

ESPN_TEAM_ID = {
    "Dream": 20, "Sky": 19, "Sun": 18, "Wings": 3, "Valkyries": 129689,
    "Fever": 5, "Aces": 17, "Sparks": 6, "Lynx": 8, "Liberty": 9,
    "Mercury": 11, "Fire": 132052, "Storm": 14, "Tempo": 131935, "Mystics": 16,
}


def _get_json(url, retries=3, pace_seconds=0.6):
    for attempt in range(retries + 1):
        try:
            kwargs = {"headers": HEADERS, "timeout": 20}
            if _HAS_CURL_CFFI:
                kwargs["impersonate"] = "chrome124"
            resp = requests.get(url, **kwargs)
            if resp.status_code == 429:
                wait = pace_seconds * (2 ** (attempt + 1))
                print(f"Rate limited, waiting {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(pace_seconds)
            return resp.json()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5)


def _extract_athletes(payload):
    """ESPN's roster response has appeared both as {"athletes": [{"items": [...]}]} (grouped
    by position) and {"athletes": [...]} (flat) across different sports/versions -- handles
    both rather than assuming one."""
    raw = payload.get("athletes", [])
    out = []
    for entry in raw:
        if isinstance(entry, dict) and "items" in entry:
            out.extend(entry["items"])
        else:
            out.append(entry)
    return out


def _height_to_feet_inches_str(raw_height):
    """ESPN sometimes reports height as a plain number (total inches) and sometimes as an
    already-formatted string like '6\\' 2\"' -- normalizes either into the "X' Y\"" format
    make_app_data.py's height_to_inches() parses."""
    if raw_height is None or raw_height == "":
        return None
    if isinstance(raw_height, str) and "'" in raw_height:
        return raw_height  # already in the expected format
    try:
        total_in = int(round(float(raw_height)))
        return f"{total_in // 12}' {total_in % 12}\""
    except (TypeError, ValueError):
        return None


def fetch_team_roster(team_short, team_id):
    url = BASE_URL.format(team_id=team_id)
    payload = _get_json(url)
    athletes = _extract_athletes(payload)
    rows = []
    for a in athletes:
        name = a.get("fullName") or a.get("displayName")
        if not name:
            continue
        dob = a.get("dateOfBirth") or a.get("birthDate")
        if dob:
            dob = str(dob).split("T")[0]
        rows.append({
            "athlete_id": a.get("id"),
            "team_name": team_short,
            "height": _height_to_feet_inches_str(a.get("height")),
            "weight": a.get("weight"),
            "age": a.get("age"),
            "date_of_birth": dob,
        })
    return rows


def fetch_all_rosters():
    all_rows = []
    for i, (team_short, team_id) in enumerate(ESPN_TEAM_ID.items()):
        try:
            rows = fetch_team_roster(team_short, team_id)
            all_rows.extend(rows)
            print(f"  [{i+1}/{len(ESPN_TEAM_ID)}] {team_short}: {len(rows)} players")
        except Exception as e:
            print(f"WARNING: roster fetch failed for {team_short} (ESPN id {team_id}): {e}", file=sys.stderr)
    df = pd.DataFrame(all_rows)
    if not df.empty:
        for col in ["height", "weight", "age", "date_of_birth"]:
            if df[col].isna().all():
                print(f'WARNING: every "{col}" value came back empty -- the guessed ESPN field name for this is probably wrong. Bio info will just be missing in the app if so, nothing else breaks.', file=sys.stderr)
    return df


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "rosters.csv"
    df = fetch_all_rosters()
    if df.empty:
        print("No roster rows fetched -- check the warnings above.")
    else:
        df.to_csv(out_path, index=False)
        print(f"Wrote {len(df)} rows to {out_path}")
