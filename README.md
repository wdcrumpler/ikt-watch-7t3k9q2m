# Irkutsk Flight Watch

An early-warning monitor for unusual disruption to flights in and out of Irkutsk (IKT).
Every night it pulls the previous day's full schedule, with flight statuses, from
[AeroDataBox](https://aerodatabox.com) (via RapidAPI). It compares Irkutsk with its own
trailing baseline and with nearby **control airports** (Krasnoyarsk, Ulan-Ude), and pushes
a phone notification through [ntfy](https://ntfy.sh) if Irkutsk diverges.

- **ALERT**: Irkutsk shows a cancellation spike or a drop in scheduled flights, and the control airports do not.
- **WATCH**: Irkutsk is disrupted, but the region is too (probably weather or an airspace closure).
- **Weekly check-in**: a low-priority "still alive" message every Sunday, so a quiet phone never means a broken monitor.
- **Run failure**: if a GitHub Actions run fails, you get a notification.

The **volume drop** signal matters as much as cancellations. Airlines often remove flights
from the schedule ahead of time, and those never show up as "cancelled".

Runs on GitHub Actions and needs only the Python standard library. The dashboard is served from `docs/` via GitHub Pages.

---

## Setup

### 1. Subscribe to AeroDataBox on RapidAPI and copy your key
1. Go to <https://rapidapi.com/aedbx-aedbx/api/aerodatabox> while logged in to RapidAPI.
2. Click **Pricing** and subscribe to a plan:
   - **Basic (free)**: 400 units/month. That's enough for the daily check of 3 airports (~360 units/month) but not for a backfill or live checks. The baseline builds itself over the first 7 days.
   - **Pro ($8/month)**: 5,000 units/month. Allows a 21-day backfill so alerts work immediately, live checks every 3 hours, and a 4th control airport.
3. Open the **Endpoints** tab (the API playground). Your key is shown in the `X-RapidAPI-Key` field. Copy it, and never commit it to the repo.

### 2. Set up ntfy on your phone
1. Install the **ntfy** app (iOS App Store / Google Play).
2. Choose an unguessable topic name, e.g. `ikt-watch-7f3k9q2m`. Anyone who knows the name can read the alerts, so treat it like a password.
3. In the app, tap **+** and subscribe to that topic on `ntfy.sh`.

### 3. Create the GitHub repo and push
Create a new **empty** repo on github.com (no README), e.g. `irkutsk-flight-watch`, then:
```bash
git remote add origin https://github.com/<you>/irkutsk-flight-watch.git
git push -u origin main
```
If you want the dashboard on GitHub Pages with a free account, the repo has to be **public**.
Nothing sensitive is stored in it: secrets live in GitHub's secret store, and the data is public flight schedules.

### 4. Add the secrets
On the repo page, go to **Settings → Secrets and variables → Actions**.
- **Secrets** tab → *New repository secret*:
  - `RAPIDAPI_KEY` = your RapidAPI key
  - `NTFY_TOPIC` = your topic name (just the name, not the URL)
- **Variables** tab (optional): `DASHBOARD_URL` = `https://<you>.github.io/irkutsk-flight-watch/`. Tapping a notification then opens the dashboard.

### 5. Turn on the dashboard
**Settings → Pages** → Source: *Deploy from a branch* → Branch `main`, folder `/docs` → Save.

### 6. Test it
Go to **Actions → Irkutsk flight watch → Run workflow** and run these in order:
1. `test-alert`: your phone should buzz.
2. `health`: shows how AeroDataBox rates its data feed for each airport (free). Check the output in the run log or in `docs/data/health.json`.
3. `daily`: fetches yesterday (~12 units).
4. `backfill` with days = `21` (**Pro plan only**, ~250 units). Builds the baseline so alerts can fire immediately.

After that it runs on its own every night at 18:15 UTC (02:15 Irkutsk time).

---

## Tuning

Everything lives in `config.json`:

| Setting | Meaning |
|---|---|
| `airports` | Target plus controls. Move `OVB` in from `_optional_airports` if you're on Pro. |
| `monthly_unit_budget` | Hard cap. The script refuses any API call that would exceed it, so you can't run up overage charges. Set to ~4800 on Pro. |
| `intraday.enabled` | Live ±6h checks every 3 hours (Pro). About 2 units per airport per run. |
| `volume_drop_ratio` | Flag if scheduled flights fall below this fraction of the usual number (default 0.75). |
| `min_cancellations`, `cancel_rate_excess`, `cancel_z` | All three must be met to flag a cancellation spike. |

## Running locally
```bash
python monitor.py --help
```
To try it without an API key, use synthetic data. In PowerShell:
```powershell
$env:FLIGHTWATCH_FAKE=1; $env:FLIGHTWATCH_FAKE_SCENARIO="ikt_cancel"; $env:FLIGHTWATCH_FAKE_EVENT_DATE="2026-10-05"
python monitor.py backfill --days 20
```
Scenarios: `normal`, `ikt_cancel`, `ikt_volume`, `regional`. Run this in a copy of the folder,
because it writes into `docs/data/`.

## Limitations
- AeroDataBox reports 100% schedule coverage for Russia but only about **74% live-status coverage**. Flights that never get a status are counted as "unconfirmed", not cancelled. The volume signal doesn't depend on status at all.
- The daily check alerts about a day after the fact. Enable intraday checks (Pro) for alerts within about 3 hours.
- Flight disruption is one signal among many, and an indirect one. It can't tell you *why* flights stopped.
