from sitk.config import ROOT, load_config


def test_example_config_parses(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/x\nDISCORD_USER_ID=42\n")
    cfg = load_config(ROOT / "config.example.toml", env)
    assert [t.handle for t in cfg.watch] == ["my_alt"]
    assert cfg.event_pages == []
    assert cfg.schedule.quiet_hours == (1, 7)
    assert cfg.discord_user_id == "42"


def test_multiple_watch_targets_and_pages(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[instagram]\nsession_user = "@me"\nevent_pages = ["@venue"]\n'
        '[[watch]]\nhandle = "@alt1"\n[[watch]]\nhandle = "alt2"\nfollowing = false\n'
        "[schedule]\nquiet_hours = []\n"
    )
    cfg = load_config(path, tmp_path / "missing.env")
    assert cfg.session_user == "me"
    assert cfg.event_pages == ["venue"]
    assert [(t.handle, t.kinds()) for t in cfg.watch] == [("alt1", ["followers", "following"]), ("alt2", ["followers"])]
    assert cfg.schedule.quiet_hours is None
