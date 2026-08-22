"""
Backfills quarter-by-quarter period scores for games whose box scores were fetched BEFORE the
pipeline started capturing periods (i.e. everything collected under the old SportsDataverse
path, plus any game whose scoreboard call happened to fail at the time).

WHY THIS IS CHEAP: period scores come from the SCOREBOARD response, not the per-game summary
response -- fetch_espn_recent_boxscores.get_completed_games() already reads them from there.
So a whole season costs ONE call per DAY (~110 calls), not one per GAME (~240 calls with a
slow per-game summary each). Same trick fetch_espn_venues.py uses for venues. Expect this to
finish in a couple of minutes, not the 5-10 the full box-score fetch takes.

Must be run from a residential IP (your own PC), not GitHub Actions -- same ESPN 403 block
that applies to every other direct-fetch script here.

Merges into the output file rather than replacing it: existing entries are preserved, and
re-running is safe and idempotent. Identical merge semantics to build.py's merge_periods().

USAGE
  # Current season, straight into the file build.py already reads:
  python backfill_periods.py

  # ...and report exactly which games are still missing periods afterwards:
  python backfill_periods.py --box-csv data/player_box_2026.csv

  # A historical season:
  python backfill_periods.py --after 2025-05-15 --through 2025-10-25 \
      --out historical_2025_periods.json --box-csv historical_2025_boxscores.csv

DIAGNOSE-ONLY (no fetching -- just tells you whether you actually have a gap):
  python backfill_periods.py --check-only --box-csv data/player_box_2026.csv
"""
import argparse
import json
import os
import sys
from datetime import date

from fetch_espn_recent_boxscores import _get_json, _dates_between, SCOREBOARD_URL

# Same convention as fetch_espn_recent_boxscores.py / fetch_espn_venues.py: exclusive, the day
# before the season's first game.
SEASON_START_2026 = "2026-05-07"
DEFAULT_OUT = os.path.join("data", "periods_2026.json")


def _clean_periods(linescores):
    """ESPN returns each period's points as `value`, sometimes as a float (21.0). Stores whole
    numbers as ints so the JSON reads like a line score instead of like a stats table. Returns
    None if any period is missing a value, rather than a list with holes in it -- a partial
    quarter breakdown is worse than none, since the app would render it as real."""
    out = []
    for p in linescores:
        v = p.get("value")
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        out.append(int(f) if f == int(f) else f)
    return out or None


def fetch_periods(after_date_str, through_date_str):
    """Returns {event_id_str: {"home": [...], "away": [...]}} for every ESPN-completed game in
    the range. home/away come straight off the competitor objects here -- no box score call and
    no team-id remapping needed, unlike the equivalent block in fetch_espn_recent_boxscores."""
    dates = _dates_between(after_date_str, through_date_str)
    found = {}
    skipped_no_linescores = []
    print(f"Checking {len(dates)} days ({after_date_str} exclusive through {through_date_str}), "
          f"one scoreboard call each...")

    for i, date_obj in enumerate(dates):
        date_str = date_obj.strftime("%Y-%m-%d")
        if (i + 1) % 20 == 0:
            print(f"  ...{i+1}/{len(dates)} days checked, {len(found)} games with periods so far")
        try:
            payload = _get_json(SCOREBOARD_URL, params={"dates": date_obj.strftime("%Y%m%d")})
        except Exception as e:
            print(f"WARNING: couldn't fetch scoreboard for {date_str}: {e}", file=sys.stderr)
            continue

        for ev in payload.get("events", []):
            status = ((ev.get("status") or {}).get("type") or {})
            if not status.get("completed"):
                continue
            comp = (ev.get("competitions") or [{}])[0]
            sides = {}
            for c in comp.get("competitors", []):
                side = c.get("homeAway")
                linescores = c.get("linescores") or []
                if side in ("home", "away") and linescores:
                    cleaned = _clean_periods(linescores)
                    if cleaned:
                        sides[side] = cleaned
            # Both sides or neither -- a one-sided line score can't be rendered.
            if len(sides) == 2:
                found[str(ev["id"])] = {"home": sides["home"], "away": sides["away"]}
            else:
                skipped_no_linescores.append((date_str, str(ev["id"])))

    if skipped_no_linescores:
        print(f"NOTE: {len(skipped_no_linescores)} completed game(s) had no usable linescores in "
              f"ESPN's scoreboard response and were skipped (these will keep showing the "
              f"'Final Score' fallback):")
        for d, eid in skipped_no_linescores[:10]:
            print(f"    {d}  event {eid}")
        if len(skipped_no_linescores) > 10:
            print(f"    ...and {len(skipped_no_linescores)-10} more")
    return found


def load_existing(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"WARNING: couldn't read existing {path} ({e}) -- treating as empty. The file will "
              f"be REPLACED, so move it aside first if you wanted to keep it.", file=sys.stderr)
        return {}


def report_coverage(periods, box_csv_path):
    """Compares the period keys against the game_ids actually present in the box score CSV --
    this is the number that matters, since a game with a box score but no periods is exactly
    what makes the app fall back to the total-only 'Final Score' box."""
    try:
        import pandas as pd
    except ImportError:
        print("NOTE: pandas isn't available here, skipping the coverage report.", file=sys.stderr)
        return
    try:
        box = pd.read_csv(box_csv_path, usecols=["game_id", "game_date"])
    except Exception as e:
        print(f"WARNING: couldn't read {box_csv_path} for the coverage report: {e}", file=sys.stderr)
        return
    # str(int(x)) matches exactly how make_app_data.py builds its lookup key.
    def key(x):
        try:
            return str(int(x))
        except (TypeError, ValueError):
            return str(x)
    by_game = box.drop_duplicates(subset=["game_id"])
    total = len(by_game)
    missing = [(key(r.game_id), r.game_date) for r in by_game.itertuples()
               if key(r.game_id) not in periods]
    have = total - len(missing)
    print(f"\nCoverage: {have}/{total} games in {os.path.basename(box_csv_path)} now have period scores.")
    if missing:
        missing.sort(key=lambda t: str(t[1]))
        print(f"{len(missing)} still missing (these render the 'Final Score' fallback):")
        for gid, d in missing[:15]:
            print(f"    {d}  game {gid}")
        if len(missing) > 15:
            print(f"    ...and {len(missing)-15} more")
    else:
        print("Every game with a box score also has quarter scores. Nothing left to backfill.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--after", default=SEASON_START_2026,
                    help=f"start date, EXCLUSIVE (default {SEASON_START_2026})")
    ap.add_argument("--through", default=None,
                    help="end date, inclusive (default: today)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"periods JSON to merge into (default {DEFAULT_OUT})")
    ap.add_argument("--box-csv", default=None,
                    help="box score CSV to report coverage against (optional but recommended)")
    ap.add_argument("--check-only", action="store_true",
                    help="don't fetch anything -- just report coverage of the existing file")
    args = ap.parse_args()

    existing = load_existing(args.out)
    print(f"Existing {args.out}: {len(existing)} games with period scores.")

    if args.check_only:
        if not args.box_csv:
            print("--check-only needs --box-csv to compare against.", file=sys.stderr)
            sys.exit(1)
        report_coverage(existing, args.box_csv)
        return

    through = args.through or date.today().strftime("%Y-%m-%d")
    fetched = fetch_periods(args.after, through)
    print(f"\nFetched period scores for {len(fetched)} completed games.")

    new_keys = [k for k in fetched if k not in existing]
    merged = dict(existing)
    merged.update(fetched)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(merged, f)
    print(f"Wrote {args.out}: {len(merged)} games total ({len(new_keys)} newly added this run).")

    if args.box_csv:
        report_coverage(merged, args.box_csv)

    print("\nNext: re-run `python build/build.py` -- make_app_data.py picks this file up "
          "automatically and attaches `periods` to every game it covers.")


if __name__ == "__main__":
    main()