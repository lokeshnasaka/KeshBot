# Daily Peanuts + Garfield → Discord (free, runs on your Mac)

This posts today's **Peanuts** and **Garfield** strips to a Discord channel once a
day, using a Discord **webhook** (no bot process, no server, no paid hosting).

### Why this runs on your Mac (and not a free cloud cron)

GoComics blocks datacenter IPs (returns HTTP 403). Every free cloud scheduler
(GitHub Actions, Oracle's free VM, Fly, etc.) runs from a datacenter, so they all
get blocked. A **residential IP — your Mac — is accepted.** So the script fetches
the image here and uploads it straight to Discord. The `launchd` + `pmset` setup
below makes it run daily even while the Mac is **asleep** (it can't run while the
Mac is fully **shut down**).

---

## One-time setup

### 1. Create a Discord webhook
In Discord: your server → the target channel → **Edit Channel** (gear) →
**Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.

### 2. Save the webhook URL (kept out of git)
```bash
cp daily/webhook.env.example daily/webhook.env
```
Open `daily/webhook.env` and paste your URL after `DISCORD_WEBHOOK_URL=`.
(`webhook.env` is gitignored — it will not be committed.)

### 3. Test it now
```bash
./.venv/bin/python daily/keshbot_daily.py
```
You should see both comics appear in your channel, and log lines ending in
`Posted 2, failed 0.`

### 4. Schedule it daily with launchd
```bash
cp daily/com.keshbot.dailycomics.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.keshbot.dailycomics.plist
```
This runs the script every day at **07:00 local time**. To change the time, edit
the `Hour`/`Minute` in the plist, then reload:
```bash
launchctl unload ~/Library/LaunchAgents/com.keshbot.dailycomics.plist
launchctl load   ~/Library/LaunchAgents/com.keshbot.dailycomics.plist
```

### 5. Make the Mac wake itself so it posts through sleep
`launchd` won't fire while the Mac is asleep — but macOS can wake itself first.
This wakes the Mac at 06:55 every day (5 min before the 07:00 job). Needs your
admin password:
```bash
sudo pmset repeat wakeorpoweron MTWRFSU 06:55:00
```
Check / cancel the wake schedule anytime:
```bash
pmset -g sched          # show it
sudo pmset repeat cancel # remove it
```
Notes:
- Works through **sleep**. It does **not** run if the Mac is fully powered off.
- On a **MacBook**, keep it plugged in — scheduled wake on battery with the lid
  closed is unreliable. A desktop Mac (mini/iMac/Studio) is ideal.
- If the Mac happens to be asleep and wasn't woken, launchd still posts at the
  next wake — you just get it late that day.

---

## Everyday use

- **Logs:** `daily/keshbot_daily.log`
- **Change comics:** edit the `COMICS` list in `keshbot_daily.py`.
- **Won't double-post:** the last strip posted per comic is remembered in
  `daily/.last_posted.json`, so re-runs the same day are skipped.
- **Stop it:** `launchctl unload ~/Library/LaunchAgents/com.keshbot.dailycomics.plist`
  and `sudo pmset repeat cancel`.
