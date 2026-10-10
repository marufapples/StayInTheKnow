from sitk.diff import ACTIVE, GONE, Rel, diff, is_complete


def test_additions_reported_immediately():
    state = {"1": Rel("alice")}
    new, changes = diff(state, {"1": "alice", "2": "bob"})
    assert [(c.change, c.username) for c in changes] == [("added", "bob")]
    assert new["2"] == Rel("bob")


def test_removal_needs_two_consecutive_misses():
    state = {"1": Rel("alice"), "2": Rel("bob")}
    s1, c1 = diff(state, {"1": "alice"})
    assert c1 == []
    assert s1["2"] == Rel("bob", ACTIVE, 1)
    s2, c2 = diff(s1, {"1": "alice"})
    assert [(c.change, c.username) for c in c2] == [("removed", "bob")]
    assert s2["2"].status == GONE


def test_flaky_absence_resets():
    s1, _ = diff({"2": Rel("bob")}, {})
    s2, c2 = diff(s1, {"2": "bob"})
    assert c2 == []
    assert s2["2"] == Rel("bob")


def test_returning_user_is_added_again():
    new, changes = diff({"2": Rel("bob", GONE)}, {"2": "bob"})
    assert [(c.change, c.username) for c in changes] == [("added", "bob")]


def test_username_change_is_not_a_change():
    new, changes = diff({"1": Rel("alice")}, {"1": "alice_new"})
    assert changes == []
    assert new["1"].username == "alice_new"


def test_gone_users_stay_gone_quietly():
    _, changes = diff({"2": Rel("bob", GONE)}, {})
    assert changes == []


def test_is_complete():
    assert is_complete(100, 100)
    assert is_complete(98, 100)
    assert not is_complete(50, 100)
    assert is_complete(0, 0)
    assert is_complete(1000, 1015)      # 2% tolerance on bigger accounts
    assert not is_complete(900, 1000)
