import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.deps import get_current_user, get_db, require_login
from app.formatting import format_size, safe_filename
from app.models import Content, User
from app.storage import EXPORT_TEMP_SUFFIX, clear_cache, is_pinned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_login)])


@router.delete("/cache")
def clear_cache_endpoint(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, int]:
    """Downloads stay; played songs download again on their next play."""
    return {"cleared": clear_cache(db, user.id)}


def _archive_name(title: str, suffix: str, used: set[str]) -> str:
    stem = safe_filename(title)
    name = stem + suffix
    counter = 2
    while name in used:
        name = f"{stem} ({counter}){suffix}"
        counter += 1
    used.add(name)
    return name


@router.get("/export")
def export_all(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> FileResponse:
    """Every download (not cache) as one zip, built on disk rather than in memory.

    The temp file lives beside the audio, not in /tmp, which is often a small tmpfs.
    """
    rows = (
        db.query(Content)
        .filter(Content.user_id == user.id, Content.status == "ready", is_pinned())
        .all()
    )
    exportable = [(row, Path(row.file_path)) for row in rows if row.file_path]
    exportable = [(row, path) for row, path in exportable if path.is_file()]
    if not exportable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Nothing to export")

    # ZIP_STORED: the archive is exactly the sum of its inputs.
    needed = sum(path.stat().st_size for _row, path in exportable)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.storage_dir).free
    if needed > free:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=f"Not enough free disk space to build the export ({format_size(needed)} needed)",
        )

    descriptor, temp_name = tempfile.mkstemp(dir=settings.storage_dir, suffix=EXPORT_TEMP_SUFFIX)
    os.close(descriptor)
    archive = Path(temp_name)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
            used_names: set[str] = set()
            for row, path in exportable:
                zf.write(path, arcname=_archive_name(row.title, path.suffix, used_names))
    except Exception:
        archive.unlink(missing_ok=True)
        raise

    return FileResponse(
        archive,
        media_type="application/zip",
        filename="spotea-downloads.zip",
        # Also runs when the client disconnects partway, which would otherwise leave a full copy on disk.
        background=BackgroundTask(_remove_archive, archive),
    )


def _remove_archive(archive: Path) -> None:
    try:
        archive.unlink(missing_ok=True)
    except OSError:
        # Leftovers are swept later (see EXPORT_TEMP_SUFFIX); don't fail a finished download.
        logger.warning("Could not remove export archive %s", archive)
