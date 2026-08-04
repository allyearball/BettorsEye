"""
Fetches current WNBA injury reports from ESPN's public, unauthenticated site API and
returns them keyed by the app's short team names, ready to merge into app_data.json
under the key `injuries`.

Source: https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{espn_team_id}/injuries
This is the same site.api.espn.com host family already used elsewhere in this pipeline
(e.g. venue data) — no API key required.

Output shape (per team):
  {
    "Liberty": [
      {"player": "Sabrina Ionescu", "position": "G", "status": "Day-To-Day",
       "detail": "Ionescu won't play in the 2026 Unrivaled season due to an injury...",
       "date": "2026-01-13"},
      ...
    ],
    ...
  }

`status` is whatever ESPN reports verbatim (commonly: Out, Doubtful, Questionable,
Day-To-Day, Injured Reserve) — the frontend decides what counts as "likely to miss the
game" rather than this script hardcoding a status whitelist, since ESPN's exact wording
has changed before and may again.
"""
import json
import sys
from datetime import datetime, timezone

import requests

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/injuries"
OUTPUT_PATH = "injuries.json"

# ESPN's numeric team IDs, confirmed against https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams
ESPN_TEAM_ID = {
    "Dream": 20, "Sky": 19, "Sun": 18, "Wings": 3, "Valkyries": 129689,
    "Fever": 5, "Aces": 17, "Sparks": 6, "Lynx": 8, "Liberty": 9,
    "Mercury": 11, "Fire": 132052, "Storm": 14, "Tempo": 131935, "Mystics": 16,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def _extract_entry(item):
    """
    Normalizes one injury entry from ESPN's response. ESPN's site API nests the athlete
    name/position either directly on the item or one level down under `athlete`, and the
    long-form note under `details.longComment`/`shortComment` or a top-level `longComment`
    depending on endpoint version — this checks both shapes rather than assuming one.
    """
    athlete = item.get("athlete") or {}
    name = athlete.get("displayName") or item.get("displayName") or item.get("shortName")
    position = (athlete.get("position") or {}).get("abbreviation") or item.get("position")
    status = item.get("status") or (item.get("type") or {}).get("description")
    details = item.get("details") or {}
    detail_text = (
        details.get("longComment") or details.get("shortComment")
        or item.get("longComment") or item.get("shortComment") or ""
    )
    date = item.get("date")
    if date:
        date = str(date).split("T")[0]
    if not name:
        return None
    return {
        "player": name,
        "position": position,
        "status": status,
        "detail": detail_text,
        "date": date,
    }


def fetch_team_injuries(team_short, team_id):
    url = BASE_URL.format(team_id=team_id)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    payload = resp.json()

    # The injuries list has appeared under a few different keys across ESPN API versions —
    # check the common ones rather than hardcoding just one.
    raw_items = payload.get("injuries") or payload.get("items") or []
    entries = []
    for item in raw_items:
        entry = _extract_entry(item)
        if entry:
            entries.append(entry)
    return entries


def fetch_all_injuries():
    """
    Returns {team_short: [entry, ...]}. A single team's request failing doesn't take down
    the whole fetch — that team just gets an empty list and a warning, same philosophy as
    the rest of this pipeline's "don't let one bad source break the whole build" approach.
    """
    out = {}
    for team_short, team_id in ESPN_TEAM_ID.items():
        try:
            out[team_short] = fetch_team_injuries(team_short, team_id)
        except Exception as e:
            print(f"WARNING: injury fetch failed for {team_short} (ESPN id {team_id}): {e}",
                  file=sys.stderr)
            out[team_short] = []
    return out


def main():
    injuries = fetch_all_injuries()
    total = sum(len(v) for v in injuries.values())
    payload = {
        "_fetchedAt": datetime.now(timezone.utc).isoformat(),
        "_source": "site.api.espn.com WNBA team injuries",
        "teams": injuries,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {total} injury entries across {len(injuries)} teams to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
