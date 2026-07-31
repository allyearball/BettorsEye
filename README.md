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
build/check_gold_bets.py       <- checks today's odds for gold-tier (96%+) bets and sends push notifications
build/generate_vapid_keys.py   <- (re)generates the crypto keys push notifications need
build/manifest.json            <- makes the app installable to your iPhone Home Screen
build/sw.js                    <- service worker; handles incoming push notifications
build/icon-192.png, icon-512.png  <- app icons for the Home Screen
requirements.txt                <- Python deps (pandas, numpy, requests, pywebpush)
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

## Push notifications for gold-tier bets (96%+, 10+ games of history)

This is a bigger setup than everything above — it adds real push notifications
to your iPhone, triggered whenever a bet clears 96% with at least 10 games of
actual history behind it (matching the same safeguard used elsewhere in the app
so a tiny sample can't fake a gold-tier signal).

**How it works, end to end:** your phone subscribes to push through the app →
that subscription is stored in a new Cloudflare KV store → every 10 minutes, the
same GitHub Action that rebuilds the app also fetches today's live odds, checks
them against your historical data, and — only for genuinely *new* qualifying
bets it hasn't already told you about — sends a real push notification.

### 1. On your iPhone: install the app (required for push to work at all)

Open your Cloudflare Pages URL in Safari → Share button → **Add to Home Screen**.
iOS only allows push notifications for installed PWAs, never for a regular
Safari tab — this step isn't optional.

### 2. Generate your push encryption keys

On your computer, from this repo:
```
pip install cryptography --break-system-packages
python build/generate_vapid_keys.py
```
This prints a **public** key and a **private** key. Keep this terminal output
somewhere safe for the next two steps.

### 3. Put the public key in the app

Open `build/app_template.html`, find the line:
```js
const VAPID_PUBLIC_KEY = '...';
```
Replace the value with the **public** key you just generated. Commit and push.

### 4. Create three new KV namespaces

Cloudflare dashboard → Workers & Pages → **KV** → Create a namespace, three times:
- `ODDS_HISTORY` (you may already have this from an earlier setup step)
- `PUSH_SUBS`
- `NOTIFIED_LEGS`

Then on your Worker (the same `odds-api-proxy` Worker from before): **Settings →
Bindings → Add**, once for each, using these exact variable names:
`ODDS_HISTORY`, `PUSH_SUBS`, `NOTIFIED_LEGS` — matched to the namespace of the
same name. Save & Deploy after adding all three.

### 5. Update the Worker code

Replace your Worker's code with the latest version (the one with `/push/subscribe`,
`/push/list`, and `/push/check-and-mark` endpoints) and deploy.

### 6. Add a notification secret to the Worker

Same place you added `ODDS_API_KEY` before (Worker → Settings → Variables and
Secrets → Add): add `NOTIFY_SECRET` = any random string you make up yourself
(this isn't from any external service — you're inventing a shared password that
only your Worker and your GitHub Action will know, to stop random people on the
internet from reading your subscriber list or spamming your notification log).

### 7. Add four new GitHub repo secrets

Same place as your Cloudflare secrets (**Settings → Secrets and variables →
Actions → New repository secret**):
- `WORKER_URL` = your Worker's URL (e.g. `https://odds-api-proxy.yourname.workers.dev`)
- `NOTIFY_SECRET` = the exact same value you just set on the Worker in step 6
- `VAPID_PRIVATE_KEY` = the **private** key from step 2 (never the public one)
- `VAPID_SUBJECT` = a contact URL identifying you, e.g. `mailto:you@example.com`

### 8. Enable notifications in the app

Open the app **from the Home Screen icon** (not from a Safari tab) → go to the
DraftKings Odds page → tap **"🔔 Enable Gold-Tier Bet Alerts"** → allow the
permission prompt. That's it — you're subscribed.

### Notes on how this actually behaves

- You'll only ever be notified about **today's games**, and only for bets that
  clear 96% with 10+ games of real history — the exact same bar used for the
  gold coloring elsewhere in the app.
- The same bet won't spam you repeatedly — once notified, it's suppressed for
  12 hours automatically.
- If you ever uninstall the app from your Home Screen or revoke notification
  permission, you'll need to redo step 8 to resubscribe — there's no way around
  this on iOS, it's how Apple's push permission model works for everyone.
- If `check_gold_bets.py` can't find `WORKER_URL`, `NOTIFY_SECRET`, or the VAPID
  secrets, it just skips itself quietly — it will never break your main
  rebuild/deploy step, even if you haven't finished this section yet.
