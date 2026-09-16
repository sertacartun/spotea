"""The retry ladder in app/downloader.py; yt_dlp.YoutubeDL is faked, nothing goes online."""

import logging
import time

import pytest
import yt_dlp

from app import downloader


class _FakeYDL:
    """Records the options it was built with and fails as `outcomes` dictates."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        _FakeYDL.calls.append(self.opts)
        outcome = _FakeYDL.outcomes[len(_FakeYDL.calls) - 1]
        if outcome is not None:
            raise yt_dlp.utils.DownloadError(outcome)


@pytest.fixture
def fake_ydl(monkeypatch, tmp_path):
    """Swaps in _FakeYDL and points storage at tmp_path."""
    _FakeYDL.calls = []
    _FakeYDL.outcomes = []
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(downloader.settings, "storage_dir", tmp_path)
    return _FakeYDL


def _clients_of(opts):
    return opts["extractor_args"]["youtube"]["player_client"]


def test_the_client_that_gets_served_goes_first(fake_ydl, tmp_path):
    """visionos: fastest measured, served every track, and needs no PO token."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 1
    assert _clients_of(fake_ydl.calls[0]) == ["visionos"]


def test_the_pinned_client_is_one_upstream_still_defends(fake_ydl):
    """Fail loudly when yt-dlp drops the pinned client from its defaults, instead of silent 403s."""
    from yt_dlp.extractor.youtube import YoutubeIE

    lead = downloader._ATTEMPTS[0].player_clients
    assert set(lead) <= set(YoutubeIE._DEFAULT_CLIENTS), (
        f"_ATTEMPTS leads with {lead}, which yt-dlp {yt_dlp.version.__version__} no longer "
        f"defaults to ({YoutubeIE._DEFAULT_CLIENTS}). Either upstream dropped it and this "
        f"ladder needs re-measuring, or the yt-dlp here is older than the image's."
    )


def test_the_clients_measured_not_to_deliver_are_not_asked_for(fake_ydl):
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    for call in fake_ydl.calls:
        clients = _clients_of(call)
        assert "android_vr" not in clients  # needs a GVS PO token; only muxed itag 18 left
        assert "web_safari" not in clients  # SABR-forced, no URLs
        assert "mweb" not in clients  # URLs 403 even with a valid token


def test_the_last_rung_is_a_different_client_from_the_first(fake_ydl):
    """If the lead client itself is broken, repeating it on the last rung can't help."""
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    assert _clients_of(fake_ydl.calls[-1]) != _clients_of(fake_ydl.calls[0])


def test_ytdlp_does_not_write_its_own_errors_to_stderr(fake_ydl):
    """yt-dlp prints errors regardless of `quiet`; a recovered rung left a bare ERROR in the log."""
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    for call in fake_ydl.calls:
        assert isinstance(call["logger"], downloader._YtdlpLogger)


def test_the_reason_a_format_was_dropped_is_still_recoverable(caplog):
    """yt-dlp warnings are the only explanation of a dropped format; kept at DEBUG, not discarded."""
    with caplog.at_level(logging.DEBUG, logger=downloader.__name__):
        downloader._YtdlpLogger().warning("android_vr client https formats require a GVS PO Token")

    assert "GVS PO Token" in caplog.text


def test_a_refused_url_gets_a_second_and_third_extraction(fake_ydl, tmp_path):
    """Refusals are per-URL, so a fresh extraction by the same client can succeed."""
    fake_ydl.outcomes = [
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        None,
    ]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    path = downloader.download_audio("vid00000001")

    assert path.name == "vid00000001.m4a"
    assert len(fake_ydl.calls) == 3


def test_no_attempt_waits_before_taking_its_shot(fake_ydl, monkeypatch):
    """Refusals are per-URL; waiting doesn't make the next URL more acceptable."""
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    assert slept == []


def test_every_attempt_reports_that_it_started_resolving(fake_ydl, tmp_path):
    """Extraction moves no bytes, so without this the client has nothing to show for seconds."""
    fake_ydl.outcomes = ["403", None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")
    events = []

    downloader.download_audio("vid00000001", on_progress=lambda phase, pct: events.append((phase, pct)))

    assert events == [("extracting", None), ("extracting", None)]


def test_a_hung_request_cannot_hold_the_ladder_open(fake_ydl, tmp_path):
    """Without a socket timeout one stalled request blocks every remaining rung."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert fake_ydl.calls[0]["socket_timeout"] == downloader.SOCKET_TIMEOUT_SECONDS


def test_giving_up_takes_three_attempts(fake_ydl):
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 3


def test_the_error_surfaced_is_the_last_one_seen(fake_ydl):
    fake_ydl.outcomes = ["403", "403", "ERROR: Video unavailable"]

    with pytest.raises(downloader.DownloadError, match="Video unavailable"):
        downloader.download_audio("vid00000001")


def test_a_download_that_produces_no_file_is_an_error(fake_ydl):
    """Otherwise the row is marked ready and playback 404s on a missing file."""
    fake_ydl.outcomes = [None]

    with pytest.raises(downloader.DownloadError, match="output file was not found"):
        downloader.download_audio("vid00000001")


def test_no_extraction_level_sleep_is_configured(fake_ydl, tmp_path):
    """sleep_interval_requests cost 1.5s per play with no measured effect on refusals."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert "sleep_interval_requests" not in fake_ydl.calls[0]


def test_an_unavailable_video_stops_the_ladder_on_the_first_attempt(fake_ydl):
    """This refusal is the same on every client, so more attempts only add request volume."""
    fake_ydl.outcomes = ["ERROR: [youtube] abc: Video unavailable. This video is not available"]

    with pytest.raises(downloader.VideoUnavailableError):
        downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 1


def test_a_refusal_that_might_pass_next_time_still_uses_the_whole_ladder(fake_ydl):
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError) as raised:
        downloader.download_audio("vid00000001")

    assert not isinstance(raised.value, downloader.VideoUnavailableError)
    assert len(fake_ydl.calls) == 3


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] x: Video unavailable. This video is not available",
        "ERROR: [youtube] x: The uploader has not made this video available in your country",
        "ERROR: [youtube] x: Private video. Sign in if you've been granted access",
        "ERROR: [youtube] x: This video is no longer available because the uploader has closed",
        "ERROR: [youtube] x: Join this channel to get access to members-only content",
        "ERROR: [youtube] x: Sign in to confirm your age",
    ],
)
def test_settled_refusals_are_recognised(message):
    assert downloader.is_permanent_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "ERROR: [youtube] x: Sign in to confirm you're not a bot",
        "ERROR: Unable to download webpage: timed out",
        "",
        None,
    ],
)
def test_retryable_refusals_are_left_alone(message):
    """The bot check means YouTube is refusing us right now, not that the video is unplayable."""
    assert not downloader.is_permanent_failure(message)


def test_quality_selects_the_capped_format(fake_ydl, tmp_path):
    """"low" only means something with a client that carries itag 139 (~49kbps), e.g. visionos."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001", quality="low")

    assert "abr<=64" in fake_ydl.calls[0]["format"]


def test_the_file_is_looked_for_where_it_was_written(fake_ydl, tmp_path):
    """Audio goes into a per-user directory; the existence check has to follow it there."""
    fake_ydl.outcomes = [None]
    written = tmp_path / "7" / "vid00000001.m4a"
    written.parent.mkdir()
    written.write_bytes(b"audio")

    assert downloader.download_audio("vid00000001", user_id=7) == written

    assert str(tmp_path / "7") in fake_ydl.calls[0]["outtmpl"]


def test_a_download_with_no_user_keeps_the_flat_layout(fake_ydl, tmp_path):
    """Older rows name their file at the top level and still have to resolve."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    assert downloader.download_audio("vid00000001") == tmp_path / "vid00000001.m4a"
