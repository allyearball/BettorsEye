"""
Fetches current WNBA injury reports from ESPN's public, unauthenticated LEAGUE-WIDE injuries
endpoint and returns them keyed by the app's short team names, ready to merge into
app_data.json under the key `injuries`.

Source: https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries
This is a SINGLE call for the whole league (no per-team loop, no API key) -- the earlier
per-team endpoint (.../teams/{id}/injuries) was returning a server-side error (HTTP 500 /
"script error") for every team; this one is confirmed working against a real response.

Response shape, confirmed directly against a real payload (not guessed):
  {"injuries": [
    {"id": "20", "displayName": "Atlanta Dream", "injuries": [
      {"status": "Out", "date": "2026-08-09T21:11Z", "shortComment": "...",
       "athlete": {"displayName": "Te-Hina Paopao", "position": {"abbreviation": "G"}},
       "details": {"type": "Leg", "side": "Right", "returnDate": "2026-08-16"}},
      ...
    ]}, ...
  ]}

Output shape (per team) -- exactly what make_app_data.py expects for DATA.injuries:
  {"Dream": [{"player": "Te-Hina Paopao", "position": "G", "status": "Out",
              "detail": "Leg (Right)", "date": "2026-08-09"}, ...], ...}
"""
import json
import sys

try:
    from curl_cffi import requests
    _HAS_CURL_CFFI = True
except ImportError:
    import requests
    _HAS_CURL_CFFI = False
    print("WARNING: curl_cffi isn't installed -- falling back to plain requests, which is very "
          "likely to still get blocked (this is a TLS-fingerprint-level block, not just headers). "
          "Run: pip install curl_cffi --break-system-packages", file=sys.stderr)

URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/wnba/",
    "Origin": "https://www.espn.com",
}

FULL_NAME_TO_SHORT = {
    "Atlanta Dream": "Dream", "Chicago Sky": "Sky", "Connecticut Sun": "Sun",
    "Dallas Wings": "Wings", "Golden State Valkyries": "Valkyries", "Indiana Fever": "Fever",
    "Las Vegas Aces": "Aces", "Los Angeles Sparks": "Sparks", "Minnesota Lynx": "Lynx",
    "New York Liberty": "Liberty", "Phoenix Mercury": "Mercury", "Portland Fire": "Fire",
    "Seattle Storm": "Storm", "Toronto Tempo": "Tempo", "Washington Mystics": "Mystics",
}


def fetch_league_injuries():
    kwargs = {"headers": HEADERS, "timeout": 20}
    if _HAS_CURL_CFFI:
        kwargs["impersonate"] = "chrome124"
    resp = requests.get(URL, **kwargs)
    resp.raise_for_status()
    payload = resp.json()

    out = {}
    for team_entry in payload.get("injuries", []):
        display_name = team_entry.get("displayName", "")
        short = FULL_NAME_TO_SHORT.get(display_name)
        if not short:
            print(f"WARNING: unrecognized team displayName {display_name!r} -- add it to FULL_NAME_TO_SHORT.", file=sys.stderr)
            continue
        entries = []
        for inj in team_entry.get("injuries", []):
            athlete = inj.get("athlete") or {}
            details = inj.get("details") or {}
            # Prefer the human-written shortComment (e.g. "Paopao (leg) has been ruled out...")
            # since it's more informative than the raw type/side fields; falls back to
            # combining those fields when shortComment is missing.
            detail_text = inj.get("shortComment")
            if not detail_text:
                parts = [p for p in [details.get("type"), details.get("side")] if p]
                detail_text = " ".join(parts) or None
            raw_date = inj.get("date") or ""
            entries.append({
                "player": athlete.get("displayName"),
                "position": (athlete.get("position") or {}).get("abbreviation"),
                "status": inj.get("status"),
                "detail": detail_text,
                "date": raw_date[:10] if raw_date else None,
            })
        if entries:
            out[short] = entries

    if not out:
        print("WARNING: no injuries found for any team -- either genuinely a clean injury report right now, or ESPN changed this endpoint's shape again. Worth a quick manual check against a live response before trusting an empty result.", file=sys.stderr)
    else:
        total = sum(len(v) for v in out.values())
        print(f"Found {total} injury entries across {len(out)} teams.")
    return out


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "injuries.json"
    result = fetch_league_injuries()
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote injuries for {len(result)} teams to {out_path}")
