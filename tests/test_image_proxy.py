"""GET /image-proxy (app/main.py): streams allowlisted YouTube CDN images without writing to disk."""

from app import main


def test_a_proxied_image_is_streamed_through(client, monkeypatch):
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\xff\xd8\xff-jpeg-bytes", "image/jpeg"

    monkeypatch.setattr(main, "fetch_image_bytes", fake_fetch)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"})

    assert res.status_code == 200
    assert res.content == b"\xff\xd8\xff-jpeg-bytes"
    assert res.headers["content-type"] == "image/jpeg"
    assert fetched == ["https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"]


def test_an_lh3_portrait_is_proxied_too(client, monkeypatch):
    """YouTube Music serves portraits and covers from lh3 nearly as often as from yt3."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"\xff\xd8\xff", "image/jpeg"))

    res = client.get(
        "/image-proxy", params={"u": "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"}
    )

    assert res.status_code == 200


def test_a_mood_playlist_track_thumbnail_is_proxied_too(client, monkeypatch):
    """Mood/mix playlist tracks report their thumbnails on i.ytimg.com."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"\xff\xd8\xff", "image/jpeg"))

    res = client.get(
        "/image-proxy",
        params={"u": "https://i.ytimg.com/vi/1lrFsXkT_rM/hqdefault.jpg?sqp=-oaymwEWCJADEOEBIAQ"},
    )

    assert res.status_code == 200


def test_a_non_allowlisted_host_is_rejected_without_being_fetched(client, monkeypatch):
    """Stops a tampered `u` from turning this into an open fetch of arbitrary hosts."""

    def fail_if_called(url):
        raise AssertionError("must never fetch a non-allowlisted host")

    monkeypatch.setattr(main, "fetch_image_bytes", fail_if_called)

    res = client.get("/image-proxy", params={"u": "https://evil.example/tracker.png"})

    assert res.status_code == 400


def test_a_lookalike_host_is_rejected(client, monkeypatch):
    """Exact hostname match only, not substring/endswith."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"x", "image/jpeg"))

    res = client.get("/image-proxy", params={"u": "https://notyt3.ggpht.com/abc"})

    assert res.status_code == 400


def test_a_failed_upstream_fetch_serves_a_blank_pixel(client, monkeypatch):
    """A failed <img> paints a broken glyph; a transparent pixel shows the CSS placeholder instead."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: None)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc"})

    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content.startswith(b"\x89PNG")


def test_the_blank_pixel_is_not_cached(client, monkeypatch):
    """Otherwise one upstream hiccup freezes a blank image in the browser cache."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: None)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc"})

    assert res.headers["cache-control"] == "no-store"


def test_image_proxy_requires_login():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as anonymous:
        res = anonymous.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc"}, follow_redirects=False)

    assert res.status_code == 303


def test_a_video_still_is_fetched_exactly_as_asked_for(client, monkeypatch):
    """No maxresdefault upgrade: rows are swapped for square song covers before they play."""
    original = "https://i.ytimg.com/vi/1lrFsXkT_rM/hqdefault.jpg?sqp=-oaymwEWCJADEOEBIAQ"
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\xff\xd8\xff", "image/jpeg"

    monkeypatch.setattr(main, "fetch_image_bytes", fake_fetch)

    res = client.get("/image-proxy", params={"u": original})

    assert res.status_code == 200
    assert fetched == [original], "one fetch, and the URL the caller named"


def test_a_square_cover_is_fetched_once_and_unchanged(client, monkeypatch):
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\xff\xd8\xff", "image/jpeg"

    monkeypatch.setattr(main, "fetch_image_bytes", fake_fetch)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc=w544-h544-l90-rj"})

    assert res.status_code == 200
    assert fetched == ["https://yt3.ggpht.com/abc=w544-h544-l90-rj"]
