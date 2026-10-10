import hashlib
import json
import os
import pickle
import plistlib
import sqlite3
import stat
from types import SimpleNamespace

import pytest
import requests
from Cryptodome.Cipher import AES

from sitk import cli, cookies
from sitk.config import Config
from sitk.cookies import (BrowserCookie, chromium_user_agent, describe_session, derive_key, load_session_file,
                          read_chromium_cookies, save_session_file, to_cookiejar)
from sitk.sources.base import AuthError
from sitk.sources.instagram import InstagramSource, build_loader

PASSWORD = b"test-safe-storage-password"
SESSION_VALUE = "FAKESESSION%3A123%3Asecretsecret"
CSRF_VALUE = "fakecsrftoken0123456789"
EXPIRES_UNIX = 1_830_000_000
CHROME_EXPIRES = (EXPIRES_UNIX + cookies.CHROME_EPOCH_OFFSET) * 1_000_000
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


def encrypt(value: str, host: str, version: int) -> bytes:
    plain = value.encode()
    if version >= 24:
        plain = hashlib.sha256(host.encode()).digest() + plain
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    return b"v10" + AES.new(derive_key(PASSWORD), AES.MODE_CBC, iv=b" " * 16).encrypt(plain)


def make_db(path, version=24, rows=None):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    db.execute("""CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT,
                  expires_utc INTEGER, has_expires INTEGER, is_secure INTEGER, is_httponly INTEGER,
                  samesite INTEGER)""")
    # host, name, value, path, has_expires, secure, httponly, samesite
    rows = rows or [
        (".instagram.com", "sessionid", SESSION_VALUE, "/", 1, 1, 1, 0),
        (".instagram.com", "csrftoken", CSRF_VALUE, "/", 1, 1, 0, -1),
        (".instagram.com", "ds_user_id", "4242", "/", 1, 1, 0, 0),
        (".instagram.com", "wd", "1280x720", "/", 0, 1, 0, 1),
        ("www.instagram.com", "ig_pr", "2", "/", 1, 1, 0, 2),
        (".facebook.com", "c_user", "nope", "/", 1, 1, 1, 0),
        (".notinstagram.com", "sessionid", "evil", "/", 1, 1, 1, 0),
    ]
    for host, name, value, path_, has_exp, secure, httponly, samesite in rows:
        db.execute("INSERT INTO cookies VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
                   (host, name, encrypt(value, host, version), path_, CHROME_EXPIRES if has_exp else 0,
                    has_exp, secure, httponly, samesite))
    db.commit()
    db.close()
    return path


@pytest.fixture
def db_path(tmp_path):
    return make_db(tmp_path / "Cookies")


@pytest.fixture
def imported(db_path):
    return {c.name: c for c in read_chromium_cookies(db_path, PASSWORD)}


def test_sessionid_attributes_preserved(imported):
    c = imported["sessionid"]
    assert c.value == SESSION_VALUE
    assert (c.domain, c.path, c.secure, c.httponly, c.samesite) == (".instagram.com", "/", True, True, "None")
    assert c.expires == EXPIRES_UNIX


def test_samesite_mapping_and_session_cookies(imported):
    assert imported["csrftoken"].samesite == "Unspecified"
    assert imported["wd"].samesite == "Lax"
    assert imported["wd"].expires is None  # has_expires = 0 -> session cookie
    assert imported["ig_pr"].samesite == "Strict"


def test_only_instagram_domains(db_path):
    got = read_chromium_cookies(db_path, PASSWORD)
    assert {c.domain for c in got} == {".instagram.com", "www.instagram.com"}
    assert "nope" not in {c.value for c in got} and "evil" not in {c.value for c in got}


def test_pre_v24_db_has_no_hash_prefix(tmp_path):
    path = make_db(tmp_path / "Cookies", version=23)
    got = {c.name: c.value for c in read_chromium_cookies(path, PASSWORD)}
    assert got["sessionid"] == SESSION_VALUE


def test_wrong_password_never_yields_real_values(db_path):
    try:
        values = {c.value for c in read_chromium_cookies(db_path, b"wrong")}
    except (UnicodeDecodeError, ValueError):
        return  # failing loudly is fine
    assert SESSION_VALUE not in values


def test_cookiejar_keeps_scoping(imported):
    jar = to_cookiejar(list(imported.values()))
    sid = next(c for c in jar if c.name == "sessionid")
    assert (sid.domain, sid.path, sid.secure, sid.expires) == (".instagram.com", "/", True, EXPIRES_UNIX)
    assert sid.has_nonstandard_attr("HttpOnly")
    assert sid.get_nonstandard_attr("SameSite") == "None"

    s = requests.Session()
    s.cookies = jar

    def cookie_header(url):
        return s.prepare_request(requests.Request("GET", url)).headers.get("Cookie", "")

    assert f"sessionid={SESSION_VALUE}" in cookie_header("https://www.instagram.com/api/v1/users/web_profile_info/")
    assert f"sessionid={SESSION_VALUE}" in cookie_header("https://www.instagram.com/graphql/query")
    assert "sessionid" not in cookie_header("https://example.com/")
    assert "sessionid" not in cookie_header("http://www.instagram.com/")  # secure-only


def test_session_file_roundtrip_and_permissions(tmp_path, imported):
    path = tmp_path / "cfg" / "session-me"
    save_session_file(path, "me", UA, list(imported.values()), source="chrome:Profile 2")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    data = load_session_file(path)
    assert data["username"] == "me" and data["user_agent"] == UA
    assert data["cookies"] == list(imported.values())


def test_old_pickle_session_rejected(tmp_path):
    path = tmp_path / "session-me"
    path.write_bytes(pickle.dumps({"sessionid": "x", "csrftoken": "y"}))
    with pytest.raises(ValueError, match="old format"):
        load_session_file(path)
    with pytest.raises(AuthError, match="re-run `sitk login`"):
        InstagramSource("me", tmp_path)


def test_describe_session_shows_attributes_not_values(imported):
    text = "\n".join(describe_session(to_cookiejar(list(imported.values())), UA))
    assert "sessionid: present" in text
    assert "domain=.instagram.com path=/ secure=True httponly=True samesite=None" in text
    assert f"User-Agent: {UA}" in text
    assert "no Instagram request made" in text
    assert SESSION_VALUE not in text and CSRF_VALUE not in text


def test_describe_session_missing_sessionid():
    text = "\n".join(describe_session(to_cookiejar([]), UA))
    assert "sessionid: MISSING" in text


def test_chromium_user_agent(tmp_path):
    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "155.0.8059.39"}))
    assert chromium_user_agent("chrome", plist) == UA


def test_loader_uses_browser_ua_and_full_cookies(imported):
    loader = build_loader("me", list(imported.values()), UA)
    session = loader.context._session
    assert session.headers["User-Agent"] == UA
    assert session.headers["X-CSRFToken"] == CSRF_VALUE
    assert loader.context.iphone_support is False
    sid = next(c for c in session.cookies if c.name == "sessionid")
    assert sid.domain == ".instagram.com" and sid.get_nonstandard_attr("SameSite") == "None"


def test_loader_requires_sessionid():
    with pytest.raises(ValueError, match="sessionid"):
        build_loader("me", [BrowserCookie("csrftoken", "x", ".instagram.com", "/", None, True, False, "Lax")], UA)


def test_source_diagnostics_record_response_and_redact(tmp_path, imported):
    save_session_file(tmp_path / "session-me", "me", UA, list(imported.values()), source="test")
    src = InstagramSource("me", tmp_path)
    fake = SimpleNamespace(url="https://www.instagram.com/api/v1/users/web_profile_info/?username=x",
                           status_code=400, text=f'{{"message":"feedback_required","echo":"{SESSION_VALUE}"}}')
    src._record(fake)
    text = "\n".join(src.diagnostics())
    assert "last response: HTTP 400 https://www.instagram.com/api/v1/users/web_profile_info/" in text
    assert "feedback_required" in text
    assert SESSION_VALUE not in text and "username=x" not in text


def test_config_user_agent_override(tmp_path, imported):
    save_session_file(tmp_path / "session-me", "me", UA, list(imported.values()), source="test")
    src = InstagramSource("me", tmp_path, user_agent_override="Custom/1.0")
    assert src.session.headers["User-Agent"] == "Custom/1.0"


def test_login_dry_run_saves_nothing_and_sends_nothing(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setattr(cookies, "find_cookie_db", lambda browser, profile: db_path)
    monkeypatch.setattr(cookies, "keychain_password", lambda browser: PASSWORD)
    monkeypatch.setattr(cookies, "chromium_user_agent", lambda browser: UA)
    monkeypatch.setattr(cli, "load_config", lambda: Config(session_user="me"))
    monkeypatch.setattr(cli, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(requests.Session, "send", lambda *a, **k: pytest.fail("network request attempted"))

    assert cli.main(["login", "--profile", "Profile 2", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "samesite=None" in out and UA in out and "Dry run" in out
    assert SESSION_VALUE not in out
    assert not (tmp_path / "sessions").exists()


def test_login_saves_session_without_network(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setattr(cookies, "find_cookie_db", lambda browser, profile: db_path)
    monkeypatch.setattr(cookies, "keychain_password", lambda browser: PASSWORD)
    monkeypatch.setattr(cookies, "chromium_user_agent", lambda browser: UA)
    monkeypatch.setattr(cli, "load_config", lambda: Config(session_user=""))
    monkeypatch.setattr(cli, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path / "data")
    (tmp_path / "config.toml").write_text('[instagram]\nsession_user = ""\n')
    monkeypatch.setattr(requests.Session, "send", lambda *a, **k: pytest.fail("network request attempted"))

    assert cli.main(["login", "--user", "me"]) == 0
    data = json.loads((tmp_path / "sessions" / "session-me").read_text())
    assert data["user_agent"] == UA and data["format"] == 2
    assert 'session_user = "me"' in (tmp_path / "config.toml").read_text()
