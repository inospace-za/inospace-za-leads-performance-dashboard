# Leads Performance Dashboard

A focused, monthly view of leads/enquiries → conversion → move-ins performance for
Inospace's storage sites, scored against agreed KPI bands. One tab per site — currently
Maitland, Salt River, and CBD (The Exchange), matching the source repo's `KNOWN_SITES`.
Same KPI bands apply to every site initially; can be split per site later if warranted.
CBD is the default tab on load.

This repo does **not** re-fetch SiteLink data. It piggybacks on the existing
[`sitelink-analytics-dashboard`](https://github.com/inospace-za/sitelink-analytics-dashboard)
repo, which already runs the daily Microsoft Graph → SiteLink XLSX → `data.json` pipeline.
This repo just reads that repo's committed `site/data.json` / `site/history.json` and
re-presents a leads-performance-specific view on top.

## How it works

```
sitelink-analytics-dashboard (private repo)
  site/data.json, site/history.json   ← already committed daily by that repo's own workflow
              │
              │  GitHub Contents API (read-only, fine-grained PAT — SEE "Auth" BELOW)
              ▼
etl/fetch_source_data.py   → _incoming/data.json, _incoming/history.json (git-ignored)
etl/build_leads_view.py    → site/leads_data.json   (per-site leads/conversion/move-in view,
                                                       scored against KPI bands)
              │
GitHub Pages ─ site/index.html  ──reads──>  leads_data.json   (static dashboard)
Cloudflare Access — same restriction as the ops dashboard (same audience).
```

**Why server-side (GitHub Actions), not a client-side fetch:** the source repo's Pages
site sits behind Cloudflare Access. A browser-side `fetch()` from this dashboard's page
would need a shared Access session across two different origins, which isn't reliable.
Reading the file directly from the source *repo* (not its published site) via the GitHub
Contents API sidesteps Cloudflare Access entirely — it's a private API call authenticated
by a token, not a website visit.

## Auth: the one thing you need to set up

Create a **fine-grained GitHub Personal Access Token**:
- Repository access: only `inospace-za/sitelink-analytics-dashboard`
- Permissions: **Contents: Read-only** (nothing else)
- Expiration: set a reminder to rotate it (same discipline as the Graph client secret in
  the source repo)

Add it as a secret named `SOURCE_REPO_PAT` in **this** repo's
Settings → Secrets and variables → Actions.

I can't create this token or the secret myself — GitHub tokens are a credential only you
or Justin can generate.

## ⚠ Before this is fully wired up

`etl/build_leads_view.py` is scaffolded but the exact field names it reads from
`data.json` / `history.json` are marked `TODO` — I haven't seen a real sample of those
files, only the source repo's README description of what they *should* contain. Send me
one real `data.json` and `history.json` (or let me pull them once the PAT secret exists
and I can run `fetch_source_data.py` for real) and I'll finish the mapping.

Also unconfirmed: whether `history.json` keeps per-site (CBD-only) leads history, or only
portfolio-blended figures before the daily pipeline started accumulating CBD-specific
data. The source README flags this same limitation on its own "Lead Velocity" panel. If
per-site history isn't there yet, this dashboard's trend will only be as long as the
number of days since this repo started running — that's expected, not a bug.

## Repo layout

```
etl/
  fetch_source_data.py   # GitHub Contents API → _incoming/*.json (git-ignored)
  build_leads_view.py    # _incoming/*.json → site/leads_data.json
  requirements.txt
site/
  index.html             # the dashboard (published by Pages)
  leads_data.json         # rebuilt each run
.github/workflows/
  update.yml            # daily cron (offset after the source repo's own cron), + manual run
.gitignore              # excludes _incoming/
```

## KPI bands (editable in etl/build_leads_view.py → BANDS dict)

- **Genuine Enquiries/month:** <40 Concerning · 40–60 Acceptable · 60–80 Good · 80+ Very Good
- **Conversion Ratio:** <20% Concerning · 20–25% Below Target · 25–30% Acceptable ·
  30–35% Good · 35–40% Very Good · 40%+ Excellent
- **Move-ins/month:** <10 Concerning · 10–15 Reasonable · 15–20 Good · 20+ Very Good
- **Occupancy % (sqm) health:** <20% Concerning · 20–30% Reasonable · 30–40% Good ·
  40–50% Very Good · 50%+ Exceptional

Per your last call: genuine/junk flagging and Website-vs-Walk-in channel split are
**dropped for now** — this dashboard uses SiteLink's raw Consolidated Lead Funnel numbers
as-is, no manual overlay.

## Local development

```bash
pip install -r etl/requirements.txt
export SOURCE_REPO_PAT=ghp_xxx
python etl/fetch_source_data.py
python etl/build_leads_view.py
cd site && python -m http.server 8000   # open http://localhost:8000
```

## Setting this up for real

1. Create this repo under `inospace-za` (I can't create repos myself — no GitHub write
   access from this session).
2. Push these files.
3. Add the `SOURCE_REPO_PAT` secret (see Auth above).
4. Enable GitHub Pages (Settings → Pages → deploy from `site/` or via the Actions
   artifact, matching however the source repo does it).
5. Send me a real `data.json`/`history.json` sample so I can finish `build_leads_view.py`.
6. If it needs the same Cloudflare Access restriction as the ops dashboard, that's set up
   at the Cloudflare zone level, same as before — Justin's call on config.
