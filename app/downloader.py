import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from app.config import settings
from app.youtube.urls import YOUTUBE_WATCH_URL

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int | None], None]

# mp4a only, so the m4a target is a remux, never a transcode. "low" needs itag 139,
# which only some clients list — keep one of those first in _ATTEMPTS.
FORMAT_BY_QUALITY = {
    "high": "bestaudio[acodec^=mp4a]/bestaudio/best",
    "low": "bestaudio[acodec^=mp4a][abr<=64]/bestaudio[acodec^=mp4a]/bestaudio/best",
}

# Short on purpose: the next rung is a fresh extraction, so failing over beats waiting.
SOCKET_TIMEOUT_SECONDS = 10


class DownloadError(Exception):
    pass


class VideoUnavailableError(DownloadError):
    """YouTube won't serve this video to any client; retrying is pointless."""


# Failures identical on every client (mostly region-locked Topic tracks). Retrying them
# only adds request volume, which itself feeds YouTube's 403/bot checks.
_PERMANENT_FAILURE_PATTERNS = (
    r"video unavailable",
    r"this video is not available",
    r"not made this video available in your country",
    r"no longer available",
    r"private video",
    r"removed by the uploader",
    r"account associated with this video has been terminated",
    r"members[- ]only",
    r"join this channel",
    r"sign in to confirm your age",
    r"age[- ]restricted",
)
_PERMANENT_FAILURE_RE = re.compile("|".join(_PERMANENT_FAILURE_PATTERNS), re.IGNORECASE)


def is_permanent_failure(message: str | None) -> bool:
    return bool(message) and _PERMANENT_FAILURE_RE.search(message) is not None


class _YtdlpLogger:
    """yt-dlp errors are re-logged with context by the caller; warnings (why formats
    were dropped) stay available at DEBUG."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)


@dataclass(frozen=True)
class Attempt:
    player_clients: tuple[str, ...]


# Rungs are client changes, not waits: refusals are per-URL. visionos needs no PO token
# and offers itag 139; test_downloader asserts yt-dlp still ships it as a default client.
_ATTEMPTS = (
    Attempt(player_clients=("visionos",)),
    Attempt(player_clients=("visionos",)),
    Attempt(player_clients=("web_embedded",)),
)


def _progress_hook(on_progress: ProgressCallback, event: dict) -> None:
    if event["status"] != "downloading":
        return
    total = event.get("total_bytes") or event.get("total_bytes_estimate")
    downloaded = event.get("downloaded_bytes")
    percent = int(downloaded / total * 100) if total and downloaded is not None else None
    on_progress("downloading", percent)


def _postprocessor_hook(on_progress: ProgressCallback, event: dict) -> None:
    # pp_key() strips the "FFmpeg" prefix from the class name.
    if event.get("postprocessor") == "ExtractAudio" and event["status"] == "started":
        on_progress("converting", None)


def user_storage_dir(user_id: int | None) -> Path:
    """Per-user dir, so one user deleting a track can't remove another's copy.
    None is the legacy flat layout older rows still reference."""
    return settings.storage_dir if user_id is None else settings.storage_dir / str(user_id)


def download_audio(
    video_id: str,
    quality: str = "high",
    on_progress: ProgressCallback | None = None,
    user_id: int | None = None,
) -> Path:
    destination = user_storage_dir(user_id)
    destination.mkdir(parents=True, exist_ok=True)
    out_template = str(destination / f"{video_id}.%(ext)s")

    codec = settings.audio_format
    postprocessor = {"key": "FFmpegExtractAudio", "preferredcodec": codec}

    def build_ydl_opts(attempt: "Attempt") -> dict:
        return {
            "format": FORMAT_BY_QUALITY.get(quality, FORMAT_BY_QUALITY["high"]),
            "outtmpl": out_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            # Neither of the two above covers errors — see _YtdlpLogger.
            "logger": _YtdlpLogger(),
            "socket_timeout": SOCKET_TIMEOUT_SECONDS,
            "postprocessors": [postprocessor],
            # A client that starts needing a PO token fails as a 403 (yt-dlp silently
            # drops its formats); the reason is only in DEBUG-level warnings.
            "extractor_args": {
                "youtube": {"player_client": list(attempt.player_clients)},
            },
        }

    if on_progress is not None:
        progress_hooks = [lambda event: _progress_hook(on_progress, event)]
        postprocessor_hooks = [lambda event: _postprocessor_hook(on_progress, event)]
    else:
        progress_hooks = postprocessor_hooks = None

    url = YOUTUBE_WATCH_URL.format(video_id=video_id)

    last_exc: yt_dlp.utils.DownloadError | None = None
    for number, attempt in enumerate(_ATTEMPTS, start=1):
        # Extraction is slow and reports no byte progress; player.js shows this stage.
        if on_progress is not None:
            on_progress("extracting", None)

        ydl_opts = build_ydl_opts(attempt)
        if progress_hooks is not None:
            ydl_opts["progress_hooks"] = progress_hooks
            ydl_opts["postprocessor_hooks"] = postprocessor_hooks
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if number > 1:
                logger.info(
                    "Download of %s recovered on attempt %d/%d (clients=%s)",
                    video_id, number, len(_ATTEMPTS), ",".join(attempt.player_clients),
                )
            last_exc = None
            break
        except yt_dlp.utils.DownloadError as exc:
            last_exc = exc
            # WARNING: nothing configures the root logger below WARNING under uvicorn.
            logger.warning(
                "Download attempt %d/%d failed for %s (clients=%s): %s",
                number, len(_ATTEMPTS), video_id, ",".join(attempt.player_clients), str(exc)[:200],
            )
            if is_permanent_failure(str(exc)):
                logger.warning("Giving up on %s: unavailable to every client", video_id)
                raise VideoUnavailableError(str(exc)) from exc

    if last_exc is not None:
        raise DownloadError(str(last_exc)) from last_exc

    final_path = destination / f"{video_id}.{codec}"
    if not final_path.exists():
        raise DownloadError("Download completed but output file was not found")

    return final_path
