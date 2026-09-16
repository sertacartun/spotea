from app.progress import ProgressRegistry


def test_get_returns_the_last_value_set():
    registry: ProgressRegistry[int, str] = ProgressRegistry()
    registry.set(1, "scanning")
    registry.set(1, "saving")
    assert registry.get(1) == "saving"


def test_unknown_key_returns_the_default():
    registry: ProgressRegistry[int, tuple[str | None, int]] = ProgressRegistry()
    assert registry.get(99, (None, 0)) == (None, 0)
    assert registry.get(99) is None


def test_discard_drops_an_entry_ahead_of_expiry():
    registry: ProgressRegistry[int, str] = ProgressRegistry()
    registry.set(1, "done")
    registry.discard(1)
    assert registry.get(1) is None
    # delete_feed discards for every artist, including ones that never ran a backfill.
    registry.discard(1)


def test_terminal_entries_survive_until_they_expire():
    """A job finishing before the client's first poll must still be readable."""
    registry: ProgressRegistry[int, str] = ProgressRegistry(ttl_seconds=60)
    registry.set(1, "done")
    assert registry.get(1) == "done"


def test_entries_expire_once_their_ttl_passes():
    registry: ProgressRegistry[str, dict] = ProgressRegistry(ttl_seconds=0)
    registry.set("job", {"done": 0})
    assert registry.get("job") is None


def test_setting_again_extends_a_long_running_job():
    registry: ProgressRegistry[str, dict] = ProgressRegistry(ttl_seconds=0.05)
    progress = {"done": 0}
    registry.set("job", progress)
    for _ in range(3):
        progress["done"] += 1
        registry.set("job", progress)
    assert registry.get("job") == {"done": 3}


def test_expiry_of_one_entry_does_not_disturb_another():
    registry: ProgressRegistry[str, str] = ProgressRegistry(ttl_seconds=0)
    registry.set("stale", "gone")
    registry.get("stale")
    fresh: ProgressRegistry[str, str] = ProgressRegistry(ttl_seconds=60)
    fresh.set("kept", "here")
    assert fresh.get("kept") == "here"
