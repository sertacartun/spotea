"""Compression and security headers (app/middleware.py); audio byte ranges must never be gzipped."""

import re

from app.config import settings


def test_html_is_compressed(client):
    res = client.get("/", headers={"Accept-Encoding": "gzip"})

    assert res.status_code == 200
    # httpx decodes transparently, so the header is what proves it happened.
    assert res.headers.get("content-encoding") == "gzip"


def test_small_responses_are_left_uncompressed(client, db_session):
    """An interest is set so Home's first-run panel doesn't push the fragment over minimum_size."""
    from app.models import User

    db_session.query(User).filter(User.id == 1).update({"interests": "rock"})
    db_session.commit()

    res = client.get("/partials/home", headers={"Accept-Encoding": "gzip"})

    assert res.status_code == 200
    assert int(res.headers["content-length"]) < 1000
    assert "content-encoding" not in res.headers


def test_a_range_request_is_never_compressed(client, db_session, tmp_path):
    """A gzipped 206 doesn't match its claimed range, which breaks <audio> seeking."""
    from app.models import Artist, Content

    artist = Artist(user_id=1, channel_id="https://example.com/range", name="Range Channel")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    audio = tmp_path / "rangevid001.m4a"
    audio.write_bytes(b"\x00" * 4096)
    content = Content(
        artist_id=artist.id,
        user_id=1,
        video_id="rangevid001",
        title="Ranged",
        status="ready",
        file_path=str(audio),
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)

    res = client.get(
        f"/content/{content.id}/stream",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=0-99"},
    )

    assert res.status_code == 206
    assert "content-encoding" not in res.headers
    assert len(res.content) == 100


def test_cached_images_are_not_compressed(client):
    """Already-compressed bytes; gzipping them is pure CPU."""
    settings.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    # Larger than the middleware's minimum_size, so a pass-through is the only
    # reason it could come back uncompressed.
    (settings.thumbnails_dir / "imgtest0001.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 4096)

    res = client.get("/thumbnails/imgtest0001.jpg", headers={"Accept-Encoding": "gzip"})

    assert res.status_code == 200
    assert "content-encoding" not in res.headers


def test_security_headers_are_present(client):
    res = client.get("/")

    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in res.headers["Permissions-Policy"]

    csp = res.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # With 'unsafe-inline' in script-src the nonce buys nothing.
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_media_src_allows_the_sources_the_player_actually_assigns(client):
    """data: is the autoplay-unlock clip, blob: the prefetched next track; a CSP block is silent in JS."""
    csp = client.get("/").headers["Content-Security-Policy"]

    media = csp.split("media-src")[1].split(";")[0]
    assert "'self'" in media
    assert "data:" in media, "the silent unlock clip is blocked"
    assert "blob:" in media, (
        "a prefetched track's audio is blocked — the handoff silently plays "
        "nothing at all, see home/overlay.js's cacheUpcomingAudio"
    )


def test_img_src_allows_cover_art_read_back_out_of_offline_storage(client):
    """Offline tracks show their saved cover from a blob: URL."""
    csp = client.get("/").headers["Content-Security-Policy"]

    img = csp.split("img-src")[1].split(";")[0]
    assert "'self'" in img
    assert "blob:" in img, (
        "an offline track's saved cover is blocked — the track plays with no "
        "artwork, see static/js/offline.js's openCoverUrl"
    )


def test_the_inline_script_carries_the_nonce_from_the_header(client):
    """A mismatched nonce blocks the pre-paint script."""
    res = client.get("/")

    header_nonce = re.search(r"'nonce-([^']+)'", res.headers["Content-Security-Policy"]).group(1)
    assert f'<script nonce="{header_nonce}">' in res.text


def test_each_response_gets_a_fresh_nonce(client):
    first = client.get("/").headers["Content-Security-Policy"]
    second = client.get("/").headers["Content-Security-Policy"]

    assert first != second, "a reused nonce defeats the point of having one"


def test_login_page_is_protected_too(client):
    res = client.get("/login", follow_redirects=False)

    assert "Content-Security-Policy" in res.headers
    assert res.headers["X-Frame-Options"] == "DENY"
