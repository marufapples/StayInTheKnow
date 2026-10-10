# Stay In The Know

Personal tool that runs on the owner's Mac. Using a session from the owner's own account, watches the owner's own Instagram alt accounts for new followers/follows, watches 2-3 public event pages for new posts, and alerts via a Discord webhook. Free only; no cloud, no web app.

Full plan, design, and phases: [PLAN.md](PLAN.md). Read it before making design decisions.

## Ground rules
- Follower/following tracking applies only to accounts the owner owns (their alts). Never add features that target other people's follow lists.

## Commands
- Tests: `.venv/bin/pytest`
- CLI: `.venv/bin/sitk {test-webhook,login,doctor,tick,status,launchd}`; see README.md.
- Never ask for or handle the Instagram password. Sessions come from the owner logging in via their browser; session files live in `~/.config/stayintheknow/` (chmod 600), never in the repo.
- Polling stays gentle: jitter, backoff, count pre-check. On a login challenge, stop and alert once; never retry in a loop.
- Diffs only on complete snapshots, with a 2-snapshot debounce for unfollows.
- Secrets (`.env`, webhook URL, sessions, `data/`) are gitignored and never logged or printed.
- Keep logic in plain modules under `src/sitk/`; Instagram access is behind the `Source` interface.
- Stay free: no paid APIs or services without asking.
