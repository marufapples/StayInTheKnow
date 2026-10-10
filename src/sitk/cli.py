from __future__ import annotations

import argparse
import fcntl
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

from .config import CONFIG_PATH, DATA_DIR, ROOT, SESSION_DIR, load_config
from .store import Store, parse_iso

log = logging.getLogger("sitk")

LAUNCHD_LABEL = "com.stayintheknow.sitk"
LAUNCHD_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
TICK_SECONDS = 15 * 60

_APP_SUPPORT = Path.home() / "Library" / "Application Support"
CHROMIUM_DIRS = {
    "chrome": _APP_SUPPORT / "Google" / "Chrome",
    "brave": _APP_SUPPORT / "BraveSoftware" / "Brave-Browser",
    "edge": _APP_SUPPORT / "Microsoft Edge",
}


def setup_logging(verbose: bool) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(DATA_DIR / "sitk.log", maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    # httpx logs full request URLs at INFO, which would include the webhook secret.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def make_app():
    from .app import App
    from .notify import Discord
    from .sources.instagram import InstagramSource

    cfg = load_config()
    store = Store(DATA_DIR / "sitk.db")
    notifier = Discord(cfg.discord_webhook_url, cfg.discord_user_id)
    return App(cfg, store, notifier, lambda: InstagramSource(cfg.session_user, SESSION_DIR))


def ensure_config() -> None:
    if not CONFIG_PATH.exists():
        shutil.copy(ROOT / "config.example.toml", CONFIG_PATH)
        print(f"Created {CONFIG_PATH.name} from the example; edit it to add accounts.")


# --- commands ---
def cmd_test_webhook(args) -> int:
    from .notify import Discord

    cfg = load_config()
    Discord(cfg.discord_webhook_url, cfg.discord_user_id).send(
        "✅ Stay In The Know is connected. This is a test ping.", ping=True
    )
    print("Sent. Check Discord.")
    return 0


def cmd_login(args) -> int:
    from instaloader.__main__ import get_cookies_from_instagram

    from .sources.instagram import new_loader, save_session

    cookie_file = None
    if args.profile:
        base = CHROMIUM_DIRS.get(args.browser)
        if not base:
            print(f"--profile is only supported for {', '.join(CHROMIUM_DIRS)}")
            return 1
        cookie_file = next((str(p) for p in (base / args.profile / "Network" / "Cookies",
                                               base / args.profile / "Cookies") if p.exists()), None)
        if not cookie_file:
            print(f"No cookie database for profile '{args.profile}' under {base}")
            return 1

    print(f"Reading your Instagram session cookie from {args.browser}"
          f"{' (' + args.profile + ')' if args.profile else ''} "
          "(macOS may ask for Keychain access; allow it).")
    cookies = get_cookies_from_instagram("instagram", args.browser, cookie_file)
    if not cookies or "sessionid" not in cookies:
        print(f"No Instagram login found in {args.browser}. Log into instagram.com there first.")
        return 1
    loader = new_loader()
    loader.context.update_cookies(cookies)
    username = loader.test_login()
    if not username:
        print("Cookies found but Instagram says they aren't logged in. Log in again in the browser.")
        return 1
    loader.context.username = username
    path = save_session(loader, SESSION_DIR, username)
    print(f"Logged in as @{username}. Session saved to {path} (owner-only permissions).")

    ensure_config()
    text = CONFIG_PATH.read_text()
    CONFIG_PATH.write_text(re.sub(r'(?m)^session_user\s*=.*$', f'session_user = "{username}"', text, count=1))

    store = Store(DATA_DIR / "sitk.db")
    if store.get("auth_broken"):
        store.set("auth_broken", None)
        print("Cleared the paused state; polling resumes on the next tick.")
    return 0


def cmd_doctor(args) -> int:
    from .notify import Discord
    from .sources.base import AuthError
    from .sources.instagram import InstagramSource

    cfg = load_config()
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"{'✓' if passed else '✗'} {label}{(': ' + detail) if detail else ''}")

    check("config.toml exists", CONFIG_PATH.exists())
    try:
        Discord(cfg.discord_webhook_url, cfg.discord_user_id)
        check("Discord webhook URL looks valid", True)
    except ValueError as e:
        check("Discord webhook URL", False, str(e))
    check("Discord user ID set", cfg.discord_user_id.isdigit())
    check("watch accounts configured", bool(cfg.watch), ", ".join("@" + t.handle for t in cfg.watch))

    store = Store(DATA_DIR / "sitk.db")
    if broken := store.get("auth_broken"):
        check("session not paused", False, broken)

    try:
        src = InstagramSource(cfg.session_user, SESSION_DIR)
        check("Instagram session", True, f"logged in as @{src.whoami()}")
        if args.deep:
            for t in cfg.watch:
                p = src.profile(t.handle)
                check(f"@{t.handle} visible", p.viewer_can_see,
                      f"{p.followers} followers, {p.following} following, {'private' if p.is_private else 'public'}")
    except AuthError as e:
        check("Instagram session", False, str(e))
    except Exception as e:  # noqa: BLE001
        check("Instagram reachable", False, repr(e))
    return 0 if ok else 1


def cmd_tick(args) -> int:
    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another tick is running")
            return 0
        app = make_app()
        ok = app.tick(force=args.force or None)
        if ok and app.cfg.healthcheck_url:
            try:
                httpx.get(app.cfg.healthcheck_url, timeout=10)
            except httpx.HTTPError:
                log.warning("healthcheck ping failed")
        return 0 if ok else 1


def cmd_status(args) -> int:
    store = Store(DATA_DIR / "sitk.db")
    if broken := store.get("auth_broken"):
        print(f"⚠️  PAUSED (session broken): {broken}")
    for job in store.all_jobs():
        nxt, last = parse_iso(job["next_run_at"]), parse_iso(job["last_ok_at"])
        fmt = "%a %-I:%M %p"
        print(f"{job['name']:<28} next {nxt.astimezone().strftime(fmt) if nxt else '-':<14} "
              f"last ok {last.astimezone().strftime(fmt) if last else 'never':<14} "
              f"{('failing ' + str(job['failures']) + 'x: ' + job['last_error'][:80]) if job['failures'] else ''}")
    print("\nRecent changes:")
    for c in store.recent_changes(args.limit):
        when = parse_iso(c["at"]).astimezone().strftime("%b %-d %-I:%M %p")
        sign = "+" if c["change"] == "added" else "-"
        print(f"  {when}  @{c['account']} {c['kind']:<9} {sign} @{c['username']}")
    return 0


def cmd_launchd(args) -> int:
    uid = os.getuid()
    if args.action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], check=False)
        if os.path.exists(LAUNCHD_PATH):
            os.remove(LAUNCHD_PATH)
        print("Removed the background job.")
        return 0

    DATA_DIR.mkdir(exist_ok=True)
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, "-m", "sitk", "tick"],
        "WorkingDirectory": str(ROOT),
        "StartInterval": TICK_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(DATA_DIR / "launchd.out.log"),
        "StandardErrorPath": str(DATA_DIR / "launchd.err.log"),
        "ProcessType": "Background",
    }
    os.makedirs(os.path.dirname(LAUNCHD_PATH), exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], check=False, capture_output=True)
    with open(LAUNCHD_PATH, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", LAUNCHD_PATH], check=True)
    print(f"Installed. Ticks every {TICK_SECONDS // 60} min; jobs run on their own schedule.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sitk", description="Stay In The Know")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test-webhook", help="send a test ping to Discord").set_defaults(fn=cmd_test_webhook)

    p = sub.add_parser("login", help="import your Instagram session from a browser")
    p.add_argument("--browser", required=True,
                   choices=["chrome", "safari", "firefox", "brave", "edge", "chromium", "opera", "vivaldi"])
    p.add_argument("--profile", help='browser profile folder, e.g. "Profile 2" (default: Default)')
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("doctor", help="check config, webhook and Instagram session")
    p.add_argument("--deep", action="store_true", help="also look up each watched profile")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("tick", help="run whatever jobs are due (what the background job calls)")
    p.add_argument("--force", nargs="*", metavar="JOB", help='run now regardless of schedule, e.g. --force all')
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("status", help="show jobs and recent changes")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("launchd", help="install/uninstall the macOS background job")
    p.add_argument("action", choices=["install", "uninstall"])
    p.set_defaults(fn=cmd_launchd)

    args = parser.parse_args(argv)
    setup_logging(args.verbose or args.cmd != "tick")
    return args.fn(args)
