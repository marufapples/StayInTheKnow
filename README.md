# StayInTheKnow
if only you knew Lol.

Runs on my Mac, checks my Instagram alts every few hours, and pings me on Discord when they gain or lose followers or follow or unfollow someone. It can also post new posts from public event pages. It's free and has no cloud piece.

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env            # DISCORD_WEBHOOK_URL, DISCORD_USER_ID (never commit)
cp config.example.toml config.toml   # accounts to watch, schedule
.venv/bin/sitk test-webhook     # should ping you in Discord
```

## Log in to Instagram (no password involved)
1. Log into instagram.com in your browser with the account that will do the checking.
2. Check what will be imported, with no request to Instagram: `.venv/bin/sitk login --profile "Profile 2" --dry-run`. Use `--browser brave` or `--browser edge` for those browsers; the default browser is Chrome and the default profile is `Default`. macOS may ask for Keychain access. Allow it.
3. Save it with `.venv/bin/sitk login --profile "Profile 2" --user <that_account>`.
   - The cookies are saved with all their attributes (domain, path, expiry, secure, HttpOnly, SameSite), along with the browser's exact User-Agent.
   - They go to `~/.config/stayintheknow/session-<user>` with owner-only permissions, and `session_user` in `config.toml` is filled in for you.
   - `login` never contacts Instagram.
4. `sitk doctor` shows the session as loaded and makes no requests. `sitk doctor --deep` contacts Instagram and prints the full diagnostics if anything fails.

If the checking account is not the watched alt, and the alt is private, the checking account must follow the alt.

## Run
```bash
.venv/bin/sitk doctor --deep        # verify everything, look up each watched profile
.venv/bin/sitk tick --force all     # run all jobs now (first run = silent baseline)
.venv/bin/sitk status               # job schedule + recent follower/following changes
.venv/bin/sitk launchd install      # run in the background (ticks every 15 min)
.venv/bin/sitk launchd uninstall
```

## How it behaves
- **Polling:** it checks counts every ~3h (±20% jitter) and fetches the full lists only when a count changed, plus once a day regardless. It sends no Instagram requests during quiet hours (1-7am by default).
- **First run:** records a baseline and sends no pings.
- **Accuracy:** it compares only complete list fetches. An unfollow is reported only after the person is missing from two complete fetches in a row.
- **Login problems:** if Instagram challenges the login, polling pauses and you get one Discord ping. Fix it by logging in again in your browser and running `sitk login` again.
- **Rate limits:** if Instagram rate-limits it, it backs off to double the interval.
- **Daily heartbeat:** a status message at 9am shows it's still alive.
- **Missed runs:** if the Mac is asleep, checks are delayed but nothing is missed, because changes are computed from saved state.

## Files
`data/` (SQLite db, logs) and `.env` / `config.toml` are gitignored. Code lives in `src/sitk/`, tests in `tests/` (`.venv/bin/pytest`).
