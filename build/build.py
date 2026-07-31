"""
Full build pipeline, run by GitHub Actions on a schedule:
  1. Download the latest box score + roster CSVs from the SportsDataverse (wehoop) releases
  2. Regenerate app_data.json from them
  3. Gzip + base64 the dataset and inject it into the HTML template
  4. Write the final, self-contained index.html into dist/ for deployment

Run locally with:  python build/build.py
"""
import gzip
import base64
import json
import os
import shutil
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from make_app_data import generate  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
DIST_DIR = os.path.join(REPO_ROOT, 'dist')

BOX_URL = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_wnba_player_boxscores/player_box_2026.csv'
ROS_URL = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_wnba_rosters/rosters_2026.csv'
PBP_URL = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_wnba_pbp/play_by_play_2026.csv'


def download(url, dest):
    print(f'Downloading {url} -> {dest}')
    urllib.request.urlretrieve(url, dest)
    size = os.path.getsize(dest)
    print(f'  wrote {size:,} bytes')


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DIST_DIR, exist_ok=True)

    box_csv = os.path.join(DATA_DIR, 'player_box_2026.csv')
    ros_csv = os.path.join(DATA_DIR, 'rosters_2026.csv')
    pbp_csv = os.path.join(DATA_DIR, 'pbp_2026.csv')
    data_json = os.path.join(DATA_DIR, 'app_data.json')

    download(BOX_URL, box_csv)
    download(ROS_URL, ros_csv)
    try:
        download(PBP_URL, pbp_csv)
    except Exception as e:
        print(f'WARNING: play-by-play download failed ({e}) — half-time markets will show as unavailable this run.')
        pbp_csv = None

    stats = generate(box_csv, ros_csv, data_json, pbp_csv)
    print('Generated dataset:', stats)

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

    # Write a marker file recording what this build actually contains, so it's easy to check
    # from outside (e.g. curling dist/build_info.json) whether a deploy actually picked up new games.
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

    # PWA assets — needed for "Add to Home Screen" + push notifications to work at all.
    build_dir = os.path.dirname(os.path.abspath(__file__))
    for asset in ['manifest.json', 'sw.js', 'icon-192.png', 'icon-512.png']:
        src = os.path.join(build_dir, asset)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DIST_DIR, asset))
        else:
            print(f'WARNING: expected PWA asset {asset} not found in build/ — Home Screen install / push notifications may not work.')

    print(f'Wrote {out_path} ({len(out_html):,} bytes)')
    print('Build info:', build_info)


if __name__ == '__main__':
    main()
