# Solar AC Monitor

Automatically monitors your battery (solar.siseli.com) and controls your TCL
split air-conditioner. Runs **always-on via GitHub Actions**, so it works even
when your PC is off.

## What it does on every scheduled run (every 10 min)
- Reads the battery State of Charge (SOC).
- **Auto turn-off:** if the AC is ON but the battery drops below `OFF_THRESHOLD` (50%), it turns the AC off.
- **Low battery alert:** if the battery drops below `ALERT_THRESHOLD` (30%), it pushes a notification to your phone via ntfy.sh.

## Manual actions (from your phone)
Trigger **"Run workflow"** in the GitHub Actions tab and pick `ac_action`:
- `on`  → turns the AC **on** (only if battery >= `ON_THRESHOLD`, 50%)
- `off` → turns the AC **off**
- `auto`→ just run the automatic checks

## Files
- `monitor.py`  — the headless monitor (battery + AC + notifications)
- `.github/workflows/monitor.yml`  — the GitHub Actions schedule
- `solar_app.py` + `index.html`  — optional local web UI (needs the PC to be on)
- `battery_ac.py`  — original one-shot script

## Setup (one time)
1. Create a new GitHub repo and push these files.
2. Add the encrypted **Secrets** and plain **Variables** (see `.env.example` for the full list):
   - Secrets: `SISELI_USER`, `SISELI_PASSWORD`, `SISELI_DEVICE_ID`, `TCL_USER`, `TCL_PASSWORD`, `TCL_AC_NICKNAME`, `NTFY_SERVER`, `NTFY_TOPIC`
   - Variables: `ON_THRESHOLD=50`, `OFF_THRESHOLD=50`, `ALERT_THRESHOLD=30`
3. The workflow runs automatically on the schedule. To also get the manual on/off buttons, enable the workflow and use **Actions → Solar AC Monitor → Run workflow**.
4. Install the free **ntfy** app, subscribe to your `NTFY_TOPIC`.

## Local testing (no GitHub needed)
```
pip install -r requirements.txt
$env:TCL_ACTION="on"; python monitor.py   # manual on
python monitor.py                          # auto
```
Set the same environment variables, or use a `.env` file.

## Notes
- Your AC must be online / WiFi-connected for the cloud commands to reach it.
- ntfy.sh is free and public by default. Use a hard-to-guess `NTFY_TOPIC` so others can't read your notifications.