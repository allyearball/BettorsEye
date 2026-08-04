#!/usr/bin/env python3
"""
Scrapes VSiN's public DraftKings betting-splits table for WNBA and writes
vsin_splits.json in the shape the frontend expects at DATA.vsinSplits:

{
  "Aces":  {"spread": {"handle": 74, "bet": 56}, "total": {"handle": 59, "bet": 42}, "ml": {"handle": 50, "bet": 69}},
  "Dream": {"spread": {"handle": 26, "bet": 44}, "total": {"handle": 41, "bet": 58}, "ml": {"handle": 50, "bet": 31}},
  ...
}

Source page: https://data.vsin.com/betting-splits/?sport=WNBA
This page renders the Handle %/Bet % numbers for Spread, Total, and Moneyline
directly in the HTML without requiring a logged-in session — it's the same
table VSiN Pro subscribers see, just also reachable by a plain GET request.
VSiN does NOT publish splits for halves, team totals, player props, alt
lines, or parlays — only full-game Moneyline / Spread / Total — so this
script only ever produces those three keys per team.

Run this on the same schedule as the rest of the data pipeline (e.g. every
10 minutes via GitHub Actions) and merge vsin_splits.json's contents into
whatever JSON blob gets embedded as `DATA` in app_template.html, under the
key `vsinSplits`.

Requires: requests, beautifulsoup4
    pip install requests beautifulsoup4
"""
import json
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

VSIN_URL = "https://data.vsin.com/betting-splits/?sport=WNBA"
OUTPUT_PATH = "vsin_splits.json"

# Maps the team-name text VSiN renders (their site uses full franchise names,
# same as ours) to the short codes used throughout app_template.html.
FULL_TO_SHORT = {
    "Las Vegas Aces": "Aces", "Atlanta Dream": "Dream", "Indiana Fever": "Fever",
    "Portland Fire": "Fire", "New York Liberty": "Liberty", "Minnesota Lynx": "Lynx",
    "Phoenix Mercury": "Mercury", "Washington Mystics": "Mystics", "Chicago Sky": "Sky",
    "Los Angeles Sparks": "Sparks", "Seattle Storm": "Storm", "Connecticut Sun": "Sun",
    "Toronto Tempo": "Tempo", "Golden State Valkyries": "Valkyries", "Dallas Wings": "Wings",
}

HEADERS = {
    # A normal desktop UA — the splits table isn't behind auth, but some sites still
    # block obviously-bot requests (blank/py user agents) at the edge.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

PCT_RE = re.compile(r"(\d+)\s*%")


def _pct(text):
    """Pulls the leading integer percent out of a cell like '74% ▼' -> 74."""
    if not text:
        return None
    m = PCT_RE.search(text)
    return int(m.group(1)) if m else None


def fetch_html():
    resp = requests.get(VSIN_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_splits(html):
    """
    VSiN's splits table is one <table> per sport with a header row, then two
    rows per game (away team row, then home team row). Each row has cells in
    this order: [rotation/logo, team name+link, Spread point, Spread Handle%,
    Spread Bet%, Total point, Total Handle%, Total Bet%, ML price, ML Handle%,
    ML Bet%]. We only need the team name plus the three Handle/Bet pairs.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    table = soup.find("table")
    if table is None:
        print("WARNING: no <table> found on VSiN splits page — layout may have "
              "changed, or WNBA has no games posted today.", file=sys.stderr)
        return out

    rows = table.find_all("tr")
    for tr in rows:
        cells = tr.find_all("td")
        if len(cells) < 8:
            continue  # header / date-divider rows, not a team row

        # Team name is usually inside a link inside one of the first couple cells.
        team_link = tr.find("a", href=re.compile(r"/wnba/teams/"))
        if not team_link:
            continue
        team_full = team_link.get_text(strip=True)
        short = FULL_TO_SHORT.get(team_full)
        if not short:
            print(f"WARNING: unrecognized VSiN team name '{team_full}', skipping row.",
                  file=sys.stderr)
            continue

        cell_texts = [c.get_text(" ", strip=True) for c in cells]
        # Find the two %-bearing cells after each of the three "point/price" cells
        # by scanning for the pattern rather than hardcoding fixed indices, since
        # an extra leading rotation-number cell sometimes shifts everything by one.
        pct_cells = [i for i, t in enumerate(cell_texts) if PCT_RE.search(t)]
        if len(pct_cells) < 6:
            print(f"WARNING: expected 6 percent cells for {team_full}, found "
                  f"{len(pct_cells)} — row: {cell_texts}", file=sys.stderr)
            continue

        spread_handle, spread_bet, total_handle, total_bet, ml_handle, ml_bet = (
            _pct(cell_texts[i]) for i in pct_cells[:6]
        )

        out[short] = {
            "spread": {"handle": spread_handle, "bet": spread_bet},
            "total": {"handle": total_handle, "bet": total_bet},
            "ml": {"handle": ml_handle, "bet": ml_bet},
        }

    return out


def main():
    html = fetch_html()
    splits = parse_splits(html)

    if not splits:
        print("No splits parsed — leaving any existing vsin_splits.json untouched "
              "so a temporary scrape failure doesn't wipe out the last good data.",
              file=sys.stderr)
        sys.exit(1)

    payload = {
        "_fetchedAt": datetime.now(timezone.utc).isoformat(),
        "_source": VSIN_URL,
        "teams": splits,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(splits)} teams' splits to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
