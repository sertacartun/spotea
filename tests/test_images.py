"""Caching remote images (app/images.py): an existing file is never refetched, so writes are atomic."""

import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest

from app import images
from app.config import settings
from app.images import (
    _TEMP_SUFFIX,
    MAX_IMAGE_BYTES,
    _download_image,
    cached_avatar_path,
    fetch_image_bytes,
    needs_thumbnail_caching,
)

JPEG = b"\xff\xd8\xff" + b"\x00" * 512


def _headers(content_type: str) -> Message:
    msg = Message()
    msg["Content-Type"] = content_type
    return msg


class _FakeResponse:
    def __init__(self, body: bytes, read_sizes: list[int], content_type: str = "image/jpeg"):
        self._body = body
        self._read_sizes = read_sizes
        self.headers = _headers(content_type)

    def read(self, size: int = -1) -> bytes:
        self._read_sizes.append(size)
        return self._body[:size] if size >= 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def network(monkeypatch):
    """`install(outcome)` returns bytes or raises; the recorder holds (request, timeout) and read sizes."""
    calls: list[tuple[object, float | None]] = []
    read_sizes: list[int] = []
    recorder = SimpleNamespace(calls=calls, read_sizes=read_sizes)

    def install(outcome, content_type: str = "image/jpeg"):
        def fake_urlopen(request, timeout=None):
            calls.append((request, timeout))
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(outcome, read_sizes, content_type)

        monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)
        return recorder

    return install


def _temp_files(directory):
    return list(directory.glob(f"*{_TEMP_SUFFIX}"))


def test_an_image_is_fetched_once_and_served_from_our_origin(network, tmp_path):
    recorder = network(JPEG)

    url = _download_image(tmp_path, "vid00000001.jpg", "https://i.ytimg.com/x.jpg", "/thumbnails")

    assert url == "/thumbnails/vid00000001.jpg"
    assert (tmp_path / "vid00000001.jpg").read_bytes() == JPEG
    assert len(recorder.calls) == 1
    assert _temp_files(tmp_path) == []


def test_the_fetch_carries_a_timeout(network, tmp_path):
    recorder = network(JPEG)

    _download_image(tmp_path, "vid00000001.jpg", "https://i.ytimg.com/x.jpg", "/thumbnails")

    assert recorder.calls[0][1] == images.FETCH_TIMEOUT_SECONDS


def test_an_already_cached_image_is_not_refetched(network, tmp_path):
    recorder = network(JPEG)
    (tmp_path / "vid00000001.jpg").write_bytes(JPEG)

    url = _download_image(tmp_path, "vid00000001.jpg", "https://i.ytimg.com/x.jpg", "/thumbnails")

    assert url == "/thumbnails/vid00000001.jpg"
    assert recorder.calls == []


def test_a_failed_fetch_leaves_nothing_behind(network, tmp_path):
    network(urllib.error.URLError("no route to host"))

    assert _download_image(tmp_path, "vid00000001.jpg", "https://i.ytimg.com/x.jpg", "/thumbnails") is None
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_response_is_neither_read_whole_nor_written(network, tmp_path):
    """The read is capped, and what came back over the cap is discarded, not written."""
    recorder = network(b"x" * (MAX_IMAGE_BYTES + 1))

    assert _download_image(tmp_path, "vid00000001.jpg", "https://i.ytimg.com/x.jpg", "/thumbnails") is None
    assert recorder.read_sizes == [MAX_IMAGE_BYTES + 1]
    assert list(tmp_path.iterdir()) == []


def test_an_empty_response_is_not_written(network, tmp_path):
    """A zero-byte file would be cached as a valid image forever."""
    network(b"")

    assert _download_image(tmp_path, "vid00000001.jpg", "https://i.ytimg.com/x.jpg", "/thumbnails") is None
    assert list(tmp_path.iterdir()) == []


def test_the_destination_never_holds_a_partial_file(network, tmp_path, monkeypatch):
    """Nothing replaces an existing file, so no bytes may appear at dest before all have arrived."""
    network(JPEG)
    dest = tmp_path / "vid00000001.jpg"
    existed_before_rename = []

    real_replace = images.os.replace

    def watching_replace(src, dst):
        existed_before_rename.append(dest.exists())
        return real_replace(src, dst)

    monkeypatch.setattr(images.os, "replace", watching_replace)

    _download_image(tmp_path, dest.name, "https://i.ytimg.com/x.jpg", "/thumbnails")

    assert existed_before_rename == [False]
    assert dest.read_bytes() == JPEG


def test_a_failed_rename_cleans_up_its_temp_file(network, tmp_path, monkeypatch):
    """Otherwise the leftovers accumulate in a directory nothing prunes."""
    network(JPEG)

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(images.os, "replace", failing_replace)

    assert _download_image(tmp_path, "vid00000001.jpg", "https://i.ytimg.com/x.jpg", "/thumbnails") is None
    assert list(tmp_path.iterdir()) == []


def test_fetch_image_bytes_returns_the_body_and_content_type(network):
    network(JPEG, content_type="image/jpeg")

    result = fetch_image_bytes("https://yt3.ggpht.com/abc")

    assert result == (JPEG, "image/jpeg")


def test_fetch_image_bytes_writes_nothing_to_disk(network, tmp_path, monkeypatch):
    """Unlike _download_image: an unfollowed channel doesn't earn a permanent local copy."""
    network(JPEG)
    monkeypatch.chdir(tmp_path)

    fetch_image_bytes("https://yt3.ggpht.com/abc")

    assert list(tmp_path.iterdir()) == []


def test_fetch_image_bytes_rejects_a_non_image_content_type(network):
    """An allowlisted host still mustn't have an error page forwarded as an image."""
    network(b"<html>not an image</html>", content_type="text/html")

    assert fetch_image_bytes("https://yt3.ggpht.com/abc") is None


def test_fetch_image_bytes_rejects_an_oversized_response(network):
    recorder = network(b"x" * (MAX_IMAGE_BYTES + 1))

    assert fetch_image_bytes("https://yt3.ggpht.com/abc") is None
    assert recorder.read_sizes == [MAX_IMAGE_BYTES + 1]


def test_fetch_image_bytes_returns_none_on_a_failed_fetch(network):
    network(urllib.error.URLError("no route to host"))

    assert fetch_image_bytes("https://yt3.ggpht.com/abc") is None


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://yt3.ggpht.com/abc", True),
        ("http://i.ytimg.com/vi/abc/mqdefault.jpg", True),
        # Already ours: nothing to fetch.
        ("/thumbnails/abc.jpg", False),
        # Nearly every track is stored in this shape.
        ("/image-proxy?u=https%3A%2F%2Fyt3.ggpht.com%2Fabc", False),
        (None, False),
        ("", False),
    ],
)
def test_only_a_fetchable_url_is_queued_for_caching(url, expected):
    assert needs_thumbnail_caching(url) is expected


def test_cached_avatar_path_reports_only_what_is_on_disk():
    settings.avatars_dir.mkdir(parents=True, exist_ok=True)
    channel_id = "UCcachedavatartest00000"
    assert cached_avatar_path(channel_id) is None

    (settings.avatars_dir / f"{channel_id}.jpg").write_bytes(JPEG)
    try:
        assert cached_avatar_path(channel_id) == f"/avatars/{channel_id}.jpg"
    finally:
        (settings.avatars_dir / f"{channel_id}.jpg").unlink()
