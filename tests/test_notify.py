import json

import httpx
import pytest

from sitk.diff import Change
from sitk.notify import Discord, relation_embeds

URL = "https://discord.com/api/webhooks/1/abc"


def make(handler):
    return Discord(URL, "42", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_ping_only_allows_owner_mention():
    sent = []
    d = make(lambda req: sent.append(json.loads(req.content)) or httpx.Response(200, json={}))
    d.send("hi", ping=True)
    d.send("quiet")
    assert sent[0]["content"] == "<@42> hi"
    assert sent[0]["allowed_mentions"] == {"users": ["42"]}
    assert sent[1]["allowed_mentions"] == {"parse": []}


def test_embeds_chunked_by_ten():
    sent = []
    d = make(lambda req: sent.append(json.loads(req.content)) or httpx.Response(200, json={}))
    d.send(embeds=[{"title": str(i)} for i in range(23)], ping=True)
    assert [len(m["embeds"]) for m in sent] == [10, 10, 3]
    assert sent[0]["content"].startswith("<@42>") and sent[1]["content"] == ""


def test_retries_on_429(monkeypatch):
    monkeypatch.setattr("sitk.notify.time.sleep", lambda s: None)
    responses = iter([httpx.Response(429, json={"retry_after": 0.1}), httpx.Response(200, json={})])
    make(lambda req: next(responses)).send("x")


def test_error_does_not_leak_url():
    d = make(lambda req: httpx.Response(404, text="Unknown Webhook"))
    with pytest.raises(RuntimeError) as e:
        d.send("x")
    assert "abc" not in str(e.value)


def test_rejects_non_discord_url():
    with pytest.raises(ValueError):
        Discord("https://example.com/hook")


def test_relation_embeds_truncate():
    changes = [Change("added", str(i), f"user{i}") for i in range(30)]
    [embed] = relation_embeds("alt", "followers", changes)
    assert embed["title"] == "New followers on @alt"
    assert "and 5 more" in embed["description"]
