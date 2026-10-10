"""Import Instagram cookies from a Chromium browser on macOS with all attributes intact,
and persist them (plus the browser's User-Agent) as the sitk session file.

Never prints or logs cookie values.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from Cryptodome.Cipher import AES
from requests.cookies import RequestsCookieJar, create_cookie

SESSION_FORMAT = 2
COOKIE_DOMAIN = "instagram.com"
CHROME_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01
SAMESITE = {-1: "Unspecified", 0: "None", 1: "Lax", 2: "Strict"}
DIAG_COOKIES = ("sessionid", "csrftoken", "ds_user_id")

_APP_SUPPORT = Path.home() / "Library" / "Application Support"


@dataclass(frozen=True)
class Browser:
    data_dir: Path
    keychain_service: str
    keychain_account: str
    app_plist: Path


BROWSERS = {
    "chrome": Browser(_APP_SUPPORT / "Google" / "Chrome", "Chrome Safe Storage", "Chrome",
                      Path("/Applications/Google Chrome.app/Contents/Info.plist")),
    "brave": Browser(_APP_SUPPORT / "BraveSoftware" / "Brave-Browser", "Brave Safe Storage", "Brave",
                     Path("/Applications/Brave Browser.app/Contents/Info.plist")),
    "edge": Browser(_APP_SUPPORT / "Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge",
                    Path("/Applications/Microsoft Edge.app/Contents/Info.plist")),
}


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str
    expires: int | None  # unix seconds; None = session cookie
    secure: bool
    httponly: bool
    samesite: str


# --- reading Chrome's cookie DB ---
def find_cookie_db(browser: str, profile: str = "Default") -> Path:
    base = BROWSERS[browser].data_dir / profile
    for candidate in (base / "Network" / "Cookies", base / "Cookies"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"no cookie database for {browser} profile '{profile}' under {base}")


def keychain_password(browser: str) -> bytes:
    b = BROWSERS[browser]
    out = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", b.keychain_service, "-a", b.keychain_account],
        capture_output=True, check=True,
    )
    return out.stdout.strip()


def derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)


def decrypt_value(encrypted: bytes, key: bytes, db_version: int) -> str:
    if not encrypted.startswith((b"v10", b"v11")):
        return encrypted.decode()
    plain = AES.new(key, AES.MODE_CBC, iv=b" " * 16).decrypt(encrypted[3:])
    plain = plain[:-plain[-1]]  # PKCS#7
    if db_version >= 24:
        plain = plain[32:]  # SHA256(host_key) prefix added in Chrome's cookie DB v24
    return plain.decode()


def chrome_time_to_unix(value: int) -> int:
    return int(value / 1_000_000 - CHROME_EPOCH_OFFSET)


def read_chromium_cookies(db_path: Path, password: bytes, domain: str = COOKIE_DOMAIN) -> list[BrowserCookie]:
    """Copy the DB first: Chrome keeps it locked while running."""
    key = derive_key(password)
    with tempfile.TemporaryDirectory() as tmp:
        for src in db_path.parent.glob(db_path.name + "*"):  # Cookies, Cookies-journal, -wal
            shutil.copy2(src, Path(tmp) / src.name)
        db = sqlite3.connect(Path(tmp) / db_path.name)
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            version = int(row[0]) if row else 0
            rows = db.execute(
                """SELECT host_key, name, value, encrypted_value, path, expires_utc, has_expires,
                          is_secure, is_httponly, samesite
                   FROM cookies WHERE host_key = ? OR host_key LIKE ?""",
                (domain, f"%.{domain}"),
            ).fetchall()
        finally:
            db.close()

    cookies = []
    for host, name, value, enc, path, expires, has_expires, secure, httponly, samesite in rows:
        cookies.append(BrowserCookie(
            name=name,
            value=value or decrypt_value(enc, key, version),
            domain=host,
            path=path,
            expires=chrome_time_to_unix(expires) if has_expires and expires else None,
            secure=bool(secure),
            httponly=bool(httponly),
            samesite=SAMESITE.get(samesite, "Unspecified"),
        ))
    return cookies


# --- User-Agent ---
def chromium_user_agent(browser: str = "chrome", plist_path: Path | None = None) -> str:
    """The exact UA Chrome sends on macOS. Chrome's UA reduction freezes the OS token and
    minor version numbers, so only the major version varies."""
    with open(plist_path or BROWSERS[browser].app_plist, "rb") as f:
        major = plistlib.load(f)["CFBundleShortVersionString"].split(".")[0]
    ua = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    return ua + (f" Edg/{major}.0.0.0" if browser == "edge" else "")


# --- requests cookie jar ---
def to_cookiejar(cookies: list[BrowserCookie]) -> RequestsCookieJar:
    jar = RequestsCookieJar()
    for c in cookies:
        rest = {"SameSite": c.samesite}
        if c.httponly:
            rest["HttpOnly"] = None
        jar.set_cookie(create_cookie(
            name=c.name, value=c.value, domain=c.domain, path=c.path,
            expires=c.expires, secure=c.secure, rest=rest,
            discard=c.expires is None,
        ))
    return jar


# --- session file ---
def save_session_file(path: Path, username: str, user_agent: str, cookies: list[BrowserCookie], source: str,
                      now: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    data = {
        "format": SESSION_FORMAT,
        "username": username,
        "user_agent": user_agent,
        "source": source,
        "imported_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "cookies": [asdict(c) for c in cookies],
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
    return path


def load_session_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("session file is in the old format; re-run `sitk login`") from e
    if not isinstance(data, dict) or data.get("format") != SESSION_FORMAT:
        raise ValueError("session file is in an unknown format; re-run `sitk login`")
    data["cookies"] = [BrowserCookie(**c) for c in data["cookies"]]
    return data


# --- diagnostics (never prints values) ---
def describe_session(jar: RequestsCookieJar, user_agent: str,
                     last_response: tuple[int, str, str] | None = None) -> list[str]:
    by_name = {c.name: c for c in jar}
    lines = []
    for name in DIAG_COOKIES:
        c = by_name.get(name)
        if c is None:
            lines.append(f"  {name}: MISSING")
            continue
        expires = datetime.fromtimestamp(c.expires, timezone.utc).date().isoformat() if c.expires else "session"
        lines.append(
            f"  {name}: present (len {len(c.value or '')}) domain={c.domain} path={c.path} secure={c.secure} "
            f"httponly={c.has_nonstandard_attr('HttpOnly')} samesite={c.get_nonstandard_attr('SameSite', 'n/a')} "
            f"expires={expires}"
        )
    lines.append(f"  instagram cookies loaded: {len(jar)}")
    lines.append(f"  User-Agent: {user_agent}")
    if last_response:
        status, url, body = last_response
        lines.append(f"  last response: HTTP {status} {url}")
        lines.append(f"  body: {body}")
    else:
        lines.append("  last response: (no Instagram request made)")
    return lines


def redact(text: str, jar: RequestsCookieJar) -> str:
    for c in jar:
        if c.value and len(c.value) >= 6:
            text = text.replace(c.value, "[redacted]")
    return text
