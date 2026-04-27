# Deployment guide — Render via GitHub

Step-by-step instructions to deploy the tech collector to Render as a
FastAPI web service with persistent disk, wired up the same way as your
existing S&P 500 Intraday and Coinbase Crypto scanners.

## Prerequisites

Before you begin:

- A GitHub account with permission to create a new repo.
- Your Render account (same one as your other scanners).
- The `tech_collector.zip` I produced earlier. This contains all nine
  code files plus the new `api.py`, `jobs.py`, `render.yaml`,
  `requirements.txt`, and `.gitignore`.
- Your Alpaca API key and secret (from `app.alpaca.markets` →
  API Keys). SIP subscription must be active.
- The original `tech_research_dataset.csv` that you uploaded at the start
  of the conversation — you'll need it on the Render disk for the
  `/validate` endpoint to work.
- A generated shared-secret string for the API key — any random string
  of 32+ characters. Use `python -c "import secrets; print(secrets.token_urlsafe(32))"`
  or just mash the keyboard in a way you'll remember to copy somewhere.

## Step 1 — Create the GitHub repository

Unzip the project locally first so you have files to push:

```bash
mkdir -p ~/projects && cd ~/projects
mv ~/Downloads/tech_collector.zip .
unzip tech_collector.zip
cd tech_collector
```

You should see these files:

```
__init__.py     cli.py              exporter.py          render.yaml
api.py          collector.py        feature_computer.py  requirements.txt
jobs.py         config.py           storage.py           validate.py
.gitignore      README.md           DEPLOYMENT.md
```

Create a new empty repo on GitHub called `tech-collector` (or any name
you like; I'll use `tech-collector` in the examples). Do not initialize
it with a README or license — we want it empty.

Then from inside the project folder:

```bash
git init
git add .
git commit -m "Initial commit — tech collector FastAPI service"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/tech-collector.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your actual GitHub username. If GitHub asks
for authentication, use a personal access token from
`github.com/settings/tokens`, not your password.

Verify the repo is pushed by visiting `github.com/YOUR_USERNAME/tech-collector`
in a browser — you should see all the files.

## Step 2 — Create the Render service from the blueprint

Render can read `render.yaml` and set up the service, disk, and env var
slots automatically. This is the same Blueprint flow you used for your
other scanners.

1. Go to `dashboard.render.com`.
2. Click **New** → **Blueprint**.
3. Connect the `tech-collector` GitHub repo you just created. If Render
   doesn't see it, you may need to authorize Render to access the repo
   under GitHub settings.
4. Render will parse `render.yaml` and show what it's going to create:
   - One web service called `tech-collector` on the `starter` plan
   - One 5 GB persistent disk called `tech-collector-data`
   - Four environment variables that need values:
     `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `API_KEY`, `DATA_DIR`
   - `DATA_DIR` will auto-populate to `/var/data`; the other three
     you'll fill in next.
5. Click **Apply** or **Create Blueprint**.

Render will start the first build immediately. It will fail at startup
because the three secrets aren't set yet. That's fine; we fix that next.

## Step 3 — Set the three secret environment variables

In the Render dashboard:

1. Click into the `tech-collector` service.
2. Go to the **Environment** tab in the left sidebar.
3. You'll see the three env var rows with empty values. Click the edit
   icon on each and paste the corresponding value:
   - `ALPACA_API_KEY` → your Alpaca key starting `PK...`
   - `ALPACA_API_SECRET` → your Alpaca secret
   - `API_KEY` → your generated shared-secret string
4. Click **Save Changes**. Render will trigger a redeploy automatically.

Wait ~2–3 minutes for the redeploy. Watch the **Logs** tab. When you see
`Uvicorn running on http://0.0.0.0:10000` and `Tech Collector starting.`,
the service is up.

## Step 4 — Open the dashboard

Open your Render service URL in a browser (shown at the top of the
service's dashboard page — something like `https://tech-collector.onrender.com`).

You'll see a dashboard UI with five sections: Setup, Collect & compute,
Validate, Evidence packs, Jobs history. If the status indicator in the
top-right says "online", your deployment is working.

If you get `503 Service Unavailable` for the first minute or two, Render
is still spinning up. Just retry.

**Use this dashboard for the remaining steps.** If for any reason you
prefer to drive the app with curl (e.g. automation scripts), every
dashboard action has a matching curl equivalent — see the endpoint
table in the README.

## Step 5 — Save the API key and upload the reference CSV

In the dashboard:

1. **Section 01 → Setup**. Paste your API key (the `API_KEY` shared
   secret you set in Step 3, not the Alpaca key) into the API key field
   and click **Save**. It's stored in your browser's localStorage so
   you won't need to retype it.

2. Right below, **Reference CSV upload**. Click **Choose file**, pick
   the `tech_research_dataset.csv` I provided in the chat (you have it
   as a separate download), click **Upload**. Takes 10–30 seconds.
   The status line underneath will confirm with the saved path and size.

That's it. The CSV now lives on Render's persistent disk and stays
there across deploys. You only do this once.

*If you'd rather use curl:* the equivalent is
`curl -X POST -H "X-API-Key: YOUR_KEY" -F "file=@tech_research_dataset.csv" https://YOUR-SERVICE.onrender.com/upload-reference`

## Step 6 — Smoke test with a one-week backfill

Before the full two-year backfill, do a one-week test to confirm
everything works end-to-end. About 5 minutes of real time.

In the dashboard:

1. **Section 02 → Collect & compute**. Change the dates to:
   - Start date: `2025-03-17`
   - End date: `2025-03-21`
2. Click **Start backfill**. You'll see a toast confirming the job
   started. Scroll down to section 05 to watch it run — status will
   be `running`, then `succeeded` after 1–3 minutes.
3. Click **Start compute** (same dates still filled in). Watch
   section 05 again; status goes to `succeeded` after ~30–60 seconds.
4. **Section 03 → Validate**. Click **Run validation**. The result
   appears inline below — a pass/fail pill at the top and a table
   of per-feature stats. **Look for the "passed" pill.**

If validation fails, the table will highlight the offending features
in red, and a line below lists `Features above 1% median diff: ...`.
Paste that list (or a screenshot) into this chat and I'll diagnose.

**Do not proceed to the full backfill until validation passes.**

## Step 7 — Run the full backfill

Once the smoke test passes, change the dates in section 02 to:
- Start date: `2024-04-19`
- End date: `2026-04-17`

Click **Start backfill**. This job runs for 30–90 minutes. Section 05
auto-refreshes every 5 seconds while jobs are active. You can close
the browser — the job keeps running on Render. Come back later and
the job list will show it as `succeeded`.

If Render restarts the service during the job (rare but possible),
the job record is lost but bars already written to SQLite are durable.
Just click **Start backfill** again with the same dates — it will
re-pull only what's missing.

Once backfill is `succeeded`, click **Start compute** with the same
dates. Expect 5–15 minutes.

## Step 8 — Generate and download the evidence pack

In the dashboard:

1. **Section 04 → Evidence packs**. The date range comes from section 02
   (same as the full backfill). Click **Generate pack**.
2. After a few seconds, the pack appears in the table below with a
   filename, size, and timestamp. Click **Download** to stream the zip
   to your laptop.

## Step 9 — Upload the pack here

```bash
curl -X POST https://tech-collector.onrender.com/pack \
     -H "X-API-Key: YOUR_SHARED_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"start":"2024-04-19","end":"2026-04-17"}'
```

Response:

```json
{
  "pack_path": "/var/data/evidence_packs/tech_research_export_2024-04-19_to_2026-04-17.zip",
  "pack_filename": "tech_research_export_2024-04-19_to_2026-04-17.zip",
  "download_url": "/packs/tech_research_export_2024-04-19_to_2026-04-17.zip"
}
```

## Step 9 — Download the pack

Two ways to get the zip off the Render disk:

**Option A — HTTP download endpoint (recommended).**

```bash
curl -H "X-API-Key: YOUR_SHARED_SECRET" \
     -o tech_research_export.zip \
     https://tech-collector.onrender.com/packs/tech_research_export_2024-04-19_to_2026-04-17.zip
```

This streams the zip directly to your laptop. Expect 10–15 MB.

**Option B — Render Shell + scp.** If the HTTP download has issues (e.g.
connection timeouts on very large files), you can use Render's SSH
access to scp the file locally. Render provides SSH under the service's
**Connect** tab. Command looks like:

```bash
# from your laptop, with Render SSH configured
scp srv-xxx@ssh.oregon.render.com:/var/data/evidence_packs/*.zip ./
```

Option A is simpler; Option B is the fallback.

## Step 10 — Upload the pack here

Drag the downloaded zip into a new message in this chat and say what
you want me to look at (e.g. "re-run the full pattern analysis" or
"compare to original research").

## Monitoring and maintenance

- **Logs:** Render's **Logs** tab streams stdout/stderr. Uvicorn logs
  every HTTP request; the app logs to stdout as well.
- **Disk usage:** check with `df -h /var/data` in the Render Shell.
  A full backfill uses ~2–3 GB for SQLite plus ~100 MB for packs.
- **Service restart:** use the **Manual Deploy** button in the
  dashboard if you need a clean restart. All disk state persists.
- **Updates:** push commits to GitHub `main`; Render auto-deploys.
  Existing disk state is preserved across deploys.

## Costs

- Render starter web service: $7/month
- 5 GB persistent disk: $0.25/GB/month = $1.25/month
- Total: ~$8.25/month

Same billing account as your other scanners.

## Security notes

- The `API_KEY` env var is the only thing protecting your endpoints from
  random internet traffic. Treat it like a password — don't commit it to
  GitHub, don't paste it in Slack.
- If you suspect the key has leaked, regenerate it in the Render
  dashboard and update any clients (your own curl commands, any IDE
  integrations). Existing running jobs aren't affected.
- The Alpaca credentials have full data-feed access. If they leak,
  someone could rack up API calls on your account (they can't trade
  without the trading permissions on the key). Rotate them in the
  Alpaca dashboard if concerned.
- Render's persistent disk is encrypted at rest by AWS EBS; the SQLite
  database sitting on it inherits that.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `503 Service Unavailable` on first request | Service still booting | Wait 1–2 minutes |
| `401 Unauthorized` | `X-API-Key` header missing or wrong | Check you copied the env var value exactly |
| `500 "Server not configured"` | `API_KEY` env var not set in Render | Set it in the Environment tab |
| Backfill job fails with `AlpacaCredentialsError` | Env vars lost during restart | Check Environment tab; Render sometimes needs them resaved |
| Backfill job status stuck on `running` | Either legitimately long, or service restarted | Check `/jobs` — if the job is gone, restart it |
| `/validate` returns `CSV not found` | Reference CSV not copied to disk | Re-do Step 5 |
| Features above 1% median diff | Feature definition mismatch | Stop, paste the JSON report into chat for diagnosis |
| Disk fills up | Backfill ran to larger range than expected | Shell in, `ls /var/data/evidence_packs/`, delete old packs |

## What's not covered

- **Render Blueprint rollback:** if you need to roll back, use Git. Push
  a revert commit to `main`; Render auto-deploys. The disk survives.
- **Scaling:** this service is single-instance by design. Multiple
  instances would race on the SQLite database. Don't change the instance
  count in Render.
- **Staging environment:** this guide sets up one environment called
  production. If you want a staging service pointing at a second GitHub
  branch, duplicate the Blueprint with a different name.

## v0.7.29 Rule034 merged monitor

This version preserves Rule009 refined, Rule029, and Rule033, and adds Rule034 conservative monitoring. After deploy, confirm `/health` reports `0.7.29` and the dashboard exposes Rule009, Rule029, Rule033, and Rule034 controls.
