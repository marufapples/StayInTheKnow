# Stay In The Know — Plan (v2)

Personal tool, runs on my Mac, alerts me through Discord. Free, no cloud, no web app.

## What it does
1. **Alt account watcher**: using a session from my own account (main or alt), watches **my own alt accounts** for *new followers*, *new follows*, and optionally unfollows/lost followers. Pings me on Discord. (Private alts must be followed by the session account.)
2. **Event pages watcher**: watches 2-3 public Instagram accounts (events/local happenings), posts new posts to Discord as embeds with parsed date/venue. Quiet, no ping unless it looks like an event.
3. **Health**: tells me when it breaks (session expired, challenged, not running).

## Scope guardrail
The follower/following tracking only ever covers the account whose session I log in with (my own alt). It is not pointed at other people's follow lists. The event watcher reads only public posts.

## Decision: Option B (logged-in unofficial access) on my Mac
- Library: **instaloader** (maintained, handles sessions, pagination, rate-limit hints). Wrapped behind my own `Source` interface so it can be swapped.
- Runs from a **residential IP** (my Mac). AWS/GCP/GitHub-runner IPs get blocked by Instagram fast, so the cloud is out for the Instagram side. AWS is not used. (If I later want 24/7 uptime: Raspberry Pi at home, same code.)
- Risk accepted: unofficial access is against Instagram's terms; the worst realistic outcome is a checkpoint challenge or a ban of the **alt**. Hence: alt only, gentle polling, never my main account.

## Login (I never handle the password)
- I log into the alt in my normal browser myself.
- Session is imported from the browser's cookies using instaloader's cookie-import approach, saved as a session file at `~/.config/stayintheknow/session-<user>` with `chmod 600`. Never in the repo.
- If Instagram challenges or invalidates the session: the app stops polling, sends **one** Discord message ("re-auth needed"), and does not retry in a loop. Retrying is what gets accounts banned.

## Polling behavior (gentle on purpose)
| Job | Interval | Notes |
|---|---|---|
| Event-page posts | every ~3 h | latest 12 posts each, skip already-seen ids (pinned posts make "stop at first seen" wrong) |
| Count pre-check per watched alt | every ~3 h | profile follower/following *counts* only |
| Full followers/following list | only when a count changed, a removal is pending confirmation, or 24 h since last full fetch | |

launchd ticks every 15 min, but a tick only does work when a job is due, so most ticks make zero requests.

- Random jitter (±20%) on every run, delays between requests, quiet hours (e.g. 1-7am) optional.
- Exponential backoff on 429/"please wait"; cap total requests per hour.
- The count pre-check means most runs make 1-2 requests instead of paginating thousands of users. A change that nets to zero (one follows, one unfollows) can be missed by counts alone, so a full fetch is still forced every ~6 hours.

## Diff correctness (avoid fake alerts)
- **First run = baseline**: store snapshot, send no alerts.
- Only diff against a **complete** fetch: the number of usernames fetched must match the profile's reported count within a small tolerance, otherwise discard the snapshot and retry later.
- A user gone from the list is only reported as "unfollowed" after being missing in **2 consecutive complete** snapshots (Instagram lists are flaky).
- Track users by numeric `user_id`, show current `@username` (usernames change).

## Discord
- One webhook URL (secret, in `.env`).
- **Pings** (`<@my_discord_id>` with `allowed_mentions` locked to me): new follower, new follow, session/health problems.
- **Quiet embeds, no ping**: new event-page posts. Event-looking posts get a distinct color and parsed fields.
- Batch if many changes at once (single message listing up to 10, "+N more"). Respect Discord rate limits (honor `Retry-After`).
- Daily heartbeat message at a set time ("alive, last successful poll X").

## Event extraction (free, heuristic)
Caption → `dateparser` + regex for date/time, venue/address cues, price ("free", "$"), keywords ("tonight", "doors", "RSVP"). Flag `is_event` with a confidence score; unparsed posts still get posted as plain embeds. Flyer-image OCR is a later phase (Tesseract, local, free).

## Storage: SQLite (`data/sitk.db`, gitignored)
- `accounts(user_id, handle, role)` role = alt | event_page
- `snapshots(id, kind[followers|following], taken_at, complete, count)`
- `snapshot_members(snapshot_id, user_id, username)`
- `relations(kind, user_id, first_seen, last_seen, missing_count, status)`
- `posts(post_id, account, caption, permalink, media_url, posted_at, first_seen, is_event, parsed_json, notified)`
- `runs(id, job, started_at, ok, error)`

## Scheduling
- **launchd** user agent (`~/Library/LaunchAgents/com.stayintheknow.plist`), fires the scheduler tick every 5 min; the scheduler itself decides which jobs are due (so intervals/jitter are config, not plist edits).
- Mac must be awake: either `caffeinate`-style power assertion while it runs, or accept gaps while the laptop sleeps. Missed windows are fine; the diff is state-based, not event-based, so nothing is lost, only delayed.
- **Dead-man's switch**: a free healthchecks.io ping per successful tick. If the Mac is off or the app is dead, healthchecks.io alerts me (it can post to the same Discord).

## Stack and layout
Python 3.12, `uv` for env, `instaloader`, `httpx`, `dateparser`, `pytest`, stdlib `sqlite3`/`tomllib`.
```
StayInTheKnow/
  PLAN.md  CLAUDE.md  README.md
  pyproject.toml
  config.example.toml        # accounts, intervals, quiet hours
  .env.example               # DISCORD_WEBHOOK_URL, DISCORD_USER_ID, HEALTHCHECK_URL
  src/sitk/
    sources/base.py          # Source protocol
    sources/instagram.py     # instaloader wrapper, session, backoff
    store.py                 # SQLite
    diff.py                  # snapshot diff + debounce logic
    extract.py               # event parsing
    notify.py                # Discord webhook, batching, rate limits
    scheduler.py             # due-job logic, jitter, quiet hours
    cli.py                   # sitk tick | baseline | doctor | test-webhook
  launchd/com.stayintheknow.plist.template
  tests/                     # diff, extract, notify (mocked), scheduler
```

## Status (2026-10-10)
Phases 0, 1, 3, 6 (launchd part) and 7 are built and tested with fakes. Discord webhook is verified live. Remaining: phase 2 (login), 4 (baseline on real accounts), 5 (event handles), healthchecks.io.

## Phases (I do the building; items marked ME need me)
0. **Scaffold**: git init, `.gitignore` (data/, .env, sessions), uv project, CI-less local pytest. Install check.
1. **Discord first**: `sitk test-webhook` to prove alerts reach my phone. **ME**: create the webhook in my server, give the URL + my Discord user ID (put in `.env` myself).
2. **Session import**: **ME**: log into the alt in my browser and tell Claude which browser. Claude runs the cookie import locally; `sitk doctor` verifies the session works with one cheap profile request.
3. **Source + store + diff** with tests against fixtures (no network), including partial-fetch and flaky-list cases.
4. **Baseline run** on the alt, then watch one real cycle by hand.
5. **Event pages**: **ME**: list the 2-3 public handles. Posts + extraction + embeds, tested against real captions.
6. **Scheduler + launchd**, healthchecks.io dead-man's switch (**ME**: free account, paste ping URL).
7. **Hardening**: backoff/challenge handling verified, daily heartbeat, README/runbook (re-auth steps).
8. **Later**: flyer OCR, Raspberry Pi move, more sources (Eventbrite, city calendars), digest of upcoming events.

## Risks
| Risk | Mitigation |
|---|---|
| Alt gets challenged/banned | gentle polling, count pre-check, stop-on-challenge, alt not main |
| Instagram changes internals, instaloader breaks | source interface; `doctor` command; pin + upgrade instaloader deliberately |
| False unfollow alerts | complete-snapshot check + 2-snapshot debounce |
| Mac asleep/off | state-based diff, dead-man's switch |
| Secrets leak | `.env` and session files outside repo / gitignored, chmod 600, webhook URL never logged |

## Open items for me
- Which browser is the alt logged into?
- The 2-3 public event handles.
- Discord webhook URL + my user ID (I will add to `.env` myself).
- Quiet hours, and whether I want unfollow alerts or just follows/followers.
