"""Library routes: albums in the output folder, their audio, covers, and Show in Finder."""
from __future__ import annotations

import asyncio
import ipaddress
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from library import Library, embedded_art

router = APIRouter(prefix="/api/library")
library: Library | None = None
_cache_dir: Path | None = None
_transcode_lock = asyncio.Lock()

MEDIA_TYPES = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".aiff": "audio/aiff", ".aif": "audio/aiff"}


def enable_library(root_getter, skip_getter, cache_dir: Path) -> Library:
    global library, _cache_dir
    library = Library(root_getter, skip_getter)
    _cache_dir = Path(cache_dir)
    return library


def _require() -> Library:
    if library is None:
        raise HTTPException(503, "The library is not available")
    return library


@router.get("")
async def list_library():
    return await asyncio.to_thread(_require().scan)


@router.get("/audio/{track_id}")
async def track_audio(track_id: str, mp3: bool = False):
    """The exported file itself (range requests work), or with ``mp3=1`` a cached MP3 copy
    for players that can't decode the original (AIFF in Chromium, for example)."""
    path = _require().track_path(track_id)
    if path is None:
        raise HTTPException(404, "Track not found. Refresh the library.")
    if not mp3 or path.suffix.lower() == ".mp3":
        return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                            filename=path.name, content_disposition_type="inline")
    from audio_processor import run_ffmpeg

    stat = path.stat()
    target = _cache_dir / f"{track_id}-{stat.st_size}-{stat.st_mtime_ns}.mp3"
    async with _transcode_lock:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp.mp3")
            result = await asyncio.to_thread(
                run_ffmpeg, ["-y", "-v", "error", "-i", str(path), "-vn", "-codec:a", "libmp3lame", "-b:a", "256k", str(temp)],
                capture_output=True)
            if result.returncode != 0 or not temp.exists():
                temp.unlink(missing_ok=True)
                raise HTTPException(500, "Could not prepare this track for playback")
            temp.replace(target)
    return FileResponse(target, media_type="audio/mpeg", filename=path.stem + ".mp3", content_disposition_type="inline")


@router.get("/cover/{album_id}")
async def album_cover(album_id: str):
    path = _require().art_path(album_id)
    if path is None:
        raise HTTPException(404, "No cover")
    if path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        return FileResponse(path, media_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg",
                            headers={"Cache-Control": "max-age=300"})
    art = await asyncio.to_thread(embedded_art, path)
    if art is None:
        raise HTTPException(404, "No cover")
    data, mime = art
    return Response(data, media_type=mime if mime.startswith("image/") else "image/jpeg",
                    headers={"Cache-Control": "max-age=300"})


@router.post("/reveal/{item_id}")
async def reveal(item_id: str, request: Request):
    """Show an album folder or a track in Finder / Explorer. Local machine only."""
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        local = False
    if not local:
        raise HTTPException(403, "Only available on this computer")
    lib = _require()
    path = lib.track_path(item_id) or lib.album_path(item_id)
    if path is None:
        raise HTTPException(404, "Not found. Refresh the library.")
    if sys.platform == "darwin":
        command = ["/usr/bin/open", "-R", str(path)]
    elif sys.platform == "win32":
        command = ["explorer", f"/select,{path}"]
    else:
        command = ["xdg-open", str(path if path.is_dir() else path.parent)]
    try:
        await asyncio.to_thread(subprocess.run, command, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(500, f"Could not open the folder: {exc}") from exc
    return {"ok": True}
