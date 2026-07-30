# WNBA Box Score Explorer — automated rebuild + deploy pipeline

This repo automatically rebuilds the WNBA app from fresh data every 10 minutes and
deploys it straight to your existing Cloudflare Pages project — no manual steps
after setup, no need to ask Claude to do it.

**Important honesty note:** this pipeline guarantees the app is refreshed within
~10-15 minutes of the *source data* (SportsDataverse's wehoop GitHub release)
being updated. It does **not** guarantee that source updates within 10 minutes of
an actual game ending — that update cadence is controlled by a third party and
isn't something this pipeline (or Claude) can speed up.

## What's in here

```
.github/workflows/update.yml   <- the scheduled GitHub Action (runs every 10 min)
build/make_app_data.py         <- turns the source CSVs into app_data.json
build/app_template.html        <- the app's HTML/JS shell (edit this to change the app itself)
build/build.py                 <- orchestrates: download data -> generate -> compress -> inject -> dist/index.html
requirements.txt                <- Python deps (pandas, numpy)
```

## One-time setup

### 1. Create the repo
Create a **public** GitHub repo (public matters — Actions minutes are unlimited on
public repos; a private repo running every 10 minutes will burn through the free
tier's 2,000 minutes/month fast). Push all these files to it, preserving the folder
structure exactly as-is.

### 2. Get a Cloudflare API token
- Cloudflare dashboard -> your profile icon (top right) -> **My Profile** -> **API Tokens**
- **Create Token** -> find the **"Edit Cloudflare Workers"** template, or create a
  custom token with **Account -> Cloudflare Pages -> Edit** permission
- Copy the token (you only see it once)

### 3. Get your Account ID
- Cloudflare dashboard -> Workers & Pages -> your account ID is shown in the
  right-hand sidebar of the overview page (a long hex string)

### 4. Add both as GitHub repo secrets
In your GitHub repo: **Settings -> Secrets and variables -> Actions -> New repository secret**
- `CLOUDFLARE_API_TOKEN` = the token from step 2
- `CLOUDFLARE_ACCOUNT_ID` = the ID from step 3

(`GITHUB_TOKEN` is automatic — you don't need to add that one yourself.)

### 5. Check the project name matches
`.github/workflows/update.yml` deploys to a Cloudflare Pages project named
`wnba-explorer` — if yours is named differently, edit the `projectName:` line
in that file to match.

### 6. Test it
Go to the **Actions** tab in your repo -> click into "Rebuild and deploy WNBA app"
-> **Run workflow** (this is the manual trigger button, don't wait for the
schedule) -> watch it run. If it succeeds, your Cloudflare Pages URL should be
serving the freshly-built app within a minute or two after that.

## After setup

You don't need to do anything else. Every 10 minutes, GitHub will:
1. Download the latest box scores + rosters
2. Rebuild the app
3. Deploy it to your existing Cloudflare Pages URL automatically

If a run fails (bad data, API hiccup, etc.), check the **Actions** tab — every run's
full logs are there, and failures don't affect the currently-live site since a
deploy only happens if the build itself succeeds.

## Changing the schedule

Edit the `cron:` line in `.github/workflows/update.yml`. Cron format is
`minute hour day month weekday`, so `*/10 * * * *` = every 10 minutes,
`*/15 * * * *` = every 15 minutes, etc. Tighter than every 5 minutes isn't
recommended — GitHub's own scheduler becomes less reliable at that frequency.

## Making changes to the app itself

Edit `build/app_template.html` (this is the same file Claude has been iterating
on throughout this project) and push — the next scheduled run (or a manual
"Run workflow" trigger) will pick up your changes automatically.
