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

import httpx

from .config import CONFIG_PATH, DATA_DIR, ROOT, SESSION_DIR, load_config
from .store import Store, parse_iso

log = logging.getLogger("sitk")

LAUNCHD_LABEL = "com.stayintheknow.sitk"
LAUNCHD_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
TICK_SECONDS = 15 * 60


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
    return App(cfg, store, notifier, lambda: InstagramSource(cfg.session_user, SESSION_DIR, cfg.user_agent))


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
    """Import cookies (all attributes) + the browser's UA. Never sends a request to Instagram."""
    from .cookies import (chromium_user_agent, describe_session, find_cookie_db, keychain_password,
                          read_chromium_cookies, save_session_file, to_cookiejar)
    from .sources.instagram import session_path

    cfg = load_config()
    try:
        db = find_cookie_db(args.browser, args.profile)
    except FileNotFoundError as e:
        print(e)
        return 1
    print(f"Reading Instagram cookies from {args.browser} profile '{args.profile}' "
          "(macOS may ask for Keychain access; allow it).")
    try:
        password = keychain_password(args.browser)
    except subprocess.CalledProcessError:
        print("Couldn't read the browser's Safe Storage key from Keychain (denied or not found).")
        return 1
    try:
        cookies = read_chromium_cookies(db, password)
    except (UnicodeDecodeError, ValueError):
        print("Couldn't decrypt the cookies (Safe Storage key doesn't match this profile's database).")
        return 1
    user_agent = cfg.user_agent or chromium_user_agent(args.browser)

    print("\nSession as it will be loaded:")
    print("\n".join(describe_session(to_cookiejar(cookies), user_agent)))
    if not {"sessionid", "csrftoken"} <= {c.name for c in cookies}:
        print(f"\nNo Instagram login in that profile. Log into instagram.com in {args.browser} first.")
        return 1
    if args.dry_run:
        print("\nDry run: nothing saved, no request sent to Instagram.")
        return 0

    username = (args.user or cfg.session_user).lstrip("@")
    if not username:
        print("\nPass --user <account logged in to that browser profile>.")
        return 1
    path = save_session_file(session_path(SESSION_DIR, username), username, user_agent, cookies,
                             source=f"{args.browser}:{args.profile}")
    print(f"\nSaved session for @{username} to {path} (owner-only permissions). No request was sent to Instagram.")

    ensure_config()
    text = CONFIG_PATH.read_text()
    CONFIG_PATH.write_text(re.sub(r'(?m)^session_user\s*=.*$', lambda m: f'session_user = "{username}"', text, count=1))

    store = Store(DATA_DIR / "sitk.db")
    if store.get("auth_broken"):
        store.set("auth_broken", None)
        print("Cleared the paused state; polling resumes on the next tick.")
    return 0


def cmd_doctor(args) -> int:
    """Without --deep: local checks only, no Instagram requests.
    With --deep: one auth check, then one profile lookup per watched account; stops at the first failure."""
    from .notify import Discord
    from .sources.base import AuthError

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

    app = make_app()
    try:
        src = app.source()  # local only: loads the session file, builds cookies + headers
    except AuthError as e:
        check("Instagram session file", False, str(e))
        return 1
    check("Instagram session file", True, f"@{cfg.session_user}, {len(src.session.cookies)} cookies")
    if not args.deep:
        print("\n".join(["Session as loaded:"] + src.diagnostics()))
        print("(no request sent; use --deep to contact Instagram)")
        return 0 if ok else 1

    try:
        check("Instagram auth", True, f"logged in as @{src.whoami()}")
        for t in cfg.watch:
            p = src.profile(t.handle)
            check(f"@{t.handle} visible", p.viewer_can_see,
                  f"{p.followers} followers, {p.following} following, {'private' if p.is_private else 'public'}")
    except AuthError as e:
        check("Instagram", False, str(e))
        app.mark_auth_broken(str(e))  # same as the scheduler: pause + one Discord ping
    except Exception as e:  # noqa: BLE001
        check("Instagram", False, repr(e))
    if not ok:
        print("\n".join(["\nDiagnostics:"] + src.diagnostics()))
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

    p = sub.add_parser("login", help="import Instagram cookies + User-Agent from a browser (no Instagram request)")
    p.add_argument("--browser", default="chrome", choices=["chrome", "brave", "edge"])
    p.add_argument("--profile", default="Default", help='browser profile folder, e.g. "Profile 2"')
    p.add_argument("--user", help="Instagram username logged in to that profile (default: session_user)")
    p.add_argument("--dry-run", action="store_true", help="print the cookie diagnostics only; save nothing")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("doctor", help="check config and session (no Instagram request without --deep)")
    p.add_argument("--deep", action="store_true",
                   help="contact Instagram: auth check + one lookup per watched profile")
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
